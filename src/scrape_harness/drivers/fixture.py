"""Offline backend: crawlee's HttpCrawler with a crawlee HttpClient that serves saved HTML from a
directory holding urls.json. It never opens a socket, and it exercises the SAME crawlee path as
the impit backend (queue, robots.txt, concurrency limits, retries, challenge stop).

urls.json maps a URL to a file name, or to {"file", "status", "final_url"} to replay a challenge,
or to {"error": "timeout"} for a GET that is sent and never answered (the circuit breaker's case);
a key "URL#variant" overrides for one variant."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from crawlee.http_clients import HttpClient, HttpCrawlingResult

from .. import pipeline
from ..interfaces import FetchResult


class _Response:
    def __init__(self, status: int, body: bytes, headers: dict | None = None) -> None:
        self._status, self._body = status, body
        self._headers = {"content-type": "text/html; charset=utf-8", **(headers or {})}

    @property
    def http_version(self) -> str:
        return "HTTP/1.1"

    @property
    def status_code(self) -> int:
        return self._status

    @property
    def headers(self) -> dict:
        return self._headers

    async def read(self) -> bytes:
        return self._body

    async def read_stream(self) -> AsyncIterator[bytes]:
        yield self._body


class FixtureHttpClient(HttpClient):
    log: list[str] = []  # every GET by every fixture client in this process (tests reset it)

    def __init__(self, root: str, variant: str | None = None) -> None:
        super().__init__()
        self.root = Path(root)
        self.map = json.loads((self.root / "urls.json").read_text())
        self.variant = variant
        self.gets: list[str] = []
        self.inflight = self.max_inflight = 0

    def _answer(self, url: str) -> tuple[int, str, bytes, dict]:
        key = f"{url}#{self.variant}" if f"{url}#{self.variant}" in self.map else url
        if key not in self.map:
            return 404, url, b"", {}
        e = self.map[key]
        e = {"file": e} if isinstance(e, str) else e
        body = (self.root / e["file"]).read_bytes() if e.get("file") else b""
        return e.get("status", 200), e.get("final_url", url), body, e.get("headers", {})

    async def _get(self, url: str) -> tuple[int, str, bytes, dict]:
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            await asyncio.sleep(
                0
            )  # yield, so overlapping requests WOULD be visible if concurrency allowed them
            self.gets.append(url)
            FixtureHttpClient.log.append(url)
            e = self.map.get(f"{url}#{self.variant}", self.map.get(url))
            if isinstance(e, dict) and e.get("error") == "timeout":
                raise TimeoutError(f"fixture: no response from {url}")  # sent, never answered
            return self._answer(url)
        finally:
            self.inflight -= 1

    async def crawl(self, request, *, session=None, proxy_info=None, statistics=None, timeout=None):
        status, final, body, headers = await self._get(request.url)
        request.loaded_url = final
        return HttpCrawlingResult(http_response=_Response(status, body, headers))

    async def send_request(
        self, url, *, method="GET", headers=None, payload=None, session=None, proxy_info=None, timeout=None
    ):
        status, _, body, headers = await self._get(url)
        return _Response(status, body, headers)

    @asynccontextmanager
    async def stream(self, url, **_):
        yield await self.send_request(url)

    async def cleanup(self) -> None:
        pass


class FixtureBackend:
    network = False
    last: FixtureHttpClient | None = None  # the most recent client, for tests to inspect

    def __init__(self, variant: str | None = None, root: str | None = None, **_: object) -> None:
        root = root or pipeline.knob("FIXTURE_DIR")
        if not root:
            raise SystemExit(
                f"fixture backend: pass --fixtures DIR or set {pipeline.knob_name('FIXTURE_DIR')}"
            )
        self.http_client = FixtureHttpClient(root, variant)
        FixtureBackend.last = self.http_client

    def crawler(self, **common):
        from crawlee.crawlers import HttpCrawler

        return HttpCrawler(http_client=self.http_client, **common)

    async def result(self, ctx) -> FetchResult:
        body = await ctx.http_response.read()
        url = ctx.request.url
        return FetchResult(
            url,
            ctx.http_response.status_code,
            ctx.request.loaded_url or url,
            body.decode(),
            dict(ctx.http_response.headers),
        )

    async def close(self) -> None:
        pass
