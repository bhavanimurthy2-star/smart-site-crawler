"""
Root conftest.py — two responsibilities:

1. sys.path fix
   Inserts the project root at the front of sys.path so pytest can import
   top-level packages (config, crawler, validators, utils, reports) without
   a ``pip install -e .`` step.

2. Custom CLI options (pytest_addoption)
   Registers --base-url, --depth, and --open-report so pytest accepts them
   on the command line and exposes them via ``request.config.getoption()``.

   Example usage:
       pytest -v --base-url=https://example.com --depth=3 --open-report
"""

import sys
from pathlib import Path

# ── 1. Project-root import fix ─────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))


# ── 2. Custom CLI options ──────────────────────────────────────────────────────

def pytest_addoption(parser: "pytest.Parser") -> None:  # type: ignore[name-defined]
    """
    Register project-specific command-line options.

    These become available as:
        request.config.getoption("--base-url")
        request.config.getoption("--depth")
        request.config.getoption("--open-report")

    They are also visible in ``pytest --help`` under the
    'Website Health Check' group.
    """
    group = parser.getgroup(
        "website-health",
        description="Website Health Check options",
    )

    group.addoption(
        "--base-url",
        action="store",
        metavar="URL",
        default=None,
        help=(
            "Base URL to start crawling from. "
            "Overrides config.py and the BASE_URL environment variable. "
            "Example: --base-url=https://example.com"
        ),
    )

    group.addoption(
        "--depth",
        action="store",
        type=int,
        metavar="N",
        default=None,
        help=(
            "Maximum BFS crawl depth (integer). "
            "Defaults to the value set in config.py (2). "
            "Example: --depth=3"
        ),
    )

    group.addoption(
        "--open-report",
        action="store_true",
        default=False,
        help=(
            "Automatically open the generated HTML report in the default "
            "browser when the test session finishes."
        ),
    )
