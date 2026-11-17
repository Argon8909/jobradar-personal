import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from playwright.sync_api import Error as BrowserError, TimeoutError as BrowserTimeout
from jobradar.config import load_settings
from jobradar.sources.base import SourceError
from jobradar.sources.hh import HHAdapter

ROOT = Path(__file__).resolve().parents[2]


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.adapter = HHAdapter(load_settings(ROOT/"config/v0.1.json"))

    def test_tls_error_is_reported_without_disabling_verification(self):
        with patch("jobradar.sources.hh.sync_playwright",side_effect=BrowserError("net::ERR_CERT_AUTHORITY_INVALID")):
            with self.assertRaises(SourceError) as result:
                with self.adapter._browser():
                    pass
        self.assertEqual(result.exception.code,"SOURCE_TLS")

    def test_timeout_retry_is_safe_and_bounded(self):
        page = Mock()
        page.url = "https://hh.ru"
        page.locator.return_value.count.return_value = 0
        page.goto.side_effect = [BrowserTimeout("timeout"),Mock(status=200)]
        self.adapter._goto(page,"https://hh.ru",time.monotonic()+30)
        self.assertEqual(page.goto.call_count,2)
        page.goto.reset_mock(side_effect=True)
        page.goto.side_effect = BrowserTimeout("timeout")
        with self.assertRaises(BrowserTimeout):
            self.adapter._goto(page,"https://hh.ru",time.monotonic()+30)
        self.assertEqual(page.goto.call_count,3)

    def test_captcha_does_not_trigger_automatic_retry_or_bypass(self):
        page = Mock()
        page.url = "https://hh.ru/captcha"
        page.goto.return_value.status = 200
        with self.assertRaises(SourceError) as result:
            self.adapter._goto(page,"https://hh.ru",time.monotonic()+30)
        self.assertEqual(result.exception.code,"CAPTCHA")
        self.assertEqual(page.goto.call_count,1)

    def test_detail_ids_cannot_be_arbitrary_urls(self):
        with self.assertRaises(SourceError) as result:
            self.adapter.fetch_details("https://example.com")
        self.assertEqual(result.exception.code,"VALIDATION_ERROR")
