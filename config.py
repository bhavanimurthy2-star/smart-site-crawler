"""
Global configuration for the Website Health Check framework.
Modify these values to control crawl depth, timeouts, and concurrency.
"""

from dataclasses import dataclass, field
from typing import List


@dataclass
class Config:
    # ── Crawl settings ───────────────────────────────────────────────────────
    base_url: str = "https://example.com"
    max_depth: int = 3
    stay_on_domain: bool = True
    max_pages: int = 0                 # 0 = no limit; set >0 for a hard cap

    # ── Smart crawler ────────────────────────────────────────────────────────
    sitemap_enabled: bool = True       # try /sitemap.xml before BFS
    crawl_concurrency: int = 15        # parallel Playwright pages during crawl
    # How long a worker waits on an empty queue before exiting.
    # Must exceed the worst-case page-processing time (crawl_first_attempt_timeout
    # + 30 s retry = up to 45 s) so that BFS workers do not exit during the
    # initial start-up gap or at depth-transition pauses.
    # All workers time out simultaneously, so end-of-crawl overhead = 1 × this
    # value (not N_workers × this value).
    worker_queue_timeout: float = 60.0

    # ── HTTP / network ───────────────────────────────────────────────────────
    request_timeout: int = 20          # seconds per HTTP request
    max_retries: int = 2
    concurrency: int = 50              # max parallel aiohttp requests (primary throughput lever)
    verify_ssl: bool = False           # set False to skip SSL validation

    # ── Playwright ───────────────────────────────────────────────────────────
    headless: bool = True
    page_load_timeout: int = 20_000    # ms — full navigation budget (used by browser fallback)
    # First-attempt timeout for the crawler's _process_page retry logic.
    # Set this to the typical page-load time of the CMS being crawled so that
    # pages which load within budget succeed on the first attempt (no wasted
    # 10 s timeout before the 30 s retry fires for each page).
    crawl_first_attempt_timeout: int = 15_000   # ms
    network_idle_timeout: int = 5_000  # ms to wait for network idle
    scroll_page: bool = True           # scroll to trigger lazy-loading

    # ── Reporting ────────────────────────────────────────────────────────────
    report_dir: str = "reports/output"
    report_filename: str = "health_report.html"
    json_export: bool = True
    csv_export: bool = True
    screenshot_on_failure: bool = True
    deadlink_report: bool = True        # generate deadlink_report_<ts>.html alongside health_report

    # ── Browser fallback ─────────────────────────────────────────────────────
    # When True, any link that the aiohttp check marks as FAIL is given a
    # second chance via a real headless-Chromium navigation.  If the browser
    # loads the page successfully the verdict is upgraded to PASS with a note
    # explaining the discrepancy.  This aligns framework results with what
    # deadlink checkers and real users actually see.
    # Set False to skip the browser pass (faster, but more false positives).
    browser_fallback_on_fail: bool = True
    # Max concurrent browser pages during the fallback phase.
    browser_fallback_concurrency: int = 5    # max concurrent browser pages during fallback phase

    # ── Ignored schemes ──────────────────────────────────────────────────────
    ignored_schemes: List[str] = field(
        default_factory=lambda: ["javascript", "mailto", "tel", "data", "blob"]
    )

    # ── User-agent ───────────────────────────────────────────────────────────
    user_agent: str = (
        "Mozilla/5.0 (compatible; WebHealthBot/1.0; +https://internal/healthcheck)"
    )


# Default singleton used throughout the framework
config = Config()
