"""Bounded, JS-capable acquisition powered by Crawl4AI (Playwright/Chromium).

Crawl4AI owns page rendering, navigation, redirects and JS execution behind
a single shared browser instance; html_extract supplies deterministic
table/record/prose extraction from the rendered page. The public contract --
``AcquiredPage`` / ``AcquisitionResult`` / ``AcquisitionError`` and a
synchronous ``.acquire(url, need)`` -- is what ``web_tools.py``'s
``web_read`` consumes; this module has no knowledge of what happens above
that seam.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import threading
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig

from html_extract import (
    AcquiredPage,
    AcquisitionError,
    AcquisitionResult,
    classify_http_status,
    extract_page,
    link_score,
)
from web_security import UnsafeTarget, validate_public_url

LOGGER = logging.getLogger("agent_webapp.crawl4ai_engine")

MAX_RESPONSE_BYTES = int(os.getenv("WEB_MAX_RESPONSE_BYTES", str(5 * 1024 * 1024)))
REQUEST_TIMEOUT_SECONDS = max(1, int(os.getenv("WEB_REQUEST_TIMEOUT_SECONDS", "15")))
ACQUIRE_TIMEOUT_SECONDS = max(1, int(os.getenv("WEB_ACQUIRE_TIMEOUT_SECONDS", "30")))
MAX_CRAWL_DEPTH = max(0, min(int(os.getenv("WEB_MAX_CRAWL_DEPTH", "1")), 3))
MAX_CRAWL_PAGES = max(1, min(int(os.getenv("WEB_MAX_CRAWL_PAGES", "3")), 10))
MAX_CONCURRENT_ACQUISITIONS = max(
    1, min(int(os.getenv("WEB_MAX_CONCURRENT_ACQUISITIONS", "4")), 16)
)
MAX_RETRIES = max(0, min(int(os.getenv("WEB_MAX_RETRIES", "1")), 2))

_TRANSIENT_MARKERS = (
    "timeout",
    "timed out",
    "dns",
    "name not resolved",
    "connection refused",
    "connection reset",
    "connection closed",
    "econnreset",
    "econnrefused",
    "net::err_connection",
    "net::err_timed_out",
    "net::err_name_not_resolved",
    "net::err_internet_disconnected",
    "net::err_network_changed",
)

# The shared Chromium instance itself has died (crashed/OOM-killed, or its
# Playwright IPC pipe broke) rather than one page failing to load. Every
# acquisition after this keeps hitting the same corpse until the process is
# restarted -- these are the actual symptoms observed in the wild (never an
# explicit "browser crashed" message, always one of these knock-on errors).
_BROWSER_DEAD_MARKERS = (
    "invalid interceptionid",
    "target page, context or browser has been closed",
    "target closed",
    "browser has been closed",
    "browser is closed",
    "browser closed",
    "pipe closed",
    "connection closed while reading",
)


def _looks_like_dead_browser(message: str) -> bool:
    lowered = (message or "").lower()
    return any(marker in lowered for marker in _BROWSER_DEAD_MARKERS)


def _classified_runtime_error(message: str) -> AcquisitionError:
    """Classify a Crawl4AI/Playwright failure without making unknowns retryable."""
    lowered = (message or "").lower()
    if _looks_like_dead_browser(lowered):
        # Retryable at the caller level, but a same-process retry would just
        # hit the identical dead browser -- acquire() also invalidates the
        # shared runtime on this reason so the retry gets a fresh Chromium.
        return AcquisitionError("browser_crashed", message, retryable=True)
    if any(marker in lowered for marker in _TRANSIENT_MARKERS):
        return AcquisitionError("network_failure", message, retryable=True)
    return AcquisitionError("acquisition_failure", message or "Crawl4AI acquisition failed.")


def _ssrf_route_hook(target_validator: Callable[[str], Any]):
    """Validate every Playwright request, including redirect-generated ones.

    Registered on ``on_page_context_created`` -- fires once per page, before
    navigation starts -- so ``page.route`` intercepts the top-level document
    request and every subresource/redirect hop it triggers, mirroring
    ``PublicNetworkMiddleware`` from the retired Scrapy engine.
    """

    async def hook(page, **_kwargs) -> Any:
        async def handle_route(route, request) -> None:
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, target_validator, request.url)
            except UnsafeTarget:
                await route.abort()
                return
            await route.continue_()

        await page.route("**/*", handle_route)
        return page

    return hook


class _Crawl4AIRuntime:
    """A single shared browser + asyncio event loop, running on its own
    daemon thread, so this module works both inside FastAPI's own loop and
    called synchronously from plain threads -- same shape as the retired
    Scrapy engine's dedicated-reactor-thread runtime.
    """

    def __init__(self, browser_config: BrowserConfig) -> None:
        self.ready = threading.Event()
        self.failure: Optional[BaseException] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.crawler: Optional[AsyncWebCrawler] = None
        self._browser_config = browser_config
        self.thread = threading.Thread(
            target=self._run, name="crawl4ai-loop", daemon=True
        )
        self.thread.start()
        if not self.ready.wait(timeout=60):
            raise RuntimeError("Crawl4AI browser did not start in time.")
        if self.failure is not None:
            raise RuntimeError(f"Crawl4AI browser failed to start: {self.failure}")

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop

        async def bootstrap() -> None:
            try:
                self.crawler = AsyncWebCrawler(config=self._browser_config)
                await self.crawler.start()
            except BaseException as exc:  # noqa: BLE001
                self.failure = exc
            finally:
                self.ready.set()

        loop.create_task(bootstrap())
        loop.run_forever()

    def submit(self, coro) -> concurrent.futures.Future:
        assert self.loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self.loop)


class Crawl4AIAcquisitionEngine:
    """Synchronous library facade over the shared Crawl4AI browser."""

    _runtime: Optional[_Crawl4AIRuntime] = None
    _runtime_lock = threading.Lock()

    def __init__(
        self,
        *,
        target_validator: Callable[[str], Any] = validate_public_url,
        timeout_seconds: int = ACQUIRE_TIMEOUT_SECONDS,
        max_pages: int = MAX_CRAWL_PAGES,
    ) -> None:
        self.target_validator = target_validator
        self.timeout_seconds = max(1, min(int(timeout_seconds), ACQUIRE_TIMEOUT_SECONDS))
        self.max_pages = max(1, min(int(max_pages), MAX_CRAWL_PAGES))
        self._slots = threading.BoundedSemaphore(MAX_CONCURRENT_ACQUISITIONS)

    @classmethod
    def _shared_runtime(cls) -> _Crawl4AIRuntime:
        with cls._runtime_lock:
            if cls._runtime is None:
                cls._runtime = _Crawl4AIRuntime(
                    BrowserConfig(
                        headless=True,
                        verbose=False,
                        ignore_https_errors=True,
                        text_mode=True,
                        # Docker's default 64MB /dev/shm is a known cause of
                        # Chromium crashing under memory pressure; also set
                        # at the container level (docker-compose.yml
                        # shm_size) as a second layer of the same fix.
                        extra_args=["--disable-dev-shm-usage"],
                    )
                )
            return cls._runtime

    @classmethod
    def _invalidate_runtime(cls, dead: _Crawl4AIRuntime) -> None:
        """Drop the shared browser after it's confirmed dead.

        Every acquire() before a fix here kept reusing the same corpse
        after a Chromium crash/IPC break, so every fetch failed as a
        false "network error" until the whole backend process was
        restarted by hand. Only clear the slot if it still holds the
        instance we just found dead — a concurrent caller may have
        already replaced it.
        """
        with cls._runtime_lock:
            if cls._runtime is dead:
                cls._runtime = None

    def acquire(
        self, url: str, need: str, *, delay_before_return_html: float = 1.5
    ) -> AcquisitionResult:
        try:
            self.target_validator(url)
        except UnsafeTarget as exc:
            reason = "ssrf_rejection" if "non-public" in str(exc).lower() else "invalid_url"
            raise AcquisitionError(reason, str(exc)) from exc
        if not self._slots.acquire(timeout=self.timeout_seconds):
            raise AcquisitionError(
                "concurrency_saturated",
                "Crawl4AI acquisition concurrency is saturated.",
            )
        try:
            runtime = self._shared_runtime()
            future = runtime.submit(
                self._crawl(
                    runtime, url, need, delay_before_return_html=delay_before_return_html
                )
            )
            try:
                return future.result(timeout=self.timeout_seconds)
            except concurrent.futures.TimeoutError as exc:
                future.cancel()
                raise AcquisitionError(
                    "timeout",
                    f"Crawl4AI acquisition exceeded {self.timeout_seconds} seconds.",
                    retryable=True,
                ) from exc
            except AcquisitionError as exc:
                if exc.reason == "browser_crashed" or _looks_like_dead_browser(
                    exc.message
                ):
                    self._invalidate_runtime(runtime)
                raise
            except Exception as exc:
                if _looks_like_dead_browser(str(exc)):
                    self._invalidate_runtime(runtime)
                raise
        finally:
            self._slots.release()

    async def _crawl(
        self,
        runtime: _Crawl4AIRuntime,
        start_url: str,
        need: str,
        *,
        delay_before_return_html: float = 1.5,
    ) -> AcquisitionResult:
        assert runtime.crawler is not None
        pages: list[AcquiredPage] = []
        first_failure: Optional[AcquisitionError] = None
        seen: set[str] = set()
        queue: list[tuple[str, int]] = [(start_url, 0)]
        allowed_host = (urlparse(start_url).hostname or "").lower()

        runtime.crawler.crawler_strategy.set_hook(
            "on_page_context_created", _ssrf_route_hook(self.target_validator)
        )

        while queue and len(pages) < self.max_pages:
            url, depth = queue.pop(0)
            key = url.split("#", 1)[0]
            if key in seen:
                continue
            seen.add(key)
            try:
                self.target_validator(url)
            except UnsafeTarget as exc:
                if first_failure is None:
                    first_failure = AcquisitionError("ssrf_rejection", str(exc))
                continue

            run_config = CrawlerRunConfig(
                cache_mode=CacheMode.BYPASS,
                page_timeout=REQUEST_TIMEOUT_SECONDS * 1000,
                wait_until="domcontentloaded",
                # Default 1.5s left client-rendered pages (a data table
                # fetched via XHR after DOMContentLoaded, e.g. a React/Vue
                # SPA) captured before their own fetch/render cycle
                # finished, producing a page with real table markup but
                # empty/placeholder cells -- structurally valid,
                # semantically blank. Comfortably inside
                # REQUEST_TIMEOUT_SECONDS (15s default) either way.
                # web_tools._read passes a longer value as a same-page retry
                # when this default already came back needing more time
                # (heavier SPAs like MSN's article pages) -- see
                # javascript_required in html_extract.py.
                delay_before_return_html=delay_before_return_html,
                remove_consent_popups=True,
                check_robots_txt=False,
                max_retries=MAX_RETRIES,
                verbose=False,
                stream=False,
            )
            try:
                result = await runtime.crawler.arun(url=url, config=run_config)
            except Exception as exc:  # noqa: BLE001
                if first_failure is None:
                    first_failure = _classified_runtime_error(str(exc))
                continue

            status = result.status_code or 0
            if not result.success:
                message = result.error_message or "Crawl4AI navigation failed."
                err = (
                    classify_http_status(url, status)
                    if status >= 400
                    else _classified_runtime_error(message)
                )
                if first_failure is None:
                    first_failure = err
                continue
            if status and status >= 400:
                err = classify_http_status(url, status)
                if first_failure is None:
                    first_failure = err
                continue

            html = result.html or ""
            body_bytes = len(html.encode("utf-8", "replace"))
            if body_bytes > MAX_RESPONSE_BYTES:
                if first_failure is None:
                    first_failure = AcquisitionError(
                        "response_too_large",
                        f"Response exceeded the {MAX_RESPONSE_BYTES}-byte limit.",
                    )
                continue

            content_type = ""
            for header_name, header_value in (result.response_headers or {}).items():
                if header_name.lower() == "content-type":
                    content_type = str(header_value).lower()
                    break
            if content_type and not any(
                allowed in content_type
                for allowed in (
                    "text/html",
                    "application/xhtml",
                    "text/plain",
                    "text/xml",
                    "application/xml",
                )
            ):
                if first_failure is None:
                    first_failure = AcquisitionError(
                        "unsupported_content_type",
                        f"{url} returned unsupported content type {content_type.split(';')[0]}.",
                    )
                continue

            page = extract_page(
                url=result.redirected_url or url,
                status=status or 200,
                html=html,
                content_type=content_type,
                body_bytes=body_bytes,
            )
            pages.append(page)

            if depth >= MAX_CRAWL_DEPTH:
                continue
            remaining = self.max_pages - len(pages)
            if remaining <= 0:
                continue
            candidates = [
                (target, label)
                for target, label in page.links
                if (urlparse(target).hostname or "").lower() == allowed_host
                and target.split("#", 1)[0] not in seen
            ]
            ranked = sorted(
                candidates,
                key=lambda item: link_score(item[1], item[0], need),
                reverse=True,
            )
            for target, label in ranked[:remaining]:
                if link_score(label, target, need) <= 0:
                    continue
                queue.append((target, depth + 1))

        if pages:
            return AcquisitionResult(
                pages=pages, page_limit_reached=len(pages) >= self.max_pages
            )
        raise first_failure or AcquisitionError(
            "acquisition_failure",
            "Crawl4AI acquisition ended without a usable response.",
        )


_DEFAULT_ENGINE: Optional[Crawl4AIAcquisitionEngine] = None
_DEFAULT_ENGINE_LOCK = threading.Lock()


def get_crawl4ai_engine() -> Crawl4AIAcquisitionEngine:
    global _DEFAULT_ENGINE
    with _DEFAULT_ENGINE_LOCK:
        if _DEFAULT_ENGINE is None:
            _DEFAULT_ENGINE = Crawl4AIAcquisitionEngine()
        return _DEFAULT_ENGINE


__all__ = [
    "ACQUIRE_TIMEOUT_SECONDS",
    "AcquiredPage",
    "AcquisitionError",
    "AcquisitionResult",
    "Crawl4AIAcquisitionEngine",
    "MAX_CRAWL_DEPTH",
    "MAX_CRAWL_PAGES",
    "MAX_RESPONSE_BYTES",
    "REQUEST_TIMEOUT_SECONDS",
    "get_crawl4ai_engine",
]
