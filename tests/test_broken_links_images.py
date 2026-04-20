"""
pytest entry point for the Website Health Check framework.

Run with:
    pytest tests/test_broken_links_images.py -v --base-url=https://example.com

Or set BASE_URL env var:
    BASE_URL=https://example.com pytest tests/test_broken_links_images.py -v
"""

from __future__ import annotations

import asyncio
import logging
import os
import webbrowser
from pathlib import Path

import pytest

from config import Config
from crawler.site_crawler import SiteCrawler
from validators.link_validator import LinkValidator
from validators.image_validator import ImageValidator
from reports.html_report import HtmlReporter
from utils.verdict import Verdict

# ── Logging setup ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ── pytest hook: custom CLI option ────────────────────────────────────────────

def pytest_addoption(parser):
    parser.addoption(
        "--base-url",
        action="store",
        default=None,
        help="Base URL to crawl (overrides config.py and BASE_URL env var)",
    )
    parser.addoption(
        "--depth",
        action="store",
        type=int,
        default=None,
        help="Crawl depth (default from config.py)",
    )
    parser.addoption(
        "--open-report",
        action="store_true",
        default=False,
        help="Automatically open the HTML report in the browser when done",
    )


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def cfg(request) -> Config:
    """Build a Config from CLI options / env vars."""
    base_url = (
        request.config.getoption("--base-url")
        or os.environ.get("BASE_URL")
        or "https://example.com"
    )
    depth = request.config.getoption("--depth")

    c = Config(base_url=base_url)
    if depth is not None:
        c.max_depth = depth
    return c


@pytest.fixture(scope="session")
def crawl_results(cfg):
    """
    Run the crawler once per session and return (pages, link_results, image_results).
    All subsequent tests reuse these results — no double-crawling.
    """
    logger.info("=" * 60)
    logger.info("Starting crawl: %s  (depth=%d)", cfg.base_url, cfg.max_depth)
    logger.info("=" * 60)

    async def _run():
        # 1. Crawl
        crawler = SiteCrawler(cfg)
        pages = await crawler.crawl()
        logger.info("Crawl complete: %d pages collected.", len(pages))

        # 2. Validate links + images concurrently
        link_validator  = LinkValidator(cfg)
        image_validator = ImageValidator(cfg)
        link_results, image_results = await asyncio.gather(
            link_validator.validate(pages),
            image_validator.validate(pages),
        )
        return pages, link_results, image_results

    return asyncio.run(_run())


@pytest.fixture(scope="session")
def report_path(cfg, crawl_results, request):
    """Generate all reports and return the HTML path."""
    pages, link_results, image_results = crawl_results
    reporter = HtmlReporter(cfg)
    result = reporter.generate(link_results, image_results, pages_scanned=len(pages))

    if request.config.getoption("--open-report"):
        webbrowser.open(f"file://{Path(result['site_report']).resolve()}")

    return result["site_report"]


# ── Tests ──────────────────────────────────────────────────────────────────────

class TestBrokenLinks:
    """Validate that no hyperlinks on the site return error status codes."""

    def test_no_broken_links(self, crawl_results, report_path):
        """
        FAIL only when a link has verdict == FAIL (definitive broken resource).

        Each result represents ONE unique broken URL (deduplication is done in
        the validator) — if the same nav link is broken on 30 pages this test
        reports it once, with the full list of affected pages.

        SKIPPED items (bot-blocked externals, tracking endpoints, reservation
        platforms) are excluded — they are noise, not real defects.
        """
        _, link_results, _ = crawl_results
        # One result per unique URL → count == number of unique broken targets
        broken = [r for r in link_results if r.verdict is Verdict.FAIL]

        if broken:
            lines = [f"\n{'=' * 70}"]
            lines.append(
                f"  BROKEN LINKS FOUND: {len(broken)} unique URL(s)"
            )
            lines.append(f"{'=' * 70}")
            for r in broken:
                # Show all pages where this broken link was found
                page_list = "\n          ".join(r.found_on_pages[:10])
                more = (
                    f"\n          … and {r.pages_count - 10} more"
                    if r.pages_count > 10 else ""
                )
                browser_note = (
                    f"\n  Browser : also failed — {r.reason.split('|')[-1].strip()}"
                    if r.browser_checked and not r.browser_loaded
                    else (
                        "\n  Browser : ✔ loaded (upgraded from HTTP fail)"
                        if r.browser_checked and r.browser_loaded
                        else ""
                    )
                )
                lines.append(
                    f"\n  URL     : {r.target_url}\n"
                    f"  Status  : {r.display_status} — {r.reason.split('|')[0].strip()}"
                    f"{browser_note}\n"
                    f"  Element : {r.anchor_text or r.css_selector}\n"
                    f"  Found on: {r.pages_count} page(s)\n"
                    f"          {page_list}{more}"
                )
            lines.append(f"\n  📄 Full report: {report_path}\n")
            pytest.fail("\n".join(lines))

    def test_link_summary(self, crawl_results):
        """
        Log link validation statistics split by verdict.
        Counts are per unique URL (not per page occurrence).
        Always passes — informational only.
        """
        _, link_results, _ = crawl_results
        total    = len(link_results)   # unique URLs checked
        passed   = sum(1 for r in link_results if r.verdict is Verdict.PASS)
        failed   = sum(1 for r in link_results if r.verdict is Verdict.FAIL)
        skipped  = sum(1 for r in link_results if r.verdict is Verdict.SKIPPED)
        upgraded = sum(1 for r in link_results if r.browser_checked and r.browser_loaded)
        logger.info(
            "Links — unique URLs: %d | PASS: %d | FAIL: %d | SKIPPED: %d "
            "| browser-upgraded: %d",
            total, passed, failed, skipped, upgraded,
        )
        assert total >= 0


class TestBrokenImages:
    """Validate that no images on the site are broken or fail to render."""

    def test_no_broken_images(self, crawl_results, report_path):
        """
        FAIL only when an image has verdict == FAIL (definitive broken resource).
        SKIPPED items (SVG render check, tracking pixels, bot-blocked hosts) are
        excluded — they are noise, not real defects.
        The HTML report lists every item with its source page and verdict.
        """
        _, _, image_results = crawl_results
        # ✅ Only count genuine FAIL verdicts — not SKIPPED
        broken = [r for r in image_results if r.verdict is Verdict.FAIL]

        if broken:
            lines = [f"\n{'=' * 70}"]
            lines.append(f"  BROKEN IMAGES FOUND: {len(broken)}")
            lines.append(f"{'=' * 70}")
            for r in broken:
                lines.append(
                    f"\n  Page    : {r.page_url}\n"
                    f"  Alt     : {r.alt_text}\n"
                    f"  Src     : {r.src}\n"
                    f"  Status  : {r.display_status} — {r.failure_reason}"
                )
            lines.append(f"\n  📄 Full report: {report_path}\n")
            pytest.fail("\n".join(lines))

    def test_image_summary(self, crawl_results):
        """Log image validation statistics split by verdict (informational)."""
        _, _, image_results = crawl_results
        total     = len(image_results)
        passed    = sum(1 for r in image_results if r.verdict is Verdict.PASS)
        failed    = sum(1 for r in image_results if r.verdict is Verdict.FAIL)
        skipped   = sum(1 for r in image_results if r.verdict is Verdict.SKIPPED)
        no_render = sum(
            1 for r in image_results
            if r.verdict is Verdict.FAIL and r.renders is False
        )
        logger.info(
            "Images — total: %d | PASS: %d | FAIL: %d | SKIPPED: %d "
            "(render failures: %d)",
            total, passed, failed, skipped, no_render,
        )
        assert total >= 0


class TestReport:
    """Sanity checks on the generated report artefacts."""

    def test_html_report_generated(self, report_path, cfg):
        """HTML report file must exist and be non-empty."""
        path = Path(report_path)
        assert path.exists(), f"HTML report not found at {path}"
        assert path.stat().st_size > 0, "HTML report is empty"
        logger.info("HTML report verified: %s (%d bytes)", path, path.stat().st_size)

    def test_json_export_generated(self, report_path, cfg):
        """JSON export must exist when cfg.json_export is True."""
        if not cfg.json_export:
            pytest.skip("json_export disabled in config")
        json_path = Path(report_path).with_suffix(".json")
        assert json_path.exists(), f"JSON export not found at {json_path}"

    def test_csv_export_generated(self, report_path, cfg):
        """CSV export must exist when cfg.csv_export is True."""
        if not cfg.csv_export:
            pytest.skip("csv_export disabled in config")
        csv_path = Path(report_path).with_suffix(".csv")
        assert csv_path.exists(), f"CSV export not found at {csv_path}"
