"""钉钉 / 飞书回调签名校验。

- 钉钉：sign = base64(HMAC-SHA256(secret, f"{timestamp}\n{secret}"))
- 飞书：X-Lark-Signature = sha256(timestamp + nonce + encrypt_key + 原始 body)

时间戳窗口 5 分钟，防重放。签名用 hmac.compare_digest 恒定时间比较。
未配置 secret 时保持向后兼容（报告 no_secret_configured，文档要求生产必须配置）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time

MAX_SKEW_S = 300
_seen_signatures: dict[str, float] = {}


def clear_nonce_cache() -> None:
    """清空重放签名缓存（测试辅助）。"""
    _seen_signatures.clear()


def _check_and_record_nonce(key: str, now: float) -> bool:
    """检查是否为重放请求；返回 True 表示首次处理（通过），False 表示重放攻击（拦截）。"""
    expired = [k for k, exp in _seen_signatures.items() if exp <= now]
    for k in expired:
        _seen_signatures.pop(k, None)

    if key in _seen_signatures:
        return False
    # A timestamp ahead of the server clock can remain valid for two skew windows.
    _seen_signatures[key] = now + 2 * MAX_SKEW_S
    return True



def dingtalk_signature(secret: str, timestamp: str) -> str:
    digest = hmac.new(secret.encode("utf-8"),
                      f"{timestamp}\n{secret}".encode("utf-8"),
                      hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def stream_signature(secret: str, timestamp: str, nonce: str, body: bytes) -> str:
    message = f"{timestamp}\n{nonce}\n".encode("utf-8") + body
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def feishu_signature(secret: str, timestamp: str, nonce: str,
                     body: bytes) -> str:
    text = f"{timestamp}{nonce}{secret}{body.decode('utf-8')}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fresh(raw: str, now: float) -> bool:
    try:
        ts = float(raw)
    except (TypeError, ValueError):
        return False
    if ts > 10_000_000_000:  # 钉钉用毫秒，飞书用秒
        ts /= 1000.0
    return abs(now - ts) <= MAX_SKEW_S


def verify_webhook(headers, body: bytes, *,
                   dingtalk_secret: str = "",
                   feishu_secret: str = "",
                   now: float | None = None) -> tuple[bool, str]:
    """返回 (是否通过, 原因)。headers 可为 Starlette Headers 或 dict。"""
    h = {k.lower(): v for k, v in dict(headers).items()}
    now = time.time() if now is None else float(now)

    if any(key.startswith("x-vmapbl-stream-") for key in h):
        ts = h.get("x-vmapbl-stream-timestamp", "")
        nonce = h.get("x-vmapbl-stream-nonce", "")
        signature = h.get("x-vmapbl-stream-signature", "")
        if not dingtalk_secret:
            return False, "dingtalk_secret_not_configured"
        if not _fresh(ts, now) or len(nonce) != 32:
            return False, "stream_timestamp_or_nonce_invalid"
        expected = stream_signature(dingtalk_secret, ts, nonce, body)
        if not signature.isascii() or not hmac.compare_digest(expected, signature):
            return False, "stream_signature_mismatch"
        if not _check_and_record_nonce(f"stream:{nonce}", now):
            return False, "stream_request_replayed"
        return True, "ok"

    if "x-lark-signature" in h:
        if not feishu_secret:
            return False, "feishu_secret_not_configured"
        ts = h.get("x-lark-request-timestamp", "")
        nonce = h.get("x-lark-request-nonce", "")
        if not _fresh(ts, now):
            return False, "feishu_timestamp_expired"
        expected = feishu_signature(feishu_secret, ts, nonce, body)
        if not hmac.compare_digest(expected, h["x-lark-signature"]):
            return False, "feishu_signature_mismatch"
        if not _check_and_record_nonce(f"fs:{nonce}:{h['x-lark-signature']}", now):
            return False, "feishu_request_replayed"
        return True, "ok"

    if "sign" in h or "timestamp" in h:
        if not dingtalk_secret:
            return False, "dingtalk_secret_not_configured"
        ts = h.get("timestamp", "")
        if not _fresh(ts, now):
            return False, "dingtalk_timestamp_expired"
        expected = dingtalk_signature(dingtalk_secret, ts)
        if not hmac.compare_digest(expected, h.get("sign", "")):
            return False, "dingtalk_signature_mismatch"
        if not _check_and_record_nonce(f"dt:{ts}:{h.get('sign', '')}", now):
            return False, "dingtalk_request_replayed"
        return True, "ok"

    if dingtalk_secret or feishu_secret:
        return False, "signature_missing"

    from core import config
    if config.butler_token():
        return False, "webhook_secrets_unconfigured"
    return True, "no_secret_configured"
