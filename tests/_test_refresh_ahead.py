"""wb_scheduler 的 Token 預刷新巡檢（refresh-ahead）。

Run with: python _test_refresh_ahead.py
No upstream credentials or outbound network are used.
"""
import atexit
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

_startup_dir = tempfile.TemporaryDirectory(prefix="refresh-ahead-")
atexit.register(_startup_dir.cleanup)
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts as accounts
import wb_scheduler as scheduler


class FakePool(object):
    def __init__(self, accounts_list):
        self.accounts = accounts_list


def make_account(uid, seconds_left, refresh_token=True, enabled=True):
    acc = accounts.Account({"uid": uid, "realm": "intl", "accessToken": "t.%s.x" % uid,
                            "refreshToken": "r" if refresh_token else "",
                            "enabled": enabled})
    acc.expires_at = time.time() + seconds_left if seconds_left is not None else 0
    return acc


class RefreshAheadTests(unittest.TestCase):
    def setUp(self):
        self.sched = scheduler.Scheduler(FakePool([]))

    def run_once(self, pool_accounts):
        self.sched.pool = FakePool(pool_accounts)
        with mock.patch.object(accounts.Account, "refresh", autospec=True) as ref:
            ref.return_value = True
            self.sched._refresh_ahead_once()
        return ref

    def test_refreshes_tokens_expiring_within_15_minutes(self):
        due = make_account("due", seconds_left=600)
        ref = self.run_once([due])
        self.assertEqual(ref.call_count, 1)
        self.assertGreater(due._refresh_ahead_at, 0)

    def test_skips_tokens_with_time_to_spare(self):
        fresh = make_account("fresh", seconds_left=7200)
        no_exp = make_account("noexp", seconds_left=None)
        ref = self.run_once([fresh, no_exp])
        self.assertEqual(ref.call_count, 0)

    def test_respects_backoff_after_a_refresh_attempt(self):
        due = make_account("due", seconds_left=600)
        self.run_once([due])
        ref = self.run_once([due])
        self.assertEqual(ref.call_count, 0, "同一帳號 5 分鐘內不應重複嘗試")

    def test_skips_disabled_and_tokenless_accounts(self):
        disabled = make_account("disabled", seconds_left=600, enabled=False)
        tokenless = make_account("tokenless", seconds_left=600, refresh_token=False)
        ref = self.run_once([disabled, tokenless])
        self.assertEqual(ref.call_count, 0)

    def test_backoff_expires_and_retries(self):
        due = make_account("due", seconds_left=600)
        self.run_once([due])
        due._refresh_ahead_at = time.time() - 301
        ref = self.run_once([due])
        self.assertEqual(ref.call_count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
