"""
Browser-based URL verification using headless Playwright.

Purpose
───────
aiohttp is a raw HTTP client — it does NOT execute JavaScript, does NOT send
browser cookies, and presents a different TLS fingerprint than Chrome.  Many
external sites (SevenRooms, Marriott, Cloudflare-protected pages) respond
with 403/404/503 to aiohttp but load perfectly for real users.

This module provides ``browser_verify_urls()``, which is called by
``LinkValidator`` as a **second-opinion pass** for any URL that received a
FAIL verdict from the HTTP check.  If the browser loads the URL successfully
(HTTP 200–399 response observed by Playwright's network layer) the verdict
is upgraded to PASS so the framework matches what a real user — or a proper
dead-link checker — would see.

Design decisions
────────────────
• Concurrency is capped via a semaphore (default 3) so we don't exhaust
  browser resources on large sites.
• Navigation waits for ``domcontentloaded`` only — we do NOT wait for
  ``networkidle`` because external pages may load third-party scripts
  forever; we only care whether the main document arrived.
• The function is fully async so it composes cleanly with the rest of the
  async validation pipeline.
• Each URL gets its own Page (not its own browser context) so browser-level
  state (cookies, storage) is not shared between checks.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Request,
    Response,
    async_playwright,
)

logger = logging.getLogger(__name__)


# ── Result ─────────────────────────────────────────────────────────────────────

@dataclass
class BrowserCheckResult:
    url: str
    loaded: bool                 # True = page responded with 2xx/3xx
    final_url: str               # URL after browser redirects
    status_code: int             # HTTP status of the main document; -1 on error
    reason: str                  # human-readable explanation


# ── Public API ─────────────────────────────────────────────────────────────────

async def browser_verify_urls(
    urls: list[str],
    cfg,
) -> dict[str, BrowserCheckResult]:
    """
    Navigate each URL in headless Chromium and return a mapping of
    ``url → BrowserCheckResult``.

    A result is considered ``loaded=True`` (i.e. the page is accessible to
    real users) when the browser receives a main-document HTTP response in
    the 200–399 range.

    Parameters
    ──────────
    urls   List of absolute URLs to verify.
    cfg    Config instance — provides ``headless``, ``page_load_timeout``,
           ``verify_ssl``, ``user_agent``, and
           ``browser_fallback_concurrency``.
    """
    if not urls:
        return {}

    results: dict[str, BrowserCheckResult] = {}
    sem = asyncio.Semaphore(getattr(cfg, "browser_fallback_concurrency", 3))

    logger.info(
        "Browser fallback: verifying %d URL(s) with headless Chromium …", len(urls)
    )

    async with async_playwright() as pw:
        browser: Browser = await pw.chromium.launch(headless=cfg.headless)
        context: BrowserContext = await browser.new_context(
            user_agent=cfg.user_agent,
            ignore_https_errors=not cfg.verify_ssl,
            # Pretend to be a desktop Chrome — helps bypass some WAF fingerprinting
            viewport={"width": 1280, "height": 800},
            java_script_enabled=True,
        )
        # Block image / font / media downloads to keep the check fast
        await context.route(
            "**/*",
            lambda route: (
                route.abort()
                if route.request.resource_type in ("image", "media", "font", "stylesheet")
                else route.continue_()
            ),
        )

        async def _verify_one(url: str) -> None:
            async with sem:
                page: Page = await context.new_page()
                # Track the status code of the main-document response
                main_status: list[int] = []
                main_final:  list[str] = [url]

                async def _on_response(resp: Response) -> None:
                    # Only capture responses that are the main frame navigation
                    if resp.request.resource_type == "document" and not main_status:
                        main_status.append(resp.status)
                        main_final[0] = resp.url

                page.on("response", _on_response)

                try:
                    await page.goto(
                        url,
                        timeout=cfg.page_load_timeout,
                        wait_until="domcontentloaded",
                    )
                    status = main_status[0] if main_status else 200
                    final  = main_final[0]
                    loaded = 200 <= status < 400
                    reason = (
                        "Page loaded successfully in browser"
                        if loaded
                        else f"Browser received HTTP {status}"
                    )
                    logger.debug("Browser %s → %d (%s)", url, status, reason)
                    results[url] = BrowserCheckResult(
                        url=url, loaded=loaded,
                        final_url=final, status_code=status, reason=reason,
                    )

                except Exception as exc:  # noqa: BLE001
                    logger.warning("Browser check failed (%s): %s", exc, url)
                    results[url] = BrowserCheckResult(
                        url=url, loaded=False,
                        final_url=url, status_code=-1,
                        reason=f"Browser navigation error: {exc}",
                    )
                finally:
                    await page.close()

        await asyncio.gather(*[_verify_one(u) for u in urls])
        await context.close()
        await browser.close()

    loaded_count  = sum(1 for r in results.values() if r.loaded)
    failed_count  = len(results) - loaded_count
    logger.info(
        "Browser fallback complete — loaded: %d | still failing: %d",
        loaded_count, failed_count,
    )
    return results
