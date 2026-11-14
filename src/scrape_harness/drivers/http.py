"""impit HTTP backend: crawlee's HttpCrawler with its default ImpitHttpClient (operator decision,
ADR-0003). No browser; used where the page variant allows plain HTTP."""

from __future__ import annotations

from ..interfaces import FetchResult


class ImpitHttpBackend:
    network = True

    def __init__(self, variant: str | None = None, **_: object) -> None:
        from crawlee.http_clients import ImpitHttpClient

        self.http_client = ImpitHttpClient()

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
            body.decode("utf-8", errors="replace"),
            dict(ctx.http_response.headers),
        )

    async def close(self) -> None:
        pass
