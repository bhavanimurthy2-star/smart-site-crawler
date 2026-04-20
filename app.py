"""
Milestone DX QA Utilities — Flask UI
"""

import asyncio
import os
import threading

from flask import Flask, jsonify, render_template, request, send_from_directory

from config import Config
from crawler.site_crawler import SiteCrawler
from validators.link_validator import LinkValidator
from validators.image_validator import ImageValidator
from reports.html_report import HtmlReporter

app = Flask(__name__)

# Absolute path to the reports output directory
_REPORTS_DIR = os.path.join(os.path.dirname(__file__), "reports", "output")

# ── Background job state ───────────────────────────────────────────────────────
# Only one analysis runs at a time.  A threading.Lock protects all reads/writes.

_job_lock = threading.Lock()
_job: dict = {
    "status":     "idle",   # idle | running | completed | failed
    "progress":   0,
    "message":    "",
    "report_url": None,     # "/report/<filename>" set on completion
    "error":      None,
    "summary":    None,     # dict with counts, set on completion
}


def _run_analysis_thread(url: str) -> None:
    """
    Full pipeline: crawl → validate links & images → generate reports.
    Runs in a daemon thread; updates _job in place.
    """

    def _update(**kw):
        with _job_lock:
            _job.update(kw)

    async def _pipeline():
        cfg = Config(base_url=url)

        _update(progress=10, message="Crawling pages…")
        pages = await SiteCrawler(cfg).crawl()

        _update(
            progress=40,
            message=f"Crawled {len(pages)} page(s) — validating links and images…",
        )
        link_results, image_results = await asyncio.gather(
            LinkValidator(cfg).validate(pages),
            ImageValidator(cfg).validate(pages),
        )

        _update(progress=90, message="Generating report…")
        result = HtmlReporter(cfg).generate(
            link_results, image_results, pages_scanned=len(pages)
        )

        broken_links  = sum(1 for r in link_results  if r.verdict.value == "FAIL")
        broken_images = sum(1 for r in image_results if r.verdict.value == "FAIL")

        return result, {
            "pages":          len(pages),
            "total_checked":  len(link_results) + len(image_results),
            "broken_links":   broken_links,
            "broken_images":  broken_images,
        }

    try:
        result, summary = asyncio.run(_pipeline())
        filename = os.path.basename(result["site_report"])
        _update(
            status="completed",
            progress=100,
            message=f"Analysis complete — {filename}",
            report_url=f"/report/{filename}",
            summary=summary,
        )
    except Exception as exc:
        _update(status="failed", progress=0, message=str(exc), error=str(exc))


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    return render_template("dashboard.html")


@app.route("/analyzer")
def analyzer():
    return render_template("analyzer.html")


@app.route("/api/run-analysis", methods=["POST"])
def run_analysis():
    """Start a background crawl.  Returns 409 if one is already running."""
    with _job_lock:
        if _job["status"] == "running":
            return jsonify({"error": "Analysis already running"}), 409

    data = request.get_json(silent=True) or {}
    url  = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url is required"}), 400

    with _job_lock:
        _job.update({
            "status":     "running",
            "progress":   5,
            "message":    "Starting…",
            "report_url": None,
            "error":      None,
            "summary":    None,
        })

    threading.Thread(target=_run_analysis_thread, args=(url,), daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/api/analysis-status")
def analysis_status():
    """Return the current job state so the frontend can poll for progress."""
    with _job_lock:
        return jsonify(dict(_job))


@app.route("/report/<filename>")
def serve_report(filename):
    """Serve a specific report file by name directly in the browser."""
    return send_from_directory(_REPORTS_DIR, filename)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
