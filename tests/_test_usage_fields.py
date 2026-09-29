"""usage.jsonl 的觀測欄位（slot / attempts / ttfb_ms / refresh_ms）。

Run with: python _test_usage_fields.py
No upstream credentials or outbound network are used.
"""
import atexit
import json
import os
import sys
import tempfile
import unittest

_startup_dir = tempfile.TemporaryDirectory(prefix="usage-fields-")
atexit.register(_startup_dir.cleanup)
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
os.environ["WB_PROXY_USAGE_DIR"] = os.path.join(_startup_dir.name, "usage")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy as proxy

USAGE_LOG = os.path.join(os.environ["WB_PROXY_USAGE_DIR"], "usage.jsonl")


def last_row():
    with open(USAGE_LOG, encoding="utf-8") as fh:
        return json.loads(fh.read().strip().splitlines()[-1])


class UsageFieldTests(unittest.TestCase):
    def test_usage_row_carries_observability_fields(self):
        proxy.record_usage("m", {"total_tokens": 10}, stream=True,
                           elapsed_ms=100, ttft_ms=50, ttfb_ms=20,
                           account="uid-1", slot="slot-3", attempts=2,
                           refresh_ms=30)
        row = last_row()
        self.assertEqual(row["slot"], "slot-3")
        self.assertEqual(row["attempts"], 2)
        self.assertEqual(row["ttfb_ms"], 20)
        self.assertEqual(row["refresh_ms"], 30)
        self.assertEqual(row["ttft_ms"], 50)

    def test_slot_defaults_to_direct_without_binding(self):
        proxy.record_usage("m", None, stream=False, elapsed_ms=1,
                           account="uid-2")
        row = last_row()
        self.assertEqual(row["slot"], "direct")
        # 沒有對應欄位時不出現在行裡，避免一堆 null。
        self.assertNotIn("attempts", row)
        self.assertNotIn("ttfb_ms", row)
        self.assertNotIn("refresh_ms", row)

    def test_error_row_carries_the_same_fields(self):
        proxy.record_error("m", 502, "boom", elapsed_ms=10,
                           account="uid-3", slot="slot-5", attempts=3,
                           ttfb_ms=15)
        row = last_row()
        self.assertTrue(row["error"])
        self.assertEqual(row["slot"], "slot-5")
        self.assertEqual(row["attempts"], 3)
        self.assertEqual(row["ttfb_ms"], 15)

    def test_attempts_meta_folding(self):
        self.assertEqual(proxy._attempts_meta(None), (None, None, None))
        self.assertEqual(proxy._attempts_meta([]), (None, None, None))
        attempts = [
            {"uid": "a", "slot": "slot-1", "pick_ms": 3, "refresh_ms": 40,
             "status": 429, "outcome": "rate_limited"},
            {"uid": "a", "slot": "fallback", "pick_ms": 0, "refresh_ms": None,
             "status": 200, "outcome": "ok"},
        ]
        count, refresh, slot = proxy._attempts_meta(attempts)
        self.assertEqual(count, 2)
        self.assertEqual(refresh, 40)
        self.assertEqual(slot, "fallback")

    def test_observability_headers(self):
        account = type("A", (), {"proxy_slot": "slot-2"})()
        headers = proxy._wb_observability_headers(account, None)
        self.assertEqual(headers, {"X-WB-Slot": "slot-2"})
        direct = type("A", (), {"proxy_slot": ""})()
        headers = proxy._wb_observability_headers(
            direct, [{"slot": "slot-1"}, {"slot": "fallback"}])
        self.assertEqual(headers["X-WB-Slot"], "fallback")
        self.assertEqual(headers["X-WB-Attempts"], "2")


if __name__ == "__main__":
    unittest.main(verbosity=2)
