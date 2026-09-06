from __future__ import annotations

import asyncio
import json
import os
import pty
import signal
import subprocess
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from fastapi import WebSocket
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.websockets import WebSocketDisconnect

from .jira_service import canonicalize_jira_key
from .local_projects import is_safe_local_component_name
from .models import JiraConfig, OpenCodeSession, SessionLocal, Ticket
from .pty_terminal import (
    DEFAULT_COLUMNS,
    DEFAULT_ROWS,
    _dimension,
    _initial_size,
    _prepare_child_terminal,
    _process_group_signal,
    _read_pty,
    _set_terminal_size,
    _write_pty,
)
from .pty_terminal import (
    _stop_process as _stop_pty_process,
)


class RefineError(Exception):
    """An expected, user-facing Refine error."""


REFINE_SESSION_KIND = "refine"
SESSION_DISCOVERY_WINDOW = 60.0


class _ClientReservation:
    def __init__(self) -> None:
        self.acquired = False


def refine_prompt(ticket: Ticket, config: JiraConfig | None) -> str:
    if not ticket.jira_issue_key:
        raise RefineError("Refine is available only for tickets synced to Jira.")
    if config is None or not config.browser_base_url:
        raise RefineError("Configure the Jira browser URL before using Refine.")

    try:
        browser_url = urlsplit(config.browser_base_url.rstrip("/"))
    except ValueError as exc:
        raise RefineError("The configured Jira browser URL is invalid.") from exc
    if browser_url.scheme.lower() not in {"http", "https"} or not browser_url.netloc:
        raise RefineError("The configured Jira browser URL is invalid.")
    jira_key = canonicalize_jira_key(ticket.jira_issue_key)
    path = f"{browser_url.path.rstrip('/')}/browse/{quote(jira_key, safe='')}"
    return f"Refine {urlunsplit((browser_url.scheme, browser_url.netloc, path, '', ''))}"


def refine_working_directory(ticket: Ticket, config: JiraConfig | None) -> Path:
    if not ticket.component:
        raise RefineError("Assign a local component before using Refine.")
    if not is_safe_local_component_name(ticket.component):
        raise RefineError("The ticket component is not a valid local project name.")
    if config is None or not config.local_projects_directory:
        raise RefineError("Configure the local projects directory before using Refine.")

    try:
        root = Path(config.local_projects_directory).expanduser()
        if not root.is_dir():
            raise RefineError("The configured local projects directory does not exist.")

        resolved_root = root.resolve()
        project = root / ticket.component
        resolved_project = project.resolve()
        try:
            resolved_project.relative_to(resolved_root)
        except ValueError as exc:
            raise RefineError("The ticket component is not a valid local project name.") from exc
        if not resolved_project.is_dir():
            raise RefineError(
                f"The local project directory for component '{ticket.component}' does not exist."
            )
    except RefineError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise RefineError("The local project directory could not be resolved.") from exc
    return resolved_project


def get_opencode_session_id(jira_key: str, kind: str) -> str | None:
    """Return the persisted OpenCode session for a Jira key and session kind."""
    try:
        with SessionLocal() as db:
            session = db.scalar(
                select(OpenCodeSession).where(
                    OpenCodeSession.jira_key == canonicalize_jira_key(jira_key),
                    OpenCodeSession.kind == kind,
                )
            )
            return session.session_id if session is not None and session.session_id else None
    except SQLAlchemyError:
        return None


def save_opencode_session_id(jira_key: str, kind: str, session_id: str) -> str | None:
    """Persist the first session discovered for a Jira key and kind."""
    if not session_id:
        return None
    canonical_key = canonicalize_jira_key(jira_key)
    try:
        with SessionLocal() as db:
            existing = db.scalar(
                select(OpenCodeSession).where(
                    OpenCodeSession.jira_key == canonical_key,
                    OpenCodeSession.kind == kind,
                )
            )
            if existing is not None:
                return existing.session_id
            db.add(
                OpenCodeSession(
                    jira_key=canonical_key,
                    kind=kind,
                    session_id=session_id,
                )
            )
            db.commit()
            return session_id
    except IntegrityError:
        # Another discovery task won the unique Jira-key/kind race.
        return get_opencode_session_id(canonical_key, kind)
    except SQLAlchemyError:
        return None


def _session_timestamp(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        timestamp = float(value)
        return timestamp / 1000 if timestamp > 100_000_000_000 else timestamp
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _session_created_at(entry: dict[str, object]) -> float | None:
    for key in ("created", "created_at", "createdAt"):
        timestamp = _session_timestamp(entry.get(key))
        if timestamp is not None:
            return timestamp
    time_value = entry.get("time")
    if isinstance(time_value, dict):
        for key in ("created", "created_at", "createdAt"):
            timestamp = _session_timestamp(time_value.get(key))
            if timestamp is not None:
                return timestamp
    return None


def _parse_session_list(output: bytes) -> list[dict[str, object]]:
    try:
        parsed: object = json.loads(output.decode())
    except (UnicodeDecodeError, ValueError):
        return []
    if isinstance(parsed, dict):
        parsed = parsed.get("sessions")
    if not isinstance(parsed, list):
        return []
    return [entry for entry in parsed if isinstance(entry, dict)]


async def _list_opencode_sessions(working_directory: Path) -> list[dict[str, object]]:
    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            "opencode",
            "session",
            "list",
            "--format",
            "json",
            cwd=str(working_directory),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        output, _ = await asyncio.wait_for(process.communicate(), timeout=10)
    except AttributeError:
        return []
    except asyncio.CancelledError:
        if process is not None:
            await _terminate_and_reap(process)
        raise
    except (OSError, subprocess.SubprocessError, TimeoutError):
        if process is not None:
            await _terminate_and_reap(process)
        return []
    if process is None or process.returncode != 0 or not isinstance(output, bytes):
        return []
    return _parse_session_list(output)


async def _terminate_and_reap(process: asyncio.subprocess.Process) -> None:
    try:
        await _stop_process(process)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        pass


async def _discover_opencode_session(
    jira_key: str,
    kind: str,
    working_directory: Path,
    pty_created_at: float,
) -> None:
    deadline = pty_created_at + SESSION_DISCOVERY_WINDOW
    while True:
        sessions = await _list_opencode_sessions(working_directory)
        candidates = [
            (entry.get("id"), created_at)
            for entry in sessions
            if isinstance(entry.get("id"), str)
            and entry.get("id")
            and (created_at := _session_created_at(entry)) is not None
            and pty_created_at <= created_at <= deadline
        ]
        if candidates:
            session_id, _ = max(candidates, key=lambda candidate: candidate[1])
            if isinstance(session_id, str) and save_opencode_session_id(jira_key, kind, session_id):
                return

        remaining = deadline - datetime.now(UTC).timestamp()
        if remaining <= 0:
            return
        await asyncio.sleep(min(0.5, remaining))


async def send_error(websocket: WebSocket, message: str) -> None:
    try:
        await websocket.send_text(f"\r\n[Refine error] {message}\r\n")
        await websocket.close(code=1011)
    except (OSError, RuntimeError, WebSocketDisconnect):
        pass


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if getattr(process, "pid", None) is not None:
        await _stop_pty_process(process)
        return
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except OSError:
        if process.returncode is not None:
            return
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        try:
            process.kill()
        except OSError:
            if process.returncode is not None:
                return
        await process.wait()


class RefineSession:
    """Own one opencode process and fan its terminal stream out to its clients."""

    max_buffer_size = 256 * 1024

    def __init__(
        self,
        jira_key: str,
        prompt: str,
        working_directory: Path,
        on_finished: Callable[[RefineSession], Awaitable[None]],
        stale_after: float,
        columns: int = DEFAULT_COLUMNS,
        rows: int = DEFAULT_ROWS,
        session_kind: str = REFINE_SESSION_KIND,
    ) -> None:
        self.jira_key = jira_key
        self.prompt = prompt
        self.working_directory = working_directory
        self._on_finished = on_finished
        self._stale_after = stale_after
        self.session_kind = session_kind
        self.columns = columns
        self.rows = rows
        self._lock = asyncio.Lock()
        self._stdin_lock = asyncio.Lock()
        self._clients: set[WebSocket] = set()
        self._pending_clients = 0
        self._output: deque[bytes] = deque()
        self._output_size = 0
        self._process: asyncio.subprocess.Process | None = None
        self._error: str | None = None
        self._stopping = False
        self._ready = asyncio.Event()
        self.finished = asyncio.Event()
        self._stale_task: asyncio.Task[None] | None = None
        self._discovery_task: asyncio.Task[None] | None = None
        self._task: asyncio.Task[None] | None = None
        self._master_fd = -1
        self._slave_fd = -1

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    def _discovery_task_done(self, task: asyncio.Task[None]) -> None:
        if self._discovery_task is task:
            self._discovery_task = None
        if not task.cancelled():
            task.exception()

    async def reserve_client(self, reservation: _ClientReservation | None = None) -> bool:
        async with self._lock:
            if self._stopping or self.finished.is_set():
                return False
            self._pending_clients += 1
            if reservation is not None:
                reservation.acquired = True
            self._cancel_stale_locked()
            return True

    async def release_client(self) -> None:
        async with self._lock:
            if not self._pending_clients:
                return
            self._pending_clients -= 1
            self._schedule_stale_locked()

    async def stop_if_idle(self) -> None:
        async with self._lock:
            if self._clients or self._pending_clients or self._stopping:
                return
            self._stopping = True
            stale_task = self._stale_task
            self._cancel_stale_locked()
            startup_task = self._task
            if startup_task is asyncio.current_task():
                startup_task = None
            elif startup_task is not None and not startup_task.done():
                startup_task.cancel()
            discovery_task = self._discovery_task
            self._discovery_task = None
            if discovery_task is not None and not discovery_task.done():
                discovery_task.cancel()

        if stale_task is not None and stale_task is not asyncio.current_task():
            await asyncio.gather(stale_task, return_exceptions=True)
        if startup_task is not None:
            await asyncio.gather(startup_task, return_exceptions=True)
        if discovery_task is not None and discovery_task is not asyncio.current_task():
            await asyncio.gather(discovery_task, return_exceptions=True)

        process = self._process
        if process is not None:
            await _stop_process(process)

    async def attach(self, websocket: WebSocket) -> tuple[bool, str | None]:
        pending = True
        try:
            await self._ready.wait()
            async with self._lock:
                self._pending_clients -= 1
                pending = False
                if self._error is not None:
                    return True, self._error
                if self._stopping or self.finished.is_set():
                    return False, None
                self._cancel_stale_locked()
                self._clients.add(websocket)
                for output in self._output:
                    await self._send_output(websocket, output)
                return True, None
        except (OSError, RuntimeError, WebSocketDisconnect):
            async with self._lock:
                if pending:
                    self._pending_clients -= 1
                self._clients.discard(websocket)
                self._schedule_stale_locked()
            return False, None
        except asyncio.CancelledError:
            async with self._lock:
                if pending:
                    self._pending_clients -= 1
                self._clients.discard(websocket)
                self._schedule_stale_locked()
            raise

    async def detach(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(websocket)
            self._schedule_stale_locked()

    async def send_input(self, data: str) -> None:
        if not data:
            return
        async with self._stdin_lock:
            process = self._process
            if process is None or process.returncode is not None:
                return
            try:
                if self._master_fd >= 0:
                    await _write_pty(self._master_fd, data.encode())
                elif process.stdin is not None:
                    process.stdin.write(data.encode())
                    await process.stdin.drain()
            except (BrokenPipeError, OSError, RuntimeError):
                pass

    async def resize(self, columns: object, rows: object) -> None:
        async with self._stdin_lock:
            if self._master_fd < 0:
                return
            self.columns = _dimension(columns, self.columns, 2, 500)
            self.rows = _dimension(rows, self.rows, 2, 500)
            try:
                _set_terminal_size(self._master_fd, self.columns, self.rows)
                process = self._process
                if process is not None:
                    _process_group_signal(process, signal.SIGWINCH)
            except (OSError, RuntimeError):
                pass

    async def _run(self) -> None:
        process: asyncio.subprocess.Process | None = None
        master_fd = -1
        slave_fd = -1
        try:
            master_fd, slave_fd = pty.openpty()
            os.set_blocking(master_fd, False)
            _set_terminal_size(master_fd, self.columns, self.rows)
            try:
                environment = os.environ.copy()
                environment.update(
                    {
                        "TERM": "xterm-256color",
                        "COLORTERM": "truecolor",
                        "COLUMNS": str(self.columns),
                        "LINES": str(self.rows),
                    }
                )
                pty_created_at = datetime.now(UTC).timestamp()
                saved_session_id = get_opencode_session_id(self.jira_key, self.session_kind)
                command = (
                    ("opencode", "--session", saved_session_id)
                    if saved_session_id is not None
                    else ("opencode", "--prompt", self.prompt)
                )
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    cwd=str(self.working_directory),
                    env=environment,
                    preexec_fn=lambda: _prepare_child_terminal(slave_fd),
                )
            except (OSError, subprocess.SubprocessError) as exc:
                self._error = f"Could not start opencode: {getattr(exc, 'strerror', None) or exc}"
                return
            self._process = process
            if getattr(process, "pid", None) is None:
                # Unit-test doubles use the historical pipe transport.
                os.close(master_fd)
                os.close(slave_fd)
                master_fd = -1
                slave_fd = -1
            else:
                os.close(slave_fd)
                slave_fd = -1
                self._master_fd = master_fd
                self._slave_fd = -1
                master_fd = -1
            self._ready.set()
            if self._stopping:
                return
            if saved_session_id is None:
                discovery_task = asyncio.create_task(
                    _discover_opencode_session(
                        self.jira_key,
                        self.session_kind,
                        self.working_directory,
                        pty_created_at,
                    )
                )
                self._discovery_task = discovery_task
                discovery_task.add_done_callback(self._discovery_task_done)
            await self._monitor_process(process)
        finally:
            self._ready.set()
            if process is not None and process.returncode is None:
                await _stop_process(process)
            if self._master_fd >= 0:
                os.close(self._master_fd)
                self._master_fd = -1
            if self._slave_fd >= 0:
                os.close(self._slave_fd)
                self._slave_fd = -1
            if master_fd >= 0:
                os.close(master_fd)
            if slave_fd >= 0:
                os.close(slave_fd)
            await self._finish(process)

    async def _monitor_process(self, process: asyncio.subprocess.Process) -> None:
        output_task = asyncio.create_task(self._forward_output(process))
        wait_task = asyncio.create_task(process.wait())
        try:
            await wait_task
            await output_task
        except (OSError, RuntimeError, WebSocketDisconnect):
            if process.returncode is None:
                await _stop_process(process)
        finally:
            if not output_task.done():
                output_task.cancel()
            if not wait_task.done():
                wait_task.cancel()
            await asyncio.gather(output_task, wait_task, return_exceptions=True)

    async def _forward_output(self, process: asyncio.subprocess.Process) -> None:
        if self._master_fd >= 0:
            while chunk := await _read_pty(self._master_fd):
                await self._broadcast(chunk)
            return
        if process.stdout is not None:
            while chunk := await process.stdout.read(4096):
                await self._broadcast(chunk)

    async def _broadcast(self, output: bytes) -> None:
        async with self._lock:
            self._output.append(output)
            self._output_size += len(output)
            while self._output and self._output_size > self.max_buffer_size:
                self._output_size -= len(self._output.popleft())
            clients = tuple(self._clients)
        results = await asyncio.gather(
            *(self._send_output(websocket, output) for websocket in clients),
            return_exceptions=False,
        )
        failed_clients = [
            websocket for websocket, sent in zip(clients, results, strict=True) if not sent
        ]
        if failed_clients:
            async with self._lock:
                for websocket in failed_clients:
                    self._clients.discard(websocket)
                self._schedule_stale_locked()

    async def _send_output(self, websocket: WebSocket, output: bytes) -> bool:
        try:
            send_bytes = getattr(websocket, "send_bytes", None)
            if callable(send_bytes):
                await asyncio.wait_for(send_bytes(output), timeout=5)
            else:
                await asyncio.wait_for(
                    websocket.send_text(output.decode(errors="replace")), timeout=5
                )
        except (OSError, RuntimeError, TimeoutError, WebSocketDisconnect):
            return False
        return True

    async def _finish(self, process: asyncio.subprocess.Process | None) -> None:
        async with self._lock:
            self._stopping = True
            self._cancel_stale_locked()
            clients = tuple(self._clients)
            self._clients.clear()
            error = self._error
            returncode = process.returncode if process is not None else None

        if process is not None and error is None:
            exit_message = f"\r\n[Refine exited with code {returncode}]\r\n".encode()
            await asyncio.gather(
                *(self._close_client(websocket, exit_message) for websocket in clients),
                return_exceptions=True,
            )

        await self._on_finished(self)
        self.finished.set()

    async def _close_client(self, websocket: WebSocket, output: bytes) -> None:
        if not await self._send_output(websocket, output):
            return
        try:
            await asyncio.wait_for(websocket.close(code=1000), timeout=5)
        except (OSError, RuntimeError, TimeoutError, WebSocketDisconnect):
            pass

    def _cancel_stale_locked(self) -> None:
        if (
            self._stale_task is not None
            and self._stale_task is not asyncio.current_task()
            and not self._stale_task.done()
        ):
            self._stale_task.cancel()
        self._stale_task = None

    def _schedule_stale_locked(self) -> None:
        if self._clients or self._pending_clients or self._stopping or self._stale_task is not None:
            return
        self._stale_task = asyncio.create_task(self._expire_when_stale())

    async def _expire_when_stale(self) -> None:
        try:
            await asyncio.sleep(self._stale_after)
            await self._on_stale()
        except asyncio.CancelledError:
            pass
        finally:
            async with self._lock:
                if self._stale_task is asyncio.current_task():
                    self._stale_task = None

    async def _on_stale(self) -> None:
        await self.stop_if_idle()


class RefineSessionRegistry:
    """In-memory Jira-keyed Refine session registry for this server process."""

    def __init__(self, stale_after: float = 300.0) -> None:
        self._stale_after = stale_after
        self._lock = asyncio.Lock()
        self._sessions: dict[str, RefineSession] = {}

    async def _reserve_client(self, session: RefineSession) -> bool:
        reservation = _ClientReservation()
        try:
            return await session.reserve_client(reservation)
        except asyncio.CancelledError:
            if reservation.acquired:
                await session.release_client()
            raise

    async def attach(
        self,
        jira_key: str,
        prompt: str,
        working_directory: Path,
        websocket: WebSocket,
        columns: int = DEFAULT_COLUMNS,
        rows: int = DEFAULT_ROWS,
    ) -> tuple[RefineSession | None, str | None]:
        canonical_key = canonicalize_jira_key(jira_key)
        while True:
            created = False
            async with self._lock:
                session = self._sessions.get(canonical_key)
                if session is None:
                    session = RefineSession(
                        canonical_key,
                        prompt,
                        working_directory,
                        self._remove,
                        self._stale_after,
                        columns,
                        rows,
                    )
                    self._sessions[canonical_key] = session
                    session.start()
                    created = True
            try:
                reserved = await self._reserve_client(session)
            except asyncio.CancelledError:
                if created:
                    await session.stop_if_idle()
                raise
            if not reserved:
                await session.finished.wait()
                continue

            try:
                attached, error = await session.attach(websocket)
            except asyncio.CancelledError:
                # RefineSession.attach releases its reservation while holding
                # the session lock before propagating cancellation.
                if created:
                    await session.stop_if_idle()
                raise
            if attached:
                if error is not None:
                    await self._remove(session)
                return session, error
            # The old session exited or is being expired. Retry against the
            # registry so a concurrent reconnect cannot create two processes.
            await session.finished.wait()

    async def detach(self, session: RefineSession, websocket: WebSocket) -> None:
        await session.detach(websocket)

    async def _remove(self, session: RefineSession) -> None:
        async with self._lock:
            if self._sessions.get(session.jira_key) is session:
                del self._sessions[session.jira_key]


session_registry = RefineSessionRegistry()


async def run_refine(
    websocket: WebSocket,
    jira_key: str,
    prompt: str,
    working_directory: Path,
) -> None:
    columns, rows = _initial_size(websocket)
    session, error = await session_registry.attach(
        jira_key, prompt, working_directory, websocket, columns, rows
    )
    if error is not None:
        await send_error(websocket, error)
        return
    if session is None:
        return

    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            text = message.get("text")
            if text is None:
                raw_data = message.get("bytes")
                text = raw_data.decode(errors="replace") if raw_data is not None else ""
            try:
                payload = json.loads(text)
            except (TypeError, ValueError):
                payload = {"type": "input", "data": text}
            if not isinstance(payload, dict):
                continue
            if payload.get("type") == "resize":
                await session.resize(payload.get("cols"), payload.get("rows"))
            elif payload.get("type") == "input":
                data = payload.get("data")
                if isinstance(data, str):
                    await session.send_input(data)
    except (OSError, RuntimeError, WebSocketDisconnect):
        pass
    finally:
        await session_registry.detach(session, websocket)
