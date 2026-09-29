"""wb_http.py —— 上游熱路徑的 httpx 連接池。

wb-proxy 之前用 urllib 打上游：每個請求都完整新建一條 TCP+TLS（urllib 的
opener 只緩存代理配置，不池化連接），TLS 握手的開銷直接計進首字延遲。
這裡按出站路徑（每個 proxy URL 一個 client）緩存 httpx.Client，SSE 流式
讀取、CONNECT 隧道（mihomo mixed 端口）與 keep-alive 複用都交給 httpx。

設計約束：
- httpx 是可選依賴：容器鏡像裡 pip 裝好；本機/舊鏡像沒有時 available()
  返回 False，調用方回落 urllib 路徑（wb_accounts.urlopen / http_json），
  測試與回滾不依賴 httpx。
- UpstreamStream 模仿 urllib 響應面（__enter__/__iter__/close/status），
  wb_proxy 的調用點形狀不變；__iter__ 按行產出 bytes，與 urllib 的文件
  迭代一致，解析代碼（strip_data_prefix / clean_chunk）兩條路徑共用。
- httpx.Client 線程安全，與 ThreadingHTTPServer 的每連接一線程模型兼容。
"""
import io
import json
import email.message
import http.client
import threading
import time
import urllib.error

try:
    import httpx
except ImportError:  # 舊鏡像 / 本機測試環境：回落 urllib。
    httpx = None

# 與原 urlopen(timeout=600) 的 socket 語義對齊：read 600s 覆蓋推理長停頓。
CONNECT_TIMEOUT = 12.0
READ_TIMEOUT = 600.0
WRITE_TIMEOUT = 120.0
POOL_TIMEOUT = 30.0
MAX_CONNECTIONS = 64
MAX_KEEPALIVE_CONNECTIONS = 8
KEEPALIVE_EXPIRY = 30.0

_clients = {}
_clients_lock = threading.Lock()


def available():
    """httpx 是否可用；False 時調用方走 urllib 路徑。"""
    return httpx is not None


def client_for(proxy):
    """一條出站路徑一個 Client（"" = 直連）。"""
    if httpx is None:
        raise RuntimeError("httpx is not installed")
    proxy = str(proxy or "").strip()
    with _clients_lock:
        client = _clients.get(proxy)
        if client is None:
            limits = httpx.Limits(
                max_connections=MAX_CONNECTIONS,
                max_keepalive_connections=MAX_KEEPALIVE_CONNECTIONS,
                keepalive_expiry=KEEPALIVE_EXPIRY,
            )
            timeout = httpx.Timeout(connect=CONNECT_TIMEOUT, read=READ_TIMEOUT,
                                    write=WRITE_TIMEOUT, pool=POOL_TIMEOUT)
            if proxy:
                # mihomo mixed 端口：http 前綴即 HTTP 代理，https 目標走 CONNECT。
                transport = httpx.HTTPTransport(proxy=proxy)
                client = httpx.Client(transport=transport, timeout=timeout,
                                      limits=limits, follow_redirects=True,
                                      trust_env=False)
            else:
                client = httpx.Client(timeout=timeout, limits=limits,
                                      follow_redirects=True, trust_env=False)
            _clients[proxy] = client
        return client


def pools_snapshot():
    """面板可見的連接池狀態（每條出站路徑的 client 是否已建）。"""
    with _clients_lock:
        return {proxy or "direct": available() for proxy in _clients}


class UpstreamStream(object):
    """httpx stream 的「進入一次」包裝，對調用方呈現 urllib 響應面。

    open() 之後 status_code 可用（urlopen 返回時響應頭已收齊，語義對齊）；
    4xx/5xx 由調用方 read_body() 取錯誤體後 close()，再交給原有的
    HTTPError 分支處理。
    """

    def __init__(self, proxy, method, url, content, headers):
        self._client = client_for(proxy)
        self._cm = self._client.stream(method, url, content=content,
                                       headers=headers or {})
        self._resp = None
        self.status_code = None
        self._closed = False

    def open(self):
        if self._closed:
            raise RuntimeError("upstream stream already closed")
        if self._resp is None:
            self._resp = self._cm.__enter__()
            self.status_code = self._resp.status_code
        return self

    def read_body(self, limit=None):
        """在 stream 上下文內讀完整錯誤體（限制字節數防止 429 響應過大）。"""
        self.open()
        body = self._resp.read()
        return body[:limit] if limit else body

    def close(self):
        if self._resp is not None:
            try:
                self._cm.__exit__(None, None, None)
            finally:
                self._resp = None
        self._closed = True

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc_info):
        self.close()
        return False

    def __iter__(self):
        # httpx 的 iter_lines 產出 str；urllib 的行迭代產出 bytes（帶換行符）。
        # 這裡編回 bytes：wb_proxy 的 strip_data_prefix 先 strip()，兩種行
        # 語義都兼容，解析代碼保持一套。
        self.open()
        for line in self._resp.iter_lines():
            yield line.encode("utf-8")


def upstream_post(proxy, url, content, headers):
    """打開一條 POST 流（尚未進入；調用方先 open() 看 status_code）。"""
    return UpstreamStream(proxy, "POST", url, content, headers)


def http_error(url, code, body, msg=None):
    """構造 urllib.error.HTTPError，讓 open_upstream 原有的 429/403/401/5xx
    分支（exc.code / exc.read(600)）零改動地處理 httpx 路徑的錯誤。"""
    hdrs = email.message.Message()
    if msg is None:
        msg = http.client.responses.get(code, "Error")
    return urllib.error.HTTPError(url, code, msg, hdrs, io.BytesIO(body or b""))


def is_transport_error(exc):
    """httpx 的網路層異常族（連接/讀寫/超時/協議），is_transient 據此重試。"""
    if httpx is None:
        return False
    return isinstance(exc, httpx.TransportError)


def post_json(url, data=None, headers=None, timeout=30, retries=3,
              backoff=1.0, log=None, proxy="", retryable=None):
    """httpx 版的 http_json：線性退避重試，返回解析後的 JSON。

    data 為 bytes（POST）或 None（GET）。retryable 由調用方傳入（wb_accounts
    的 _retryable），保持與 urllib 路徑相同的重試判定。
    """
    attempts = max(1, int(retries or 1))
    client = client_for(proxy)
    method = "POST" if data is not None else "GET"
    last = None
    for attempt in range(1, attempts + 1):
        try:
            resp = client.request(method, url, content=data, headers=headers or {},
                                  timeout=timeout)
            return json.loads(resp.content.decode("utf-8"))
        except Exception as exc:
            last = exc
            if attempt >= attempts or (retryable and not retryable(exc)):
                break
            if log:
                log("network retry %d/%d after %s" % (attempt, attempts, exc))
            time.sleep(backoff * attempt)
    raise last
