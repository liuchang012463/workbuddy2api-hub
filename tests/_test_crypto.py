"""P6：賬號憑證靜態加密（wb_crypto）。

Run with: python _test_crypto.py
cryptography 未安裝時只驗證明文降級路徑（與歷史行為一致），其餘用例 skip。
"""
import atexit
import base64
import json
import os
import stat
import sys
import tempfile
import unittest
import unittest.mock

_startup_dir = tempfile.TemporaryDirectory(prefix="crypto-")
atexit.register(_startup_dir.cleanup)
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
os.environ.setdefault("WB_PROXY_USAGE_DIR", os.path.join(_startup_dir.name, "usage"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts as accounts
import wb_crypto
import wb_settings as settings


class CryptoBase(unittest.TestCase):
    def setUp(self):
        wb_crypto.reset()
        for var in ("WB_ENCRYPTION_KEY", "WB_ENCRYPTION_KEY_FILE"):
            os.environ.pop(var, None)
        wb_crypto.configure(_startup_dir.name)
        self.addCleanup(wb_crypto.reset)


@unittest.skipUnless(wb_crypto.enabled(), "cryptography not installed")
class EncryptModeTests(CryptoBase):
    def test_roundtrip(self):
        env = wb_crypto.encrypt_field("secret-token-value")
        self.assertTrue(wb_crypto.is_encrypted(env))
        self.assertEqual(env[wb_crypto.ENVELOPE_MARKER], "v1")
        self.assertEqual(env["alg"], "AESGCM")
        self.assertNotIn("secret", json.dumps(env))
        self.assertEqual(wb_crypto.decrypt_field(env), "secret-token-value")

    def test_plaintext_passthrough_on_decrypt(self):
        self.assertEqual(wb_crypto.decrypt_field("legacy-plain"), "legacy-plain")
        self.assertEqual(wb_crypto.decrypt_field(None), "")
        self.assertEqual(wb_crypto.decrypt_field(""), "")

    def test_empty_value_not_enveloped(self):
        self.assertEqual(wb_crypto.encrypt_field(""), "")
        self.assertIsNone(wb_crypto.encrypt_field(None))

    def test_key_autogeneration_and_permissions(self):
        account = accounts.Account({"uid": "enc-me", "realm": "cn",
                                    "accessToken": "t.a.b", "refreshToken": "r"})
        path = account.save(_startup_dir.name)
        key_path = os.path.join(_startup_dir.name, "encryption.key")
        self.assertTrue(os.path.exists(key_path))
        mode = stat.S_IMODE(os.stat(key_path).st_mode)
        self.assertEqual(mode, 0o600, "密鑰文件必須 0600")
        raw = base64.b64decode(open(key_path).read().strip(), validate=True)
        self.assertEqual(len(raw), 32)

    def test_save_writes_envelope_reload_returns_plaintext(self):
        account = accounts.Account({"uid": "round-trip", "realm": "cn",
                                    "accessToken": "t.abc.def", "refreshToken": "rt-1"})
        path = account.save(_startup_dir.name)
        with open(path, encoding="utf-8") as fh:
            stored = json.load(fh)
        self.assertTrue(wb_crypto.is_encrypted(stored["accessToken"]))
        self.assertTrue(wb_crypto.is_encrypted(stored["refreshToken"]))
        self.assertNotIn("t.abc.def", json.dumps(stored))
        reloaded = accounts.Account(stored, path=path)
        self.assertEqual(reloaded.access_token, "t.abc.def")
        self.assertEqual(reloaded.refresh_token, "rt-1")
        # 內存值與 to_dict() 保持明文（導出/導入鏈路不變）
        self.assertEqual(account.to_dict()["accessToken"], "t.abc.def")

    def test_wrong_key_fails_loud(self):
        account = accounts.Account({"uid": "keyed", "realm": "cn",
                                    "accessToken": "t.x.y"})
        path = account.save(_startup_dir.name)
        with open(path, encoding="utf-8") as fh:
            stored = json.load(fh)
        wb_crypto.reset()
        os.environ["WB_ENCRYPTION_KEY"] = base64.b64encode(b"x" * 32).decode()
        self.addCleanup(os.environ.pop, "WB_ENCRYPTION_KEY", None)
        with self.assertRaises(wb_crypto.CryptoError) as ctx:
            accounts.Account(stored, path=path)
        self.assertIn("encryption.key", str(ctx.exception))

    def test_env_key_takes_precedence(self):
        os.environ["WB_ENCRYPTION_KEY"] = base64.b64encode(b"y" * 32).decode()
        self.addCleanup(os.environ.pop, "WB_ENCRYPTION_KEY", None)
        env = wb_crypto.encrypt_field("v")
        wb_crypto.reset()
        os.environ["WB_ENCRYPTION_KEY"] = base64.b64encode(b"y" * 32).decode()
        self.assertEqual(wb_crypto.decrypt_field(env), "v")
        self.assertFalse(os.path.exists(os.path.join(_startup_dir.name, "encryption.key")),
                         "提供了環境變量密鑰就不該生成文件")


class DisabledModeTests(CryptoBase):
    """降級路徑靠強制模擬：cryptography 是否真實安裝屬環境巧合，
    這裡 patch 掉 AESGCM 才能在任何環境下都覆蓋到。"""

    def setUp(self):
        super().setUp()
        patcher = unittest.mock.patch.object(wb_crypto, "AESGCM", None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_without_cryptography_behaviour_is_legacy(self):
        self.assertFalse(wb_crypto.enabled())
        self.assertEqual(wb_crypto.encrypt_field("t"), "t")
        self.assertEqual(wb_crypto.decrypt_field("t"), "t")
        account = accounts.Account({"uid": "plain", "realm": "cn",
                                    "accessToken": "t.a.b"})
        path = account.save(_startup_dir.name)
        with open(path, encoding="utf-8") as fh:
            stored = json.load(fh)
        self.assertEqual(stored["accessToken"], "t.a.b", "降級模式必須是明文（歷史行為）")
        self.assertFalse(os.path.exists(os.path.join(_startup_dir.name, "encryption.key")))

    def test_decrypting_envelope_without_crypto_fails_loud(self):
        env = {"$wbEncrypted": "v1", "alg": "AESGCM", "ct": "AAAA"}
        with self.assertRaises(wb_crypto.CryptoError) as ctx:
            wb_crypto.decrypt_field(env)
        self.assertIn("cryptography", str(ctx.exception))


@unittest.skipUnless(wb_crypto.enabled(), "cryptography not installed")
class ExportCompatibilityTests(CryptoBase):
    def test_export_document_stays_plaintext(self):
        account = accounts.Account({"uid": "exported", "realm": "cn",
                                    "accessToken": "t.export.here", "refreshToken": "r"})
        account.save(_startup_dir.name)
        exported = accounts.account_to_export(account)
        self.assertEqual(exported["accessToken"], "t.export.here")
        # 導入側拿明文建 Account 不需要密鑰
        imported = accounts.Account(dict(exported))
        self.assertEqual(imported.access_token, "t.export.here")


@unittest.skipUnless(wb_crypto.enabled(), "cryptography not installed")
class SecondBootTests(CryptoBase):
    """回歸釘子：bootstrap 必須在 POOL.load() 之前 configure()，否則重啟後
    密鑰會回退到 CWD 解析、生成一把錯鑰，整池賬號解密失敗（真實事故）。"""

    def test_server_boots_with_preencrypted_store(self):
        import socket
        import subprocess
        import time as _time
        import urllib.request

        account = accounts.Account({"uid": "boot-me", "realm": "cn",
                                    "accessToken": "t.boot.because", "refreshToken": "r"})
        account.save(_startup_dir.name)

        def free_port():
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            s.close()
            return port

        port = free_port()
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        proc = subprocess.Popen(
            [sys.executable, os.path.join(root, "wb_proxy.py"), "--port", str(port),
             "--host", "127.0.0.1", "--accounts-dir", _startup_dir.name],
            cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        try:
            ready = None
            for _ in range(40):
                _time.sleep(0.5)
                try:
                    with urllib.request.urlopen(
                            "http://127.0.0.1:%d/health" % port, timeout=2) as resp:
                        ready = json.loads(resp.read().decode("utf-8"))
                        break
                except Exception:
                    if proc.poll() is not None:
                        break
            self.assertIsNotNone(ready, "server did not start")
            self.assertGreaterEqual(ready.get("accounts_ready", 0), 1,
                                    "重启后加密账号必须能解密加载: %s" % ready)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()


if __name__ == "__main__":
    unittest.main(verbosity=2)
