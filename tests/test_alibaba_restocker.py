import unittest
from unittest.mock import Mock, patch

import alibaba_restocker


class FakeDebug:
    def __init__(self):
        self.events = []

    def log(self, event, payload=None):
        self.events.append((event, payload or {}))


class AlibabaRestockerInspectionTests(unittest.TestCase):
    def test_wait_ends_on_page_close_event_without_polling(self):
        page = Mock()
        page.is_closed.return_value = False
        page.wait_for_event.return_value = None
        debug = FakeDebug()

        with patch.object(alibaba_restocker.time, "sleep") as sleep:
            result = alibaba_restocker.wait_for_inspection_or_page_close(page, 300, debug)

        self.assertEqual(result, "page_closed")
        page.wait_for_event.assert_called_once_with("close", timeout=300000)
        sleep.assert_not_called()
        self.assertEqual(debug.events[-1][0], "inspection_page_closed")

    def test_wait_keeps_300_second_timeout(self):
        page = Mock()
        page.is_closed.return_value = False
        page.wait_for_event.side_effect = alibaba_restocker.PlaywrightTimeoutError("timeout")
        debug = FakeDebug()

        result = alibaba_restocker.wait_for_inspection_or_page_close(page, 300, debug)

        self.assertEqual(result, "timeout")
        page.wait_for_event.assert_called_once_with("close", timeout=300000)
        self.assertEqual(debug.events[-1][0], "inspection_wait_timeout")


if __name__ == "__main__":
    unittest.main()
