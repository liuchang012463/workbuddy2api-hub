"""P4：reasoning 透傳開關與默認推理力度覆蓋。

Run with: python _test_reasoning_forward.py
No upstream credentials or outbound network are used.
"""
import atexit
import os
import sys
import tempfile
import unittest
from unittest import mock

_startup_dir = tempfile.TemporaryDirectory(prefix="reasoning-forward-")
atexit.register(_startup_dir.cleanup)
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
os.environ.setdefault("WB_PROXY_USAGE_DIR", os.path.join(_startup_dir.name, "usage"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy as proxy
import wb_settings as settings


class HeadersStub(object):
    def __init__(self, mapping=None):
        self._m = mapping or {}

    def get(self, key, default=None):
        return self._m.get(key, default)


def chunk(**delta):
    return '{"choices":[{"delta":%s}]}' % (
        __import__("json").dumps(delta, ensure_ascii=False))


class ForwardReasoningTests(unittest.TestCase):
    def test_default_forwards_nonempty_reasoning(self):
        raw = chunk(reasoning_content="thinking...", content="")
        kept = proxy.clean_chunk(raw)
        self.assertIn("thinking...", kept)

    def test_empty_reasoning_is_noise_in_both_modes(self):
        raw = chunk(reasoning_content="")
        self.assertEqual(proxy.clean_chunk(raw), "")
        self.assertEqual(proxy.clean_chunk(raw, strip_reasoning=True), "")

    def test_strip_reasoning_removes_even_nonempty(self):
        raw = chunk(reasoning_content="thinking...", content="")
        stripped = proxy.clean_chunk(raw, strip_reasoning=True)
        self.assertNotIn("reasoning_content", stripped)
        # 其他噪音鍵照舊剔除
        raw2 = chunk(reasoning_content="x", refusal="", content="hi")
        kept = proxy.clean_chunk(raw2, strip_reasoning=True)
        self.assertNotIn("refusal", kept)
        self.assertIn("hi", kept)

    def test_header_overrides_setting(self):
        with mock.patch.object(settings, "forward_reasoning", return_value=True):
            self.assertTrue(proxy.forward_reasoning_for(HeadersStub()))
            self.assertFalse(proxy.forward_reasoning_for(
                HeadersStub({"X-WB-Forward-Reasoning": "false"})))
            self.assertTrue(proxy.forward_reasoning_for(
                HeadersStub({"X-WB-Forward-Reasoning": "1"})))
        with mock.patch.object(settings, "forward_reasoning", return_value=False):
            self.assertTrue(proxy.forward_reasoning_for(
                HeadersStub({"X-WB-Forward-Reasoning": "true"})))
            self.assertFalse(proxy.forward_reasoning_for(HeadersStub()))

    def test_settings_roundtrip(self):
        self.assertTrue(settings.forward_reasoning(_startup_dir.name),
                        "缺省必須等於歷史行為（透傳）")
        settings.set_forward_reasoning(_startup_dir.name, False)
        self.assertFalse(settings.forward_reasoning(_startup_dir.name))
        settings.set_forward_reasoning(_startup_dir.name, True)


class EffortOverrideTests(unittest.TestCase):
    def setUp(self):
        # resolve_default_effort / build_upstream_body 讀的是模組常量
        # proxy.ACCOUNTS_DIR，測試指向臨時目錄避免污染真實設置。
        patcher = mock.patch.object(proxy, "ACCOUNTS_DIR", _startup_dir.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        settings.set_reasoning_effort_overrides(_startup_dir.name, {})
        self.addCleanup(settings.set_reasoning_effort_overrides,
                        _startup_dir.name, {})

    def test_overrides_shape_and_validation(self):
        self.assertEqual(settings.reasoning_effort_overrides(_startup_dir.name), {})
        cleaned = settings.set_reasoning_effort_overrides(_startup_dir.name,
                                                          {"deepseek-v4.1-flash": "Medium"})
        self.assertEqual(cleaned, {"deepseek-v4.1-flash": "medium"})
        self.assertEqual(settings.reasoning_effort_overrides(_startup_dir.name),
                         {"deepseek-v4.1-flash": "medium"})
        with self.assertRaises(ValueError):
            settings.set_reasoning_effort_overrides(_startup_dir.name,
                                                    {"m": "ultra"})
        with self.assertRaises(ValueError):
            settings.set_reasoning_effort_overrides(_startup_dir.name, ["x"])
        # 非法輸入不落盤
        self.assertEqual(settings.reasoning_effort_overrides(_startup_dir.name),
                         {"deepseek-v4.1-flash": "medium"})

    def test_resolve_order_override_catalog_fallback(self):
        settings.set_reasoning_effort_overrides(_startup_dir.name,
                                                {"deepseek-v4.1-flash": "low"})
        with mock.patch.object(proxy, "model_default_effort", return_value="high") as cat:
            self.assertEqual(proxy.resolve_default_effort("deepseek-v4.1-flash"), "low")
            cat.assert_not_called()
        # 無覆蓋的模型回落目錄 / high
        self.assertEqual(proxy.resolve_default_effort("deepseek-other"),
                         proxy.model_default_effort("deepseek-other") or "high")

    def test_build_upstream_body_uses_override(self):
        settings.set_reasoning_effort_overrides(_startup_dir.name,
                                                {"deepseek-v4.1-flash": "low"})
        body = proxy.build_upstream_body(
            {"model": "deepseek-v4.1-flash",
             "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(body.get("reasoning_effort"), "low")
        self.assertEqual((body.get("thinking") or {}).get("type"), "enabled")
        settings.set_reasoning_effort_overrides(_startup_dir.name, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
