"""wb_crypto.py —— 賬號憑證的靜態加密（at-rest）。

accounts/<uid>.json 裡的 accessToken / refreshToken 一直是明文 JWT（README
裡自己也這麼承認）。這裡借用 gpt-load 的 encryption.key 思路：一個 32 字節
主密鑰 + AES-GCM，加密只發生在文件讀寫邊界（Account.save() 落盤前加密，
Account.__init__ 讀盤後解密），內存中的運行時值和 to_dict()/導出文檔保持
明文，導入導出鏈路不需要任何改動。

密鑰解析順序（第一次用到時解析並緩存）：
1. WB_ENCRYPTION_KEY 環境變量（base64 的 32 字節）
2. WB_ENCRYPTION_KEY_FILE 環境變量指向的文件
3. <accounts_dir>/encryption.key（默認；缺失時自動生成，0600）
cryptography 庫不可用時整體降級為明文模式（enabled() = False），行為與
歷史版本一致，啟動日誌會提示。

密文信封沿用倉庫裡已有的 $wbEncrypted 約定（CN 暱稱加密就是這個殼）：
    {"$wbEncrypted": "v1", "alg": "AESGCM", "ct": "<base64(nonce||ciphertext)>"}
版本號 v1 給將來的密鑰輪換留口子。解密失敗（換了機器/丟了密鑰）會拋出
帶行動建議的錯誤，讓啟動直接失敗而不是拿著垃圾 token 去打上游。
"""
import base64
import os
import stat

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:  # 明文降級：舊鏡像 / 本機測試環境。
    AESGCM = None

ENVELOPE_MARKER = "$wbEncrypted"
ALGORITHM = "AESGCM"
_NONCE_BYTES = 12
_KEY_BYTES = 32

# 由 bootstrap（wb_proxy._bootstrap_runtime）或第一次 save() 注入的賬號目錄，
# 作為默認密鑰文件 <accounts_dir>/encryption.key 的解析基準。
_configured_dir = None
_key = None
_key_resolved = False
_key_lock = __import__("threading").Lock()


class CryptoError(Exception):
    """密鑰缺失或解密失敗（通常是換了機器或密鑰文件不匹配）。"""


def configure(accounts_dir):
    """設置默認密鑰文件的解析目錄（冪等，bootstrap 調用）。"""
    global _configured_dir
    _configured_dir = accounts_dir


def reset():
    """清空緩存的密鑰（測試用）。"""
    global _key, _key_resolved
    with _key_lock:
        _key = None
        _key_resolved = False


def enabled():
    """cryptography 可用即啟用加密（密鑰會在首次加解密時解析/生成）。"""
    return AESGCM is not None


def _key_file_path():
    env_file = os.environ.get("WB_ENCRYPTION_KEY_FILE")
    if env_file:
        return env_file
    base = _configured_dir or os.environ.get("ACCOUNTS_DIR") or "."
    return os.path.join(base, "encryption.key")


def _resolve_key():
    """按優先級解析主密鑰；默認文件缺失時自動生成。"""
    global _key, _key_resolved
    with _key_lock:
        if _key_resolved:
            return _key
        _key_resolved = True
        env_key = os.environ.get("WB_ENCRYPTION_KEY")
        if env_key:
            _key = _decode_key(env_key, "WB_ENCRYPTION_KEY")
            return _key
        path = _key_file_path()
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                _key = _decode_key(fh.read().strip(), path)
            return _key
        # 自動生成：讓首次部署零配置啟用加密。丟失該文件 = 賬號 token
        # 全部無法解密，日誌裡要反覆強調備份。
        raw = os.urandom(_KEY_BYTES)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(base64.b64encode(raw).decode("ascii") + "\n")
        except Exception:
            os.unlink(path)
            raise
        _key = raw
        return _key


def _decode_key(raw, source):
    try:
        key = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise CryptoError("encryption key in %s is not valid base64: %s" % (source, exc))
    if len(key) != _KEY_BYTES:
        raise CryptoError("encryption key in %s must decode to %d bytes, got %d"
                          % (source, _KEY_BYTES, len(key)))
    return key


def encrypt_field(value):
    """明文字符串 → $wbEncrypted 信封；空值/未啟用時原樣返回。"""
    if not enabled() or not value:
        return value
    key = _resolve_key()
    nonce = os.urandom(_NONCE_BYTES)
    ct = AESGCM(key).encrypt(nonce, str(value).encode("utf-8"), None)
    return {
        ENVELOPE_MARKER: "v1",
        "alg": ALGORITHM,
        "ct": base64.b64encode(nonce + ct).decode("ascii"),
    }


def decrypt_field(value):
    """$wbEncrypted 信封 → 明文；明文輸入原樣返回（兼容歷史文件/導入文檔）。"""
    if not isinstance(value, dict) or ENVELOPE_MARKER not in value:
        return value if value is not None else ""
    if not enabled():
        raise CryptoError(
            "account tokens are encrypted but 'cryptography' is not installed - "
            "install it or restore the plaintext file")
    raw = value.get("ct")
    if not isinstance(raw, str) or not raw:
        raise CryptoError("encrypted envelope has no 'ct' payload")
    try:
        blob = base64.b64decode(raw)
        nonce, ct = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
        key = _resolve_key()
        return AESGCM(key).decrypt(nonce, ct, None).decode("utf-8")
    except CryptoError:
        raise
    except Exception as exc:
        raise CryptoError(
            "token decryption failed (%s) - the encryption.key does not match "
            "the one that encrypted this account; restore the original key "
            "file or re-add the account" % type(exc).__name__)


def is_encrypted(value):
    return isinstance(value, dict) and ENVELOPE_MARKER in value
