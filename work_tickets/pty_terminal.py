from __future__ import annotations

import asyncio
import errno
import fcntl
import os
import signal
import struct
import termios
from typing import Any

from fastapi import WebSocket

DEFAULT_COLUMNS = 80
DEFAULT_ROWS = 24
MIN_COLUMNS = 2
MIN_ROWS = 2
MAX_COLUMNS = 500
MAX_ROWS = 500


def _dimension(value: Any, fallback: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(minimum, min(maximum, parsed))


def _initial_size(websocket: WebSocket) -> tuple[int, int]:
    return (
        _dimension(websocket.query_params.get("cols"), DEFAULT_COLUMNS, MIN_COLUMNS, MAX_COLUMNS),
        _dimension(websocket.query_params.get("rows"), DEFAULT_ROWS, MIN_ROWS, MAX_ROWS),
    )


def _set_terminal_size(master_fd: int, columns: Any, rows: Any) -> None:
    size = struct.pack(
        "HHHH",
        _dimension(rows, DEFAULT_ROWS, MIN_ROWS, MAX_ROWS),
        _dimension(columns, DEFAULT_COLUMNS, MIN_COLUMNS, MAX_COLUMNS),
        0,
        0,
    )
    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, size)


def _prepare_child_terminal(slave_fd: int) -> None:
    os.setsid()
    fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)


def _process_group_signal(process: asyncio.subprocess.Process, signum: signal.Signals) -> None:
    if process.returncode is None:
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            pass


async def _read_pty(master_fd: int) -> bytes:
    loop = asyncio.get_running_loop()
    result: asyncio.Future[bytes] = loop.create_future()

    def read_ready() -> None:
        try:
            data = os.read(master_fd, 4096)
        except OSError as exc:
            if exc.errno == errno.EIO:
                data = b""
            elif not result.done():
                result.set_exception(exc)
                loop.remove_reader(master_fd)
                return
            else:
                return
        if not result.done():
            result.set_result(data)
        loop.remove_reader(master_fd)

    loop.add_reader(master_fd, read_ready)
    try:
        return await result
    finally:
        loop.remove_reader(master_fd)


async def _write_pty(master_fd: int, data: bytes) -> None:
    loop = asyncio.get_running_loop()
    pending = memoryview(data)
    while pending:
        try:
            written = os.write(master_fd, pending)
        except BlockingIOError:
            ready = loop.create_future()

            def write_ready(ready: asyncio.Future[None] = ready) -> None:
                if not ready.done():
                    ready.set_result(None)

            loop.add_writer(master_fd, write_ready)
            try:
                await ready
            finally:
                loop.remove_writer(master_fd)
        else:
            pending = pending[written:]


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        if process.returncode is not None:
            return
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            if process.returncode is not None:
                return
            process.kill()
        await process.wait()
