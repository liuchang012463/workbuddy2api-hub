"""槽位感知重試與備選出口（open_upstream + AccountPool.pick）。

用 mock 的 urlopen 模擬上游：不需要真網路，憑證全是合成的。
"""
import atexit
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

_startup_dir = tempfile.TemporaryDirectory(prefix="slot-retry-")
atexit.register(_startup_dir.cleanup)
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
os.environ.setdefault("USAGE_DIR", os.path.join(_startup_dir.name, "usage"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts as accounts
import wb_proxy as proxy
import wb_settings as settings


class FakeResponse(object):
    """Just enough of the urllib response surface for open_upstream."""

    def __init__(self, marker):
        self.marker = marker
        self.attempts = None

    def close(self):
        pass

    def read(self, n=-1):
        return b"{}"


def http_error(code):
    return urllib.error.HTTPError("https://upstream", code, "err", None,
                                  io.BytesIO(b'{"msg":"x"}'))


def make_pool():
    """slot-1: a1+a2 (proxy 17901), slot-2: a3 (proxy 17902), a4: direct."""
    pool = accounts.AccountPool(_startup_dir.name)
    for uid, slot, port in (("a1", "slot-1", 17901), ("a2", "slot-1", 17901),
                            ("a3", "slot-2", 17902), ("a4", "", 0)):
        acc = accounts.Account({"uid": uid, "realm": "intl", "accessToken": "t.%s.x" % uid,
                                "proxySlot": slot})
        acc.proxy = "http://172.17.0.1:%d" % port if slot else ""
        pool.accounts.append(acc)
    return pool


PAYLOAD = {"model": "some-intl-model", "messages": [{"role": "user", "content": "hi"}],
           "stream": True}


class SlotAwareRetryTests(unittest.TestCase):
    def setUp(self):
        self.pool = make_pool()
        proxy.POOL = self.pool
        self.addCleanup(setattr, proxy, "POOL", None)
        # open_upstream 讀的是模組常量 proxy.ACCOUNTS_DIR（不是環境變量），
        # 測試把它指到臨時目錄，避免讀寫真實的 src/accounts。
        patcher = mock.patch.object(proxy, "ACCOUNTS_DIR", _startup_dir.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        settings.set_slot_fallback_url(_startup_dir.name, "off")

    def open_upstream(self):
        return proxy.open_upstream(dict(PAYLOAD), target_realm="intl")

    def test_pick_honours_exclude_slots(self):
        got = self.pool.pick(realm="intl", exclude_slots={"slot-1"})
        self.assertEqual(got.uid, "a3")
        got = self.pool.pick(realm="intl", exclude_slots={"slot-1", "slot-2"})
        self.assertEqual(got.uid, "a4")
        got = self.pool.pick(realm="intl", exclude_slots={"slot-1", "slot-2", "direct"})
        # 池子打空：放開槽位偏好，仍然要給出帳號。
        self.assertIsNotNone(got)

    def test_direct_accounts_respect_the_direct_marker(self):
        got = self.pool.pick(realm="intl", exclude_slots={"direct"})
        self.assertIn(got.uid, ("a1", "a2", "a3"))

    def test_transient_failure_moves_off_the_failed_slot(self):
        calls = []

        def fake_urlopen(req, timeout=30, proxy=""):
            calls.append(proxy)
            if proxy == "http://172.17.0.1:17901":
                raise ConnectionResetError("connection reset by peer")
            return FakeResponse("slot-2")

        with mock.patch.object(accounts, "urlopen", side_effect=fake_urlopen):
            resp, account = self.open_upstream()
        self.assertEqual(account.uid, "a3")
        self.assertEqual(resp.marker, "slot-2")
        self.assertEqual(calls, ["http://172.17.0.1:17901", "http://172.17.0.1:17902"])
        self.assertEqual(len(resp.attempts), 2)
        self.assertEqual(resp.attempts[0]["slot"], "slot-1")
        self.assertEqual(resp.attempts[0]["outcome"], "transient")
        self.assertEqual(resp.attempts[1]["slot"], "slot-2")
        self.assertEqual(resp.attempts[1]["outcome"], "ok")

    def test_fallback_exit_retries_same_account_direct(self):
        settings.set_slot_fallback_url(_startup_dir.name, "")
        calls = []

        def fake_urlopen(req, timeout=30, proxy=""):
            calls.append(proxy)
            if proxy:
                raise ConnectionResetError("connection reset by peer")
            return FakeResponse("direct")

        with mock.patch.object(accounts, "urlopen", side_effect=fake_urlopen):
            resp, account = self.open_upstream()
        # a1 在 slot-1 上瞬態失敗 → 同帳號走 DIRECT 備選 → 成功。
        self.assertEqual(calls, ["http://172.17.0.1:17901", ""])
        self.assertEqual(account.uid, "a1")
        self.assertEqual(len(resp.attempts), 2)
        self.assertEqual(resp.attempts[-1]["slot"], "fallback")
        self.assertEqual(resp.attempts[-1]["outcome"], "ok")

    def test_429_rotates_accounts_and_records_attempts(self):
        def fake_urlopen(req, timeout=30, proxy=""):
            raise http_error(429)

        with mock.patch.object(accounts, "urlopen", side_effect=fake_urlopen):
            with self.assertRaises(proxy.RateLimited) as ctx:
                self.open_upstream()
        exc = ctx.exception
        self.assertTrue(hasattr(exc, "attempts"))
        # a1-a3 各吃一次 429（連擊遞進把 a2/a3 帳號級冷卻），a4 直連再吃一次，
        # 之後 pick 全部落空 → 收尾上拋。
        self.assertEqual(len(exc.attempts), 4)
        self.assertTrue(all(a["outcome"] == "rate_limited" for a in exc.attempts))

    def test_no_fallback_when_disabled(self):
        settings.set_slot_fallback_url(_startup_dir.name, "off")
        calls = []

        def fake_urlopen(req, timeout=30, proxy=""):
            calls.append(proxy)
            if proxy == "http://172.17.0.1:17901":
                raise ConnectionResetError("connection reset by peer")
            return FakeResponse("slot-2")

        with mock.patch.object(accounts, "urlopen", side_effect=fake_urlopen):
            resp, account = self.open_upstream()
        self.assertNotIn("", calls)
        self.assertEqual(account.uid, "a3")

    def test_fallback_never_reroutes_an_already_direct_account(self):
        settings.set_slot_fallback_url(_startup_dir.name, "")
        # 池裡只剩直連帳號：瞬態失敗後備選出口也是 DIRECT，不該重複排程。
        pool = accounts.AccountPool(_startup_dir.name)
        a4 = accounts.Account({"uid": "a4", "realm": "intl", "accessToken": "t.a4.x"})
        a4.proxy = ""
        pool.accounts = [a4]
        proxy.POOL = pool
        self.addCleanup(setattr, proxy, "POOL", None)
        calls = []

        def fake_urlopen(req, timeout=30, proxy=""):
            calls.append(proxy)
            if len(calls) == 1:
                raise ConnectionResetError("connection reset by peer")
            return FakeResponse("ok")

        with mock.patch.object(accounts, "urlopen", side_effect=fake_urlopen):
            resp, account = self.open_upstream()
        self.assertEqual(calls, ["", ""])
        self.assertEqual(account.uid, "a4")
        self.assertTrue(all(a["slot"] == "direct" for a in resp.attempts))


if __name__ == "__main__":
    unittest.main(verbosity=2)
