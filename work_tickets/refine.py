from __future__ import annotations

import asyncio
import os
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from .local_projects import is_safe_local_component_name
from .models import JiraConfig, Ticket


class RefineError(Exception):
    """An expected, user-facing Refine error."""


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
    path = f"{browser_url.path.rstrip('/')}/browse/{quote(ticket.jira_issue_key, safe='')}"
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


async def send_error(websocket: WebSocket, message: str) -> None:
    try:
        await websocket.send_text(f"\r\n[Refine error] {message}\r\n")
        await websocket.close(code=1011)
    except (RuntimeError, WebSocketDisconnect):
        pass


async def _forward_output(process: asyncio.subprocess.Process, websocket: WebSocket) -> None:
    assert process.stdout is not None
    while chunk := await process.stdout.read(4096):
        await websocket.send_text(chunk.decode(errors="replace"))


async def _forward_input(process: asyncio.subprocess.Process, websocket: WebSocket) -> bool:
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return False
            data = message.get("text")
            if data is None:
                raw_data = message.get("bytes")
                data = raw_data.decode(errors="replace") if raw_data is not None else ""
            if process.stdin is not None and data:
                process.stdin.write(data.encode())
                await process.stdin.drain()
    except (OSError, WebSocketDisconnect, RuntimeError):
        return False


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        process.kill()
        await process.wait()


async def run_refine(websocket: WebSocket, prompt: str, working_directory: Path) -> None:
    try:
        process = await asyncio.create_subprocess_exec(
            "opencode",
            "--prompt",
            prompt,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(working_directory),
            env=os.environ.copy(),
        )
    except OSError as exc:
        await send_error(websocket, f"Could not start opencode: {exc.strerror or exc}")
        return

    input_task = asyncio.create_task(_forward_input(process, websocket))
    output_task = asyncio.create_task(_forward_output(process, websocket))
    wait_task = asyncio.create_task(process.wait())
    connected = True
    try:
        done, _ = await asyncio.wait(
            {input_task, output_task, wait_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if input_task in done:
            try:
                connected = input_task.result()
            except (OSError, RuntimeError, WebSocketDisconnect):
                connected = False
        if output_task in done:
            try:
                output_task.result()
            except (OSError, RuntimeError, WebSocketDisconnect):
                connected = False
        if not connected:
            await _stop_process(process)
        else:
            await wait_task
            if not output_task.done():
                try:
                    await output_task
                except (OSError, RuntimeError, WebSocketDisconnect):
                    connected = False
    finally:
        if process.returncode is None:
            await _stop_process(process)
        for task in (input_task, output_task, wait_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(input_task, output_task, wait_task, return_exceptions=True)

    if connected:
        try:
            await websocket.send_text(f"\r\n[Refine exited with code {process.returncode}]\r\n")
            await websocket.close(code=1000)
        except (RuntimeError, WebSocketDisconnect):
            pass
