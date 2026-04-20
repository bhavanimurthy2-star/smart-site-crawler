"""
URL normalisation and domain-membership helpers.
"""

import re
from urllib.parse import urlparse, urljoin, urlunparse
from typing import Optional

import tldextract


# ── Normalisation ──────────────────────────────────────────────────────────────

def normalise(url: str, base: str = "") -> str:
    """
    Return a cleaned, absolute URL.
    - Resolve relative URLs against *base*.
    - Strip fragments (#…).
    - Remove trailing slashes (except bare root).
    """
    if base:
        url = urljoin(base, url.strip())
    parsed = urlparse(url)
    # Drop fragment
    cleaned = parsed._replace(fragment="")
    result = urlunparse(cleaned)
    # Strip trailing slash unless it is the root path
    if result.endswith("/") and urlparse(result).path != "/":
        result = result.rstrip("/")
    return result


def is_ignored_scheme(url: str, ignored_schemes: list[str]) -> bool:
    """Return True when the URL uses a scheme we never want to check."""
    scheme = urlparse(url).scheme.lower()
    return scheme in ignored_schemes or url.startswith("#")


def is_valid_http_url(url: str) -> bool:
    """Return True when the URL uses http or https."""
    return urlparse(url).scheme in ("http", "https")


# ── Domain helpers ─────────────────────────────────────────────────────────────

def registered_domain(url: str) -> str:
    """Return 'example.com' from any variation of that host."""
    ext = tldextract.extract(url)
    return f"{ext.domain}.{ext.suffix}" if ext.suffix else ext.domain


def same_domain(url: str, base_url: str) -> bool:
    """True when *url* lives on the same registered domain as *base_url*."""
    return registered_domain(url) == registered_domain(base_url)


def get_domain(url: str) -> str:
    """Return scheme + netloc (e.g. 'https://example.com')."""
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


# ── Reservation / hospitality platforms ───────────────────────────────────────
#
# These platforms serve JavaScript-heavy booking widgets that require an
# authenticated browser session or hotel/restaurant-specific tokens to render
# meaningful content.  Even a headless Playwright browser cannot reliably
# verify them without the correct session state — so they are SKIPPED
# immediately, before any HTTP or browser check is attempted.
#
# Distinguishing them from generic bot-blocking domains is intentional:
#   • Generic bot-blockers (social media, CDNs) return 403/429 to aiohttp but
#     load fine in a real browser → they should go through the HTTP check and
#     browser-fallback pipeline so legitimate links are not falsely SKIPPED.
#   • Reservation platforms return 404/403 even in a headless browser without
#     session context → browser fallback would always fail, wasting resources.

RESERVATION_PLATFORMS: frozenset[str] = frozenset({
    "sevenrooms.com",       # restaurant / hotel reservation widget
    "opentable.com",        # restaurant reservations
    "resy.com",             # restaurant reservations
    "tock.com",             # ticketed dining
    "exploretock.com",
    "bookatable.com",       # Michelin / UK restaurant reservations
    "tablecheck.com",       # Asia-Pacific restaurant reservations
    "yelpreservations.com",
    "quandoo.com",          # European reservations
    "thefork.com",          # European / Australian reservations
})


def is_reservation_platform(url: str) -> bool:
    """
    Return True when *url* belongs to a reservation or hospitality platform
    that requires an authenticated session to serve meaningful content.

    These links are SKIPPED rather than checked — even a real browser cannot
    verify them without hotel/restaurant-specific session tokens, so running
    an HTTP or browser check would always produce a false FAIL.
    """
    return registered_domain(url) in RESERVATION_PLATFORMS


# ── Bot-blocking domain list ───────────────────────────────────────────────────
#
# Platforms that actively block automated HTTP clients with 403 / 429 / resets,
# but which DO load correctly in a real browser.
#
# Unlike RESERVATION_PLATFORMS these are NOT skipped before the HTTP check.
# Instead they go through the normal HTTP + browser-fallback pipeline:
#   • If aiohttp gets a bot-detection code (403/429/5xx) → SKIPPED
#   • If aiohttp gets a surprising 404               → browser fallback runs
#     and can upgrade the result to PASS when the page loads fine in Chrome.
#
# Keeping this list accurate prevents social / CDN domains from polluting the
# actionable FAIL count while still allowing the browser fallback to rescue
# any that aiohttp misidentifies as definitively broken.

BOT_BLOCKING_DOMAINS: frozenset[str] = frozenset({
    # Social media — return 403 / 429 to automated clients
    "twitter.com",
    "x.com",
    "facebook.com",
    "fb.com",
    "instagram.com",
    "linkedin.com",
    "tiktok.com",
    "pinterest.com",
    "snapchat.com",
    # Review / travel aggregators
    "tripadvisor.com",
    "yelp.com",
    "trustpilot.com",
    "booking.com",
    "expedia.com",
    # Standards bodies that block automated clients
    "w3.org",
    # CDNs / platforms with aggressive WAF fingerprinting
    "google.com",
    "youtube.com",
    "cloudflare.com",
    "apple.com",
    "marriott.com",         # hotel booking engine (Cloudflare WAF)
})


def is_bot_blocking_domain(url: str) -> bool:
    """
    Return True when *url* belongs to a platform known to block automated
    HTTP clients (returning 403 / 429 / connection resets) but which loads
    correctly in a real browser.

    Note: this check is NOT used as an early-exit in the classification engine.
    These domains go through the HTTP check and browser-fallback pipeline so
    that legitimate links can still be upgraded to PASS by Playwright.
    Use ``is_reservation_platform()`` for domains that must always be SKIPPED.
    """
    return registered_domain(url) in BOT_BLOCKING_DOMAINS


# ── Tracking / analytics URL detection ───────────────────────────────────────
#
# These hostnames and path fragments belong to tracking pixels, beacon endpoints,
# and analytics collectors.  They are NOT real navigable resources:
# • They often return HTTP 200 with a 1×1 transparent GIF body.
# • Their domains are heavily bot-gated — any non-200 is noise, not a defect.
# • Flagging them as broken images pollutes the actionable failure count.
#
# URLs matched here are SKIPPED in image_validator and link_validator.

_TRACKING_HOSTS: frozenset[str] = frozenset({
    # ── Google tag / analytics stack ──────────────────────────────────────
    "bat.bing.com",
    "www.googletagmanager.com",
    "googletagmanager.com",
    "www.google-analytics.com",
    "google-analytics.com",
    "analytics.google.com",
    # ── Google ad network ─────────────────────────────────────────────────
    "stats.g.doubleclick.net",
    "doubleclick.net",
    "ad.doubleclick.net",
    "cm.g.doubleclick.net",
    "googlesyndication.com",        # Google ad syndication (1×1 impression pixels)
    "googleadservices.com",         # Google ad conversion pixels
    "adservice.google.com",         # Google ad service pixel endpoint
    # ── Third-party ad networks ───────────────────────────────────────────
    "yieldoptimizer.com",           # YieldOptimizer ad pixel (naturalWidth == 0)
    "ads.yieldoptimizer.com",
    "pixel.advertising.com",
    "pixel.quantserve.com",
    "casalemedia.com",
    "dsum-sec.casalemedia.com",
    "adsystem.com",                 # generic ad-system pixel endpoints
    "adnxs.com",                    # AppNexus / Xandr
    "rubiconproject.com",           # Rubicon/Magnite
    "openx.net",                    # OpenX
    "moatads.com",                  # Oracle Moat viewability pixel
    "adsrvr.org",                   # The Trade Desk
    "tapad.com",                    # Tapad cross-device
    "turn.com",                     # Amobee (formerly Turn)
    "mathtag.com",                  # MediaMath
    # ── Social pixels ────────────────────────────────────────────────────
    "www.facebook.com",             # pixel endpoint (also bot-blocking)
    "connect.facebook.net",
    "ct.pinterest.com",
    "analytics.tiktok.com",
    # ── Measurement / DMP ────────────────────────────────────────────────
    "b.scorecardresearch.com",
    "sb.scorecardresearch.com",
    "beacon.krxd.net",
    "tags.tiqcdn.com",
    "cdn.heapanalytics.com",
    "heapanalytics.com",
    "sc-static.net",
})

_TRACKING_PATH_FRAGMENTS: tuple[str, ...] = (
    "/action/",         # bat.bing.com/action/…
    "/collect",         # Google Analytics /collect endpoint
    "/beacon",          # generic beacon path
    "/pixel",           # generic pixel path
    "/track",           # generic tracking path
    "/analytics",
    "/gtag/",
    "/ga.js",
    "/analytics.js",
    "/gtm.js",
)


def is_tracking_url(url: str) -> bool:
    """
    Return True when *url* is a tracking pixel, analytics beacon, or ad-tech
    collector — resources that are intentionally opaque to automated clients.

    Matched URLs are SKIPPED rather than checked so they never appear as false
    failures in the health report.
    """
    parsed = urlparse(url)
    host   = parsed.netloc.lower().lstrip("www.")

    # Exact host match
    if host in _TRACKING_HOSTS or parsed.netloc.lower() in _TRACKING_HOSTS:
        return True

    # Registered-domain fallback (catches subdomains like eu.bat.bing.com)
    try:
        import tldextract
        ext = tldextract.extract(url)
        rd  = f"{ext.domain}.{ext.suffix}"
        if rd in _TRACKING_HOSTS:
            return True
    except Exception:
        pass

    # Path-fragment match (only when host is not our own site)
    path = parsed.path.lower()
    if any(frag in path for frag in _TRACKING_PATH_FRAGMENTS):
        # Narrow to known analytics-style query params to avoid over-matching
        qs = parsed.query.lower()
        if any(k in qs for k in ("tid=", "t=event", "cid=", "v=1", "ea=", "ec=")):
            return True

    return False


# ── SVG detection ──────────────────────────────────────────────────────────────

def is_svg_url(url: str) -> bool:
    """
    Return True when the URL path ends with ``.svg``.

    SVG images have ``naturalWidth == 0`` in all browsers when opened without
    an explicit ``width`` attribute, so the naturalWidth render check MUST be
    skipped for SVGs.
    """
    path = urlparse(url).path.lower()
    return path.endswith(".svg")


# ── Extraction helpers ─────────────────────────────────────────────────────────

# Matches absolute or relative URLs that look like static assets we can skip
_ASSET_RE = re.compile(r"\.(pdf|zip|tar|gz|exe|dmg|mp4|mp3|avi|mov)$", re.I)


def is_asset(url: str) -> bool:
    """Return True for large binary assets we should not crawl."""
    return bool(_ASSET_RE.search(urlparse(url).path))
