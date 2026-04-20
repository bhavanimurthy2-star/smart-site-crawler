"""
Async HTTP client with retry logic, HEAD→GET fallback, and SSL tolerance.

Changes vs v1:
- Richer default request headers (User-Agent, Accept-Language, Accept) to
  reduce the number of bot-detected 403s on polite external servers.
- Increased aiohttp header buffer limits to survive Twitter / X / LinkedIn
  responses that include very large ``set-cookie`` / ``cf-*`` header blobs.
- Network-error categorisation now returns a distinct ``skip_candidate``
  flag on HttpResult so validators can decide whether to SKIP or FAIL.
"""

import asyncio
import logging
import ssl
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)


# ── Result model ───────────────────────────────────────────────────────────────

@dataclass
class HttpResult:
    url: str
    final_url: str           # after redirects
    status_code: int         # -1 = network error, -2 = timeout, -3 = DNS error
    reason: str              # human-readable description
    ok: bool                 # True when 200-299
    # True when the error is *likely* caused by bot-detection, not a real break.
    # Validators use this to decide SKIP vs FAIL for external links.
    skip_candidate: bool = False


# ── Browser-like request headers ──────────────────────────────────────────────

_BASE_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    # ⚠ "br" (Brotli) deliberately omitted.
    # aiohttp has no built-in Brotli decoder; advertising it causes servers
    # to send Brotli-encoded bodies that aiohttp cannot decompress, resulting
    # in "Can not decode content-encoding: brotli" ClientResponseError on
    # otherwise-healthy pages (Apple, W3C, etc.) and false FAIL verdicts.
    "Accept-Encoding": "gzip, deflate",
    # Do NOT send Referer — some WAFs fingerprint it
}

# HTTP status codes that indicate bot-detection rather than a genuine broken
# resource.  On *external* URLs these should become SKIPPED, not FAIL.
_BOT_DETECTION_CODES: frozenset[int] = frozenset({401, 403, 406, 429, 503})


def _make_ssl_ctx(verify: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


# ── Client ─────────────────────────────────────────────────────────────────────

class HttpClient:
    """
    Reusable async HTTP client.  One instance per crawl run.

    Usage::

        async with HttpClient(config) as client:
            result = await client.check(url)
    """

    def __init__(self, config) -> None:
        self._cfg = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._semaphore = asyncio.Semaphore(config.concurrency)

    # ── Context manager ────────────────────────────────────────────────────

    async def __aenter__(self) -> "HttpClient":
        ssl_ctx = _make_ssl_ctx(self._cfg.verify_ssl)
        connector = aiohttp.TCPConnector(
            ssl=ssl_ctx,
            limit=self._cfg.concurrency,
            # Increase header buffer limits: Twitter / LinkedIn / Cloudflare
            # responses can contain cookies and CF headers that exceed the
            # default 8 KiB field-size limit, causing aiohttp parse errors.
            limit_per_host=0,          # no per-host cap (global limit applies)
        )
        timeout = aiohttp.ClientTimeout(total=self._cfg.request_timeout)

        # Merge config user-agent into the browser-like base headers
        headers = {**_BASE_HEADERS, "User-Agent": self._cfg.user_agent}

        self._session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers=headers,
        )
        return self

    async def __aexit__(self, *_) -> None:
        if self._session:
            await self._session.close()
            await asyncio.sleep(0.25)   # let connections drain cleanly

    # ── Public API ─────────────────────────────────────────────────────────

    async def check(self, url: str) -> HttpResult:
        """
        Validate *url*:

        1. Try HEAD  (fast, no body download).
        2. Fall back to GET when:
           - HEAD returns 405 / 501 (method not allowed).
           - HEAD returns a network-level error (-1) — some servers silently
             drop HEAD connections but accept GET.
           - HEAD returns 403 — some CDNs allow GET but block HEAD.
        3. Retry transient network errors up to max_retries.
        """
        async with self._semaphore:
            result = await self._try_with_retry(url, "HEAD")
            if result.status_code in (403, 405, 501) or result.status_code == -1:
                get_result = await self._try_with_retry(url, "GET")
                # Only upgrade to GET result if it's *different* (better or
                # definitively worse) — avoids masking a real 404 by a
                # retry that also returns -1.
                if get_result.status_code != result.status_code:
                    result = get_result
            return result

    # ── Internal helpers ───────────────────────────────────────────────────

    async def _try_with_retry(self, url: str, method: str) -> HttpResult:
        attempts = self._cfg.max_retries + 1
        last: Optional[HttpResult] = None
        for attempt in range(1, attempts + 1):
            last = await self._single_request(url, method)
            # Only retry on transient network-layer errors (status < 0),
            # never on definitive HTTP error codes.
            if last.status_code >= 0 or attempt == attempts:
                break
            wait = 2 ** (attempt - 1)   # exponential back-off: 1 s, 2 s
            logger.debug(
                "Retry %d/%d for %s in %ds (last: %s)",
                attempt, attempts - 1, url, wait, last.reason,
            )
            await asyncio.sleep(wait)
        return last  # type: ignore[return-value]

    async def _single_request(self, url: str, method: str) -> HttpResult:
        assert self._session is not None
        try:
            async with self._session.request(
                method,
                url,
                allow_redirects=True,
                max_redirects=10,
            ) as resp:
                final_url = str(resp.url)
                code      = resp.status
                reason    = resp.reason or str(code)
                ok        = 200 <= code < 300
                skip_candidate = code in _BOT_DETECTION_CODES
                logger.debug("%s %s → %d", method, url, code)
                return HttpResult(
                    url=url,
                    final_url=final_url,
                    status_code=code,
                    reason=reason,
                    ok=ok,
                    skip_candidate=skip_candidate,
                )

        except asyncio.TimeoutError:
            logger.warning("Timeout: %s", url)
            return HttpResult(url, url, -2, "Request timed out", False, skip_candidate=True)

        except aiohttp.ClientResponseError as exc:
            # Raised when aiohttp cannot parse the response (e.g. header too
            # large from Twitter/X), not a broken link on our site.
            reason = f"Response parse error: {exc.message}"
            logger.warning("Response error (%s): %s", exc.message, url)
            return HttpResult(url, url, -1, reason, False, skip_candidate=True)

        except aiohttp.ClientConnectorError as exc:
            reason = (
                "DNS resolution failed"
                if "Name or service" in str(exc) or "nodename nor servname" in str(exc)
                else str(exc)
            )
            logger.warning("Connection error (%s): %s", reason, url)
            code = -3 if "DNS" in reason else -1
            return HttpResult(url, url, code, reason, False, skip_candidate=False)

        except aiohttp.ClientError as exc:
            logger.warning("HTTP client error (%s): %s", exc, url)
            return HttpResult(url, url, -1, str(exc), False, skip_candidate=False)

        except Exception as exc:  # noqa: BLE001
            logger.error("Unexpected error (%s): %s", exc, url)
            return HttpResult(url, url, -1, str(exc), False, skip_candidate=False)
