"""Mac-side client for talking to the Windows peer agent.

Usage model:

    peer = Peer.from_env()                    # picks up HC_PEER_URL, HC_PC_SIDE
    if peer.enabled:
        await peer.start()                    # kicks off health + windows polling
    # ... later ...
    if peer.healthy:
        await peer.mouse_move(dx, dy)

Everything network-facing is async and uses a long-lived
``httpx.AsyncClient`` so we get TCP keepalive — important for the
high-frequency mouse-move events, which otherwise would pay a
handshake per packet and feel sluggish.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, List, Optional

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]


@dataclass
class PeerWindow:
    """A Cursor window living on the PC. Mirrors CursorWindow on the
    Mac side but tagged with ``host="pc"`` so the deck can route the
    right events at the right machine."""

    title: str
    host: str = "pc"


@dataclass
class PeerState:
    enabled: bool = False
    base_url: str = ""
    side: str = "right"   # 'left' | 'right' | 'above' | 'below'
    token: str = ""
    healthy: bool = False
    hostname: str = ""
    screen_w: int = 0
    screen_h: int = 0
    last_error: str = ""
    last_checked_ts: float = 0.0
    windows: List[PeerWindow] = field(default_factory=list)


class Peer:
    """Async client for the Hand Control peer agent on the PC."""

    def __init__(
        self,
        base_url: str,
        side: str = "right",
        token: str = "",
        on_windows_change: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        self.state = PeerState(
            enabled=True,
            base_url=base_url.rstrip("/"),
            side=side if side in ("left", "right", "above", "below") else "right",
            token=token,
        )
        self._on_windows_change = on_windows_change
        self._client: Optional[httpx.AsyncClient] = None
        self._health_task: Optional[asyncio.Task[None]] = None
        self._windows_task: Optional[asyncio.Task[None]] = None
        self._stopping = False

    # --- construction -------------------------------------------------------

    @classmethod
    def from_env(
        cls,
        on_windows_change: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> Optional["Peer"]:
        """Return a Peer if HC_PEER_URL is set, else None.

        Keeping construction optional means single-machine users don't
        pay any startup cost or see any "peer unhealthy" spam.
        """
        url = os.environ.get("HC_PEER_URL", "").strip()
        if not url:
            return None
        if httpx is None:
            print(
                "[peer] HC_PEER_URL is set but httpx isn't installed. "
                "Run ./run.sh to refresh deps."
            )
            return None
        side = os.environ.get("HC_PC_SIDE", "right").strip().lower()
        token = os.environ.get("HC_PEER_TOKEN", "").strip()
        return cls(
            base_url=url,
            side=side,
            token=token,
            on_windows_change=on_windows_change,
        )

    # --- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        # One long-lived client = TCP keepalive. Timeouts are tight on
        # the fast path (mouse move) and generous on the slow path
        # (hold_end, which waits for Wispr to finish typing).
        self._client = httpx.AsyncClient(
            base_url=self.state.base_url,
            headers=self._auth_headers(),
            timeout=httpx.Timeout(
                connect=1.0, read=8.0, write=1.0, pool=1.0
            ),
            limits=httpx.Limits(max_keepalive_connections=4, max_connections=8),
        )
        await self._health_check_once()
        self._health_task = asyncio.create_task(self._health_loop())
        self._windows_task = asyncio.create_task(self._windows_loop())

    async def stop(self) -> None:
        self._stopping = True
        for t in (self._health_task, self._windows_task):
            if t:
                t.cancel()
        if self._client:
            await self._client.aclose()
            self._client = None

    # --- poll loops ---------------------------------------------------------

    async def _health_loop(self) -> None:
        # Fast retries right after a failure, slower when stable.
        while not self._stopping:
            await asyncio.sleep(5.0 if self.state.healthy else 2.0)
            await self._health_check_once()

    async def _health_check_once(self) -> None:
        if not self._client:
            return
        try:
            r = await self._client.get("/peer/health", timeout=1.5)
            r.raise_for_status()
            data = r.json()
            scr = data.get("screen") or {}
            was_healthy = self.state.healthy
            self.state.healthy = bool(data.get("ok"))
            self.state.hostname = str(data.get("hostname", ""))
            self.state.screen_w = int(scr.get("w", 0))
            self.state.screen_h = int(scr.get("h", 0))
            self.state.last_error = ""
            self.state.last_checked_ts = time.monotonic()
            if not was_healthy:
                print(
                    f"[peer] up: {self.state.hostname} "
                    f"{self.state.screen_w}x{self.state.screen_h}"
                )
        except Exception as exc:
            was_healthy = self.state.healthy
            self.state.healthy = False
            self.state.last_error = str(exc)
            self.state.last_checked_ts = time.monotonic()
            if was_healthy:
                print(f"[peer] down: {exc}")

    async def _windows_loop(self) -> None:
        while not self._stopping:
            if self.state.healthy:
                await self._refresh_windows_once()
            await asyncio.sleep(1.5)

    async def _refresh_windows_once(self) -> None:
        if not self._client:
            return
        try:
            r = await self._client.get("/peer/windows", timeout=2.0)
            r.raise_for_status()
            data = r.json()
            new_titles = [
                PeerWindow(title=w["title"])
                for w in data.get("windows", [])
                if isinstance(w, dict) and isinstance(w.get("title"), str)
            ]
            old = [w.title for w in self.state.windows]
            new = [w.title for w in new_titles]
            if old != new:
                self.state.windows = new_titles
                if self._on_windows_change:
                    try:
                        await self._on_windows_change()
                    except Exception as exc:
                        print(f"[peer] on_windows_change error: {exc}")
        except Exception:
            # Transient network hiccups shouldn't flip health state —
            # let the health loop own that.
            pass

    # --- Fast-path fire-and-forget calls ------------------------------------
    #
    # For mouse moves we intentionally don't await the response body
    # and swallow timeouts — we care about latency, not acknowledgement.
    # A dropped packet on a 60Hz stream is effectively invisible.

    async def mouse_move(self, dx: float, dy: float) -> None:
        await self._fast_post("/peer/mouse_move", {"dx": dx, "dy": dy})

    async def mouse_scroll(self, dx: float, dy: float) -> None:
        await self._fast_post("/peer/mouse_scroll", {"dx": dx, "dy": dy})

    async def warp_cursor(self, x: int, y: int) -> None:
        await self._fast_post("/peer/warp_cursor", {"x": int(x), "y": int(y)})

    async def _fast_post(self, path: str, payload: dict) -> None:
        if not self._client or not self.state.healthy:
            return
        try:
            await self._client.post(path, json=payload, timeout=0.4)
        except Exception:
            # Don't log — this runs at 60Hz; a spammy log here would
            # drown everything else.
            pass

    # --- Slower reliable calls ----------------------------------------------

    async def mouse_click(self, button: str = "left") -> None:
        await self._reliable_post("/peer/mouse_click", {"button": button})

    async def focus_window(self, title: str) -> bool:
        data = await self._reliable_post("/peer/focus_window", {"title": title})
        return bool(data.get("ok")) if data else False

    async def hold_start(self, title: Optional[str] = None) -> None:
        payload = {"title": title} if title else {}
        await self._reliable_post("/peer/hold_start", payload)

    async def hold_end(self) -> bool:
        """Returns True if Wispr auto-submitted (press-enter voice cmd)."""
        data = await self._reliable_post("/peer/hold_end", {}, timeout=10.0)
        return bool(data.get("auto_submitted")) if data else False

    async def submit(self) -> None:
        await self._reliable_post("/peer/submit", {})

    async def delete(self) -> None:
        await self._reliable_post("/peer/delete", {})

    async def type_string(self, text: str) -> None:
        await self._reliable_post("/peer/type_string", {"text": text})

    async def press_enter(self) -> None:
        await self._reliable_post("/peer/press_enter", {})

    async def focus_chat_input(self) -> None:
        """Cursor's Ctrl+L on the PC side — fired after focus_window
        so the phone's new-card swipe lands on a ready chat input."""
        await self._reliable_post("/peer/focus_chat_input", {})

    async def _reliable_post(
        self,
        path: str,
        payload: dict,
        timeout: float = 4.0,
    ) -> Optional[dict]:
        if not self._client:
            return None
        if not self.state.healthy:
            print(f"[peer] skipping {path} (peer unreachable)")
            return None
        try:
            r = await self._client.post(path, json=payload, timeout=timeout)
            r.raise_for_status()
            return r.json() if r.headers.get("content-type", "").startswith(
                "application/json"
            ) else {}
        except Exception as exc:
            print(f"[peer] {path} failed: {exc}")
            return None

    # --- Helpers ------------------------------------------------------------

    def _auth_headers(self) -> dict:
        return {"X-HC-Token": self.state.token} if self.state.token else {}
