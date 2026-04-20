"""
Generates the HTML, JSON, and CSV health reports.

Changes vs v1
─────────────
• Row dicts now carry a ``verdict`` key ("PASS" / "FAIL" / "SKIPPED") and
  an ``is_external`` flag.  The old ``passed`` bool is retained for JSON /
  CSV backward-compatibility but is derived from ``verdict``.

• Summary counts are split into total_passed / total_failed / total_skipped.
  Pass-rate is computed over actionable checks only (excludes SKIPPED) so
  false-positive noise does not artificially deflate the score.

• Sort order: FAIL first → SKIPPED second → PASS last (within each page).

The Jinja2 template remains purely presentational — all logic lives here.
"""

from __future__ import annotations

import csv
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import List
from urllib.parse import urlparse

from jinja2 import Environment, FileSystemLoader

from config import Config, config as default_config
from utils.verdict import Verdict
from utils.url_utils import registered_domain
from validators.link_validator import LinkResult
from validators.image_validator import ImageResult

logger = logging.getLogger(__name__)


# ── Row builders ───────────────────────────────────────────────────────────────

def _dict_from_link(r: LinkResult) -> dict:
    return {
        # Type / traceability
        "type":           "Link",
        "page_url":       r.page_url,          # first page (backward compat)
        "found_on_pages": r.found_on_pages,    # full list of pages
        "pages_count":    r.pages_count,       # quick count for template
        "is_external":    r.is_external,
        # Element description
        "element":        r.anchor_text or r.css_selector,
        # Target
        "target_url":     r.target_url,
        "final_url":      r.final_url,
        # Status
        "status_code":    r.display_status,
        # Verdict
        "verdict":        r.verdict.value,
        "status_label":   r.status_label,
        "passed":         r.passed,
        # Reason (blank for PASS rows)
        "reason":         r.reason if not r.passed else "",
        # Browser fallback metadata
        "browser_checked": r.browser_checked,
        "browser_loaded":  r.browser_loaded,
    }


def _dict_from_image(r: ImageResult) -> dict:
    svg_note    = " [SVG]" if r.is_svg else ""
    element     = f'<img alt="{r.alt_text}"{svg_note}> ({r.css_selector})'
    is_external = bool(
        r.src
        and r.page_url
        and registered_domain(r.src) != registered_domain(r.page_url)
    )
    return {
        "type":        "Image",
        "page_url":    r.page_url,
        "is_external": is_external,
        "element":     element,
        "target_url":  r.src,
        "final_url":   r.final_url,
        "status_code": r.display_status,
        "verdict":     r.verdict.value,
        "status_label": r.status_label,
        "passed":      r.passed,
        "reason":      r.failure_reason,
    }


# ── Reporter ───────────────────────────────────────────────────────────────────

class HtmlReporter:
    """
    Accepts validated link/image results and writes:

    - health_report.html        (primary deliverable — dark-themed dashboard)
    - health_report.json        (if cfg.json_export is True)
    - health_report.csv         (if cfg.csv_export is True)
    - <site_name>_<ts>.html     (always — FAIL-only DeadLinkChecker-style report)
    """

    def __init__(self, cfg: Config = default_config) -> None:
        self._cfg = cfg
        self._out = Path(cfg.report_dir)
        self._out.mkdir(parents=True, exist_ok=True)

        templates_dir = Path(__file__).parent / "templates"
        self._env = Environment(
            loader=FileSystemLoader(str(templates_dir)),
            autoescape=True,
        )

    # ── Public ─────────────────────────────────────────────────────────────

    def generate(
        self,
        link_results: List[LinkResult],
        image_results: List[ImageResult],
        pages_scanned: int,
    ) -> dict:
        """
        Build all report artefacts.  Returns a dict with both report paths::

            {
                "health_report": "reports/output/health_report_20240409_143022.html",
                "site_report":   "reports/output/luma2026_20240409_143022.html",
            }

        Every invocation generates a unique timestamp suffix so that
        consecutive test runs never overwrite each other.
        """
        # One timestamp shared by all artefacts from this run
        ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
        stem      = self._cfg.report_filename.replace(".html", "")   # "health_report"

        # Site-based stem used for the dead-link report AND the JSON/CSV exports
        # so that all three share the same base name (tests derive JSON/CSV paths
        # via Path(site_report).with_suffix(".json / .csv")).
        parsed    = urlparse(self._cfg.base_url)
        hostname  = parsed.hostname or "site"
        site_stem = hostname.split(".")[0]          # e.g. "preferredtravelmag-2026"

        rows     = self._build_rows(link_results, image_results)
        summary  = self._build_summary(rows, pages_scanned)
        grouped  = self._group_by_page(rows)
        sections = self._build_sections(rows)

        html_path = self._write_html(summary, grouped, rows, sections, stem, ts)

        if self._cfg.json_export:
            self._write_json(summary, rows, site_stem, ts)
        if self._cfg.csv_export:
            self._write_csv(rows, site_stem, ts)

        # Always generate the site-based dead-link report regardless of config flag
        site_path = self._write_deadlink_report(rows, summary, ts)

        logger.info("Reports written to: %s  (timestamp: %s)", self._out, ts)
        return {
            "health_report": str(html_path),
            "site_report":   str(site_path),
        }

    # ── Builders ───────────────────────────────────────────────────────────

    @staticmethod
    def _build_rows(
        link_results: List[LinkResult],
        image_results: List[ImageResult],
    ) -> list[dict]:
        rows: list[dict] = []
        for r in link_results:
            rows.append(_dict_from_link(r))
        for r in image_results:
            rows.append(_dict_from_image(r))

        # Sort: FAIL → SKIPPED → PASS, then by page URL within each group
        _order = {"FAIL": 0, "SKIPPED": 1, "PASS": 2}
        rows.sort(key=lambda x: (_order.get(x["verdict"], 9), x["page_url"]))
        return rows

    @staticmethod
    def _build_summary(rows: list[dict], pages_scanned: int) -> dict:
        links  = [r for r in rows if r["type"] == "Link"]
        images = [r for r in rows if r["type"] == "Image"]

        def _count(lst, verdict):
            return sum(1 for r in lst if r["verdict"] == verdict)

        lp = _count(links,  "PASS");    lf = _count(links,  "FAIL");    ls = _count(links,  "SKIPPED")
        ip = _count(images, "PASS");    if_ = _count(images, "FAIL");   is_ = _count(images, "SKIPPED")

        total_passed  = lp + ip
        total_failed  = lf + if_
        total_skipped = ls + is_
        total_checked = len(rows)

        # Pass-rate excludes SKIPPED from denominator — only actionable items
        actionable = total_passed + total_failed
        pass_pct   = round(total_passed / actionable * 100, 1) if actionable else 0.0

        # Browser-fallback stats (link rows only)
        browser_upgraded = sum(
            1 for r in links
            if r.get("browser_checked") and r.get("browser_loaded")
        )

        # Internal / external splits for links
        int_links = [r for r in links  if not r.get("is_external")]
        ext_links = [r for r in links  if r.get("is_external")]
        int_imgs  = [r for r in images if not r.get("is_external")]
        ext_imgs  = [r for r in images if r.get("is_external")]

        return {
            "generated_at":      datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "pages_scanned":     pages_scanned,
            # ── Link breakdown ────────────────────────────────────────────
            "total_links":            len(links),
            "internal_links_count":   len(int_links),
            "external_links_count":   len(ext_links),
            "links_passed":           lp,
            "links_failed":           lf,
            "links_skipped":          ls,
            "internal_links_passed":  _count(int_links, "PASS"),
            "internal_links_failed":  _count(int_links, "FAIL"),
            "internal_links_skipped": _count(int_links, "SKIPPED"),
            "external_links_passed":  _count(ext_links, "PASS"),
            "external_links_failed":  _count(ext_links, "FAIL"),
            "external_links_skipped": _count(ext_links, "SKIPPED"),
            "browser_upgraded":       browser_upgraded,
            # ── Image breakdown ───────────────────────────────────────────
            "total_images":            len(images),
            "internal_images_count":   len(int_imgs),
            "external_images_count":   len(ext_imgs),
            "images_passed":           ip,
            "images_failed":           if_,
            "images_skipped":          is_,
            "internal_images_passed":  _count(int_imgs, "PASS"),
            "internal_images_failed":  _count(int_imgs, "FAIL"),
            "internal_images_skipped": _count(int_imgs, "SKIPPED"),
            "external_images_passed":  _count(ext_imgs, "PASS"),
            "external_images_failed":  _count(ext_imgs, "FAIL"),
            "external_images_skipped": _count(ext_imgs, "SKIPPED"),
            # ── Aggregates (dashboard cards) ──────────────────────────────
            "total_checked":     total_checked,
            "total_passed":      total_passed,
            "total_failed":      total_failed,
            "total_skipped":     total_skipped,
            # Convenience aliases kept for backward-compat
            "broken_links":      lf,
            "broken_images":     if_,
            "pass_pct":          pass_pct,
        }

    @staticmethod
    def _build_sections(rows: list[dict]) -> dict:
        """
        Split rows into the four reporting sections:
          internal_links / external_links / internal_images / external_images

        Each section is a dict with three lists: passed / failed / skipped.
        The ``failed`` list is the primary table data; passed/skipped are
        shown as counts only in the template.
        """
        links  = [r for r in rows if r["type"] == "Link"]
        images = [r for r in rows if r["type"] == "Image"]

        int_links = [r for r in links  if not r.get("is_external")]
        ext_links = [r for r in links  if r.get("is_external")]
        int_imgs  = [r for r in images if not r.get("is_external")]
        ext_imgs  = [r for r in images if r.get("is_external")]

        def _split(lst: list[dict]) -> dict:
            p = [r for r in lst if r["verdict"] == "PASS"]
            f = [r for r in lst if r["verdict"] == "FAIL"]
            s = [r for r in lst if r["verdict"] == "SKIPPED"]
            return {
                "passed":  p,
                "failed":  f,
                "skipped": s,
                # Pre-sorted: FAILs first (most urgent), SKIPs next, PASSes last.
                # This is the list passed to the template's unified filter table.
                "all":     f + s + p,
            }

        return {
            "internal_links":  _split(int_links),
            "external_links":  _split(ext_links),
            "internal_images": _split(int_imgs),
            "external_images": _split(ext_imgs),
        }

    @staticmethod
    def _group_by_page(rows: list[dict]) -> dict[str, list[dict]]:
        grouped: dict[str, list[dict]] = {}
        for row in rows:
            grouped.setdefault(row["page_url"], []).append(row)
        return grouped

    # ── Writers ────────────────────────────────────────────────────────────
    # Each method receives the base stem ("health_report") and the run
    # timestamp ("20240409_143022") so it can construct a unique filename.

    def _write_html(
        self,
        summary: dict,
        grouped: dict[str, list[dict]],
        rows: list[dict],
        sections: dict,
        stem: str,
        ts: str,
    ) -> Path:
        template = self._env.get_template("report.html")
        html     = template.render(
            summary=summary,
            grouped=grouped,
            rows=rows,
            sections=sections,
        )
        path = self._out / f"{stem}_{ts}.html"
        path.write_text(html, encoding="utf-8")
        logger.info("HTML report: %s", path)
        return path

    def _write_json(self, summary: dict, rows: list[dict], stem: str, ts: str) -> None:
        path    = self._out / f"{stem}_{ts}.json"
        payload = {"summary": summary, "results": rows}
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        logger.info("JSON export: %s", path)

    def _write_csv(self, rows: list[dict], stem: str, ts: str) -> None:
        path = self._out / f"{stem}_{ts}.csv"
        fieldnames = [
            "type", "page_url", "is_external", "element",
            "target_url", "final_url", "status_code",
            "verdict", "status_label", "reason",
        ]
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        logger.info("CSV export: %s", path)

    def _write_deadlink_report(
        self,
        rows: list[dict],
        summary: dict,
        ts: str,
    ) -> Path:
        """
        Write a self-contained DeadLinkChecker-style HTML report that contains
        ONLY items with verdict == FAIL.

        File naming: ``deadlink_report_<timestamp>.html`` in the same output
        directory as the main health report.  The stem is fixed to
        "deadlink_report" so the two report families are clearly distinct on
        disk.

        Template variables passed:
            failed_items  — list of row dicts filtered to FAIL verdict only,
                            sorted: internal first, then external; links before
                            images within each group.
            summary       — the same summary dict used by the health report
                            (provides page count, total counts, base URL, etc.)
            base_url      — cfg.base_url for the report header
            generated_at  — human-readable timestamp string
            ts            — raw timestamp string for the filename / page title
        """
        # ── Filter to FAIL only; sub-sort for maximum scannability ─────────
        failed: list[dict] = [r for r in rows if r["verdict"] == "FAIL"]
        # Sub-sort: internal before external, links before images, then by URL
        _ext_order  = {False: 0, True: 1}   # internal first
        _type_order = {"Link": 0, "Image": 1}
        failed.sort(key=lambda r: (
            _ext_order.get(r.get("is_external", False), 1),
            _type_order.get(r["type"], 2),
            r["target_url"],
        ))

        # ── Count helpers for the summary bar ─────────────────────────────
        failed_links  = sum(1 for r in failed if r["type"] == "Link")
        failed_images = sum(1 for r in failed if r["type"] == "Image")
        int_failures  = sum(1 for r in failed if not r.get("is_external"))
        ext_failures  = sum(1 for r in failed if r.get("is_external"))

        # ── Sorted list of every crawled page URL (for the dropdown) ───────
        # Derived from all rows (not just failures) so every scanned page is
        # represented, regardless of whether it had any broken items.
        scanned_pages: list[str] = sorted(set(r["page_url"] for r in rows))

        template = self._env.get_template("deadlink_report.html")
        html = template.render(
            failed_items=failed,
            all_items=rows,       # full result set — drives the "checks performed" dropdown
            summary=summary,
            base_url=self._cfg.base_url,
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ts=ts,
            # Pre-computed counts for the stats bar
            total_failed=len(failed),
            failed_links=failed_links,
            failed_images=failed_images,
            int_failures=int_failures,
            ext_failures=ext_failures,
            # Full sorted list of crawled pages — drives the "pages scanned" dropdown
            scanned_pages=scanned_pages,
        )

        # ── Derive a short, filesystem-safe site name from base_url ──────
        parsed    = urlparse(self._cfg.base_url)
        hostname  = parsed.hostname or "site"          # e.g. "luma2026.web5cms.milestoneinternet.info"
        site_name = hostname.split(".")[0]             # e.g. "luma2026"

        path = self._out / f"{site_name}_{ts}.html"
        path.write_text(html, encoding="utf-8")
        logger.info("Dead-link report: %s", path)
        return path
