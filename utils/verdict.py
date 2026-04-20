"""
Shared verdict enum used by both link_validator and image_validator.

Four possible outcomes for any checked resource:

  PASS    — URL responded successfully and (for images) rendered correctly.
  FAIL    — URL is definitively broken: 4xx on internal, 5xx on any, or
            render failure on non-SVG images.
  WARNING — Resource returned HTTP 200 but could not be fully verified —
            most commonly a browser render timeout.  The image is likely
            valid but needs manual review.  NOT counted as a hard failure.
  SKIPPED — URL could not be reliably assessed: external domain that blocks
            bots (403/429), known social/review platform, or a network-level
            error on an external host.  These are NOT treated as failures.

Using ``str, Enum`` means instances serialise naturally to JSON strings
("PASS", "FAIL", "WARNING", "SKIPPED") without extra conversion.
"""

from enum import Enum


class Verdict(str, Enum):
    PASS    = "PASS"
    FAIL    = "FAIL"
    WARNING = "WARNING"
    SKIPPED = "SKIPPED"

    # ── Convenience predicates ─────────────────────────────────────────────

    @property
    def is_pass(self) -> bool:
        return self is Verdict.PASS

    @property
    def is_fail(self) -> bool:
        return self is Verdict.FAIL

    @property
    def is_warning(self) -> bool:
        return self is Verdict.WARNING

    @property
    def is_skipped(self) -> bool:
        return self is Verdict.SKIPPED

    # ── Sort key (FAIL → WARNING → SKIPPED → PASS) ────────────────────────

    @property
    def sort_order(self) -> int:
        return {"FAIL": 0, "WARNING": 1, "SKIPPED": 2, "PASS": 3}[self.value]
