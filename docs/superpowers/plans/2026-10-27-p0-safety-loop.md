# P0 Safety Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用最小兼容改动修复跌倒候选闭锁、外部通知误判送达、未起床无应答不通知和测试真实联网问题。

**Architecture:** 保留 classic 场景引擎和现有 `notify.send()` 返回契约。新增 `send_with_receipt()` 作为安全场景的结构化回执入口，复用原有本地落库、总线和渠道负载；跌倒场景只有收到外部平台成功回执才开启静默。

**Tech Stack:** Python 3.13、asyncio、httpx、pytest/pytest-asyncio、现有 SQLite EventLog、OpenCV 合成帧测试。

**规格：** `docs/superpowers/specs/2026-10-27-p0-safety-loop-design.md`

**执行约束：** 不修改 VLA 生产代码，不引入 Home Assistant 或持久化队列，不执行 Git commit；每个 Task 结束只检查 diff，作为可提交检查点。

---

## 文件结构

- `tests/conftest.py`：全局测试禁网边界。
- `tests/test_no_network.py`：新增，验证禁网夹具本身。
- `sensors/perception.py`：跌倒候选使用静止开始时的人形驻留状态。
- `skills/notify.py`：结构化回执、平台业务回执解析、有限重试和结果日志。
- `actions/fall.py`：紧急通知等待回执，按真实送达结果控制静默。
- `actions/wakeup.py`：首检/复查无应答的分级通知。
- `config/settings.yaml`：通知超时、次数和退避参数。
- `tests/test_m6_scenes.py`：跌倒姿态丢失和静默联动回归。
- `tests/test_m5_scenes.py`：未起床无应答分级回归。
- `tests/test_notify_webhook.py`：保留旧通知契约并修正平台成功响应假件。
- `tests/test_notify_delivery.py`：新增通知回执和重试测试。

### Task 1: 默认测试禁网

**Files:**
- Modify: `tests/conftest.py`
- Create: `tests/test_no_network.py`

- [ ] **Step 1: 在任何新功能测试前安装网络边界**

在 `tests/conftest.py` 追加：

```python
@pytest.fixture(autouse=True)
def _block_cloud_network(monkeypatch):
    """默认禁止真实模型调用；单测可用自己的 monkeypatch 显式覆盖。"""
    from skills import llm, vision

    def blocked(*args, **kwargs):
        raise RuntimeError("测试禁网：请用 monkeypatch 替换模型调用边界")

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(vision, "_CLIENT", None)
    monkeypatch.setattr(vision, "_chat_sync", blocked)
    # llm 在 import 时从 vision 复制了 _chat_sync 引用，必须分别拦截。
    monkeypatch.setattr(llm, "_chat_sync", blocked)
```

- [ ] **Step 2: 写禁网夹具验证测试**

创建 `tests/test_no_network.py`：

```python
"""默认测试不得访问真实模型服务。"""
import os

import pytest

from skills import llm, vision


def test_api_key_is_not_exposed_to_tests():
    assert os.getenv("DEEPSEEK_API_KEY") is None


def test_vision_network_boundary_is_blocked():
    with pytest.raises(RuntimeError, match="测试禁网"):
        vision._chat_sync([], 1)


def test_llm_network_boundary_is_blocked():
    with pytest.raises(RuntimeError, match="测试禁网"):
        llm._chat_sync([], 1)


async def test_llm_uncertain_path_does_not_need_network():
    result = await llm.chat_json("返回一个 JSON 对象")
    assert result["uncertain"] is True
    assert "测试禁网" in result["error"]
```

- [ ] **Step 3: 运行禁网测试**

Run: `.venv/bin/python -m pytest tests/test_no_network.py -q`

Expected: 4 passed；无 HTTP 请求，无真实 Key 出现在命令或输出中。

- [ ] **Step 4: 检查可提交检查点，不 commit**

Run: `git diff --check -- tests/conftest.py tests/test_no_network.py && git diff --stat -- tests/conftest.py tests/test_no_network.py`

Expected: 无空白错误；只显示本任务文件。

### Task 2: 跌倒候选在姿态丢失后仍可触发

**Files:**
- Modify: `tests/test_m6_scenes.py`
- Modify: `sensors/perception.py:216-237`

- [ ] **Step 1: 扩展感知测试夹具以注入姿态器**

把 `_run_and_collect` 的签名和姿态构造改为：

```python
async def _run_and_collect(frames, *, hold_s, spike_ratio,
                           still_seconds=1.0, spike_window_s=3.0,
                           pose=None):
    from sensors.motion import OccupancyTracker
    from sensors.perception import run_perception
    from test_m4_perception import _FakeCamera, _FakePose
    sudden, stills = [], []
    pose = pose or _FakePose()
```

并把调用中的 `pose=_FakePose()` 改为：

```python
            pose=pose, occ=OccupancyTracker(hold_s=hold_s),
```

其余事件订阅和返回逻辑保持不变。

- [ ] **Step 2: 写姿态丢失正例和从未见人负例**

在 `tests/test_m6_scenes.py` 的感知测试区加入：

```python
class _FirstSeenThenLostPose:
    def __init__(self):
        self.calls = 0
        self.loaded_path = Path("fake-pose.pt")

    def infer(self, frame):
        self.calls += 1
        if self.calls == 1 and (frame > 200).mean() > 0.05:
            return [PersonPose(bbox=np.array([16., 16., 48., 48.]),
                               keypoints=np.zeros((17, 2)),
                               kconf=np.full(17, 0.9), conf=0.9)]
        return []


class _NeverPersonPose(_FirstSeenThenLostPose):
    def infer(self, frame):
        self.calls += 1
        return []


async def test_perception_sudden_still_survives_pose_loss():
    """静止开始时尚在驻留窗口内的人，不能因静止等待期姿态丢失而被吞掉。"""
    pose = _FirstSeenThenLostPose()
    frames = _spike_then_long_quiet_frames(rect=(16, 16, 48, 48))
    sudden, stills = await _run_and_collect(
        frames, hold_s=3.0, spike_ratio=0.20,
        still_seconds=5.0, spike_window_s=3.0, pose=pose)
    assert len(stills) == 1
    assert len(sudden) == 1
    assert pose.calls < len(frames)  # 不靠每帧姿态推理或增大 hold_s 通过


async def test_perception_sudden_still_requires_person_at_quiet_start():
    """从未确认人形时，运动突变不能单独构成跌倒候选。"""
    pose = _NeverPersonPose()
    frames = _spike_then_long_quiet_frames(rect=(16, 16, 48, 48))
    sudden, stills = await _run_and_collect(
        frames, hold_s=3.0, spike_ratio=0.20,
        still_seconds=5.0, spike_window_s=3.0, pose=pose)
    assert len(stills) == 1
    assert sudden == []
```

把旧 `test_perception_sudden_still_requires_person_present` 改名为 `test_perception_sudden_still_allows_confirmation_after_person_loss`，并保留其时间线，但将最后断言改为：

```python
    assert len(stills) == 1
    # 曾见人后离开与跌倒后姿态丢失在现有传感器中不可区分；只进入候选和二次确认，
    # 不等于直接报警。真正的“无人”负例由 _NeverPersonPose 覆盖。
    assert len(sudden) == 1
```

- [ ] **Step 3: 先运行新测试确认旧实现失败**

Run: `.venv/bin/python -m pytest tests/test_m6_scenes.py::test_perception_sudden_still_survives_pose_loss tests/test_m6_scenes.py::test_perception_sudden_still_requires_person_at_quiet_start tests/test_m6_scenes.py::test_perception_sudden_still_allows_confirmation_after_person_loss -q`

Expected: 姿态丢失正例和曾见人时间线在旧实现上因 `len(sudden) == 0` 失败；从未见人负例通过。

- [ ] **Step 4: 实现静止开始时刻的 occupancy 判定**

把 `sensors/perception.py` 中：

```python
                if spike >= spike_ratio and st.occupancy:
```

替换为：

```python
                occupancy_at_quiet = (
                    quiet_since is not None and occ.occupied(quiet_since))
                if spike >= spike_ratio and occupancy_at_quiet:
```

不得增大 `occupancy.hold_s`，不得增加姿态推理调用。

- [ ] **Step 5: 运行感知和场景回归**

Run: `.venv/bin/python -m pytest tests/test_m6_scenes.py tests/test_m4_perception.py -q`

Expected: 全部通过。

- [ ] **Step 6: 检查可提交检查点，不 commit**

Run: `git diff --check -- sensors/perception.py tests/test_m6_scenes.py && git diff --stat -- sensors/perception.py tests/test_m6_scenes.py`

Expected: 无空白错误；实现改动仅为静止开始时刻的判定。

### Task 3: 通知平台回执和有限重试

**Files:**
- Modify: `skills/notify.py`
- Modify: `config/settings.yaml:122-126`
- Modify: `tests/test_notify_webhook.py`
- Create: `tests/test_notify_delivery.py`

- [ ] **Step 1: 写回执和重试失败测试**

创建 `tests/test_notify_delivery.py`，包含以下测试工具和用例：

```python
"""外部通知平台回执、有限重试和旧 send 契约。"""
import httpx
import pytest

from core import config
from core.log import EventLog, set_default
from skills import notify


@pytest.fixture
def delivery_env(tmp_path, monkeypatch):
    log = EventLog(tmp_path / "delivery.db")
    set_default(log)
    monkeypatch.setattr(config, "settings", lambda: {
        "notify": {"timeout_s": 0.01, "max_attempts": 3, "retry_backoff_s": 0}
    })
    yield
    set_default(None)
    log.close()


def _urls(monkeypatch, *, dingtalk=False, feishu=False):
    monkeypatch.setenv("DINGTALK_WEBHOOK",
                       "https://oapi.dingtalk.com/robot/send" if dingtalk else "")
    monkeypatch.setenv("FEISHU_WEBHOOK",
                       "https://open.feishu.cn/bot" if feishu else "")
    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "")


async def test_dingtalk_business_error_is_not_delivered(delivery_env, monkeypatch):
    _urls(monkeypatch, dingtalk=True)
    calls = []

    async def fake_post(self, url, **kwargs):
        calls.append(url)
        return httpx.Response(200, json={"errcode": 310000})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    result = await notify.send_with_receipt("测试告警", urgent=True)
    assert result.attempted is True
    assert result.delivered is False
    assert result.channels["dingtalk"] == "failed"
    assert len(calls) == 1  # 明确的业务拒绝不重试


async def test_network_failure_retries_then_succeeds(delivery_env, monkeypatch):
    _urls(monkeypatch, dingtalk=True)
    calls = []

    async def fake_post(self, url, **kwargs):
        calls.append(url)
        if len(calls) < 3:
            raise httpx.ConnectTimeout("模拟超时")
        return httpx.Response(200, json={"errcode": 0})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    result = await notify.send_with_receipt("测试告警", urgent=True)
    assert result.delivered is True
    assert result.channels["dingtalk"] == "delivered"
    assert len(calls) == 3


async def test_one_successful_channel_marks_delivery(delivery_env, monkeypatch):
    _urls(monkeypatch, dingtalk=True, feishu=True)

    async def fake_post(self, url, **kwargs):
        if "dingtalk" in str(url):
            return httpx.Response(200, json={"errcode": 310000})
        return httpx.Response(200, json={"code": 0})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    result = await notify.send_with_receipt("测试告警", urgent=True)
    assert result.delivered is True
    assert result.channels == {"dingtalk": "failed", "feishu": "delivered"}


async def test_no_channel_is_not_external_delivery(delivery_env, monkeypatch):
    _urls(monkeypatch)
    result = await notify.send_with_receipt("测试告警", urgent=True)
    assert result.msg_id is not None
    assert result.attempted is False
    assert result.delivered is False


async def test_old_send_contract_survives_external_failure(delivery_env, monkeypatch):
    _urls(monkeypatch, dingtalk=True)

    async def fake_post(self, url, **kwargs):
        raise httpx.ConnectTimeout("模拟超时")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    msg_id = await notify.send("普通消息", wait_dispatch=True)
    assert isinstance(msg_id, int)  # 旧调用方仍获得本地消息 ID
```

同时把 `tests/test_notify_webhook.py` 中成功响应假件改为按平台返回：

```python
        body = {"code": 0} if "feishu" in str(url) else {"errcode": 0}
        return httpx.Response(200, json=body)
```

- [ ] **Step 2: 先运行确认新接口不存在或旧逻辑失败**

Run: `.venv/bin/python -m pytest tests/test_notify_delivery.py -q`

Expected: 旧实现没有 `send_with_receipt`，测试失败；不发起真实网络请求。

- [ ] **Step 3: 增加配置和结构化结果**

在 `config/settings.yaml` 的 `notify` 分区增加：

```yaml
  timeout_s: 2.5             # 单次平台请求超时
  max_attempts: 3            # 网络错误、429、5xx 的单渠道最大尝试次数
  retry_backoff_s: 0.5       # 指数退避基数；测试注入 0
```

在 `skills/notify.py` 顶部加入：

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class NotificationResult:
    msg_id: int | None
    attempted: bool
    delivered: bool
    channels: dict[str, str]
```

- [ ] **Step 4: 实现平台响应判断和单渠道重试**

用以下两个辅助函数替换现有 `_send_one_webhook`；保留函数名以减少调用面变化：

```python
def _platform_acknowledged(name: str, resp: httpx.Response) -> bool:
    if name == "钉钉":
        return bool(resp.json().get("errcode") == 0)
    if name == "飞书":
        return bool(resp.json().get("code") == 0)
    return 200 <= resp.status_code < 300


async def _send_one_webhook(client: httpx.AsyncClient, name: str,
                            url: str, payload: dict[str, Any]) -> tuple[str, int]:
    from core import config
    cfg = config.settings().get("notify") or {}
    max_attempts = max(1, int(cfg.get("max_attempts", 3)))
    backoff = max(0.0, float(cfg.get("retry_backoff_s", 0.5)))

    for attempt in range(1, max_attempts + 1):
        retryable = False
        try:
            resp = await client.post(url, json=payload)
            if resp.status_code == 429 or resp.status_code >= 500:
                retryable = True
            elif resp.status_code >= 400:
                return "failed", attempt
            else:
                try:
                    acknowledged = _platform_acknowledged(name, resp)
                except (ValueError, TypeError, AttributeError):
                    return "failed", attempt
                return ("delivered" if acknowledged else "failed"), attempt
        except httpx.HTTPError as e:
            retryable = True
            print(f"[notify] {name} 第 {attempt} 次网络异常（{type(e).__name__}）")

        if not retryable:
            return "failed", attempt
        if attempt < max_attempts:
            await asyncio.sleep(backoff * (2 ** (attempt - 1)))
    return "failed", max_attempts
```

`httpx.AsyncClient` 的超时继续使用 `notify.timeout_s`：

```python
        timeout = float((config.settings().get("notify") or {}).get("timeout_s", 2.5))
        async with httpx.AsyncClient(timeout=timeout) as client:
```

在 `dispatch_external` 顶部局部 `from core import config`，避免增加新的全局配置依赖。

- [ ] **Step 5: 让 dispatch_external 返回真实回执并记录结果**

保留现有三种渠道的 payload 构造，把任务列表改为携带渠道名：

```python
    jobs: list[tuple[str, Any]] = []
```

每次现有 `coros.append(_send_one_webhook(...))` 改为对应：

```python
            jobs.append(("dingtalk", _send_one_webhook(client, "钉钉", urls["dingtalk"], {
                "msgtype": "text",
                "text": {"content": formatted_msg},
                "at": {"isAtAll": urgent},
            })))
```

飞书和通用渠道分别使用：

```python
            jobs.append(("feishu", _send_one_webhook(client, "飞书", urls["feishu"], {
                "msg_type": "text",
                "content": {"text": formatted_msg},
            })))
            jobs.append(("generic", _send_one_webhook(client, "通用Webhook", urls["generic"], {
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
            })))
```

将派发尾部改为：

```python
            if jobs:
                raw_results = await asyncio.gather(
                    *(job for _name, job in jobs), return_exceptions=True)
                channels: dict[str, str] = {}
                attempts: dict[str, int] = {}
                for (name, _job), raw in zip(jobs, raw_results):
                    if isinstance(raw, Exception):
                        channels[name], attempts[name] = "failed", 1
                    else:
                        channels[name], attempts[name] = raw
                result = NotificationResult(
                    msg_id=msg_id,
                    attempted=True,
                    delivered=any(v == "delivered" for v in channels.values()),
                    channels=channels,
                )
                log = default_log()
                if log is not None:
                    log.log_event(
                        "notify",
                        "notify_delivered" if result.delivered else "notify_failed",
                        {"msg_id": msg_id, "channels": channels, "attempts": attempts},
                    )
                return result
```

`dispatch_external` 在无渠道和 `direction != "to_family"` 时返回：

```python
    return NotificationResult(msg_id=msg_id, attempted=False,
                              delivered=False, channels={})
```

外层异常兜底返回：

```python
    except Exception as e:
        print(f"[notify] 外部推送客户端异常（{type(e).__name__}）")
        channels = {name: "failed" for name, url in urls.items() if url}
        result = NotificationResult(
            msg_id=msg_id,
            attempted=bool(channels),
            delivered=False,
            channels=channels,
        )
        log = default_log()
        if log is not None:
            log.log_event("notify", "notify_failed", {
                "msg_id": msg_id,
                "channels": channels,
                "error_type": type(e).__name__,
            })
        return result
```

如果 `jobs` 为空，函数末尾返回 `NotificationResult(msg_id=msg_id, attempted=False, delivered=False, channels={})`。结果日志写入失败不得改变已经获得的外部平台回执；日志写操作用独立 `try/except Exception` 包裹并只打印异常类型。所有分支都以 `NotificationResult` 返回，不再只返回 `None`。

- [ ] **Step 6: 增加 send_with_receipt，同时保留旧 send**

给现有 `send()` 增加私有关键字参数 `_dispatch: bool = True`，并把外发条件改为：

```python
    if direction == "to_family" and _dispatch:
```

在 `send()` 后新增：

```python
async def send_with_receipt(msg: str, urgent: bool = False, *,
                            direction: str = "to_family", sender: str = "老人",
                            recipient: str = "家人") -> NotificationResult:
    """等待外部平台回执；本地消息仍通过原 send 路径落库和广播。"""
    msg_id = await send(
        msg, urgent=urgent, direction=direction, sender=sender,
        recipient=recipient, wait_dispatch=False, _dispatch=False)
    if direction != "to_family" or not (msg or "").strip():
        return NotificationResult(msg_id=msg_id, attempted=False,
                                  delivered=False, channels={})
    formatted_msg = (
        msg if (msg.startswith("🚨") or "紧急预警" in msg)
        else f"🚨【紧急预警】{msg}"
    ) if urgent else msg
    return await dispatch_external(
        formatted_msg, urgent=urgent, direction=direction,
        sender=sender, recipient=recipient, msg_id=msg_id)
```

现有 `send()` 的 `msg_id | None` 返回值、后台任务跟踪、`wait_dispatch` 和 `wait_pending()` 行为保持不变。

- [ ] **Step 7: 运行通知测试**

Run: `.venv/bin/python -m pytest tests/test_notify_delivery.py tests/test_notify_webhook.py -q`

Expected: 全部通过；无真实网络请求。

- [ ] **Step 8: 检查可提交检查点，不 commit**

Run: `git diff --check -- skills/notify.py config/settings.yaml tests/test_notify_delivery.py tests/test_notify_webhook.py && git diff --stat -- skills/notify.py config/settings.yaml tests/test_notify_delivery.py tests/test_notify_webhook.py`

Expected: 无空白错误；旧 `send()` 调用方无需修改。

### Task 4: 跌倒静默只由真实送达武装

**Files:**
- Modify: `actions/fall.py:90-124`
- Modify: `tests/test_m6_scenes.py`

- [ ] **Step 1: 把跌倒通知测试改为结构化回执**

在 `tests/test_m6_scenes.py` 的跌倒测试区加入工具：

```python
def _receipt(*, delivered: bool):
    from skills.notify import NotificationResult
    return NotificationResult(
        msg_id=1,
        attempted=True,
        delivered=delivered,
        channels={"dingtalk": "delivered" if delivered else "failed"},
    )
```

将 `test_fall_confirm_distress_beats_fine_words`、`test_fall_confirm_no_reply_alerts_then_suppresses`、`test_fall_confirm_delivery_failure_keeps_retryable`、`test_fall_confirm_real_distress_still_alerts` 和 `test_fall_scene_uncertain_chain_via_engine` 中的 `notify.send` 替换为 `notify.send_with_receipt`。成功用例采用：

```python
    async def fake_send(msg, urgent=False, **kw):
        sent.append((msg, urgent))
        return _receipt(delivered=True)
    monkeypatch.setattr(notify, "send_with_receipt", fake_send)
```

`test_fall_confirm_delivery_failure_keeps_retryable` 使用：

```python
    async def fake_send(msg, urgent=False, **kw):
        sent.append((msg, urgent))
        return _receipt(delivered=False)
    monkeypatch.setattr(notify, "send_with_receipt", fake_send)
```

并把该测试文档改为“本地消息已保存但外部未送达时，不武装静默”。成功、撤警和无应答测试的消息文本断言保持不变。

- [ ] **Step 2: 先运行确认旧实现未走回执接口**

Run: `.venv/bin/python -m pytest tests/test_m6_scenes.py -q -k 'fall_confirm or fall_scene'`

Expected: 紧急用例失败，因为旧生产代码仍调用 `notify.send`，没有调用测试注入的回执接口。

- [ ] **Step 3: 实现送达后静默**

在 `actions/fall.py` 把紧急发送段改为：

```python
    if reply:
        receipt = await notify.send_with_receipt(
            f"疑似跌倒！老人语音回复：「{reply}」，请立即查看！", urgent=True)
    else:
        receipt = await notify.send_with_receipt(
            "疑似跌倒且语音询问无应答，请立即查看！", urgent=True)
    if receipt.delivered:
        _stopped_until = time.time() + float(cfg.get("re_alert_s", 1800))
    return "alerted"
```

保留 `global _stopped_until`、现有消息文本和 `cancelled` 分支。

- [ ] **Step 4: 运行跌倒回归**

Run: `.venv/bin/python -m pytest tests/test_m6_scenes.py -q -k 'fall or sudden_still'`

Expected: 全部通过；未送达用例 `fall.stopped()` 为 False，送达用例为 True。

- [ ] **Step 5: 检查可提交检查点，不 commit**

Run: `git diff --check -- actions/fall.py tests/test_m6_scenes.py && git diff --stat -- actions/fall.py tests/test_m6_scenes.py`

Expected: 无空白错误；没有改变场景 YAML。

### Task 5: 未起床无应答分级通知

**Files:**
- Modify: `actions/wakeup.py:58-75`
- Modify: `tests/test_m5_scenes.py`

- [ ] **Step 1: 写首检和复查无应答测试**

在 `tests/test_m5_scenes.py` 的 wake_up 区加入：

```python
async def test_wakeup_first_unknown_no_reply_notifies_normal(
        fake_settings, _spoken, monkeypatch):
    from actions import wakeup
    from skills import stt
    sent = []

    async def fake_notify(msg, *, urgent):
        sent.append((msg, urgent))

    monkeypatch.setattr(wakeup, "notify_send", fake_notify)
    monkeypatch.setattr(stt, "listen", lambda *a, **kw: _reply(""))
    wakeup.reset_pending()
    perception.clear_current()

    result = await wakeup.wakeup_check({})
    assert result["phase"] == "first"
    assert result["end"] == "no_reply"
    assert len(sent) == 1 and sent[0][1] is False
    assert "无应答" in sent[0][0]
    assert wakeup._pending["date"] is None


async def test_wakeup_recheck_no_reply_notifies_urgent(
        fake_settings, _spoken, monkeypatch):
    from actions import wakeup
    from skills import stt
    sent = []

    async def fake_notify(msg, *, urgent):
        sent.append((msg, urgent))

    monkeypatch.setattr(wakeup, "notify_send", fake_notify)
    monkeypatch.setattr(stt, "listen", lambda *a, **kw: _reply(""))
    wakeup.reset_pending()
    perception.register_current(
        make_hist([(0.0, make_pose(bbox=(150, 150, 250, 350)))]))

    first = await wakeup.wakeup_check({})
    assert first["act"] == "remind"
    result = await wakeup.wakeup_check({})
    assert result["phase"] == "recheck"
    assert result["end"] == "no_reply"
    assert len(sent) == 1 and sent[0][1] is True
    assert "无应答" in sent[0][0]
    assert wakeup._pending["date"] is None
```

- [ ] **Step 2: 先运行确认旧实现失败**

Run: `.venv/bin/python -m pytest tests/test_m5_scenes.py -q -k 'no_reply_notifies'`

Expected: 两个用例都因 `len(sent) == 0` 失败。

- [ ] **Step 3: 实现分级通知**

把 `_voice_check` 签名改为：

```python
async def _voice_check(ctx: dict, question: str, *, phase: str,
                       no_reply_urgent: bool) -> str:
```

把无应答分支改为：

```python
    if not reply:
        if log is not None:
            log.log_event("wake_up", "wakeup_voice", {"phase": phase, "reply": ""})
        level = "紧急" if no_reply_urgent else "普通跟进"
        await notify_send(
            f"老人晨间{phase}语音询问无应答（{level}），请电话联系确认。",
            urgent=no_reply_urgent)
        return "no_reply"
```

首检调用改为：

```python
        end = await _voice_check(
            ctx, "奶奶，您在吗？还好吗？", phase="确认", no_reply_urgent=False)
```

复查调用改为：

```python
        end = await _voice_check(
            ctx, "奶奶，您还好吗？不舒服就跟我说一声。",
            phase="询问", no_reply_urgent=True)
```

- [ ] **Step 4: 运行未起床和 M5 回归**

Run: `.venv/bin/python -m pytest tests/test_m5_scenes.py -q`

Expected: 全部通过；普通回复和求助关键词测试不改变。

- [ ] **Step 5: 检查可提交检查点，不 commit**

Run: `git diff --check -- actions/wakeup.py tests/test_m5_scenes.py && git diff --stat -- actions/wakeup.py tests/test_m5_scenes.py`

Expected: 无空白错误；只有分级通知相关改动。

### Task 6: 全量禁网回归与变更审查

**Files:**
- Verify all files above; no new production scope.

- [ ] **Step 1: 用显式不可达模型地址运行全量测试**

Run: `DEEPSEEK_BASE_URL=http://127.0.0.1:9 .venv/bin/python -m pytest -q`

Expected: 全部通过；没有真实云端请求；没有测试依赖 API Key。

- [ ] **Step 2: 检查差异质量**

Run: `git diff --check && git status --short && git diff --stat`

Expected: 无空白错误；只出现本计划文件、既有未跟踪 `deliverables/`、`logs/` 和用户原有文件；没有 `.env`、数据库或模型文件变更。

- [ ] **Step 3: 核对规格边界**

逐项确认：

```text
classic 场景 YAML 未改接口
VLA 生产代码未改
notify.send 仍返回本地 msg_id | None
外部未送达不武装跌倒静默
首检无应答普通通知，复查无应答紧急通知
未实现持久化重试、双向聊天、真机标定或 7×24 运维的声明
```

- [ ] **Step 4: 不 commit，向用户报告验证结果和剩余边界**

输出：修改文件清单、测试总数、关键行为验证结果，以及仍需下一阶段处理的远程双向通信、统一语音、真机标定和常驻运维。
