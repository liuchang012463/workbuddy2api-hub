"""P5 端到端：槽位健康報告端點（登入 → POST 回灌 → GET 合併）。

Starts a real server on a spare port with a throwaway store and the default
panel password, so it needs no network access and does not touch the real
accounts. Mirrors the bootstrap of _test_connection_reuse.py.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

PASS = 0
FAIL = 0


def check(label, ok, extra=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (label, extra))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def http(method, path, body=None, token=None):
    # 面板鉴权只认 X-Panel-Token 头（_panel_token 刻意 header-only）。
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (port, path),
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Content-Type": "application/json",
                 **({"X-Panel-Token": token} if token else {})})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


port = free_port()
work = tempfile.mkdtemp(prefix="slothealth_e2e_")
store = os.path.join(work, "accounts")
os.makedirs(store)
with open(os.path.join(store, "settings.json"), "w", encoding="utf-8") as fh:
    # 面板保持默認密碼（admin），測試才能登入；api_keys 覆蓋 /v1 鑑權。
    json.dump({"api_keys": [{"id": "k1", "name": "t", "key": "GOODKEY",
                             "enabled": True}]}, fh)

proc = subprocess.Popen(
    [sys.executable, os.path.join(ROOT, "wb_proxy.py"), "--port", str(port),
     "--host", "127.0.0.1", "--accounts-dir", store],
    cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    env=dict(os.environ, WB_PROXY_USAGE_DIR=os.path.join(work, "usage")),
)

try:
    ready = False
    for _ in range(40):
        time.sleep(0.5)
        try:
            c = socket.create_connection(("127.0.0.1", port), timeout=2)
            c.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            ok = b"200" in c.recv(200)
            c.close()
            if ok:
                ready = True
                break
        except Exception:
            if proc.poll() is not None:
                break
    if not ready:
        print("  [FAIL] server did not start")
        sys.exit(1)

    print("[1] panel login (default password)")
    token = http("POST", "/panel/login",
                 {"password": "admin"}).get("token", "")
    check("token acquired", bool(token))

    print("[2] POST /proxy/slots/health accepts the script report")
    reply = http("POST", "/proxy/slots/health",
                 {"slots": [{"slot": 1, "ok": True, "latency_ms": 245, "upload_ms": 880},
                            {"port": 17907, "ok": False}],
                  "reported_at": time.time()}, token=token)
    check("2 slots accepted", reply.get("accepted") == 2, reply)

    print("[3] GET /proxy/slots merges the health snapshot")
    view = http("GET", "/proxy/slots", token=token)
    health = view.get("health") or {}
    check("slot-1 present", "slot-1" in health, sorted(health))
    check("port 17907 mapped to slot-7", "slot-7" in health, sorted(health))
    check("latency carried", health.get("slot-1", {}).get("reported", {}).get("latency_ms") == 245)
    check("failure flagged", health.get("slot-7", {}).get("reported", {}).get("ok") is False)

    print("[4] unauthenticated report is rejected")
    try:
        http("POST", "/proxy/slots/health", {"slots": []})
        check("401 without token", False)
    except urllib.error.HTTPError as exc:
        check("401 without token", exc.code == 401, exc.code)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
    shutil.rmtree(work, ignore_errors=True)

print()
print("SUMMARY: PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
