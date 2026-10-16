"""Launch helpers for browser tests."""

import unittest

_MISSING_EXECUTABLE = "Executable doesn't exist"


def launch_chromium(playwright, **kwargs):
    """Launch bundled Chromium.

    A missing browser executable becomes ``unittest.SkipTest``. Any other
    launch failure still propagates.
    """
    try:
        return playwright.chromium.launch(**kwargs)
    except Exception as exc:
        if _MISSING_EXECUTABLE in str(exc):
            raise unittest.SkipTest("Playwright Chromium executable is missing") from exc
        raise
