"""Hand Control — Mac server.

Serves the phone UI and handles phone → Mac control events over WebSocket.

Control flow:
    phone hold_start
        → focus selected Cursor window
        → press-and-hold Right Option (Wispr Flow hotkey)

    phone hold_end
        → release Right Option
        → wait for Wispr to finish typing (CGEventTap keystroke watcher)
        → press Enter

    phone switch_prev / switch_next / select
        → update the server-side selected window index
        → focus that window immediately so user can see which one is active
"""

from __future__ import annotations

import asyncio
import json
import socket
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from .cursor_windows import CursorWindow, focus_window, list_windows
from .key_control import press_enter, right_option_down, right_option_up
from .keystroke_watcher import KeystrokeWatcher

PHONE_DIR = Path(__file__).resolve().parent.parent / "phone"
POLL_INTERVAL_S = 1.0
ENTER_IDLE_MS = 400
ENTER_MAX_WAIT_S = 8.0


class State:
    def __init__(self) -> None:
        self.windows: list[CursorWindow] = []
        self.selected_index: int = 0
        self.clients: set[WebSocket] = set()
        self.watcher = KeystrokeWatcher()
        self.lock = asyncio.Lock()

    def selected_window(self) -> Optional[CursorWindow]:
        if not self.windows:
            return None
        idx = max(0, min(self.selected_index, len(self.windows) - 1))
        return self.windows[idx]

    def to_payload(self) -> dict:
        return {
            "type": "state",
            "windows": [
                {"title": w.title, "project": w.project} for w in self.windows
            ],
            "selected": self.selected_index if self.windows else -1,
        }


state = State()


async def broadcast(payload: dict) -> None:
    message = json.dumps(payload)
    dead: list[WebSocket] = []
    for client in state.clients:
        try:
            await client.send_text(message)
        except Exception:
            dead.append(client)
    for c in dead:
        state.clients.discard(c)


async def poll_windows() -> None:
    """Periodically refresh the list of open Cursor windows."""
    prev_key: tuple = ()
    while True:
        try:
            windows = list_windows()
        except Exception as exc:
            print(f"[poll_windows] error: {exc}")
            windows = []

        key = tuple((w.title, w.project) for w in windows)
        async with state.lock:
            if key != prev_key:
                state.windows = windows
                if state.selected_index >= len(windows):
                    state.selected_index = max(0, len(windows) - 1)
                prev_key = key
                await broadcast(state.to_payload())
        await asyncio.sleep(POLL_INTERVAL_S)


async def handle_hold_start() -> None:
    win = state.selected_window()
    if win is not None:
        focus_window(win.title)
        # Give the WM a beat before pressing the modifier
        await asyncio.sleep(0.08)
    right_option_down()


async def handle_hold_end() -> None:
    right_option_up()
    release_ts = time.monotonic()
    # Run the potentially-blocking settle wait in a thread.
    await asyncio.to_thread(
        state.watcher.wait_for_typing_to_settle,
        release_ts,
        ENTER_IDLE_MS,
        ENTER_MAX_WAIT_S,
    )
    press_enter()


async def handle_select(index: int) -> None:
    async with state.lock:
        if 0 <= index < len(state.windows):
            state.selected_index = index
            win = state.windows[index]
            await broadcast(state.to_payload())
        else:
            win = None
    if win is not None:
        focus_window(win.title)


async def handle_switch(delta: int) -> None:
    async with state.lock:
        if not state.windows:
            return
        state.selected_index = (state.selected_index + delta) % len(state.windows)
        win = state.windows[state.selected_index]
        await broadcast(state.to_payload())
    focus_window(win.title)


@asynccontextmanager
async def lifespan(_: FastAPI):
    state.watcher.start()
    task = asyncio.create_task(poll_windows())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def index() -> FileResponse:
    return FileResponse(PHONE_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(PHONE_DIR)), name="static")


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    state.clients.add(websocket)
    try:
        await websocket.send_text(json.dumps(state.to_payload()))
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = msg.get("type")
            if kind == "hold_start":
                await handle_hold_start()
            elif kind == "hold_end":
                await handle_hold_end()
            elif kind == "switch_prev":
                await handle_switch(-1)
            elif kind == "switch_next":
                await handle_switch(+1)
            elif kind == "select":
                idx = msg.get("index")
                if isinstance(idx, int):
                    await handle_select(idx)
            elif kind == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        print(f"[ws] error: {exc}")
    finally:
        state.clients.discard(websocket)
        # Safety: if the phone disconnects mid-hold, release the modifier.
        try:
            right_option_up()
        except Exception:
            pass


def get_lan_ip() -> str:
    """Best-effort LAN IP discovery."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def main() -> None:
    import uvicorn

    ip = get_lan_ip()
    print("\n" + "=" * 52)
    print("Hand Control running.")
    print(f"  On your phone, open:  http://{ip}:8000")
    print("=" * 52 + "\n")
    uvicorn.run("server.main:app", host="0.0.0.0", port=8000, log_level="info")


if __name__ == "__main__":
    main()
