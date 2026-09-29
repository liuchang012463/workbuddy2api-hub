"""wb_slothealth.py —— 槽位健康的運行時追蹤、隔離與外部報告回灌。

兩個信息來源：
1. 真實流量：open_upstream 每次嘗試結束時 record() 一次。成功更新 resp_ms
   的 EWMA 並清零連敗；瞬態失敗（網路抖動 / 5xx）累計連敗。
2. slot-cleanup.sh 每 30 分鐘的巡檢報告：POST /proxy/slots/health 回灌
   latency / 500KB 上傳耗時 / 存活狀態，面板直接展示。

隔離（quarantine）：一個出口連續 QUARANTINE_THRESHOLD 次瞬態失敗後，本進程
內把它標記為隔離——open_upstream 之後的 pick 會跳過綁在該出口上的帳號
（池子打空時 pick 會自動放開，不會把請求打死）。隔離帶 TTL：到期後下一個
請求自然成為半開探測，成功即解除，再失敗就重新隔離。刻意不寫
settings.json——持久禁用仍是外部腳本的職責，這裡只做自愈。
"""
import threading
import time

# 連續多少次瞬態失敗觸發隔離。5 次大約等於 1-2 個被放大重試的請求，
# 偶發抖動（1-2 次）永遠夠不著。
QUARANTINE_THRESHOLD = 5
# 隔離時長（秒）：到期即半開，成功解除，失敗重新隔離。
QUARANTINE_TTL = 600.0
# 成功嘗試 resp_ms 的 EWMA 平滑係數。
EWMA_ALPHA = 0.3
# 面板快照裡保留的最近錯誤描述長度。
_MAX_ERR = 120

_lock = threading.Lock()
_slots = {}


def reset():
    """清空全部狀態（測試用）。"""
    with _lock:
        _slots.clear()


def _entry(slot):
    return _slots.setdefault(str(slot or "direct"), {
        "ewma_ms": None, "samples": 0, "ok_count": 0, "error_count": 0,
        "consecutive_errors": 0, "quarantined_until": 0.0,
        "last_error": "", "last_error_at": None,
        "reported": None,
    })


def record(slot, ok, resp_ms=None, outcome=None):
    """記錄一次嘗試結果。outcome 僅用於錯誤描述，429/403 等配額類不應傳入。"""
    now = time.time()
    with _lock:
        e = _entry(slot)
        if ok:
            e["ok_count"] += 1
            e["consecutive_errors"] = 0
            e["quarantined_until"] = 0.0
            if isinstance(resp_ms, (int, float)) and resp_ms >= 0:
                e["ewma_ms"] = round(resp_ms if e["ewma_ms"] is None
                                     else e["ewma_ms"] * (1 - EWMA_ALPHA) + resp_ms * EWMA_ALPHA, 1)
                e["samples"] += 1
        else:
            e["error_count"] += 1
            e["consecutive_errors"] += 1
            if outcome:
                e["last_error"] = str(outcome)[:_MAX_ERR]
                e["last_error_at"] = now
            if e["consecutive_errors"] >= QUARANTINE_THRESHOLD:
                e["quarantined_until"] = now + QUARANTINE_TTL


def quarantined():
    """當前處於隔離中的出口 key 集合（供 pick 的 exclude_slots 併入）。"""
    now = time.time()
    with _lock:
        return {slot for slot, e in _slots.items()
                if e["quarantined_until"] > now}


def is_quarantined(slot):
    now = time.time()
    with _lock:
        e = _slots.get(str(slot or "direct"))
        return bool(e and e["quarantined_until"] > now)


def apply_report(slots, reported_at=None):
    """回灌 slot-cleanup.sh 的巡檢結果（不觸發隔離，禁用仍是腳本的職責）。

    slots: [{slot|port, ok, latency_ms, upload_ms}]。slot 支持數字（1-12，
    腳本裡的寫法）或 "slot-N" 字符串；port 為 179NN 時按 17901→slot-1 …
    17912→slot-12 統一反推。
    """
    reported_at = reported_at or time.time()
    accepted = 0
    with _lock:
        for row in slots or []:
            if not isinstance(row, dict):
                continue
            slot = _normalize_slot(row.get("slot"))
            if not slot:
                slot = _slot_from_port(row.get("port"))
            if not slot:
                continue
            e = _entry(slot)
            e["reported"] = {
                "ok": bool(row.get("ok")),
                "latency_ms": row.get("latency_ms"),
                "upload_ms": row.get("upload_ms"),
                "reported_at": reported_at,
            }
            accepted += 1
    return accepted


def _normalize_slot(raw):
    if isinstance(raw, int) and 1 <= raw <= 99:
        return "slot-%d" % raw
    text = str(raw or "").strip()
    if text.isdigit() and 1 <= int(text) <= 99:
        return "slot-%d" % int(text)
    return text


def _slot_from_port(port):
    try:
        n = int(str(port))
    except (TypeError, ValueError):
        return None
    if 17901 <= n <= 17912:
        return "slot-%d" % (n - 17900)
    return None


def snapshot():
    """面板可見的完整快照。"""
    now = time.time()
    with _lock:
        out = {}
        for slot, e in _slots.items():
            row = dict(e)
            row["quarantined"] = e["quarantined_until"] > now
            row["quarantined_for"] = (round(e["quarantined_until"] - now)
                                      if row["quarantined"] else None)
            out[slot] = row
        return out
