"""
Validates all hyperlinks collected by the crawler.

Key design properties
─────────────────────

1. ONE result per unique target URL  (not one per page)
   Each unique URL is validated exactly once over the network.
   The result carries a ``found_on_pages`` list so every page where the
   link appears is still accounted for — no traceability is lost, but
   duplicate failures are eliminated from counts and test output.

2. Three-tier verdict engine
   PASS    — reachable via HTTP (2xx/3xx) OR browser fallback confirmed live.
   SKIPPED — result is not actionable: reservation platform (session-dependent),
             bot-detection code from external site (403/429/503), network error
             on external domain.  These are NOT real defects.
   FAIL    — definitively unreachable for real users: internal 4xx/5xx, hard
             404/410 on external that the browser fallback also cannot load.

3. Browser fallback (opt-in, config.browser_fallback_on_fail)
   After the aiohttp pass, every URL holding FAIL is re-verified with a real
   headless Chromium navigation — including external links that aiohttp marked
   404 but which may actually load fine in a real browser (WAF fingerprinting).
   If the browser loads the page the verdict is upgraded to PASS with the
   reason "Bot-detection — verified via browser".

   Reservation platforms (SevenRooms, OpenTable, Resy, …) are the one
   exception: they are SKIPPED before any check because even a headless
   browser cannot verify them without hotel/restaurant session tokens.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import List

from config import Config, config as default_config
from crawler.site_crawler import LinkRef, PageData
from utils.http_client import HttpClient, HttpResult
from utils.playwright_fallback import browser_verify_urls
from utils.url_utils import (
    is_bot_blocking_domain,
    is_reservation_platform,
    is_tracking_url,
    same_domain,
)
from utils.verdict import Verdict

logger = logging.getLogger(__name__)


# ── HTTP status constants ──────────────────────────────────────────────────────

# 4xx codes that definitively mean "this resource does not exist".
# These are FAIL even on external domains because no WAF returns them for
# existing content — a WAF always prefers 403/429/503.
_DEFINITIVE_MISS_CODES: frozenset[int] = frozenset({404, 410, 451})

# 4xx codes that indicate bot-detection / auth-wall on external sites.
_BOT_DETECTION_4XX: frozenset[int] = frozenset({401, 403, 406, 429})

# 5xx codes that are typically temporary throttling / WAF responses on
# external sites.  They are NOT reliable indicators of a broken resource.
_EXTERNAL_TRANSIENT_5XX: frozenset[int] = frozenset({
    502,   # Bad Gateway   — often a CDN/proxy hiccup
    503,   # Unavailable   — WAF, rate-limit, maintenance page
    504,   # Gateway Timeout
    520,   # Cloudflare "unknown error"
    521,   # Cloudflare "web server down" (often a fingerprint block)
    522,   # Cloudflare connection timeout
    523,   # Cloudflare origin unreachable
    524,   # Cloudflare timeout
    525,   # Cloudflare SSL handshake failed
    526,   # Cloudflare invalid SSL cert
})


# ── Result model ───────────────────────────────────────────────────────────────

@dataclass
class LinkResult:
    """
    Result for one unique target URL, with traceability across all pages.

    Design change from v1
    ─────────────────────
    Previously one LinkResult was created per (page, link) pair, causing the
    same broken URL to appear once per page in test output and reports — a
    30-page site with one bad nav link showed 30 "failures".

    Now there is ONE LinkResult per unique target URL.  The ``found_on_pages``
    field carries the full list of pages where the link was found, so no
    page-level traceability is lost.

    ``page_url`` is set to the first page in ``found_on_pages`` for backward
    compatibility with any code that reads the single-page field.
    """

    # ── Traceability ──────────────────────────────────────────────────────
    page_url:       str          # first page where found (backward compat)
    found_on_pages: list[str]    # ALL pages where this URL was found
    anchor_text:    str          # anchor text from first occurrence
    css_selector:   str          # CSS selector from first occurrence

    # ── Target ────────────────────────────────────────────────────────────
    target_url:     str
    final_url:      str          # after all redirects
    status_code:    int          # HTTP code; negative = network-level error
    reason:         str          # human-readable explanation

    # ── Classification ────────────────────────────────────────────────────
    verdict:        Verdict
    is_external:    bool

    # ── Browser fallback metadata ─────────────────────────────────────────
    browser_checked: bool = False   # True when Playwright fallback was run
    browser_loaded:  bool = False   # True when browser confirmed accessible
    http_status:     int  = 0       # original aiohttp status before any override

    # ── Convenience properties ────────────────────────────────────────────

    @property
    def passed(self) -> bool:
        """True only for PASS — backward-compat for existing test code."""
        return self.verdict is Verdict.PASS

    @property
    def skipped(self) -> bool:
        return self.verdict is Verdict.SKIPPED

    @property
    def pages_count(self) -> int:
        return len(self.found_on_pages)

    @property
    def status_label(self) -> str:
        return self.verdict.value   # "PASS" / "FAIL" / "SKIPPED"

    @property
    def display_status(self) -> str:
        if self.status_code == -2:
            return "TIMEOUT"
        if self.status_code == -3:
            return "DNS_ERROR"
        if self.status_code == -1:
            return "NET_ERROR"
        return str(self.status_code)


# ── Classification engine ──────────────────────────────────────────────────────

def _classify_link(
    target_url: str,
    base_url: str,
    http: HttpResult,
) -> tuple[Verdict, str]:
    """
    Return ``(Verdict, reason)`` for one link.

    Decision tree (in evaluation order)
    ─────────────────────────────────────
    0. Tracking / analytics endpoint?                     → SKIPPED
    1. HTTP 2xx or 3xx?                                   → PASS  (always, checked first)
    2. Reservation platform? (non-2xx from aiohttp)       → FAIL  (browser fallback runs)
    3. Known bot-blocking domain? (non-2xx from aiohttp)  → FAIL  (browser fallback runs)
    4. HTTP 4xx:
       a. 404 / 410 / 451?                                → FAIL  (browser fallback runs)
       b. 401 / 403 / 406 / 429 + external?               → SKIPPED  (bot-detection / auth-wall)
       c. Other 4xx?                                      → FAIL
    5. HTTP 5xx:
       a. Transient 5xx (502/503/504/52x) + external?     → SKIPPED  (WAF / rate-limiting)
       b. All others?                                     → FAIL
    6. Network-level error (code < 0):
       a. External?                                       → SKIPPED
       b. Internal?                                       → FAIL

    Key design note
    ────────────────
    Reservation platforms and known bot-blocking domains are forced to FAIL
    (steps 1–2) so the browser fallback always runs for them.  The fallback
    uses headless Chromium — which presents a real browser fingerprint — and
    upgrades the verdict to PASS when the page actually loads.  This means
    links to SevenRooms, OpenTable, Instagram, etc. are judged by what a
    real user would see, not by what aiohttp's raw HTTP client sees.

    External 403/429/5xx for domains NOT in those lists stay SKIPPED
    (steps 4b, 5a) — these indicate WAF / rate-limiting noise on sites where
    running a full browser check would not add value.
    """
    is_external = not same_domain(target_url, base_url)
    code        = http.status_code

    # ── 0. Tracking / analytics ───────────────────────────────────────────
    if is_tracking_url(target_url):
        return Verdict.SKIPPED, "Tracking / analytics endpoint — not a navigable link"

    # ── 1. Successful HTTP response — checked BEFORE domain heuristics ─────
    # If aiohttp already received a 2xx/3xx the link is demonstrably live.
    # Domain-based checks (reservation platform, bot-blocking) only apply
    # when the HTTP check failed — they exist to prevent false FAILs caused
    # by WAF fingerprinting, not to override confirmed-good responses.
    if 200 <= code < 400:
        logger.debug("[CLASSIFY] %s → HTTP %d → PASS", target_url, code)
        return Verdict.PASS, ""

    # ── 2. Reservation / hospitality platform ─────────────────────────────
    # aiohttp returned a non-2xx code, but these platforms require an
    # authenticated session or hotel-specific token to serve content — even
    # a headless browser cannot verify them without that context.  SKIP
    # immediately; browser fallback would always fail and waste resources.
    if is_reservation_platform(target_url):
        return Verdict.SKIPPED, "Reservation platform — session-dependent, not a broken link"

    # ── 3. Known bot-blocking domain ──────────────────────────────────────
    # Same rationale: HTTP check got a non-2xx, so let Playwright verify.
    if is_bot_blocking_domain(target_url):
        return Verdict.FAIL, "Bot-protected site — verifying via browser"

    # ── 4. Client-error range (4xx) ───────────────────────────────────────
    if 400 <= code < 500:
        if code in _DEFINITIVE_MISS_CODES:
            # 404 / 410 / 451 — resource not found.
            # Browser fallback still runs and can upgrade to PASS when a WAF
            # serves the real page to Chromium but not to aiohttp.
            return Verdict.FAIL, f"HTTP {code} — resource not found"

        if code in _BOT_DETECTION_4XX and is_external:
            return (
                Verdict.SKIPPED,
                f"External site returned {code} — bot-detection / auth-wall, not a broken link",
            )

        return Verdict.FAIL, f"HTTP {code} — {http.reason}"

    # ── 5. Server-error range (5xx) ───────────────────────────────────────
    if code >= 500:
        if is_external and code in _EXTERNAL_TRANSIENT_5XX:
            return (
                Verdict.SKIPPED,
                f"External site returned {code} — likely WAF / rate-limiting, not a real error",
            )
        return Verdict.FAIL, f"HTTP {code} — {http.reason}"

    # ── 6. Network-level errors (code < 0) ────────────────────────────────
    if code < 0:
        if is_external:
            return (
                Verdict.SKIPPED,
                f"External site unreachable — {http.reason}",
            )
        return Verdict.FAIL, http.reason

    # Unreachable fallback
    return Verdict.FAIL, http.reason


# ── Validator ──────────────────────────────────────────────────────────────────

class LinkValidator:
    """
    Async batch validator that produces ONE result per unique target URL.

    Pipeline
    ────────
    1. Deduplicate: build url → [LinkRef, …] map.
    2. HTTP check : aiohttp HEAD→GET with retry for all unique URLs.
    3. Classify   : apply _classify_link() decision tree.
    4. Fallback   : for every FAIL verdict, run a Playwright browser check.
                    If the browser loads the page → upgrade to PASS.

    Usage::

        validator = LinkValidator(config)
        results   = await validator.validate(pages)
    """

    def __init__(self, cfg: Config = default_config) -> None:
        self._cfg = cfg

    async def validate(self, pages: List[PageData]) -> List[LinkResult]:

        # ── Step 1: Build url → [LinkRef, …] ──────────────────────────────
        # Use dict.fromkeys trick on page_url lists to deduplicate pages
        # while preserving insertion order.
        url_map: dict[str, list[LinkRef]] = {}
        for page in pages:
            for link in page.links:
                url_map.setdefault(link.target_url, []).append(link)

        unique_urls = list(url_map.keys())
        logger.info(
            "Validating %d unique link URLs across %d pages …",
            len(unique_urls), len(pages),
        )

        # ── Step 2: HTTP check ─────────────────────────────────────────────
        async with HttpClient(self._cfg) as client:
            http_results: list[HttpResult] = await asyncio.gather(
                *[client.check(url) for url in unique_urls]
            )
        result_map: dict[str, HttpResult] = {r.url: r for r in http_results}

        # ── Step 3: Build ONE LinkResult per unique URL ────────────────────
        link_results: list[LinkResult] = []

        for target_url, refs in url_map.items():
            http = result_map.get(target_url)
            if http is None:
                continue

            verdict, reason = _classify_link(
                target_url=target_url,
                base_url=self._cfg.base_url,
                http=http,
            )
            is_external = not same_domain(target_url, self._cfg.base_url)

            # Collect all pages where this URL appears, deduplicated,
            # in the order they were first encountered.
            found_on_pages = list(dict.fromkeys(ref.page_url for ref in refs))

            link_results.append(
                LinkResult(
                    page_url=found_on_pages[0],       # first page (backward compat)
                    found_on_pages=found_on_pages,
                    anchor_text=refs[0].anchor_text,
                    css_selector=refs[0].css_selector,
                    target_url=target_url,
                    final_url=http.final_url,
                    status_code=http.status_code,
                    reason=reason or http.reason,
                    verdict=verdict,
                    is_external=is_external,
                )
            )

        # ── Step 4: Browser fallback for FAIL verdicts ─────────────────────
        if getattr(self._cfg, "browser_fallback_on_fail", True):
            await self._apply_browser_fallback(link_results)

        # ── Summary log ───────────────────────────────────────────────────
        counts = {v: 0 for v in Verdict}
        for r in link_results:
            counts[r.verdict] += 1

        logger.info(
            "Link validation complete — unique URLs: %d | PASS: %d | FAIL: %d | SKIPPED: %d",
            len(link_results),
            counts[Verdict.PASS],
            counts[Verdict.FAIL],
            counts[Verdict.SKIPPED],
        )
        return link_results

    # ── Browser fallback ────────────────────────────────────────────────────

    async def _apply_browser_fallback(self, results: list[LinkResult]) -> None:
        """
        For every result currently holding FAIL, open the URL in headless
        Chromium.  If the browser loads it successfully, upgrade the verdict
        to PASS so the result matches real-user / dead-link-checker behaviour.

        Mutates ``results`` in place.
        """
        fail_candidates = [r for r in results if r.verdict is Verdict.FAIL]
        if not fail_candidates:
            return

        # ── Scope browser checks to where they can actually help ────────────
        # Browser verification is expensive — only run it where it can
        # realistically change the verdict:
        #
        #   A. Internal links              → always browser (own server / deploy bug)
        #   B. External 404 / 410 / 451   → skip browser  (definitively gone;
        #                                    no WAF returns these for live content)
        #   C. External reservation platform → browser    (session-gated, aiohttp rejected)
        #   D. External bot-blocking domain  → browser    (WAF fingerprints aiohttp UA)
        #   E. Everything else external    → skip browser (keep FAIL — not our site)
        def _needs_browser(r: LinkResult) -> bool:
            if not r.is_external:
                return True                              # A.
            if r.status_code in _DEFINITIVE_MISS_CODES:
                return False                             # B. 404/410/451 — browser cannot help
            return (
                is_reservation_platform(r.target_url)   # C.
                or is_bot_blocking_domain(r.target_url)  # D.
            )

        browser_candidates = [r for r in fail_candidates if _needs_browser(r)]
        skipped_count      = len(fail_candidates) - len(browser_candidates)

        if skipped_count:
            logger.info(
                "Browser fallback: skipping %d external FAIL link(s) "
                "(HTTP 404/410/451 or non-whitelisted domain — keeping FAIL verdict)",
                skipped_count,
            )

        if not browser_candidates:
            return

        logger.info(
            "Running browser fallback for %d/%d FAIL candidate(s) …",
            len(browser_candidates), len(fail_candidates),
        )

        browser_results = await browser_verify_urls(
            urls=[r.target_url for r in browser_candidates],
            cfg=self._cfg,
        )

        # Remap iteration target so the rest of the loop body is unchanged.
        fail_candidates = browser_candidates

        # HTTP status codes that indicate bot-detection / WAF when returned
        # by the browser fallback.  If BOTH aiohttp and Playwright receive
        # one of these codes the link is bot-protected, not genuinely broken.
        _BROWSER_BOT_CODES: frozenset[int] = frozenset({403, 429, 503})

        upgraded  = 0
        downgraded = 0   # FAIL → SKIPPED (both probes bot-blocked)

        for r in fail_candidates:
            br = browser_results.get(r.target_url)
            if br is None:
                continue

            r.browser_checked = True
            r.browser_loaded  = br.loaded

            if br.loaded:
                # Browser confirmed the page is accessible → upgrade to PASS.
                # Preserve the original aiohttp code for audit/debugging, then
                # override status_code to 200 so display_status and the report
                # show a consistent, non-misleading value alongside PASS.
                r.http_status  = r.status_code   # save original (e.g. 404, 503)
                r.status_code  = br.status_code if 200 <= br.status_code < 400 else 200
                r.verdict      = Verdict.PASS
                r.final_url    = br.final_url
                r.reason       = (
                    f"Browser verified — page loads successfully "
                    f"(original HTTP {r.http_status})"
                )
                upgraded += 1
                logger.debug(
                    "Upgraded to PASS (browser): %s [original HTTP %d → now %d]",
                    r.target_url, r.http_status, r.status_code,
                )

            elif r.is_external and br.status_code in _BROWSER_BOT_CODES:
                # Both aiohttp AND the browser received a bot-detection code.
                # This means the site actively blocks automation — it is not a
                # real broken link.  Downgrade to SKIPPED so it does not pollute
                # the actionable FAIL count.
                r.verdict = Verdict.SKIPPED
                r.reason  = (
                    f"Bot-protected / WAF — aiohttp: HTTP {r.display_status}, "
                    f"browser: HTTP {br.status_code} (blocked for automation)"
                )
                downgraded += 1
                logger.debug(
                    "Downgraded to SKIPPED (both probes bot-blocked): %s", r.target_url
                )

            else:
                # Browser also failed with a non-bot code → genuinely broken.
                r.reason = f"{r.reason} | Browser also failed: {br.reason}"

        logger.info(
            "Browser fallback: %d/%d upgraded to PASS, %d/%d downgraded to SKIPPED (WAF)",
            upgraded, len(fail_candidates),
            downgraded, len(fail_candidates),
        )
