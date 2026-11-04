#!/usr/bin/env python3
"""钉钉 Stream 模式长连接网关（免公网 IP、免内网穿透接入工具）。

对标参考项目：yanfeng17/dingtalk-ha-gateway 与 《独居老人的 AI 守护管家》。
背景与解决痛点：
- 普通家庭宽带无公网 IPv4，常规 Webhook 需要公网反向代理（如 cpolar/ngrok）或域名与 SSL 证书；
- 钉钉 Stream 模式通过客户端向钉钉开放平台发起出站 WebSocket 长连接，NAT 网络内部直接打通双向通信；
- 收到家属在钉钉群或私聊中给管家机器人发的文字/语音消息后，网关解析并转发至本地 VMAPBL Webhook：
  POST http://127.0.0.1:8321/api/webhook/family_reply
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import fcntl
import json
import logging
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from server.webhook_auth import stream_signature

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("dingtalk_stream_gateway")


class GatewayAlreadyRunning(RuntimeError):
    pass


@contextmanager
def gateway_instance_lock(path: Path):
    """Hold an OS-released lock for the gateway lifetime; never unlink lock files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise GatewayAlreadyRunning("钉钉网关已在运行，请勿重复启动") from exc
        yield
    finally:
        os.close(fd)


def compute_dingtalk_headers(secret: str | None, raw_body: bytes) -> dict[str, str]:
    """生成带防重放与 HMAC-SHA256 签名的 HTTP 头部。"""
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if not secret:
        return headers

    ts = str(int(time.time() * 1000))
    nonce = secrets.token_hex(16)
    headers["X-VMAPBL-Stream-Timestamp"] = ts
    headers["X-VMAPBL-Stream-Nonce"] = nonce
    headers["X-VMAPBL-Stream-Signature"] = stream_signature(secret, ts, nonce, raw_body)
    return headers


def forward_to_vmapbl(
    target_url: str,
    text: str,
    sender: str = "家人",
    secret: str | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """将消息 POST 转发给本地 VMAPBL Webhook。"""
    payload = {
        "text": text,
        "sender": sender,
        "senderNick": sender,
        "timestamp": int(time.time() * 1000),
    }
    raw_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = compute_dingtalk_headers(secret, raw_body)

    req = urllib.request.Request(
        url=target_url,
        data=raw_body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
            return {"ok": isinstance(data, dict) and data.get("ok") is True,
                    "status": resp.status}
    except urllib.error.HTTPError as e:
        log.error("VMAPBL Webhook HTTP %d", e.code)
        return {"ok": False, "status": e.code}
    except Exception as e:
        log.error("转发至 VMAPBL 失败 (%s)", type(e).__name__)
        return {"ok": False}


class DingTalkStreamForwarder:
    """钉钉 Stream 消息处理器。"""

    def __init__(self, target_url: str, secret: str | None = None):
        self.target_url = target_url
        self.secret = secret

    def handle_bot_message(self, message_data: dict[str, Any]) -> dict[str, Any]:
        """解析钉钉 Stream 回调报文并转发。"""
        # 钉钉机器人消息体规范：
        # message_data 通常包含 text.content, senderNick, sessionWebhook 等
        text_obj = message_data.get("text")
        if isinstance(text_obj, dict):
            text_obj = text_obj.get("content")
        text = text_obj.strip() if isinstance(text_obj, str) else ""

        sender = (
            message_data.get("senderNick")
            or message_data.get("sender_nick")
            or message_data.get("sender")
            or "家人"
        )
        if not text:
            log.warning("收到空消息，略过转发")
            return {"status": "ignored_empty"}

        res = forward_to_vmapbl(
            target_url=self.target_url,
            text=text,
            sender=sender,
            secret=self.secret,
        )
        return res


def run_stream_client(
    client_id: str,
    client_secret: str,
    target_url: str,
    sign_secret: str | None = None,
) -> None:
    """启动钉钉官方 dingtalk-stream SDK 长连接服务。"""
    try:
        from dingtalk_stream import AckMessage, ChatbotHandler, Credential, DingTalkStreamClient
        from dingtalk_stream.chatbot import ChatbotMessage
    except ImportError:
        log.error(
            "未安装 dingtalk-stream 依赖库。在实际生产部署前，请先安装：\n"
            "    pip install dingtalk-stream\n"
            "或使用 --simulate 参数在本地验证转发链路。"
        )
        sys.exit(1)

    forwarder = DingTalkStreamForwarder(target_url=target_url, secret=sign_secret)

    class GatewayBotHandler(ChatbotHandler):
        async def process(self, callback):
            data = callback.data or {}
            res = await asyncio.to_thread(forwarder.handle_bot_message, data)
            if res.get("ok") or res.get("status") == "ignored_empty":
                return AckMessage.STATUS_OK, "OK"
            return AckMessage.STATUS_SYSTEM_EXCEPTION, "VMAPBL did not accept the message"

    log.info("正在建立钉钉 Stream WebSocket 长连接 (ClientID: %s)...", client_id[:4] + "****")
    credential = Credential(client_id, client_secret)
    # The SDK's default INFO logger prints short-lived connection tickets.
    sdk_logger = logging.getLogger("vmapbl.dingtalk_sdk")
    sdk_logger.setLevel(logging.WARNING)
    client = DingTalkStreamClient(credential, logger=sdk_logger)
    client.register_callback_handler(ChatbotMessage.TOPIC, GatewayBotHandler())
    client.start_forever()


def main():
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    parser = argparse.ArgumentParser(description="钉钉 Stream 模式免公网 IP 消息网关")
    parser.add_argument("--client-id", default=os.getenv("DINGTALK_CLIENT_ID", ""), help="钉钉开放平台 AppKey / ClientID")
    parser.add_argument("--client-secret", default=os.getenv("DINGTALK_CLIENT_SECRET", ""), help="钉钉开放平台 AppSecret")
    parser.add_argument("--target-url", default=os.getenv("VMAPBL_WEBHOOK_URL", "http://127.0.0.1:8321/api/webhook/family_reply"), help="VMAPBL Webhook URL")
    parser.add_argument("--sign-secret", default=os.getenv("DINGTALK_SIGN_SECRET", ""), help="钉钉 Webhook 验签密钥")
    parser.add_argument("--simulate", default="", help="本地模拟模式：直接发送测试文本至 VMAPBL")
    parser.add_argument("--sender", default="女儿", help="模拟测试时的发件人称谓")
    args = parser.parse_args()

    if args.simulate:
        res = forward_to_vmapbl(
            target_url=args.target_url,
            text=args.simulate,
            sender=args.sender,
            secret=args.sign_secret or None,
        )
        log.info("模拟转发响应: %s", res)
        if res.get("ok"):
            print("OK: 模拟转发成功")
            sys.exit(0)
        else:
            print(f"FAILED: 转发失败: {res}")
            sys.exit(1)

    if not args.client_id or not args.client_secret or not args.sign_secret:
        parser.print_help()
        print("\n[错误] 请配置 DINGTALK_CLIENT_ID、DINGTALK_CLIENT_SECRET 和 DINGTALK_SIGN_SECRET。")
        sys.exit(1)

    try:
        with gateway_instance_lock(Path(__file__).resolve().parent.parent / "data" / "dingtalk-stream.lock"):
            run_stream_client(
                client_id=args.client_id,
                client_secret=args.client_secret,
                target_url=args.target_url,
                sign_secret=args.sign_secret or None,
            )
    except GatewayAlreadyRunning as exc:
        log.error("%s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
