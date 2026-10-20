"""HTTP entry-point helper tests."""
from __future__ import annotations

import unittest

from run_api import access_urls


class TestAccessUrls(unittest.TestCase):
    def test_wildcard_bind_exposes_localhost_and_lan_url(self):
        urls = access_urls("0.0.0.0", 8000, lan_ip="192.168.1.25")

        self.assertEqual(
            urls,
            ["http://127.0.0.1:8000", "http://192.168.1.25:8000"],
        )


if __name__ == "__main__":
    unittest.main()
