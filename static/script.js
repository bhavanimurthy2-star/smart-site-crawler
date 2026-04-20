/* ── Milestone DX QA Utilities — UI script ────────────────────────── */

"use strict";

// ── Logging helpers ────────────────────────────────────────────────────────────

function getLog() {
  return document.getElementById("log-panel");
}

function appendLog(message, type) {
  const panel = getLog();
  if (!panel) return;

  // Remove placeholder on first real entry
  const placeholder = panel.querySelector(".muted");
  if (placeholder) placeholder.remove();

  const line = document.createElement("span");
  line.className = "log-line" + (type ? " " + type : "");
  const now = new Date();
  const ts = now.toTimeString().slice(0, 8);
  line.textContent = "[" + ts + "] " + message;
  panel.appendChild(line);
  panel.scrollTop = panel.scrollHeight;
}

function clearLog() {
  const panel = getLog();
  if (!panel) return;
  panel.innerHTML = '<span class="log-line muted">— Log cleared —</span>';
}


// ── Tab switching ──────────────────────────────────────────────────────────────

function switchTab(tabName, btn) {
  // Deactivate all tabs and panels
  document.querySelectorAll(".tab").forEach(function (t) { t.classList.remove("active"); });
  document.querySelectorAll(".tab-panel").forEach(function (p) { p.classList.remove("active"); });

  // Activate selected
  if (btn) btn.classList.add("active");
  const panel = document.getElementById("panel-" + tabName);
  if (panel) panel.classList.add("active");
}


// ── Status indicator ───────────────────────────────────────────────────────────

function setStatus(state, label) {
  const dot  = document.getElementById("status-dot");
  const text = document.getElementById("status-label");
  if (!dot || !text) return;

  dot.className  = "status-dot" + (state ? " " + state : "");
  text.textContent = label;
}


// ── Progress bar ───────────────────────────────────────────────────────────────

function setProgress(pct) {
  const bar   = document.getElementById("progress-bar");
  const label = document.getElementById("progress-label");
  if (!bar || !label) return;
  const clamped = Math.min(100, Math.max(0, pct));
  bar.style.width    = clamped + "%";
  label.textContent  = clamped + "%";
}


// ── Button state ───────────────────────────────────────────────────────────────

function setButtons(state) {
  const start  = document.getElementById("btn-start");
  const stop   = document.getElementById("btn-stop");
  const report = document.getElementById("btn-view-report");
  if (!start) return;

  if (state === "running") {
    start.disabled  = true;
    stop.disabled   = false;
    report.disabled = true;
  } else if (state === "done") {
    start.disabled  = false;
    stop.disabled   = true;
    // report button enabled separately after backend confirms report exists
  } else {
    // idle / stopped
    start.disabled  = false;
    stop.disabled   = true;
    report.disabled = true;
  }
}


// ── Tab count badges ───────────────────────────────────────────────────────────

function updateCount(id, n) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = n;
  el.classList.toggle("has-items", n > 0);
}


// ── Results rendering ──────────────────────────────────────────────────────────

function renderRows(tbodyId, rows) {
  const tbody = document.getElementById(tbodyId);
  if (!tbody) return;
  tbody.innerHTML = "";

  if (!rows || rows.length === 0) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="5">No broken items found.</td></tr>';
    return;
  }

  rows.forEach(function (r) {
    const tr = document.createElement("tr");
    tr.innerHTML =
      '<td><a href="' + r.url + '" target="_blank" rel="noopener">' + esc(r.url) + "</a></td>" +
      '<td><span class="status-badge fail">' + r.status + "</span></td>" +
      '<td><span class="category-badge">' + esc(r.category) + "</span></td>" +
      "<td>" + esc(r.foundOn) + "</td>" +
      "<td>" + esc(r.anchor) + "</td>";
    tbody.appendChild(tr);
  });
}

function esc(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}


// ── Analysis state ─────────────────────────────────────────────────────────────

var _pollTimer        = null;
var _lastLogMessage   = null;
var _currentReportUrl = null;

// ── Start analysis ─────────────────────────────────────────────────────────────

function startAnalysis() {
  var input = document.getElementById("url-input");
  var url   = input ? input.value.trim() : "";

  if (!url) {
    input.focus();
    appendLog("Please enter a URL before starting.", "warn");
    return;
  }

  // Reset UI
  _currentReportUrl = null;
  _lastLogMessage   = null;
  setProgress(0);
  setStatus("running", "Running");
  setButtons("running");
  updateCount("count-links",  0);
  updateCount("count-images", 0);
  resetSummary();

  var tbody  = document.getElementById("tbody-links");
  var tbody2 = document.getElementById("tbody-images");
  if (tbody)  tbody.innerHTML  = '<tr class="empty-row"><td colspan="5">Crawling…</td></tr>';
  if (tbody2) tbody2.innerHTML = '<tr class="empty-row"><td colspan="5">Crawling…</td></tr>';

  appendLog("Starting analysis for: " + url, "info");

  fetch("/api/run-analysis", {
    method:  "POST",
    headers: { "Content-Type": "application/json" },
    body:    JSON.stringify({ url: url }),
  })
    .then(function (res) { return res.json(); })
    .then(function (data) {
      if (data.error) {
        appendLog("Error: " + data.error, "error");
        setStatus("", "Error");
        setButtons("idle");
        return;
      }
      appendLog("Crawler started — polling for progress…");
      _startPolling();
    })
    .catch(function (err) {
      appendLog("Failed to reach backend: " + err, "error");
      setStatus("", "Error");
      setButtons("idle");
    });
}

// ── Polling ────────────────────────────────────────────────────────────────────

function _startPolling() {
  _pollTimer = setInterval(function () {
    fetch("/api/analysis-status")
      .then(function (res) { return res.json(); })
      .then(function (data) {
        // Update progress bar
        if (typeof data.progress === "number") setProgress(data.progress);

        // Append new backend log messages (deduplicated)
        if (data.message && data.message !== _lastLogMessage) {
          appendLog(data.message, data.status === "failed" ? "error" : "");
          _lastLogMessage = data.message;
        }

        if (data.status === "completed") {
          _stopPolling();
          _onComplete(data);
        } else if (data.status === "failed") {
          _stopPolling();
          setStatus("error", "Failed");
          setButtons("idle");
        }
      })
      .catch(function () { /* network hiccup — keep polling */ });
  }, 2000);
}

function _stopPolling() {
  if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
}

// ── Completion handler ─────────────────────────────────────────────────────────

function _onComplete(data) {
  setStatus("done", "Completed");
  setButtons("done");

  // Wire View Report to the exact file generated by this run
  if (data.report_url) {
    _currentReportUrl = data.report_url;
    var btn = document.getElementById("btn-view-report");
    if (btn) btn.disabled = false;
  }

  // Populate summary from real counts returned by backend
  if (data.summary) {
    var s  = data.summary;
    var el = function (id) { return document.getElementById(id); };
    if (el("s-pages"))         el("s-pages").textContent         = s.pages          !== undefined ? s.pages          : "—";
    if (el("s-checked"))       el("s-checked").textContent       = s.total_checked   !== undefined ? s.total_checked  : "—";
    if (el("s-broken-links"))  el("s-broken-links").textContent  = s.broken_links    !== undefined ? s.broken_links   : "—";
    if (el("s-broken-images")) el("s-broken-images").textContent = s.broken_images   !== undefined ? s.broken_images  : "—";
    var empty = document.getElementById("summary-empty");
    if (empty) empty.style.display = "none";

    updateCount("count-links",  s.broken_links  || 0);
    updateCount("count-images", s.broken_images || 0);
  }

  // Tables: point user to the full report (row-level data lives there)
  var msg = "Open the report for full details.";
  var tbody  = document.getElementById("tbody-links");
  var tbody2 = document.getElementById("tbody-images");
  if (tbody)  tbody.innerHTML  = '<tr class="empty-row"><td colspan="5">' + msg + '</td></tr>';
  if (tbody2) tbody2.innerHTML = '<tr class="empty-row"><td colspan="5">' + msg + '</td></tr>';
}

// ── Stop / View Report ─────────────────────────────────────────────────────────

function stopAnalysis() {
  _stopPolling();
  setStatus("", "Stopped");
  setButtons("idle");
  appendLog("Polling stopped. Background crawl will finish on its own.", "warn");
}

function viewReport() {
  if (_currentReportUrl) {
    window.open(_currentReportUrl, "_blank");
  }
}

function resetSummary() {
  ["s-pages","s-checked","s-broken-links","s-broken-images","s-internal","s-external"].forEach(function (id) {
    var el = document.getElementById(id);
    if (el) el.textContent = "—";
  });
  var empty = document.getElementById("summary-empty");
  if (empty) empty.style.display = "";
}


// ── Dashboard: open reports folder ────────────────────────────────────────────

function openReportsFolder() {
  appendLog("Opening reports folder… (wire to backend when ready)", "info");
}
