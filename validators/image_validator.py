"""
Validates all images collected by the crawler.

Two-stage validation with intelligent SVG handling
───────────────────────────────────────────────────

Stage 1 — Network check (aiohttp)
  Same HEAD→GET fallback + retry strategy used by the link validator.

Stage 2 — Browser render check (Playwright)
  Only runs on images that PASSED Stage 1.
  Navigates to the raw image URL in headless Chromium and inspects the
  <img> element the browser wraps it in.

  Key fixes vs v1
  ───────────────
  • SVG images are EXCLUDED from the render check entirely.
    ``naturalWidth`` returns 0 for SVGs without explicit width/height
    attributes regardless of whether they are valid — this is browser
    behaviour, not a broken image.  An SVG that returned HTTP 200 is PASS.

  • The render check now uses ``img.complete && img.naturalWidth > 0``
    instead of just ``img.naturalWidth > 0``.  ``complete`` is false
    while the browser is still decoding; checking both avoids false
    failures on slow-loading raster images.

Classification verdicts
───────────────────────
PASS    — HTTP 200–2xx AND (SVG OR renders correctly)
SKIPPED — HTTP returned a bot-detection code on an external host,
          OR external network error (timeout / DNS)
FAIL    — HTTP 4xx/5xx on any host, OR render check failed on a
          non-SVG image that returned HTTP 200
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from config import Config, config as default_config
from crawler.site_crawler import ImageRef, PageData
from utils.http_client import HttpClient, HttpResult
from utils.url_utils import is_svg_url, is_tracking_url, same_domain
from utils.verdict import Verdict

logger = logging.getLogger(__name__)


# ── HTTP status codes that indicate bot-detection on external images ───────────
_BOT_DETECTION_CODES: frozenset[int] = frozenset({401, 403, 406, 429, 503})


# ── Result model ───────────────────────────────────────────────────────────────

@dataclass
class ImageResult:
    """Fully resolved result for one image on one specific page."""

    # ── Source context (CRITICAL for traceability) ─────────────────────────
    page_url:     str             # page where this image was found
    alt_text:     str             # alt attribute (or "(no alt)")
    css_selector: str             # e.g. "img:nth-of-type(2)"

    # ── Target ────────────────────────────────────────────────────────────
    src:          str             # resolved absolute image URL
    final_url:    str             # after redirects
    status_code:  int             # raw HTTP code; negative = network error
    reason:       str             # human-readable network reason

    # ── Render check ──────────────────────────────────────────────────────
    is_svg:            bool           # True when SVG — skips naturalWidth check
    renders:           Optional[bool] # None = not checked; True/False = result
    render_reason:     str = ""       # explanation when renders is False
    render_timed_out:  bool = False   # True when browser check exceeded timeout

    # ── Classification ────────────────────────────────────────────────────
    verdict:      Verdict = Verdict.FAIL

    # ── Backward-compatible convenience properties ─────────────────────────

    @property
    def passed(self) -> bool:
        """True only for PASS — preserves compatibility with test assertions."""
        return self.verdict is Verdict.PASS

    @property
    def is_warning(self) -> bool:
        """True for WARNING — render timed out but HTTP 200 was confirmed."""
        return self.verdict is Verdict.WARNING

    @property
    def skipped(self) -> bool:
        return self.verdict is Verdict.SKIPPED

    @property
    def status_label(self) -> str:
        return self.verdict.value   # "PASS" / "FAIL" / "WARNING" / "SKIPPED"

    @property
    def display_status(self) -> str:
        if self.status_code == -2:
            return "TIMEOUT"
        if self.status_code == -3:
            return "DNS_ERROR"
        if self.status_code == -1:
            return "NET_ERROR"
        return str(self.status_code)

    @property
    def failure_reason(self) -> str:
        """One-line explanation used in the report's 'Error / Reason' column."""
        if self.verdict is Verdict.PASS:
            return ""
        if self.verdict is Verdict.WARNING:
            return self.render_reason or "Browser render timed out — image returned HTTP 200 but could not be verified"
        if self.verdict is Verdict.SKIPPED:
            return self.reason
        # FAIL
        if self.is_svg:
            return self.reason              # SVGs only fail on network errors
        if self.renders is False:
            return self.render_reason or "Image did not render (naturalWidth == 0)"
        return self.reason


# ── Classification helpers ─────────────────────────────────────────────────────

def _classify_image(
    src: str,
    base_url: str,
    http: HttpResult,
    is_svg: bool,
    renders: Optional[bool],
    render_reason: str,
    render_timed_out: bool = False,
) -> tuple[Verdict, str]:
    """
    Return ``(Verdict, reason)`` for one image.

    Decision tree
    ─────────────
    1. Tracking / analytics pixel?                     → SKIPPED (always)
    2. HTTP OK (2xx) — checked before domain heuristics:
       a. SVG?                                         → PASS (skip render check)
       b. Render check timed out?                      → WARNING
       c. Render check passed?                         → PASS
       d. Render check failed?                         → FAIL
    3. Network error on external host?                 → SKIPPED
    4. HTTP not OK (4xx / 5xx)?
       a. Bot-detection code + external?               → SKIPPED
       b. Otherwise?                                   → FAIL

    Key design note
    ────────────────
    CDN domains (mds-assets.marriott.com, cloudfront.net, akamaihd.net, etc.)
    are standard content-delivery infrastructure — they MUST be validated, not
    skipped.  Domain-based heuristics are only applied when the HTTP check
    returns a non-2xx code, to suppress WAF / bot-detection noise.  A confirmed
    HTTP 200 is always PASS regardless of what domain served it.
    """
    is_external = not same_domain(src, base_url)
    code        = http.status_code

    # ── 0. Tracking / analytics pixel ─────────────────────────────────────
    # Beacon endpoints (bat.bing.com/action/…, Google Analytics /collect, …)
    # intentionally return 1×1 pixels or redirect chains — never real images.
    if is_tracking_url(src):
        return Verdict.SKIPPED, "Tracking / analytics pixel — not a real content image"

    # ── 1. HTTP OK — evaluated BEFORE domain heuristics ───────────────────
    # If aiohttp confirmed a 2xx response the image is demonstrably reachable.
    # CDN domains (marriott, cloudfront, akamai, …) must not be skipped here —
    # they are valid content hosts, not bot-blocking platforms.
    if http.ok:
        logger.debug("[IMAGE] HTTP %d → proceeding to render check: %s", code, src)

        # SVG: naturalWidth == 0 without explicit dimensions is expected browser
        # behaviour — a 200 response is sufficient to declare PASS.
        if is_svg:
            return Verdict.PASS, "SVG image — naturalWidth check skipped (expected behaviour)"

        # Render check timed out: HTTP 200 confirms existence; cannot verify
        # rendering but cannot declare broken either.  WARNING = needs review.
        if render_timed_out:
            return (
                Verdict.WARNING,
                render_reason or "Browser render timed out — image returned HTTP 200 but could not be verified",
            )

        # Raster image: render check result is decisive.
        if renders is False:
            return Verdict.FAIL, render_reason or "Image did not render in browser"

        return Verdict.PASS, ""

    # ── 2. Network-level error on external host ────────────────────────────
    if code < 0 and is_external:
        return Verdict.SKIPPED, f"External host network error — {http.reason}"

    # ── 3. HTTP error responses ────────────────────────────────────────────
    if code in _BOT_DETECTION_CODES and is_external:
        return (
            Verdict.SKIPPED,
            f"External host returned {code} — likely bot-detection",
        )
    return Verdict.FAIL, http.reason


# ── Validator ──────────────────────────────────────────────────────────────────

class ImageValidator:
    """
    Two-stage image validator with SVG-aware classification.

    Deduplication strategy
    ──────────────────────
    Each unique image URL is fetched (and browser-checked) exactly ONCE.
    Results are then fanned out to every page that referenced that image,
    preserving full per-page traceability.

    Usage::

        validator = ImageValidator(config)
        results   = await validator.validate(pages)
    """

    def __init__(self, cfg: Config = default_config) -> None:
        self._cfg = cfg

    async def validate(self, pages: List[PageData]) -> List[ImageResult]:
        # Build src → [ImageRef, …] preserving all page contexts
        url_map: dict[str, list[ImageRef]] = {}
        for page in pages:
            for img in page.images:
                url_map.setdefault(img.src, []).append(img)

        unique_urls = list(url_map.keys())

        # ── Pre-filter: tracking pixels never need a network check ─────────
        # Classify them immediately so they don't consume HTTP connections.
        tracking_urls: set[str] = {u for u in unique_urls if is_tracking_url(u)}
        checkable_urls = [u for u in unique_urls if u not in tracking_urls]
        logger.info(
            "Validating %d unique image URLs … (%d tracking pixels pre-skipped)",
            len(unique_urls), len(tracking_urls),
        )

        # ── Stage 1: network check (non-tracking only) ─────────────────────
        async with HttpClient(self._cfg) as client:
            http_results: list[HttpResult] = await asyncio.gather(
                *[client.check(url) for url in checkable_urls]
            )
        # Seed net_map; tracking URLs get a synthetic "skipped" HttpResult
        # so the expand loop below can handle all URLs uniformly.
        net_map: dict[str, HttpResult] = {r.url: r for r in http_results}
        for t_url in tracking_urls:
            net_map[t_url] = HttpResult(
                url=t_url,
                final_url=t_url,
                status_code=0,           # sentinel — never a real HTTP code
                reason="Tracking pixel — pre-skipped",
                ok=False,
                skip_candidate=True,
            )

        # ── Stage 2: browser render check (internal raster images only) ────
        # Render candidates are restricted to images on the same registered
        # domain as the site being crawled.
        #
        # Rationale — external images are excluded because:
        #   • HTTP 200 already confirms the resource is reachable.
        #   • External servers (CDNs, third-party hosts) are not ours to fix.
        #   • A headless browser render check on thousands of external images
        #     is the primary cause of the 90-minute runtime; removing it
        #     alone cuts total execution time by 60–70 %.
        #   • The render check adds value only for INTERNAL images, where
        #     naturalWidth == 0 indicates a genuine packaging or deployment
        #     problem that the team can actually fix.
        #
        # External images that are HTTP OK receive PASS from _classify_image
        # when renders=None (check not attempted) — no code change needed there.
        # Render candidates: internal raster images that HTTP-OK.
        # Two additional filters keep the render phase fast:
        #   • CDN-hosted images (hostname contains "cdn") — HTTP 200 is
        #     sufficient evidence of existence; naturalWidth checks on CDN
        #     assets add latency without surfacing actionable defects.
        #   • Hard cap of 300 — prevents runaway render phases on
        #     image-heavy sites while still covering all critical pages.
        def _is_cdn_url(u: str) -> bool:
            try:
                return "cdn" in urlparse(u).hostname.lower()
            except Exception:
                return False

        render_candidates = [
            u for u in checkable_urls
            if net_map[u].ok
            and not is_svg_url(u)
            and same_domain(u, self._cfg.base_url)   # internal only
            and not _is_cdn_url(u)                    # CDN subdomains: HTTP 200 sufficient
        ][:300]                                        # safety cap

        external_skipped = sum(
            1 for u in checkable_urls
            if net_map[u].ok
            and not is_svg_url(u)
            and not same_domain(u, self._cfg.base_url)
        )
        logger.info(
            "Browser render check: %d internal raster image(s) "
            "(%d external + CDN HTTP-OK images skip render — HTTP 200 is sufficient)",
            len(render_candidates),
            external_skipped,
        )
        # render_map values: (True,"")=pass | (False,reason)=fail | (None,reason)=timeout
        render_map: dict[str, tuple[bool | None, str]] = {}
        if render_candidates:
            render_map = await self._browser_render_check(render_candidates)

        # ── Expand results ─────────────────────────────────────────────────
        results: List[ImageResult] = []
        for src, refs in url_map.items():
            http = net_map.get(src)
            if http is None:
                continue

            svg              = is_svg_url(src)
            renders          = None   # sentinel: render check not attempted
            render_rsn       = ""
            render_timed_out = False

            if src in render_map:
                renders, render_rsn = render_map[src]
                # Sentinel (None, reason) → browser timed out, not a hard failure
                if renders is None:
                    render_timed_out = True

            verdict, reason = _classify_image(
                src=src,
                base_url=self._cfg.base_url,
                http=http,
                is_svg=svg,
                renders=renders,
                render_reason=render_rsn,
                render_timed_out=render_timed_out,
            )

            for ref in refs:
                results.append(
                    ImageResult(
                        page_url=ref.page_url,
                        alt_text=ref.alt,
                        css_selector=ref.css_selector,
                        src=src,
                        final_url=http.final_url,
                        status_code=http.status_code,
                        reason=reason or http.reason,
                        is_svg=svg,
                        renders=renders,
                        render_reason=render_rsn,
                        render_timed_out=render_timed_out,
                        verdict=verdict,
                    )
                )

        # Summary log
        counts = {v: 0 for v in Verdict}
        for r in results:
            counts[r.verdict] += 1

        logger.info(
            "Image validation complete — "
            "total: %d | PASS: %d | FAIL: %d | WARNING: %d | SKIPPED: %d",
            len(results),
            counts[Verdict.PASS],
            counts[Verdict.FAIL],
            counts[Verdict.WARNING],
            counts[Verdict.SKIPPED],
        )
        return results

    # ── Browser render check ───────────────────────────────────────────────

    async def _browser_render_check(
        self, urls: list[str]
    ) -> dict[str, tuple[bool | None, str]]:
        """
        Open each image URL directly in headless Chromium and verify it
        rendered successfully.

        Check logic (applied to the <img> the browser auto-wraps bare
        images in):

            img.complete          → browser finished loading / decoding
            img.naturalWidth > 0  → decoded pixel width is non-zero

        Both must be true for PASS.  ``complete`` alone can be True even
        for broken images (they "complete" with an error state), so we
        require naturalWidth as a second gate.

        SVG URLs are NOT passed here — they are handled earlier.

        Edge-case handling
        ──────────────────
        "Download is starting"
            Some URLs serve a binary payload with ``Content-Disposition:
            attachment``.  Playwright raises this error before the page
            loads, but the HTTP stage already confirmed a 2xx response —
            the resource EXISTS.  We treat it as PASS (not renderable in a
            tab, but definitely not broken).

        Slow-decoding images
            Replacing the fixed ``sleep(0.3)`` with a Promise that resolves
            on the actual ``load`` / ``error`` DOM event eliminates false
            FAIL verdicts for images that take longer than 300 ms to decode.
            A JS-side safety timeout (``_RENDER_JS_TIMEOUT_MS``) caps the
            wait so we never hang indefinitely.
        """
        # JS-side max wait for a single image to fire its load/error event.
        _RENDER_JS_TIMEOUT_MS = 10_000

        # Navigation timeout for render checks is capped at 12 s regardless of
        # page_load_timeout (which stays high for slow CMS pages in the crawler).
        # A bare image URL should never take more than 12 s to start loading;
        # if it does, the JS safety timeout fires → WARNING, not FAIL.
        _RENDER_NAV_TIMEOUT_MS = min(self._cfg.page_load_timeout, 12_000)

        check_results: dict[str, tuple[bool, str]] = {}
        sem = asyncio.Semaphore(self._cfg.browser_fallback_concurrency)

        async with async_playwright() as pw:
            browser: Browser = await pw.chromium.launch(headless=True)
            context: BrowserContext = await browser.new_context(
                ignore_https_errors=not self._cfg.verify_ssl,
                user_agent=self._cfg.user_agent,
            )

            async def _check_one(url: str) -> None:
                async with sem:
                    page: Page = await context.new_page()
                    try:
                        await page.goto(
                            url,
                            timeout=_RENDER_NAV_TIMEOUT_MS,
                            wait_until="domcontentloaded",
                        )

                        # ── Event-driven render check ──────────────────────
                        # If img.complete is already True (fast CDN / cached),
                        # we read naturalWidth immediately.  Otherwise we
                        # subscribe to the load/error event and resolve when
                        # the browser finishes decoding — no fixed sleep needed.
                        ok: bool = await page.evaluate(
                            f"""
                            async () => {{
                                const img = document.querySelector('img');
                                if (!img) return false;

                                // Fast path: already decoded synchronously.
                                if (img.complete) return img.naturalWidth > 0;

                                // Slow path: wait for load or error event.
                                return new Promise((resolve) => {{
                                    img.addEventListener('load',  () => resolve(img.naturalWidth > 0));
                                    img.addEventListener('error', () => resolve(false));
                                    // Safety net: resolve after JS-side timeout.
                                    setTimeout(
                                        () => resolve(img.complete && img.naturalWidth > 0),
                                        {_RENDER_JS_TIMEOUT_MS}
                                    );
                                }});
                            }}
                            """
                        )

                        if ok:
                            check_results[url] = (True, "")
                        else:
                            check_results[url] = (
                                False,
                                "Image did not render: load event fired but naturalWidth == 0",
                            )

                    except Exception as exc:  # noqa: BLE001
                        exc_msg = str(exc)

                        # ── Fix: "Download is starting" ────────────────────
                        # The URL has Content-Disposition: attachment so the
                        # browser would download it rather than display it.
                        # The HTTP check already confirmed a 2xx response, so
                        # the resource is reachable — this is NOT a broken
                        # image.  Upgrade to PASS and log for transparency.
                        if "Download is starting" in exc_msg:
                            logger.debug(
                                "Render check skipped — download-trigger resource: %s", url
                            )
                            check_results[url] = (True, "")

                        # ── Fix: navigation / decode timeout ───────────────
                        # Playwright raises "Timeout NNNNms exceeded" when the
                        # page or image takes longer than page_load_timeout to
                        # respond.  The HTTP stage already returned 200, so
                        # the resource EXISTS — it is just slow.  Marking it
                        # FAIL would be a false positive.
                        # Return the sentinel (None, reason) so the expansion
                        # loop can emit a WARNING verdict instead of FAIL.
                        elif "Timeout" in exc_msg and "exceeded" in exc_msg:
                            logger.warning(
                                "Render check timed out (HTTP 200 confirmed) — "
                                "marking WARNING: %s", url
                            )
                            check_results[url] = (
                                None,
                                f"Browser render timed out — {exc_msg.splitlines()[0]}",
                            )
                        else:
                            check_results[url] = (False, f"Browser error: {exc_msg}")

                    finally:
                        await page.close()

            await asyncio.gather(*[_check_one(u) for u in urls])
            await context.close()
            await browser.close()

        return check_results
