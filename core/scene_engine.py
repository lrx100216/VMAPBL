"""场景引擎：加载 scenes/*.yaml，解释 trigger / condition / action 三段式（规范第五节）。

触发器接线进度：manual（M2）、speech（M3，wire_speech_triggers 订阅 speech.final
按 intent 路由）；M5 起 state/event/time 全部接线：
- wire_state_triggers 订阅 state.* 事件，匹配 entity + to（to 缺省=任何变化都算）；
- wire_event_triggers 按 YAML 的 event 名订阅总线（如 sensor.still）；
- wire_time_triggers 跑一个每日定时循环（time.at，默认 30s  tick），
  每个触发时刻每天至多记账一次，且只有动作链真正跑过才记账
  （被冷却/条件跳过的到点不消耗当天名额，下个 tick 再试）。
三类触发都进 run_queued 仲裁（跌倒 100 > 未起床 90 > 起身 80 > 其他 0）。

表达式一律走 simpleeval（functions={} 纯求值，禁止函数调用，
尤其杜绝 rand() 这类非确定性函数），禁止裸 eval。

条件语义：
- cooldown:     距本场景上次【成功】触发不足 seconds 秒 → 不通过；seconds 缺省时用
                引擎默认冷却（来自 settings.yaml 的 global.default_cooldown_s）；
                被拦截的尝试不刷新冷却；动作链失败的运行不记冷却（允许重试）
- time_window:  当前本地时间不在 [after, before] 窗口内 → 不通过（支持跨午夜窗口）
- template:     simpleeval 求值 expr 为假 → 不通过；
                可用变量：occupancy / is_still / speaking / medication_taken /
                last_motion_ts 以及动作链上下文（含 result）

动作语义：
- skill:  调用 actions/ 里注册的技能（async def fn(ctx, **args)），返回值写进 ctx["result"]
- speak:  TTS 播报，走 skills/tts.py（唯一发声出口，内部半双工互斥）；
          text 为静态文案，expr 为 simpleeval 表达式（可用 result）动态生成，二者恰选一
- choose: 顺序求值 branches 的 when 表达式（可用 result），命中第一个即执行其 do 列表

异常语义：动作链抛异常时落一条 kind="error" 的事件到 SQLite 后原样上抛；
trigger() 直接上抛给调用方；run_queued() 按场景逐个隔离（一个失败不拖垮整批）。
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import yaml
from simpleeval import SimpleEval

from . import bus
from .log import EventLog
from .state import StateStore

_ALLOWED_TRIGGER = {"manual", "state", "event", "time", "speech"}
_ALLOWED_COND = {"cooldown", "time_window", "template"}
_ALLOWED_ACT = {"skill", "choose", "speak"}

_MAX_CHOOSE_DEPTH = 8  # choose 嵌套上限：加载期与运行期共用，防 YAML 写出无限递归


class SceneError(ValueError):
    """场景 YAML 不合法或运行期解释错误。"""


@dataclass
class Scene:
    """一个场景的运行时表示（与 YAML 一一对应）。"""

    id: str
    name: str
    trigger: list[dict]
    condition: list[dict] = field(default_factory=list)
    action: list[dict] = field(default_factory=list)
    mode: str = "single"   # single：运行中不重入
    priority: int = 0      # 仲裁优先级：跌倒 100 > 未起床 90 > 起身 80 > 其他 0


def _parse_hms(s: Any, where: str) -> "datetime.time":
    """把 "HH:MM:SS" 解析为 time 对象，失败时抛出带位置的 SceneError。"""
    try:
        return datetime.strptime(str(s), "%H:%M:%S").time()
    except ValueError:
        raise SceneError(f"{where}: 时间 {s!r} 不是 HH:MM:SS 格式") from None


def _check_expr_syntax(expr: Any, where: str) -> None:
    """加载期表达式语法预检：只查语法（名字运行期才绑定），防「配置错误躺到触发时才炸」。"""
    try:
        ast.parse(str(expr), mode="eval")
    except SyntaxError as e:
        raise SceneError(f"{where}: 表达式 {expr!r} 语法错误：{e.msg}") from None


def _validate_seconds(sec: Any, where: str) -> None:
    """cooldown 的 seconds 必须是非负有限数值（拒绝 bool/负数/NaN——NaN 会让冷却永不生效）。"""
    if sec is None:
        return  # 缺省合法：运行期回落到引擎默认冷却
    if (isinstance(sec, bool) or not isinstance(sec, (int, float))
            or sec < 0 or sec != sec):  # sec != sec 即 NaN 自检
        raise SceneError(f"{where}: cooldown 条件 seconds 必须是非负数值，收到 {sec!r}")


def _validate_actions(actions: Any, where: str, depth: int = 0) -> None:
    """递归校验动作列表（choose 的 do 列表递归检查，加载期即执行深度上限）。"""
    if not isinstance(actions, list) or not actions:
        raise SceneError(f"{where}: action 必须是非空列表")
    if depth > _MAX_CHOOSE_DEPTH:
        raise SceneError(f"{where}: choose 嵌套超过 {_MAX_CHOOSE_DEPTH} 层")
    for act in actions:
        if not isinstance(act, dict) or "type" not in act:
            raise SceneError(f"{where}: 每个动作必须是含 type 字段的映射，收到 {act!r}")
        atype = act["type"]
        if atype not in _ALLOWED_ACT:
            raise SceneError(f"{where}: 未知动作类型 {atype!r}（支持 {sorted(_ALLOWED_ACT)}）")
        if atype == "skill":
            if not act.get("name"):
                raise SceneError(f"{where}: skill 动作缺少 name")
            args = act.get("args")
            if args is not None and not isinstance(args, dict):
                raise SceneError(f"{where}: skill 动作 args 必须是映射，收到 {args!r}")
            if isinstance(args, dict) and "ctx" in args:
                raise SceneError(f"{where}: skill 动作 args 不得包含保留键 'ctx'")
        if atype == "speak":
            has_text = bool(str(act.get("text", "")).strip())
            has_expr = bool(str(act.get("expr", "")).strip())
            if has_text == has_expr:  # 异或：必须恰好其一
                raise SceneError(
                    f"{where}: speak 动作需要 text（静态文案）或 expr（动态表达式）恰好其一")
            if has_expr:
                _check_expr_syntax(act["expr"], f"{where}→speak.expr")
        if atype == "choose":
            branches = act.get("branches")
            if not isinstance(branches, list) or not branches:
                raise SceneError(f"{where}: choose 动作必须有非空 branches")
            for branch in branches:
                if not isinstance(branch, dict) or "when" not in branch:
                    raise SceneError(f"{where}: choose 分支必须有 when 表达式")
                _check_expr_syntax(branch["when"], f"{where}→choose.when")
                _validate_actions(branch.get("do") or [], f"{where}→choose", depth + 1)


def load_scene(path: Path) -> Scene:
    """加载并严格校验一个场景 YAML；任何不合法都在加载期抛 SceneError。"""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SceneError(f"{path.name}: 顶层必须是映射")

    missing = [k for k in ("id", "name", "trigger", "action") if k not in data]
    if missing:
        raise SceneError(f"{path.name}: 缺少必填字段 {missing}")
    known = {"id", "name", "trigger", "condition", "action", "mode", "priority"}
    unknown = set(data) - known
    if unknown:
        raise SceneError(f"{path.name}: 未知字段 {sorted(unknown)}（支持 {sorted(known)}）")

    trigger = data["trigger"]
    if not isinstance(trigger, list) or not trigger:
        raise SceneError(f"{path.name}: trigger 必须是非空列表")
    for t in trigger:
        if not isinstance(t, dict) or t.get("type") not in _ALLOWED_TRIGGER:
            raise SceneError(f"{path.name}: 未知触发器 {t!r}（支持 {sorted(_ALLOWED_TRIGGER)}）")
        ttype = t["type"]
        if ttype == "state":
            if not t.get("entity"):
                raise SceneError(f"{path.name}: state 触发器缺少 entity")
            if "to" in t and (t["to"] is None
                              or isinstance(t["to"], (list, dict))):
                raise SceneError(
                    f"{path.name}: state 触发器 to 必须是字符串/数值/布尔，"
                    f"收到 {t['to']!r}（不写 to 表示任何变化都触发）")
        if ttype == "event" and not t.get("event"):
            raise SceneError(f"{path.name}: event 触发器缺少 event（如 sensor.still）")
        if ttype == "time":
            _parse_hms(t.get("at"), f"{path.name} time.at")

    condition = data.get("condition") or []
    if not isinstance(condition, list):
        raise SceneError(f"{path.name}: condition 必须是列表")
    for c in condition:
        if not isinstance(c, dict) or c.get("type") not in _ALLOWED_COND:
            raise SceneError(f"{path.name}: 未知条件 {c!r}（支持 {sorted(_ALLOWED_COND)}）")
        if c["type"] == "cooldown":
            _validate_seconds(c.get("seconds"), path.name)
        if c["type"] == "time_window":
            _parse_hms(c.get("after", "00:00:00"), f"{path.name} time_window.after")
            _parse_hms(c.get("before", "23:59:59"), f"{path.name} time_window.before")
        if c["type"] == "template":
            if not str(c.get("expr", "")).strip():
                raise SceneError(f"{path.name}: template 条件需要非空 expr")
            _check_expr_syntax(c["expr"], f"{path.name} template.expr")

    _validate_actions(data["action"], path.name)

    mode = data.get("mode", "single")
    if mode != "single":
        raise SceneError(f"{path.name}: 目前只支持 mode: single")

    raw_prio = data.get("priority", 0)
    if isinstance(raw_prio, bool) or not isinstance(raw_prio, (int, float)):
        raise SceneError(f"{path.name}: priority 必须是整数，收到 {raw_prio!r}")
    return Scene(
        id=str(data["id"]), name=str(data["name"]),
        trigger=trigger, condition=condition, action=data["action"],
        mode=mode, priority=int(raw_prio),
    )


class SceneEngine:
    """场景引擎：加载 scenes/ 目录，接收手动触发，按 条件→动作 执行并全程落库。"""

    def __init__(self, scenes_dir: str | Path, state: StateStore, log: EventLog,
                 skills: dict[str, Callable] | None = None,
                 default_cooldown_s: float = 60.0):
        self._state = state
        self._log = log
        self._skills = dict(skills or {})
        self._default_cooldown_s = float(default_cooldown_s)
        self._scenes: dict[str, Scene] = {}
        self._running: set[str] = set()
        self._time_fired: dict[str, str] = {}   # time 触发器：场景 id → 已触发日期
        self._time_task = None
        self._load_all(Path(scenes_dir))

    def _load_all(self, scenes_dir: Path) -> None:
        for path in sorted(scenes_dir.glob("*.yaml")):
            scene = load_scene(path)  # 校验失败直接抛：配置错误必须在启动期暴露
            if scene.id in self._scenes:
                raise SceneError(f"场景 id 重复：{scene.id}")
            self._scenes[scene.id] = scene

    def wire_speech_triggers(self) -> None:
        """M3 接线：订阅 speech.final 事件，把 intent 匹配的场景投入仲裁队列。

        语音回路（main.py --voice）：listen() 识别 → intent.classify() 分类 →
        publish speech.final {"text", "intent"} → 这里按场景的 speech 触发器
        （可带 intent 过滤；不带则匹配任意意图）路由到 run_queued 优先级仲裁。
        """
        async def _route(event: dict) -> None:
            payload = event.get("payload") or {}
            intent = payload.get("intent")
            text = payload.get("text", "")
            matched = [s.id for s in self._scenes.values()
                       if any(t.get("type") == "speech"
                              and (t.get("intent") is None or t.get("intent") == intent)
                              for t in s.trigger)]
            if not matched:
                return  # 无场景认领的意图由语音回路自己兜底（闲聊回复）
            self._log.log_event("speech_router", "speech_route",
                                {"intent": intent, "matched": matched, "text": text})
            await self.run_queued(matched, ctx={"text": text, "intent": intent})

        bus.subscribe("speech.final", _route)

    async def _fire(self, scene_ids: list[str], source: str,
                    detail: dict, ctx: dict | None = None) -> dict[str, bool | None]:
        """触发器命中后的统一入口：落一条 trigger_fire 审计事件，进仲裁队列。

        ctx 会透传给场景动作（wire_event_triggers 用它把原始事件交给 skill，
        让场景7 的 fall_decide 能读 sensor.sudden_still 载荷里的 quiet_since）。
        返回仲裁结果 {场景id: True 成功 / False 被跳过 / None 异常}；
        time 触发器靠它决定当天名额是否记账（见 check_time_triggers）。
        """
        if not scene_ids:
            return {}
        self._log.log_event("engine", "trigger_fire",
                            {"source": source, "matched": scene_ids, **detail})
        return await self.run_queued(scene_ids, ctx=ctx)

    def wire_state_triggers(self) -> None:
        """M5 接线：订阅 state.*，把 entity/to 匹配的场景投入仲裁。

        YAML 形如 `- type: state, entity: occupancy, to: "on"`；to 以字符串
        比较（"on"→True、"off"→False、数字串转数值），缺省时任何变化都算命中。
        """
        def _coerce(to: Any) -> Any:
            if isinstance(to, str):
                low = to.strip().lower()
                if low in ("on", "true"):
                    return True
                if low in ("off", "false"):
                    return False
                try:
                    return float(low)
                except ValueError:
                    return to
            return to

        async def _on_state(event: dict) -> None:
            payload = event.get("payload") or {}
            entity, new = payload.get("entity"), payload.get("new")
            matched = []
            for s in self._scenes.values():
                for t in s.trigger:
                    if t.get("type") != "state" or t.get("entity") != entity:
                        continue
                    if "to" not in t or _coerce(t.get("to")) == new:
                        matched.append(s.id)
                        break  # 同一场景多个匹配触发器只投一次
            await self._fire(matched, "state",
                             {"entity": entity, "new": new})

        bus.subscribe("state.*", _on_state)

    def wire_event_triggers(self) -> None:
        """M5 接线：按 YAML 的 event 名订阅总线（如 sensor.still）。

        不同名的事件通道合并成一条订阅（fnmatch 语义无重叠时逐一比对）。
        """
        by_event: dict[str, list[str]] = {}
        for s in self._scenes.values():
            for t in s.trigger:
                if t.get("type") == "event":
                    by_event.setdefault(str(t["event"]), []).append(s.id)
        for etype, scene_ids in by_event.items():
            async def _on_bus(event: dict, ids=list(scene_ids),
                              et=etype) -> None:
                await self._fire(ids, "event",
                                 {"event": et, "payload": event.get("payload")},
                                 ctx={"event": event})
            bus.subscribe(etype, _on_bus)

    def wire_time_triggers(self, tick_s: float = 30.0):
        """M5 接线：每日定时触发（`- type: time, at: "08:30:00"`）。

        每 tick_s 秒对齐检查一次；每个触发时刻每天至多触发一次（去重键
        「场景@时刻」存 _time_fired——场景5 一天有两个触发点）。
        返回 asyncio.Task（unwire() 统一取消）。测试请直接调
        check_time_triggers(now, source=...)，不要等 tick。
        """
        async def _tick() -> None:
            while True:
                await asyncio.sleep(tick_s)
                try:
                    await self.check_time_triggers(datetime.now())
                except Exception as e:
                    # 一次异常不能杀死定时循环（CancelledError 是 BaseException，
                    # 走不到这里，unwire() 的 cancel 照常生效）
                    print(f"[engine] time 触发器 tick 异常（循环继续）："
                          f"{type(e).__name__}: {e}")
                    try:
                        self._log.log_event(
                            "engine", "error",
                            {"阶段": "time_tick", "错误类型": type(e).__name__,
                             "错误": str(e)[:500]})
                    except Exception:
                        pass  # 日志库本身故障时不再级联

        self._time_task = asyncio.get_running_loop().create_task(
            _tick(), name="engine-time-triggers")
        return self._time_task

    async def check_time_triggers(self, now: datetime,
                                  source: str = "time") -> list[str]:
        """到点检查：at ≤ now 且今天未记账的场景投仲裁。返回本次记账的场景 id。

        独立成方法是为了可测（注入任意 now），也是回放/手动补触发的入口
        （source 可传 "manual-replay" 等，落库可溯源）。

        记账在仲裁之后：动作链跑过（成功或异常）才消耗当天名额——异常也记，
        否则每个 tick 重试会把错误落库刷屏；被冷却/条件跳过的（False）不记，
        下个 tick 再试，一次冷却拦截不至于吞掉场景5 当天的首检。
        """
        today = now.date().isoformat()
        now_t = now.time()
        due: list[tuple[str, str]] = []
        for s in self._scenes.values():
            for t in s.trigger:
                if t.get("type") != "time":
                    continue
                # 去重键是「场景@时刻」而非场景：场景5 一天要点名两次
                # （08:30 首检 + 09:00 复查），按场景去重会吞掉第二个触发点
                key = f"{s.id}@{t.get('at')}"
                if self._time_fired.get(key) == today:
                    continue
                at = _parse_hms(t.get("at"), f"{s.id} time.at")
                if at <= now_t:
                    due.append((s.id, key))
                    break  # 同一场景一次 tick 只投一次
        results = await self._fire([sid for sid, _ in due], source,
                                   {"now": now.isoformat()})
        fired = []
        for sid, key in due:
            if results.get(sid) is not False:
                self._time_fired[key] = today
                fired.append(sid)
        return fired

    def unwire(self) -> None:
        """停掉定时循环（总线订阅随进程/测试 clear_subscriptions 一起清）。"""
        task = getattr(self, "_time_task", None)
        if task is not None:
            task.cancel()
            self._time_task = None

    @property
    def scenes(self) -> dict[str, Scene]:
        return dict(self._scenes)

    async def trigger(self, scene_id: str, ctx: dict | None = None,
                      source: str = "manual") -> bool:
        """触发一个场景。返回 True = 动作执行成功；False = 被重入/条件拦截。

        动作链抛出的异常在落库 kind="error" 后原样上抛（见模块 docstring 异常语义）。
        """
        scene = self._scenes.get(scene_id)
        if scene is None:
            raise SceneError(f"未知场景 id：{scene_id}（用 --list 查看已有场景）")
        self._log.log_event(scene_id, "trigger", {"source": source})
        if scene.mode == "single" and scene_id in self._running:
            self._log.log_event(scene_id, "skipped", {"reason": "mode=single，场景正在运行中"})
            return False
        self._running.add(scene_id)  # 检查与加入之间无 await，asyncio 下原子
        try:
            return await self._run(scene, dict(ctx or {}))
        finally:
            self._running.discard(scene_id)

    async def run_queued(self, scene_ids: list[str],
                         ctx: dict | None = None) -> dict[str, bool | None]:
        """多场景同时命中时的仲裁入口：按 priority 降序逐个执行，单场景失败不中断整批。

        返回 {场景id: True 成功 / False 被冷却等条件跳过 / None 动作链异常}。
        """
        scenes = []
        for scene_id in scene_ids:
            scene = self._scenes.get(scene_id)
            if scene is None:
                raise SceneError(f"未知场景 id：{scene_id}（用 --list 查看已有场景）")
            scenes.append(scene)
        results: dict[str, bool | None] = {}
        for scene in sorted(scenes, key=lambda s: -s.priority):
            try:
                results[scene.id] = await self.trigger(scene.id, ctx,
                                                       source="arbiter")
            except Exception as e:
                # error 事件已由 _run 落库；这里只隔离，让剩余场景继续
                results[scene.id] = None
                print(f"[engine] 场景 {scene.id} 执行失败，继续下一个："
                      f"{type(e).__name__}: {e}")
        return results

    # ---------- 内部：条件 ----------

    def _eval_names(self, ctx: dict) -> dict:
        """表达式可用变量：StateStore 字段快照 + 动作链上下文（含 result）。"""
        s = self._state
        return {
            "occupancy": s.occupancy,
            "is_still": s.is_still,
            "speaking": s.speaking,
            "last_motion_ts": s.last_motion_ts,
            "medication_taken": dict(s.medication_taken),
            **ctx,
        }

    def _safe_eval(self, expr: str, ctx: dict) -> Any:
        """用 simpleeval 纯求值（禁用函数表）；失败时抛出含可用变量清单的 SceneError。"""
        names = self._eval_names(ctx)
        try:
            return SimpleEval(names=names, functions={}).eval(expr)
        except Exception as e:
            raise SceneError(
                f"表达式求值失败：{expr!r}（{type(e).__name__}: {e}）。"
                f"可用变量：{sorted(names)}") from None

    def _eval_condition(self, scene: Scene, cond: dict, ctx: dict) -> tuple[bool, dict]:
        """求值单个条件，返回 (是否通过, 诊断细节)。"""
        ctype = cond["type"]
        if ctype == "cooldown":
            seconds = cond.get("seconds")
            if seconds is None:
                seconds = self._default_cooldown_s
            remain = seconds - (time.time() - self._state.last_trigger.get(scene.id, 0.0))
            return remain <= 0, {"剩余冷却秒": round(max(remain, 0.0), 1)}
        if ctype == "time_window":
            now = datetime.now().time()
            after = _parse_hms(cond.get("after", "00:00:00"), "time_window.after")
            before = _parse_hms(cond.get("before", "23:59:59"), "time_window.before")
            if after <= before:
                ok = after <= now <= before
            else:  # 跨午夜窗口，如 22:00:00–06:00:00
                ok = now >= after or now <= before
            return ok, {"now": now.isoformat(timespec="seconds"),
                        "window": f"{after}~{before}"}
        if ctype == "template":
            return bool(self._safe_eval(str(cond["expr"]), ctx)), {"expr": cond["expr"]}
        raise SceneError(f"未知条件类型 {ctype!r}")  # 加载期已拦，这里是双保险

    async def _check_conditions(self, scene: Scene, ctx: dict) -> bool:
        for cond in scene.condition:
            ok, detail = self._eval_condition(scene, cond, ctx)
            self._log.log_event(scene.id, "condition",
                                {"cond": cond, "ok": ok, **detail})
            if not ok:
                return False
        return True

    # ---------- 内部：动作 ----------

    async def _run(self, scene: Scene, ctx: dict) -> bool:
        if not await self._check_conditions(scene, ctx):
            self._log.log_event(scene.id, "skipped", {"reason": "条件未全部通过"})
            return False
        try:
            await self._run_actions(scene, scene.action, ctx, depth=0)
        except Exception as e:
            self._log.log_event(scene.id, "error",
                                {"阶段": "action", "错误类型": type(e).__name__,
                                 "错误": str(e)[:500]})
            raise  # 落库后原样上抛，由 trigger 的调用方决定如何应对
        # 动作全部成功才算「成功触发」：写冷却（经 StateStore.set 发布状态事件），
        # 失败运行不记冷却，修复后可直接重试
        self._state.set("last_trigger",
                        {**self._state.last_trigger, scene.id: time.time()})
        self._log.log_event(scene.id, "done", {})
        return True

    async def _run_actions(self, scene: Scene, actions: list[dict],
                           ctx: dict, depth: int) -> None:
        if depth > _MAX_CHOOSE_DEPTH:
            raise SceneError(f"场景 {scene.id}: choose 嵌套超过 {_MAX_CHOOSE_DEPTH} 层")
        for act in actions:
            atype = act["type"]
            if atype == "skill":
                name = act["name"]
                fn = self._skills.get(name)
                if fn is None:
                    raise SceneError(
                        f"场景 {scene.id} 调用了未注册的技能 {name!r}"
                        f"（已注册：{sorted(self._skills)}）")
                args = dict(act.get("args") or {})
                self._log.log_event(scene.id, "action",
                                    {"type": "skill", "name": name, "args": args})
                result = fn(ctx, **args)
                if inspect.isawaitable(result):
                    result = await result
                ctx["result"] = result
                self._log.log_event(scene.id, "action_result",
                                    {"name": name, "result": result})
            elif atype == "speak":
                if "expr" in act and str(act.get("expr", "")).strip():
                    text = str(self._safe_eval(str(act["expr"]), ctx))
                else:
                    text = str(act.get("text", ""))
                self._log.log_event(scene.id, "action",
                                    {"type": "speak", "text": text})
                from skills.tts import speak as tts_speak  # 唯一发声出口（接口契约）
                await tts_speak(text)
            elif atype == "choose":
                matched = False
                for branch in act["branches"]:
                    if self._safe_eval(str(branch["when"]), ctx):
                        matched = True
                        await self._run_actions(scene, branch.get("do") or [],
                                                ctx, depth + 1)
                        break
                if not matched:
                    self._log.log_event(scene.id, "action",
                                        {"type": "choose", "结果": "无分支命中"})
