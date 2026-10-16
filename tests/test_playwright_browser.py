"""Browser tests skip only when the Playwright Chromium executable is missing."""

import unittest

from tests.playwright_browser import launch_chromium


class _BrowserType:
    def __init__(self, launch):
        self._launch = launch
        self.kwargs = None

    def launch(self, **kwargs):
        self.kwargs = kwargs
        return self._launch(**kwargs)


class _Playwright:
    def __init__(self, launch):
        self.chromium = _BrowserType(launch)


class LaunchChromiumTests(unittest.TestCase):
    def test_missing_executable_skips(self):
        def launch(**kwargs):
            raise RuntimeError(
                "BrowserType.launch: Executable doesn't exist at /tmp/chrome-linux/chrome\n"
                "Please run the following command to download new browsers:\n"
                "    playwright install"
            )

        with self.assertRaises(unittest.SkipTest):
            launch_chromium(_Playwright(launch), args=["--no-sandbox"])

    def test_other_launch_errors_propagate(self):
        def launch(**kwargs):
            raise RuntimeError("Host system is missing dependencies to run browsers")

        with self.assertRaises(RuntimeError) as caught:
            launch_chromium(_Playwright(launch), headless=True)
        self.assertNotIn("Executable doesn't exist", str(caught.exception))

    def test_successful_launch_returns_browser(self):
        browser = object()

        def launch(**kwargs):
            return browser

        runtime = _Playwright(launch)
        self.assertIs(launch_chromium(runtime, args=["--no-sandbox"]), browser)
        self.assertEqual(runtime.chromium.kwargs, {"args": ["--no-sandbox"]})


if __name__ == "__main__":
    unittest.main()
