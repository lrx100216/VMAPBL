"""唯一对外通知出口（接口契约第四节）。

M3：消息落 SQLite messages 表 + 发 bus 事件 notify.family（M7 家人端控制台
订阅这个事件做 WebSocket 推送）。M3 阶段家人端还没建，同时打印到终端便于调试。
支持多渠道外部通知派发（钉钉、飞书机器人、通用 Webhook 如 HomeAssistant/ServerChan/PushPlus/Bark）。
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, replace
from typing import Any

import httpx

from core import bus
from core.log import default as default_log

_BACKGROUND_TASKS: set[asyncio.Task] = set()
_IN_FLIGHT_MSG_IDS: set[int] = set()


@dataclass(frozen=True)
class NotificationResult:
    """一次外部派发的可审计结果；msg_id 仅表示本地落库。"""

    msg_id: int | None
    attempted: bool
    delivered: bool
    channels: dict[str, str]
    pending: bool = False
    retry_after_s: float = 0.0
    permanent: bool = False

    @property
    def status(self) -> str:
        if self.delivered:
            return "delivered"
        return "pending" if self.pending else "undelivered"


def get_webhook_urls() -> dict[str, str]:
    """读取配置中启用的外部通知 Webhook 地址。"""
    try:
        from core import config
        cfg = config.settings().get("notify") or {}
    except Exception:
        cfg = {}

    dingtalk = os.getenv("DINGTALK_WEBHOOK", "").strip() or str(cfg.get("dingtalk_webhook") or "").strip()
    feishu = os.getenv("FEISHU_WEBHOOK", "").strip() or str(cfg.get("feishu_webhook") or "").strip()
    generic = os.getenv("NOTIFY_WEBHOOK_URL", "").strip() or str(cfg.get("webhook_url") or "").strip()
    return {
        "dingtalk": dingtalk,
        "feishu": feishu,
        "generic": generic,
    }


def configured_channel_names() -> list[str]:
    from skills.dingtalk_direct import configured
    names = [name for name, url in get_webhook_urls().items() if url]
    if configured():
        names.append("dingtalk_direct")
    return names


def _is_zero_code(value: Any) -> bool:
    """平台成功码必须是数值零；JSON 的 false 在 Python 里等于 0，必须排除。"""
    return (isinstance(value, (int, float))
            and not isinstance(value, bool) and value == 0)


def _platform_acknowledged(key: str, resp: httpx.Response) -> bool:
    """HTTP 2xx 之外再检查平台业务回执，避免签名错误被当成成功。"""
    if key == "dingtalk":
        return _is_zero_code(resp.json().get("errcode"))
    if key == "feishu":
        return _is_zero_code(resp.json().get("code"))
    return 200 <= resp.status_code < 300


def _format_external_message(msg: str, urgent: bool) -> str:
    """紧急消息统一前缀；send 与 send_with_receipt 共用，避免两条路径漂移。"""
    if not urgent:
        return msg
    return (msg if (msg.startswith("🚨") or "紧急预警" in msg)
            else f"🚨【紧急预警】{msg}")


async def _send_one_webhook(client: httpx.AsyncClient, key: str, name: str,
                            url: str, payload: dict[str, Any],
                            retry_hint: dict | None = None) -> tuple[str, int]:
    """发送单个渠道，返回（结果标签，尝试次数）；只重试临时故障。"""
    from core import config
    try:
        cfg = config.settings().get("notify") or {}
        max_attempts = max(1, int(cfg.get("max_attempts", 3)))
        backoff = max(0.0, float(cfg.get("retry_backoff_s", 0.5)))
        max_retry_after = max(0.0, float(cfg.get("max_retry_after_s", 10.0)))
    except (TypeError, ValueError):
        # 配置错误时一个请求都没发出，不能伪报“已尝试”。
        return "failed", 0

    for attempt in range(1, max_attempts + 1):
        delay = backoff * (2 ** (attempt - 1))
        try:
            resp = await client.post(url, json=payload)
            if resp.status_code == 429:
                try:
                    retry_after = float(resp.headers.get("Retry-After", "0"))
                except ValueError:
                    retry_after = 0.0
                delay = max(delay, min(max(0.0, retry_after), max_retry_after))
                if retry_hint is not None:
                    retry_hint["after_s"] = max(
                        float(retry_hint.get("after_s", 0.0)),
                        min(max(0.0, retry_after), max_retry_after))
            elif resp.status_code >= 500:
                pass  # 服务端临时故障可重试
            elif not 200 <= resp.status_code < 300:
                if retry_hint is not None:
                    retry_hint["permanent"] = True
                return "failed", attempt
            else:
                try:
                    acknowledged = _platform_acknowledged(key, resp)
                except (ValueError, TypeError, AttributeError):
                    return "failed", attempt
                return ("delivered" if acknowledged else "failed"), attempt
        except (httpx.UnsupportedProtocol, httpx.TooManyRedirects, httpx.InvalidURL) as e:
            print(f"[notify] {name} 确定性请求错误（{type(e).__name__}），不重试")
            if retry_hint is not None:
                retry_hint["permanent"] = True
            return "failed_permanent", attempt
        except httpx.TransportError as e:
            print(f"[notify] {name} 第 {attempt} 次传输异常（{type(e).__name__}）")
        except httpx.HTTPError as e:
            print(f"[notify] {name} 非临时错误（{type(e).__name__}），不重试")
            if retry_hint is not None:
                retry_hint["permanent"] = True
            return "failed_permanent", attempt

        if attempt < max_attempts:
            await asyncio.sleep(delay)
    return "failed", max_attempts


def _record_delivery_result(result: NotificationResult, *,
                            attempts: dict[str, int] | None = None,
                            error_type: str | None = None) -> None:
    """日志故障不得篡改已经取得的平台回执，也不记录敏感配置。"""
    log = default_log()
    if log is None:
        return
    detail: dict[str, Any] = {"msg_id": result.msg_id, "channels": result.channels}
    if attempts is not None:
        detail["attempts"] = attempts
    if error_type is not None:
        detail["error_type"] = error_type
    try:
        log.log_event("notify", "notify_delivered" if result.delivered else "notify_failed",
                      detail)
    except Exception as e:
        print(f"[notify] 送达结果落库失败（{type(e).__name__}）")


async def dispatch_external(formatted_msg: str, *, urgent: bool,
                            direction: str = "to_family", sender: str = "老人",
                            recipient: str = "家人", msg_id: int | None = None) -> NotificationResult:
    """并发派发已配置渠道，并返回平台确认后的结构化结果。"""
    from core import config
    from skills import dingtalk_direct
    urls = get_webhook_urls()
    direct_cfg = dingtalk_direct.configured()
    if direction != "to_family" or not (any(urls.values()) or direct_cfg):
        return NotificationResult(msg_id=msg_id, attempted=False,
                                  delivered=False, channels={})

    jobs: list[tuple[str, Any]] = []
    retry_hints: dict[str, dict] = {}
    try:
        timeout = float((config.settings().get("notify") or {}).get("timeout_s", 2.5))
        async with httpx.AsyncClient(timeout=timeout) as client:
            if direct_cfg:
                jobs.append(("dingtalk_direct", dingtalk_direct.send(client, direct_cfg, formatted_msg)))
            if urls["dingtalk"]:
                jobs.append(("dingtalk", _send_one_webhook(client, "dingtalk", "钉钉", urls["dingtalk"], {
                    "msgtype": "text",
                    "text": {"content": formatted_msg},
                    "at": {"isAtAll": urgent},
                }, retry_hint=retry_hints.setdefault("dingtalk", {}))))
            if urls["feishu"]:
                jobs.append(("feishu", _send_one_webhook(client, "feishu", "飞书", urls["feishu"], {
                    "msg_type": "text",
                    "content": {"text": formatted_msg},
                }, retry_hint=retry_hints.setdefault("feishu", {}))))
            if urls["generic"]:
                jobs.append(("generic", _send_one_webhook(client, "generic", "通用Webhook", urls["generic"], {
                    "title": "🚨【紧急预警】" if urgent else "【管家通知】",
                    "text": formatted_msg,
                    "msg": formatted_msg,
                    "content": formatted_msg,
                    "body": formatted_msg,
                    "desp": formatted_msg,
                    "urgent": urgent,
                    "sender": sender,
                    "recipient": recipient,
                    "direction": direction,
                    "msg_id": msg_id,
                }, retry_hint=retry_hints.setdefault("generic", {}))))
            if not jobs:
                return NotificationResult(msg_id=msg_id, attempted=False,
                                          delivered=False, channels={})
            raw_results = await asyncio.gather(
                *(job for _name, job in jobs), return_exceptions=True)

        channels: dict[str, str] = {}
        attempts: dict[str, int] = {}
        for (name, _job), raw in zip(jobs, raw_results):
            if isinstance(raw, Exception):
                channels[name], attempts[name] = "failed", 0
            else:
                channels[name], attempts[name] = raw
        result = NotificationResult(
            msg_id=msg_id,
            attempted=any(n > 0 for n in attempts.values()),
            delivered=any(v == "delivered" for v in channels.values()),
            channels=channels,
            retry_after_s=max((hint.get("after_s", 0.0)
                               for hint in retry_hints.values()), default=0.0),
            permanent=(bool(channels) and all(
                retry_hints.get(name, {}).get("permanent", False)
                for name in channels)),
        )
        _record_delivery_result(result, attempts=attempts)
        if result.delivered and msg_id is not None:
            log = default_log()
            if log is not None:
                try:
                    log.mark_notification_delivered(msg_id)
                except Exception as e:
                    print(f"[notify] 关闭 outbox 失败（{type(e).__name__}）")
        return result
    except Exception as e:
        print(f"[notify] 外部推送客户端异常（{type(e).__name__}）")
        channels = {name: "failed" for name in configured_channel_names()}
        result = NotificationResult(
            msg_id=msg_id,
            attempted=bool(jobs),  # 客户端都建不起来时一个请求都没发
            delivered=False,
            channels=channels,
        )
        _record_delivery_result(result, error_type=type(e).__name__)
        return result


async def _dispatch_with_deadline(formatted_msg: str, **kwargs) -> NotificationResult:
    """限制整次回执等待，避免慢响应拖住安全场景和重试 worker。"""
    from core import config
    cfg = config.settings().get("notify") or {}
    deadline = float(cfg.get("receipt_deadline_s", 10.0))
    try:
        return await asyncio.wait_for(
            dispatch_external(formatted_msg, **kwargs), timeout=deadline)
    except asyncio.TimeoutError:
        names = configured_channel_names()
        result = NotificationResult(
            msg_id=kwargs.get("msg_id"),
            attempted=bool(names),
            delivered=False,
            channels={name: "timeout" for name in names},
        )
        _record_delivery_result(result, error_type="TimeoutError")
        return result


async def _dispatch_claimed(formatted_msg: str, **kwargs) -> NotificationResult:
    """执行已登记 msg_id 的派发，并保证取消/异常后释放在途标记。"""
    try:
        return await _dispatch_with_deadline(formatted_msg, **kwargs)
    finally:
        msg_id = kwargs.get("msg_id")
        if msg_id is not None:
            _IN_FLIGHT_MSG_IDS.discard(msg_id)


async def wait_pending() -> None:
    """等待当前所有后台推送任务完成（供单测与排空使用）。"""
    if _BACKGROUND_TASKS:
        await asyncio.gather(*list(_BACKGROUND_TASKS), return_exceptions=True)


async def send(msg: str, urgent: bool = False, *,
               direction: str = "to_family", sender: str = "老人",
               recipient: str = "家人", wait_dispatch: bool = False,
               _dispatch: bool = True) -> int | None:
    """发一条消息给家人端，返回消息 id（未落库时 None）。绝不抛异常。"""
    msg = (msg or "").strip()
    if not msg:
        return None
    msg_id = None
    try:
        log = default_log()
        if log is not None:
            msg_id = log.save_message(direction, msg, sender, recipient, urgent)
    except Exception as e:
        print(f"[notify] 落库失败（{type(e).__name__}: {e}）")
    # 所有家人外发先于网络等待持久化；取消、崩溃或未配置渠道都保留补发资格
    formatted_msg = _format_external_message(msg, urgent)
    if direction == "to_family" and msg_id is not None:
        from core import config
        outbox_log = default_log()
        if outbox_log is not None:
            try:
                cfg = config.settings().get("notify") or {}
                delay = max(0.0, float(cfg.get("retry_backoff_s", 0.5)))
                outbox_log.enqueue_notification(
                    msg_id, formatted_msg, urgent=urgent, sender=sender,
                    recipient=recipient, direction=direction,
                    next_attempt_ts=time.time() + delay, error="pending")
            except Exception as e:
                print(f"[notify] 通知预入队失败（{type(e).__name__}）")
    try:
        await bus.publish({"type": "notify.family", "payload": {
            "id": msg_id, "msg": msg, "urgent": urgent, "direction": direction}})
    except Exception as e:
        print(f"[notify] 事件发布失败（{type(e).__name__}: {e}）")
    tag = "紧急｜" if urgent else ""
    print(f"[notify→{direction}] {tag}{msg}")

    # 外部渠道推送（仅向家人方向的消息推送）
    if direction == "to_family" and _dispatch:
        if msg_id is not None:
            _IN_FLIGHT_MSG_IDS.add(msg_id)
        try:
            task = asyncio.create_task(
                _dispatch_claimed(
                    formatted_msg,
                    urgent=urgent,
                    direction=direction,
                    sender=sender,
                    recipient=recipient,
                    msg_id=msg_id,
                )
            )
            _BACKGROUND_TASKS.add(task)
            task.add_done_callback(_BACKGROUND_TASKS.discard)
            if wait_dispatch:
                await task
        except Exception as e:
            if msg_id is not None:
                _IN_FLIGHT_MSG_IDS.discard(msg_id)
            print(f"[notify] 创建外部推送后台任务失败（{type(e).__name__}: {e}）")

    return msg_id


async def send_with_receipt(msg: str, urgent: bool = False, *,
                            direction: str = "to_family", sender: str = "老人",
                            recipient: str = "家人") -> NotificationResult:
    """等待外部平台回执；本地消息仍通过原 send 路径落库和广播。"""
    msg = (msg or "").strip()
    msg_id = await send(
        msg, urgent=urgent, direction=direction, sender=sender,
        recipient=recipient, wait_dispatch=False, _dispatch=False)
    if direction != "to_family" or not msg:
        return NotificationResult(msg_id=msg_id, attempted=False,
                                  delivered=False, channels={})
    formatted_msg = _format_external_message(msg, urgent)
    if msg_id is not None:
        _IN_FLIGHT_MSG_IDS.add(msg_id)
    result = await _dispatch_claimed(
        formatted_msg, urgent=urgent, direction=direction,
        sender=sender, recipient=recipient, msg_id=msg_id)
    if not result.delivered and not result.attempted:
        # 无渠道或客户端未建立：安全场景同样要留下“未外部送达”的审计记录。
        _record_delivery_result(result)
    if not result.delivered and msg_id is not None:
        # send() 已在外发前预入队；这里只反映真实 pending 状态，不重复插入
        log = default_log()
        if log is not None and log.has_pending_notification(msg_id):
            result = replace(result, pending=True)
    return result


async def notification_retry_loop(log, *, stop_event: asyncio.Event | None = None,
                                  interval_s: float | None = None,
                                  max_rounds: int | None = None) -> None:
    """消费 SQLite outbox；重启后从 pending 恢复，超过总次数转 dead。"""
    from core import config
    cfg = config.settings().get("notify") or {}
    interval = float(interval_s if interval_s is not None
                     else cfg.get("retry_worker_interval_s", 5.0))
    max_total = max(1, int(cfg.get("retry_total_attempts", 8)))
    base_backoff = max(0.0, float(cfg.get("retry_backoff_s", 0.5)))
    concurrency = max(1, int(cfg.get("retry_worker_concurrency", 3)))
    semaphore = asyncio.Semaphore(concurrency)
    rounds = 0

    def stopped() -> bool:
        return stop_event is not None and stop_event.is_set()

    while not stopped():
        try:
            rows = log.due_notifications(time.time())

            async def _retry_one(row):
                async with semaphore:
                    if stopped():
                        return
                    msg_id = row["msg_id"]
                    if msg_id is not None and msg_id in _IN_FLIGHT_MSG_IDS:
                        return  # 首次派发仍在等待回执，本轮保留 pending，下轮再看
                    if msg_id is not None:
                        _IN_FLIGHT_MSG_IDS.add(msg_id)
                    result = await _dispatch_claimed(
                        row["text"], urgent=bool(row["urgent"]),
                        direction=row["direction"], sender=row["sender"],
                        recipient=row["recipient"], msg_id=msg_id)
                    if not result.delivered:
                        attempt_no = int(row["attempts"]) + 1
                        delay = min(60.0, base_backoff * (2 ** min(attempt_no, 6)))
                        max_after = float(cfg.get("max_retry_after_s", 10.0))
                        delay = max(delay, min(float(result.retry_after_s), max_after))
                        log.mark_notification_attempt(
                            row["id"], delivered=False,
                            next_attempt_ts=time.time() + delay,
                            error="permanent_failure" if result.permanent else "undelivered",
                            max_attempts=1 if result.permanent else max_total)
                    # 成功时 dispatch_external 已按 msg_id 关闭 pending

            if rows:
                results = await asyncio.gather(
                    *(_retry_one(row) for row in rows), return_exceptions=True)
                for result in results:
                    if isinstance(result, Exception):
                        print(f"[notify] 单条重试失败（{type(result).__name__}）")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[notify] 重试 worker 本轮失败（{type(e).__name__}）")

        rounds += 1
        if max_rounds is not None and rounds >= max_rounds:
            return
        if stop_event is not None:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=max(0.01, interval))
            except asyncio.TimeoutError:
                pass
        else:
            await asyncio.sleep(max(0.01, interval))
