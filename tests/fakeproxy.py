"""A local HTTP proxy for tests that REQUIRES Basic auth and records every target exactly as the client
named it (a hostname, or an IP if the client resolved it itself). Only *.invalid targets are served (by
`upstream`); any other target is refused and recorded, so nothing a test runs ever leaves the machine."""

from __future__ import annotations

import asyncio
import base64
import threading
from urllib.parse import urlsplit


class FakeProxy:
    def __init__(self, user: str, password: str, upstream: tuple[str, int]):
        self.user, self.password, self.upstream = user, password, upstream
        self.seen: list[tuple[str, str]] = []  # (how, host as the client sent it)
        self.refused: list[str] = []  # non-.invalid targets: never connected
        self.paths: list[tuple[str, str]] = []  # (host, path) of each absolute-URI (http) request
        self.unauthenticated = 0

    # ── lifecycle: its own loop in a thread, so blocking clients (urllib) cannot deadlock it ──
    def start(self) -> int:
        self.loop = asyncio.new_event_loop()
        server = self.loop.run_until_complete(asyncio.start_server(self._handle, "127.0.0.1", 0))
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        return server.sockets[0].getsockname()[1]

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)

    # ── the proxy ──
    async def _pipe(self, r, w):
        try:
            while data := await r.read(65536):
                w.write(data)
                await w.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            w.close()

    async def _handle(self, r, w):
        try:
            head = await r.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
            w.close()
            return
        lines = head.decode("latin1").split("\r\n")
        method, target, _ = lines[0].split(" ", 2)
        hdrs = {ln.split(":", 1)[0].lower(): ln.split(":", 1)[1].strip() for ln in lines[1:] if ":" in ln}
        want = "Basic " + base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        if hdrs.get("proxy-authorization") != want:
            self.unauthenticated += 1
            w.write(
                b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                b'Proxy-Authenticate: Basic realm="test"\r\nContent-Length: 0\r\n\r\n'
            )
            await w.drain()
            w.close()
            return
        if method == "CONNECT":
            host, how, first = target.rsplit(":", 1)[0], "connect", b""  # every target goes to upstream
        else:  # an absolute-URI request for an http:// target
            u = urlsplit(target)
            host, how = u.hostname or "", "absolute"
            path = (u.path or "/") + (f"?{u.query}" if u.query else "")
            rest = [ln for ln in lines[1:] if ln and not ln.lower().startswith("proxy-")]
            first = (f"{method} {path} HTTP/1.1\r\n" + "\r\n".join(rest) + "\r\n\r\n").encode("latin1")
        if not host.endswith(".invalid"):
            self.refused.append(host)
            w.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            await w.drain()
            w.close()
            return
        self.seen.append((how, host))
        if how == "absolute":
            self.paths.append((host, urlsplit(target).path))
        ur, uw = await asyncio.open_connection(*self.upstream)
        if how == "connect":
            w.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await w.drain()
        else:
            uw.write(first)
            await uw.drain()
        await asyncio.gather(self._pipe(r, uw), self._pipe(ur, w))
