"""429 連擊遞進冷卻（Account.note_rate_limit）與持久化行為。

Run with: python _test_progressive_cooldown.py
No upstream credentials or outbound network are used.
"""
import atexit
import json
import os
import sys
import tempfile
import time
import unittest

_startup_dir = tempfile.TemporaryDirectory(prefix="progressive-cooldown-")
atexit.register(_startup_dir.cleanup)
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts as accounts


class ProgressiveCooldownTests(unittest.TestCase):
    def acct(self, **kw):
        data = {"uid": "u1", "realm": "cn", "accessToken": "t"}
        data.update(kw)
        return accounts.Account(data)

    def test_first_429_is_model_scoped_only(self):
        a = self.acct()
        a.note_rate_limit("429", model="m1", until=time.time() + 60)
        self.assertEqual(a.rate_streak, 1)
        self.assertFalse(a.public()["inCooldown"])
        self.assertGreater(a.throttle_wait(model="m1"), 0)
        self.assertEqual(a.throttle_wait(model="m2"), 0.0)

    def test_second_429_escalates_account_wide(self):
        a = self.acct()
        a.note_rate_limit("429", model="m1")
        a.note_rate_limit("429", model="m1")
        self.assertTrue(a.public()["inCooldown"])
        wait = a.throttle_wait(model="m1")
        self.assertGreaterEqual(wait, 295)
        self.assertLessEqual(wait, 300)

    def test_third_and_beyond_hit_the_cap(self):
        a = self.acct()
        for _ in range(4):
            a.note_rate_limit("429", model="m")
        wait = a.throttle_wait(model="m")
        self.assertGreaterEqual(wait, 890)
        self.assertLessEqual(wait, 900)

    def test_single_account_pool_never_escalates(self):
        a = self.acct()
        for _ in range(3):
            a.note_rate_limit("429", model="m", single_account=True)
        self.assertEqual(a.rate_streak, 3)
        self.assertFalse(a.public()["inCooldown"])

    def test_clear_error_resets_streak_and_cooldown(self):
        a = self.acct()
        a.note_rate_limit("429", model="m")
        a.note_rate_limit("429", model="m")
        self.assertTrue(a.public()["inCooldown"])
        a.clear_error()
        self.assertEqual(a.rate_streak, 0)
        self.assertEqual(a.throttle_wait(model="m"), 0.0)
        self.assertFalse(a.public()["inCooldown"])

    def test_streak_and_cooldown_persist_via_file(self):
        d = tempfile.mkdtemp(prefix="pcool-store-")
        a = accounts.Account({"uid": "persist-me", "realm": "cn", "accessToken": "t"})
        a.path = a.save(d)
        a.note_rate_limit("429", model="m")
        a.note_rate_limit("429", model="m")
        with open(a.path, encoding="utf-8") as fh:
            stored = json.load(fh)
        self.assertEqual(stored.get("rateStreak"), 2)
        self.assertGreater(float(stored.get("cooldownUntil") or 0), time.time())
        # 重啟後讀回：連擊與冷卻都還在（VOLATILE 只在導出時剔除）。
        reloaded = accounts.Account(stored, path=a.path)
        self.assertEqual(reloaded.rate_streak, 2)
        self.assertTrue(reloaded.public()["inCooldown"])

    def test_import_drops_the_streak(self):
        import base64

        def b64(obj):
            return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

        token = "%s.%s.%s" % (b64({"alg": "none", "typ": "JWT"}),
                              b64({"sub": "import-uid",
                                   "exp": int(time.time()) + 3600}),
                              "sig")
        # 導入文檔裡帶著舊機器的冷卻狀態：normalise_import_row 逐欄重建，
        # rateStreak / cooldownUntil 不在其中，導入後帳號必須是乾淨的。
        row = accounts.normalise_import_row({
            "accessToken": token,
            "rateStreak": 5,
            "cooldownUntil": time.time() + 900,
        })
        self.assertNotIn("rateStreak", row)
        self.assertEqual(accounts.Account(row).rate_streak, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
