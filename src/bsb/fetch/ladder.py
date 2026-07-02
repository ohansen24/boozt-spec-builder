"""Fetcher ladder (build kit 6.3): polite httpx first, Playwright fallback.

Politeness defaults are always on: max 1 request per 2 seconds per host,
exponential backoff on 429/5xx/transport errors, per-host stop-loss, and
cache-first (a cache hit never touches the network). Sessions are minted
fresh at runtime — no captured cookies are ever hardcoded.

The Playwright rung exists for bot-shell responses: it opens the referer PDP
once, accepts the consent banner, keeps the context alive, and routes
subsequent calls through the context's APIRequestContext so they inherit the
real runtime cookies/fingerprint. Firecrawl (rung 3) is not implemented —
httpx currently succeeds cookie-less against narscosmetics.eu.
"""

import time
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from bsb.fetch.cache import CachedFetch, HttpCache

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)

CONSENT_SELECTORS = [
    "#onetrust-accept-btn-handler",
    "button#truste-consent-button",
    'button[aria-label="Accept all"]',
]


class FetchError(RuntimeError):
    pass


class HostStopLoss(FetchError):
    """Too many consecutive failures for one host — stop hammering it."""


class BotShell(FetchError):
    """Response came back 200 but failed the caller's payload validator."""

    def __init__(self, message: str, fetch: CachedFetch):
        super().__init__(message)
        self.fetch = fetch


class HostRateLimiter:
    def __init__(self, min_interval: float = 2.0):
        self.min_interval = min_interval
        self._last: dict[str, float] = {}

    def wait(self, host: str) -> None:
        now = time.monotonic()
        last = self._last.get(host)
        if last is not None:
            delta = now - last
            if delta < self.min_interval:
                time.sleep(self.min_interval - delta)
        self._last[host] = time.monotonic()


class PoliteFetcher:
    def __init__(
        self,
        cache: HttpCache,
        user_agent: str = DEFAULT_UA,
        min_interval: float = 2.0,
        max_retries: int = 3,
        stop_loss: int = 5,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,  # injectable for tests
    ):
        self.cache = cache
        self.limiter = HostRateLimiter(min_interval)
        self.max_retries = max_retries
        self.stop_loss = stop_loss
        self._consecutive_failures: dict[str, int] = {}
        self.user_agent = user_agent
        self._client = httpx.Client(
            headers={"user-agent": user_agent, "accept-language": "en-US,en;q=0.9"},
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def _record_failure(self, host: str) -> None:
        self._consecutive_failures[host] = self._consecutive_failures.get(host, 0) + 1

    def get(
        self,
        url: str,
        *,
        referer: str | None = None,
        ajax: bool = False,
        use_cache: bool = True,
        validator=None,
    ) -> CachedFetch:
        """Cache-first GET. `validator(text) -> bool` marks bot-shells: a 200
        that fails validation raises BotShell (and is not cached) so the
        caller can escalate to the next rung."""
        if use_cache:
            cached = self.cache.get(url)
            if cached is not None:
                return cached

        host = urlsplit(url).netloc
        if self._consecutive_failures.get(host, 0) >= self.stop_loss:
            raise HostStopLoss(f"{host}: {self.stop_loss} consecutive failures — stop-loss hit")

        headers = {
            "accept": "text/html, */*; q=0.01"
            if ajax
            else "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        }
        if ajax:
            headers["x-requested-with"] = "XMLHttpRequest"
        if referer:
            headers["referer"] = referer

        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            self.limiter.wait(host)
            try:
                response = self._client.get(url, headers=headers)
            except httpx.TransportError as exc:
                last_error = exc
                self._record_failure(host)
                time.sleep(2.0 * 2**attempt)
                continue

            if response.status_code in (429, 500, 502, 503, 504):
                last_error = FetchError(f"{url}: HTTP {response.status_code}")
                self._record_failure(host)
                time.sleep(2.0 * 2**attempt)
                continue

            if response.status_code != 200:
                self._record_failure(host)
                raise FetchError(f"{url}: HTTP {response.status_code}")

            fetch = CachedFetch(
                url=url,
                final_url=str(response.url),
                status=response.status_code,
                content_type=response.headers.get("content-type", ""),
                text=response.text,
                fetched_at=datetime.now(UTC),
            )
            if validator is not None and not validator(fetch.text):
                self._record_failure(host)
                raise BotShell(f"{url}: 200 but payload failed validation (bot shell?)", fetch)

            self._consecutive_failures[host] = 0
            return self.cache.put(fetch)

        raise FetchError(f"{url}: giving up after {self.max_retries} attempts") from last_error


class PlaywrightSession:
    """Fallback rung: real browser context, consent accepted once, controller
    calls routed through the context's APIRequestContext. Started lazily —
    never launched while plain httpx keeps working."""

    def __init__(self, cache: HttpCache, limiter: HostRateLimiter, user_agent: str = DEFAULT_UA):
        self.cache = cache
        self.limiter = limiter
        self.user_agent = user_agent
        self._pw = None
        self._context = None
        self._page = None

    def _ensure_started(self, warmup_url: str) -> None:
        if self._context is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover
            raise FetchError("playwright not installed") from exc
        self._pw = sync_playwright().start()
        try:
            browser = self._pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - browser binary missing
            raise FetchError(
                "Chromium not installed for Playwright — run: .venv/bin/playwright install chromium"
            ) from exc
        self._context = browser.new_context(user_agent=self.user_agent, locale="en-US")
        self._page = self._context.new_page()
        self.limiter.wait(urlsplit(warmup_url).netloc)
        self._page.goto(warmup_url, wait_until="domcontentloaded")
        for selector in CONSENT_SELECTORS:
            try:
                self._page.locator(selector).click(timeout=3000)
                break
            except Exception:
                continue

    def get(
        self, url: str, *, referer: str, ajax: bool = True, use_cache: bool = True
    ) -> CachedFetch:
        if use_cache:
            cached = self.cache.get(url)
            if cached is not None:
                return cached
        self._ensure_started(referer)
        self.limiter.wait(urlsplit(url).netloc)
        headers = {"referer": referer}
        if ajax:
            headers["x-requested-with"] = "XMLHttpRequest"
            headers["accept"] = "text/html, */*; q=0.01"
        response = self._context.request.get(url, headers=headers)
        if response.status != 200:
            raise FetchError(f"{url}: HTTP {response.status} via playwright")
        fetch = CachedFetch(
            url=url,
            final_url=response.url,
            status=response.status,
            content_type=response.headers.get("content-type", ""),
            text=response.text(),
            fetched_at=datetime.now(UTC),
            via="playwright",
        )
        return self.cache.put(fetch)

    def close(self) -> None:  # pragma: no cover
        if self._context is not None:
            self._context.browser.close()
        if self._pw is not None:
            self._pw.stop()
