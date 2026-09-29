"""wb_http 連接池：keep-alive 複用、SSE 行迭代、HTTPError 構造。

本地起一個帶連接計數的 HTTP/1.1 mock 服務器，不需要外網。
Run with: python _test_httpx_pool.py
httpx 未安裝時整個測試 skip（對應 urllib 回落路徑，行為由其他測試覆蓋）。
"""
import http.server
import json
import os
import socketserver
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_http

if not wb_http.available():
    print("  [SKIP] httpx not installed - urllib fallback path covers behaviour")
    sys.exit(0)

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (label, extra))


counts = {"conns": 0, "requests": 0}
_lock = threading.Lock()

SSE_BODY = 'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n'


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        with _lock:
            counts["requests"] += 1
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.path.endswith("/json"):
            body = b'{"ok": true}'
            ctype = "application/json"
        else:
            body = SSE_BODY.encode("utf-8")
            ctype = "text/event-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class CountingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

    def get_request(self):
        sock, addr = self.socket.accept()
        with _lock:
            counts["conns"] += 1
        return sock, addr


server = CountingServer(("127.0.0.1", 0), Handler)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()

try:
    print("[1] keep-alive: 第二個請求復用同一條 TCP 連接")
    url = "http://127.0.0.1:%d/v2/chat/completions" % port

    stream = wb_http.upstream_post("", url, b"{}", {"Content-Type": "application/json"})
    stream.open()
    check("status available after open()", stream.status_code == 200, stream.status_code)
    lines = list(stream)
    stream.close()
    check("SSE lines iterate as bytes", lines[0].startswith(b"data: "), lines[:1])
    check("[DONE] marker passes through", b"data: [DONE]" in lines, lines)

    stream2 = wb_http.upstream_post("", url, b"{}", {"Content-Type": "application/json"})
    stream2.open()
    list(stream2)
    stream2.close()

    check("two requests share one TCP connection", counts["conns"] == 1,
          "conns=%d requests=%d" % (counts["conns"], counts["requests"]))
    check("client cache returns the same client",
          wb_http.client_for("") is wb_http.client_for(""))

    print("[2] HTTPError 構造：exc.code / exc.read() 可用")
    err = wb_http.http_error(url, 429, b'{"msg":"rate"}')
    check("code carried", err.code == 429, err.code)
    check("body readable via exc.read(n)", err.read(600) == b'{"msg":"rate"}')

    print("[3] is_transient 認得 httpx 傳輸層異常")
    import wb_proxy as proxy
    check("ConnectError is transient",
          proxy.is_transient(wb_http.httpx.ConnectError("boom")))
    check("ReadTimeout is transient",
          proxy.is_transient(wb_http.httpx.ReadTimeout("slow")))
    check("plain ValueError is not transient",
          not proxy.is_transient(ValueError("nope")))

    print("[4] post_json：響應解析與重試參數")
    payload = wb_http.post_json(url + "/json", data=b"{}", headers={}, timeout=10,
                                proxy="", retryable=lambda exc: True)
    check("post_json decodes JSON", isinstance(payload, dict) and payload.get("ok"),
          payload)

    counts_before = counts["requests"]
    try:
        wb_http.post_json("http://127.0.0.1:1/nope", data=b"{}", timeout=1,
                          retries=2, backoff=0.05, proxy="",
                          retryable=lambda exc: True)
        check("unreachable endpoint raises", False)
    except Exception:
        check("unreachable endpoint raises", True)
finally:
    server.shutdown()
    server.server_close()

print()
print("SUMMARY: PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
