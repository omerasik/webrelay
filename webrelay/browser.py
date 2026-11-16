import asyncio
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from playwright.async_api import Error, async_playwright

from .templates import Template, validate_rows


class BrowserFault(Exception):
    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def normalize_origin(origin: str):
    value = urlparse(origin)
    if (value.scheme != "http" or value.hostname != "127.0.0.1" or value.username or value.password
        or not value.port or value.path not in {"", "/"} or value.query or value.fragment):
        raise ValueError("Set SAMPLE_ORIGIN to http://127.0.0.1:<port> for the authorized sample service.")
    return f"http://127.0.0.1:{value.port}"


@dataclass(frozen=True)
class BrowserResult:
    rows: list[dict]
    screenshot: bytes
    duration_ms: int
    source: str


class BrowserRunner:
    def __init__(self, origin: str):
        self.origin = normalize_origin(origin)

    async def retrieve(self, template: Template, search: str, *, unavailable: bool = False):
        target = self.origin + template.path
        started = time.monotonic()
        try:
            async with asyncio.timeout(35), async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True, timeout=20_000)
                try:
                    context = await browser.new_context(
                        viewport={"width": 1100, "height": 680}, service_workers="block", accept_downloads=False,
                        java_script_enabled=False,
                        extra_http_headers={"X-WebRelay-Demo-Fault": "unavailable"} if unavailable else None,
                    )
                    try:
                        async def route_policy(route):
                            request = route.request
                            if request.method != "GET" or request.url != target or request.resource_type != "document":
                                await route.abort("blockedbyclient")
                                return
                            upstream = await route.fetch(max_redirects=0, timeout=7000)
                            if 300 <= upstream.status < 400:
                                await route.abort("blockedbyclient")
                                return
                            await route.fulfill(response=upstream)

                        async def close_socket(socket):
                            await socket.close()

                        await context.route("**/*", route_policy)
                        await context.route_web_socket("**/*", close_socket)
                        page = await context.new_page()
                        response = await page.goto(target, wait_until="domcontentloaded", timeout=8_000)
                        if page.url != target or response is None:
                            raise BrowserFault("unexpected_navigation")
                        if response.status in {408, 429} or response.status >= 500:
                            raise BrowserFault("sample_unavailable", retryable=True)
                        if not response.ok:
                            raise BrowserFault("sample_rejected")
                        rows = await page.locator("tbody tr").all()
                        if not 1 <= len(rows) <= 50:
                            raise BrowserFault("invalid_row_count")
                        raw = [await row.locator("td").all_inner_texts() for row in rows]
                        try:
                            records = validate_rows(template, [[cell.strip() for cell in row] for row in raw], search)
                        except ValueError as error:
                            raise BrowserFault("invalid_sample_data") from error
                        screenshot = await page.screenshot(animations="disabled", timeout=5000)
                    finally:
                        await context.close()
                finally:
                    await browser.close()
        except (TimeoutError, Error) as error:
            raise BrowserFault("browser_unavailable", retryable=True) from error
        return BrowserResult(records, screenshot, round((time.monotonic() - started) * 1000), template.path)
