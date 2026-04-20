"""
Smart async Playwright crawler.

Strategy
--------
1. Sitemap-first  — fetch /sitemap.xml (and any nested sitemap indexes),
                    seed the queue with every <loc> URL found.
2. BFS fallback   — if no sitemap exists, start BFS from base_url.

Architecture
------------
- asyncio.Queue  : shared work queue of (url, depth) tuples.
- N workers      : each picks an item, opens a Playwright page, extracts
                   links/images, enqueues new URLs, then calls task_done().
- Semaphore      : caps the number of concurrently open browser pages.
- visited set    : global deduplication (no URL is crawled twice).

URL normalisation (per item, before queuing)
--------------------------------------------
  fragment  (#…)  stripped
  query     (?…)  stripped   — removes utm_/ref/session noise
  trailing  /     stripped   — so /page/ == /page

Smart filtering (skip, not fail)
---------------------------------
  Path segments:  /search  /filter  /sort  /calendar
  Always:         mailto:  tel:  javascript:  + ignored_schemes from config
  Assets:         .pdf  .zip  .mp4  etc.   (via is_asset())
  External:       if stay_on_domain, only same registered domain is queued

Depth
-----
  max_depth controls how many hops from the seed layer are followed.
  It is NOT a page count — every page at every depth is crawled.
"""

from __future__ import annotations

import asyncio
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import List, Optional, Set
from urllib.parse import urlparse, urlunparse

import aiohttp
from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from config import Config, config as default_config
from utils.url_utils import (
    is_asset,
    is_ignored_scheme,
    is_valid_http_url,
    normalise,
    same_domain,
)

logger = logging.getLogger(__name__)


# ── Skip patterns applied to crawl candidates (path-level, post-normalization) ─

_SKIP_PATH_FRAGMENTS: tuple[str, ...] = (
    "/search",
    "/filter",
    "/sort",
    "/calendar",
    "/session",
    "/login",
    "/logout",
    "/cart",
    "/checkout",
    "/account",
    "/print",
)


# ── Data models ────────────────────────────────────────────────────────────────

@dataclass
class LinkRef:
    """A hyperlink found on a page."""
    page_url: str
    target_url: str
    anchor_text: str
    css_selector: str = ""


@dataclass
class ImageRef:
    """An image found on a page."""
    page_url: str
    src: str
    alt: str
    css_selector: str = ""


@dataclass
class PageData:
    """All references collected from a single crawled page."""
    url: str
    links: List[LinkRef] = field(default_factory=list)
    images: List[ImageRef] = field(default_factory=list)
    screenshot_path: Optional[str] = None
    error: Optional[str] = None


# ── Smart Crawler ──────────────────────────────────────────────────────────────

class SiteCrawler:
    """
    Async smart crawler: sitemap-first, parallel workers, no page-count limit.

    Usage::

        crawler = SiteCrawler(config)
        pages   = await crawler.crawl()
    """

    def __init__(self, cfg: Config = default_config) -> None:
        self._cfg = cfg
        self._visited: Set[str] = set()
        self._results: List[PageData] = []

    # ── Public entry point ─────────────────────────────────────────────────

    async def crawl(self) -> List[PageData]:
        """Launch browser, run the smart crawl, return PageData list."""
        async with async_playwright() as pw:
            browser: Browser = await pw.chromium.launch(headless=self._cfg.headless)
            context: BrowserContext = await browser.new_context(
                user_agent=self._cfg.user_agent,
                ignore_https_errors=not self._cfg.verify_ssl,
            )
            try:
                await self._run(context)
            finally:
                await context.close()
                await browser.close()
        logger.info("Crawl complete — %d pages collected.", len(self._results))
        return self._results

    # ── Orchestration ──────────────────────────────────────────────────────

    async def _run(self, context: BrowserContext) -> None:
        queue: asyncio.Queue[tuple[str, int]] = asyncio.Queue()

        # ── Phase 1: seed the queue ────────────────────────────────────────
        if self._cfg.sitemap_enabled:
            seed_urls = await self._fetch_sitemap_urls()
        else:
            seed_urls = []

        if seed_urls:
            logger.info("Sitemap: seeding %d URL(s) at depth 0.", len(seed_urls))
            for url in seed_urls:
                norm = self._normalize(url)
                if norm and norm not in self._visited and self._is_crawlable(norm):
                    self._visited.add(norm)
                    await queue.put((norm, 0))
        else:
            logger.info("No sitemap — BFS from base URL.")
            start = self._normalize(self._cfg.base_url)
            self._visited.add(start)
            await queue.put((start, 0))

        # ── Phase 2: pre-create page pool ─────────────────────────────────
        # The pool acts as the concurrency limiter — workers block on
        # page_pool.get() until a page is available, so no semaphore needed.
        page_pool: asyncio.Queue[Page] = asyncio.Queue()
        for _ in range(self._cfg.crawl_concurrency):
            page_pool.put_nowait(await context.new_page())

        workers = [
            asyncio.create_task(self._worker(queue, page_pool))
            for _ in range(self._cfg.crawl_concurrency)
        ]

        # Workers self-terminate via timeout-based queue consumption.
        # No queue.join() needed — gather waits for all workers to exit.
        await asyncio.gather(*workers, return_exceptions=True)

        # ── Phase 3: close pooled pages ────────────────────────────────────
        while not page_pool.empty():
            page = page_pool.get_nowait()
            await page.close()

    # ── Worker ─────────────────────────────────────────────────────────────

    async def _worker(
        self,
        queue: asyncio.Queue,
        page_pool: "asyncio.Queue[Page]",
    ) -> None:
        """Continuously pick URLs from the queue and crawl them.

        Uses timeout-based queue consumption: if no item arrives within
        ``worker_queue_timeout`` seconds the worker exits cleanly instead of
        blocking forever.  This prevents deadlocks when the queue empties
        between bursts of link discovery.

        Pages are borrowed from the shared pool and returned after each URL
        so they are reused across iterations rather than recreated per page.
        """
        while True:
            try:
                url, depth = await asyncio.wait_for(
                    queue.get(), timeout=self._cfg.worker_queue_timeout
                )
            except asyncio.TimeoutError:
                # Queue has been empty for the full timeout window — done.
                break
            page = await page_pool.get()
            try:
                # Optional hard cap (max_pages == 0 means unlimited)
                if self._cfg.max_pages > 0 and len(self._results) >= self._cfg.max_pages:
                    logger.info("Hard cap max_pages=%d reached.", self._cfg.max_pages)
                    queue.task_done()
                    return

                logger.info("[depth=%d] Crawling: %s", depth, url)
                page_data = await self._process_page(page, url)

                self._results.append(page_data)

                # Enqueue discovered links if we can go deeper.
                # Intentionally runs even when page_data.error is set — a
                # partial load may still yield links worth following.
                if depth < self._cfg.max_depth:
                    for link in page_data.links:
                        candidate = link.target_url
                        # candidate is already normalised by _extract_links
                        if (
                            candidate not in self._visited
                            and self._is_crawlable(candidate)
                            and (
                                not self._cfg.stay_on_domain
                                or same_domain(candidate, self._cfg.base_url)
                            )
                        ):
                            self._visited.add(candidate)
                            await queue.put((candidate, depth + 1))

            except Exception as exc:  # noqa: BLE001
                logger.error("Worker error on %s: %s", url, exc)
            finally:
                await page_pool.put(page)
                queue.task_done()

    # ── Sitemap discovery ──────────────────────────────────────────────────

    async def _fetch_sitemap_urls(self) -> List[str]:
        """
        Fetch /sitemap.xml from base_url.
        Handles both regular sitemaps and sitemap index files.
        Returns a deduplicated list of <loc> URLs.
        """
        base = self._cfg.base_url.rstrip("/")
        sitemap_url = f"{base}/sitemap.xml"
        urls: List[str] = []

        try:
            connector = aiohttp.TCPConnector(ssl=False)
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(
                connector=connector, timeout=timeout
            ) as session:
                text = await self._get_text(session, sitemap_url)
                if text is None:
                    # /sitemap.xml not found — check robots.txt for a Sitemap: directive
                    # before falling back to BFS.  Many sites publish their sitemap
                    # at a non-standard path and advertise it only in robots.txt.
                    text = await self._sitemap_from_robots(session, base)
                if text is None:
                    logger.info("No sitemap found (tried /sitemap.xml and robots.txt) — will BFS.")
                    return []

            root = ET.fromstring(text)
            ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

            # Sitemap index: contains <sitemap><loc>…</loc></sitemap> entries
            sub_locs = [
                el.text.strip()
                for el in root.findall(".//sm:sitemap/sm:loc", ns)
                if el.text
            ]
            if sub_locs:
                logger.info("Sitemap index found — fetching %d sub-sitemaps.", len(sub_locs))
                connector2 = aiohttp.TCPConnector(ssl=False)
                async with aiohttp.ClientSession(
                    connector=connector2, timeout=timeout
                ) as session2:
                    tasks = [self._get_text(session2, loc) for loc in sub_locs]
                    texts = await asyncio.gather(*tasks, return_exceptions=True)

                for sub_text in texts:
                    if isinstance(sub_text, str):
                        urls.extend(self._parse_sitemap_locs(sub_text, ns))
            else:
                # Regular sitemap: contains <url><loc>…</loc></url> entries
                urls.extend(self._parse_sitemap_locs(text, ns))

        except ET.ParseError as exc:
            logger.warning("Sitemap XML parse error: %s — falling back to BFS.", exc)
            return []
        except Exception as exc:
            logger.info("Sitemap fetch failed (%s) — falling back to BFS.", exc)
            return []

        # Deduplicate while preserving order
        seen: Set[str] = set()
        unique: List[str] = []
        for u in urls:
            if u not in seen:
                seen.add(u)
                unique.append(u)

        logger.info("Sitemap yielded %d unique URL(s).", len(unique))
        return unique

    @staticmethod
    async def _get_text(
        session: aiohttp.ClientSession, url: str
    ) -> Optional[str]:
        """GET *url* and return the body text, or None on any error."""
        try:
            async with session.get(url, allow_redirects=True) as resp:
                if resp.status == 200:
                    return await resp.text(errors="replace")
                logger.debug("Sitemap GET %s → HTTP %d", url, resp.status)
                return None
        except Exception as exc:
            logger.debug("Sitemap GET %s failed: %s", url, exc)
            return None

    @staticmethod
    async def _sitemap_from_robots(
        session: aiohttp.ClientSession, base: str
    ) -> Optional[str]:
        """
        Fetch ``/robots.txt`` and look for a ``Sitemap:`` directive.
        Returns the XML text of the first advertised sitemap, or None.

        Example robots.txt entry handled::

            Sitemap: https://example.com/sitemap_index.xml
        """
        robots = await SiteCrawler._get_text(session, f"{base}/robots.txt")
        if not robots:
            return None
        for line in robots.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("sitemap:"):
                sm_url = stripped.split(":", 1)[1].strip()
                if sm_url:
                    logger.info("robots.txt advertises sitemap → %s", sm_url)
                    return await SiteCrawler._get_text(session, sm_url)
        return None

    @staticmethod
    def _parse_sitemap_locs(text: str, ns: dict) -> List[str]:
        """Extract all <loc> text values from a sitemap XML string."""
        try:
            root = ET.fromstring(text)
            return [
                el.text.strip()
                for el in root.findall(".//sm:url/sm:loc", ns)
                if el.text
            ]
        except ET.ParseError:
            return []

    # ── URL normalization ──────────────────────────────────────────────────

    @staticmethod
    def _normalize(url: str, base: str = "") -> str:
        """
        Return a canonical URL suitable for deduplication and queuing.

        Transformations applied (in order):
          1. Resolve relative URL against *base* (if provided).
          2. Strip URL fragment  (#section).
          3. Strip query string  (?key=val) — removes utm_/ref/session noise.
          4. Strip trailing slash (except bare root /).
        """
        # Step 1 + 2: use existing helper (resolves relative, strips fragment)
        abs_url = normalise(url, base) if base else normalise(url)
        # Step 3: drop query string
        try:
            p = urlparse(abs_url)
            abs_url = urlunparse((p.scheme, p.netloc, p.path, "", "", ""))
        except Exception:
            pass
        # Step 4: trailing slash already handled by normalise()
        return abs_url

    # ── URL filtering ──────────────────────────────────────────────────────

    def _is_crawlable(self, url: str) -> bool:
        """
        Return True only when *url* should be added to the crawl queue.

        Rejects:
          - non-http(s) schemes
          - ignored schemes from config (javascript:, mailto:, tel:, …)
          - binary assets (.pdf, .zip, .mp4, …)
          - path fragments that indicate utility/session pages
        """
        if not is_valid_http_url(url):
            return False
        if is_ignored_scheme(url, self._cfg.ignored_schemes):
            return False
        if is_asset(url):
            return False
        path = urlparse(url).path.lower()
        if any(frag in path for frag in _SKIP_PATH_FRAGMENTS):
            return False
        return True

    # ── Page processing ────────────────────────────────────────────────────

    async def _process_page(self, page: Page, url: str) -> PageData:
        """Navigate *page* to *url*, extract links and images, and return PageData.

        The caller owns the page lifecycle — this method never closes the page
        so it can be returned to the pool and reused for the next URL.

        Navigation strategy:
          - First attempt  : cfg.crawl_first_attempt_timeout ms (default 15 000).
          - On any failure : one retry at 30 000 ms (slow CMS headroom).
          - If retry fails : error is recorded; extraction is still attempted
            on whatever the page managed to load (best-effort).
        """
        page_data = PageData(url=url)

        # ── Step 1: navigate with retry ────────────────────────────────────
        # First attempt uses cfg.crawl_first_attempt_timeout (default 15 s).
        # Setting this to match the CMS's typical load time avoids wasting the
        # full first-attempt budget before the 30 s retry fires on every page.
        first_timeout = getattr(self._cfg, "crawl_first_attempt_timeout", 15_000)
        try:
            await page.goto(url, timeout=first_timeout, wait_until="domcontentloaded")
        except Exception as first_exc:  # noqa: BLE001
            logger.warning(
                "First attempt failed for %s (%s) — retrying with 30 s timeout",
                url, first_exc,
            )
            try:
                await page.goto(url, timeout=30_000, wait_until="domcontentloaded")
                logger.info("Retry succeeded for %s", url)
            except Exception as retry_exc:  # noqa: BLE001
                logger.error("Retry also failed for %s: %s", url, retry_exc)
                page_data.error = str(retry_exc)
                # Fall through — attempt extraction on whatever loaded.

        # ── Step 2: best-effort extraction (runs even after navigation error) ─
        try:
            # networkidle removed — scroll already fires lazy-load events.
            if self._cfg.scroll_page:
                await self._scroll(page)

            page_data.links  = await self._extract_links(page, url)
            page_data.images = await self._extract_images(page, url)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Extraction failed for %s (non-fatal): %s", url, exc)

        return page_data

    # ── Scrolling ──────────────────────────────────────────────────────────

    @staticmethod
    async def _scroll(page: Page) -> None:
        """Scroll the full page height to trigger lazy-loaded content."""
        try:
            await page.evaluate("""
                async () => {
                    await new Promise((resolve) => {
                        let total = 0;
                        const step = 300;
                        const timer = setInterval(() => {
                            window.scrollBy(0, step);
                            total += step;
                            if (total >= document.body.scrollHeight) {
                                clearInterval(timer);
                                window.scrollTo(0, document.body.scrollHeight);
                                resolve();
                            }
                        }, 80);
                    });
                }
            """)
            await asyncio.sleep(0.2)  # brief pause for lazy images to fire
        except Exception as exc:
            logger.debug("Scroll failed (non-fatal): %s", exc)

    # ── Link extraction ────────────────────────────────────────────────────

    async def _extract_links(self, page: Page, page_url: str) -> List[LinkRef]:
        """
        Extract all <a href> elements using eval_on_selector_all.
        Each href is resolved, normalised (query stripped), and deduplicated
        within the page before being returned.
        """
        raw = await page.eval_on_selector_all(
            "a[href]",
            """(elements) => elements.map((el, idx) => ({
                href: el.getAttribute('href') || '',
                text: (el.innerText || el.textContent || '').trim().slice(0, 120),
                idx:  idx
            }))""",
        )

        seen_on_page: Set[str] = set()
        refs: List[LinkRef] = []

        for item in raw:
            href: str = item["href"].strip()
            if not href:
                continue
            if is_ignored_scheme(href, self._cfg.ignored_schemes):
                continue

            abs_url = self._normalize(href, page_url)
            if not is_valid_http_url(abs_url):
                continue
            if abs_url in seen_on_page:
                continue

            seen_on_page.add(abs_url)
            refs.append(
                LinkRef(
                    page_url=page_url,
                    target_url=abs_url,
                    anchor_text=item["text"] or abs_url,
                    css_selector=f"a:nth-of-type({item['idx'] + 1})",
                )
            )

        logger.debug("  %d links extracted from %s", len(refs), page_url)
        return refs

    # ── Image extraction ───────────────────────────────────────────────────

    async def _extract_images(self, page: Page, page_url: str) -> List[ImageRef]:
        """
        Extract ALL image references from the page after scrolling.

        Sources checked (in priority order per element):
          1. <img>  — data-src / data-lazy-src / data-original / data-lazy /
                      data-img / data-url / data-image / src (all lazy attrs),
                      then srcset / data-srcset (first entry).
          2. <picture><source> — srcset / data-srcset (first entry).
          3. CSS inline style — background-image: url(…).
          4. Lazy-bg attributes — data-bg / data-background /
                                   data-background-image on any element.

        All sources are deduplicated within the page.
        Image query strings are preserved (CDN signing parameters, etc.).
        """
        raw = await page.evaluate("""
            () => {
                const results = [];
                const seen    = new Set();

                // ── Lazy-load attribute priority list for <img> ──────────────
                const LAZY_ATTRS = [
                    'data-src', 'data-lazy-src', 'data-original',
                    'data-lazy', 'data-img', 'data-url', 'data-image', 'src',
                ];

                // Helper: push a candidate if it looks like a URL and is new.
                function push(src, alt, selector) {
                    const s = (src || '').trim();
                    if (!s || s.startsWith('data:') || seen.has(s)) return;
                    seen.add(s);
                    results.push({ src: s, alt: alt || '', selector: selector });
                }

                // 1. <img> elements — all lazy-load attrs then srcset fallback
                document.querySelectorAll('img').forEach((el, idx) => {
                    let src = '';
                    for (const attr of LAZY_ATTRS) {
                        const v = el.getAttribute(attr);
                        if (v && v.trim()) { src = v.trim(); break; }
                    }
                    if (!src) {
                        const ss = (el.getAttribute('srcset')
                                 || el.getAttribute('data-srcset') || '');
                        src = ss.split(',')[0].trim().split(' ')[0];
                    }
                    const alt = (el.getAttribute('alt') || '').trim().slice(0, 120);
                    push(src, alt, 'img:nth-of-type(' + (idx + 1) + ')');
                });

                // 2. <picture><source> — captures art-directed or format-switched images
                document.querySelectorAll('picture source').forEach((el) => {
                    const ss = (el.getAttribute('srcset')
                             || el.getAttribute('data-srcset') || '');
                    const src = ss.split(',')[0].trim().split(' ')[0];
                    push(src, '(picture source)', 'picture source');
                });

                // 3. Inline CSS background-image: url(…)
                document.querySelectorAll('[style]').forEach((el) => {
                    const style = el.getAttribute('style') || '';
                    // Match url("…"), url('…'), or url(…)
                    const m = style.match(/url\\(['"\\s]?([^'"\\)\\s]+)['"\\s]?\\)/);
                    if (m && m[1]) {
                        push(m[1], '(CSS background)', el.tagName.toLowerCase());
                    }
                });

                // 4. Lazy-bg data attributes (popular with lazyload.js / lazysizes)
                const BG_ATTRS = ['data-bg', 'data-background', 'data-background-image'];
                document.querySelectorAll(BG_ATTRS.map(a => '[' + a + ']').join(',')).forEach((el) => {
                    for (const attr of BG_ATTRS) {
                        const v = el.getAttribute(attr);
                        if (v && v.trim()) {
                            // Strip wrapping url(…) if present
                            const m = v.trim().match(/^url\\(['"\\s]?([^'"\\)\\s]+)['"\\s]?\\)$/);
                            push(m ? m[1] : v.trim(), '(lazy bg)', el.tagName.toLowerCase());
                            break;
                        }
                    }
                });

                return results;
            }
        """)

        refs: List[ImageRef] = []
        for item in raw:
            src: str = item["src"].strip()
            if not src:
                continue
            abs_url = normalise(src, page_url)
            if not is_valid_http_url(abs_url):
                continue
            refs.append(
                ImageRef(
                    page_url=page_url,
                    src=abs_url,
                    alt=item.get("alt") or "(no alt)",
                    css_selector=item.get("selector", "img"),
                )
            )

        logger.debug("  %d images extracted from %s", len(refs), page_url)
        return refs
