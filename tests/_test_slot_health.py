"""P5：wb_slothealth 連敗隔離、EWMA、外部報告回灌與 pick 集成。

Run with: python _test_slot_health.py
No upstream credentials or outbound network are used.
"""
import atexit
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

_startup_dir = tempfile.TemporaryDirectory(prefix="slot-health-")
atexit.register(_startup_dir.cleanup)
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
os.environ.setdefault("WB_PROXY_USAGE_DIR", os.path.join(_startup_dir.name, "usage"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts as accounts
import wb_proxy as proxy
import wb_slothealth as health


class SlotHealthTests(unittest.TestCase):
    def setUp(self):
        health.reset()
        self.addCleanup(health.reset)

    def test_consecutive_failures_quarantine_and_success_clears(self):
        for i in range(health.QUARANTINE_THRESHOLD - 1):
            health.record("slot-3", False, outcome="transient")
        self.assertFalse(health.is_quarantined("slot-3"))
        health.record("slot-3", False, outcome="transient")
        self.assertTrue(health.is_quarantined("slot-3"))
        # 半開：成功一次即解除
        health.record("slot-3", True, resp_ms=250)
        self.assertFalse(health.is_quarantined("slot-3"))
        self.assertEqual(health._entry("slot-3")["consecutive_errors"], 0)

    def test_ttl_expiry_reopens_the_slot(self):
        for _ in range(health.QUARANTINE_THRESHOLD):
            health.record("slot-5", False, outcome="http_502")
        self.assertTrue(health.is_quarantined("slot-5"))
        with mock.patch.object(health.time, "time",
                               return_value=time.time() + health.QUARANTINE_TTL + 1):
            self.assertFalse(health.is_quarantined("slot-5"))
            self.assertEqual(health.quarantined(), set())

    def test_ok_attempts_track_ewma(self):
        health.record("slot-1", True, resp_ms=200)
        self.assertEqual(health._entry("slot-1")["ewma_ms"], 200)
        health.record("slot-1", True, resp_ms=400)
        expected = round(200 * 0.7 + 400 * 0.3, 1)
        self.assertEqual(health._entry("slot-1")["ewma_ms"], expected)

    def test_report_normalizes_slot_and_port(self):
        accepted = health.apply_report([
            {"slot": 1, "ok": True, "latency_ms": 245, "upload_ms": 900},
            {"port": 17907, "ok": True, "latency_ms": 30},
            {"slot": "slot-3", "ok": False},
            {"ok": True, "latency_ms": 5},
        ])
        self.assertEqual(accepted, 3)
        snap = health.snapshot()
        self.assertIn("slot-1", snap)
        self.assertIn("slot-7", snap)
        self.assertIn("slot-3", snap)
        self.assertEqual(snap["slot-1"]["reported"]["upload_ms"], 900)
        self.assertFalse(snap["slot-3"]["reported"]["ok"])
        # 報告本身不觸發隔離
        self.assertEqual(health.quarantined(), set())

    def test_direct_key_roundtrip(self):
        health.record("direct", False, outcome="transient")
        self.assertIn("direct", health.snapshot())
        health.record("", True, resp_ms=100)
        # 空 slot 與 "direct" 是同一個 key
        snap = health.snapshot()
        self.assertNotIn("", snap)
        self.assertEqual(snap["direct"]["ok_count"], 1)


class QuarantinePickIntegrationTests(unittest.TestCase):
    def setUp(self):
        health.reset()
        self.addCleanup(health.reset)
        self.pool = accounts.AccountPool(_startup_dir.name)
        for uid, slot, port in (("a1", "slot-1", 17901), ("a3", "slot-2", 17902)):
            acc = accounts.Account({"uid": uid, "realm": "intl",
                                    "accessToken": "t.%s.x" % uid, "proxySlot": slot})
            acc.proxy = "http://172.17.0.1:%d" % port
            self.pool.accounts.append(acc)
        self.old_pool = proxy.POOL
        proxy.POOL = self.pool
        self.addCleanup(setattr, proxy, "POOL", self.old_pool)

    def test_quarantined_slot_is_skipped_by_pick(self):
        # 連敗把 slot-1 隔離：pick 該跳過 a1 直接給 a3
        for _ in range(health.QUARANTINE_THRESHOLD):
            health.record("slot-1", False, outcome="transient")
        got = self.pool.pick(realm="intl", exclude_slots=health.quarantined())
        self.assertEqual(got.uid, "a3")
        # 池子裡只有被隔離的出口：放開限制，仍然給出帳號（餓死保護）
        only = accounts.Account({"uid": "solo", "realm": "intl",
                                 "accessToken": "t.solo.x", "proxySlot": "slot-1"})
        only.proxy = "http://172.17.0.1:17901"
        self.pool.accounts = [only]
        got = self.pool.pick(realm="intl", exclude_slots=health.quarantined())
        self.assertEqual(got.uid, "solo")


if __name__ == "__main__":
    unittest.main(verbosity=2)
