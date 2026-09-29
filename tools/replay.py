"""离线回放统计工具：逐场景跑 N 次预录视频，出准确率统计表（规范 M5/M8 验证手段）。

用法（素材文件名必须带真实标签，规范第八节）：
    python tools/replay.py door_key --expect out tests/media/door_out_*.mp4 --repeat 5
    python tools/replay.py get_up --expect turn_over tests/media/turn_over_*.mp4 --repeat 20
    python tools/replay.py wake_up --expect in_bed tests/media/wakeup_inbed_*.mp4
    python tools/replay.py fall --expect fall tests/media/fall_real_*.mp4 --repeat 20
    python tools/replay.py pillbox --expect present tests/media/pillbox_real_*.mp4 --repeat 20

语义：
- door_key / get_up / fall 是触发驱动场景：每次回放完整走 真实管线
  （Camera 文件回放 → MOG2 → pose → 事件 → 引擎），监听该场景技能的判定结果，
  首个结果就是本次回放的判定（冷却本来就会挡住后续重复触发）；
- wake_up 是定时场景：回放完直接调 actions/wakeup.classify_wakeup 的三分类
  （与场景动作同一个函数），与期望标签比对；
- pillbox 是检测器场景：回放视频逐秒采样，跑 5 帧滑窗 ≥3 投票判「药盒在场/不在场」
  （生产两阶段的阶段一；阶段二手腕投票依赖实时姿态与构图，以实测为准）。
  需要已训练权重 models/pillbox.pt（tools/train_pillbox.py 产出）；
- 每次回放都是全新 StateStore/相机/日志库（冷却不跨次泄漏），姿态模型全程
  只加载一次（PoseEstimator 跨次复用，20 次回放只付一次加载）；
- 按文件帧率定速回放，墙钟耗时 ≈ 素材时长 × 次数——这是保真代价，不要加速。
- 回放期间 TTS 静音、STT 打桩（不真开腔不真录音）、notify 打桩（只计数不推送）。

结果表打印到 stdout，明细 JSON 存 data/replay_<场景>_<时间戳>.json（data/ 不入库）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from collections import deque
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # 允许从任意 cwd 运行

from core import bus, config  # noqa: E402
from sensors.pose import PoseEstimator  # noqa: E402  （轻量导入；模型懒加载）

# 触发驱动场景：技能名 → 该技能的返回值即「本次回放的判定」
_DECIDE_SKILL = {"door_key": "door_decision", "get_up": "getup_decide",
                 "fall": "fall_decide"}
# _run_once 认领的判定值（出现在 action_result 里就算一次判定）
_KNOWN_RESULTS = {"out", "in", "getup", "turn_over", "uncertain", "fall", "no_fall"}


async def _run_once(scene_id: str, scene_yaml: Path, video: Path,
                    tmp: Path, run_idx: int) -> dict:
    """单次回放：全套真管线跑完一个视频，返回 {predicted, detail}。"""
    from actions import SKILLS, wakeup
    from core.log import EventLog
    from core.scene_engine import SceneEngine
    from core.state import StateStore
    from sensors import perception
    from sensors.perception import run_perception

    bus.clear_subscriptions()  # 本工具是测量 harness，逐次回放必须隔离订阅
    st = StateStore()
    log = EventLog(tmp / f"run{run_idx}.db")

    if scene_id == "fall":
        from actions import fall as _fall
        _fall.reset_stop()  # 防重复静默是模块级进程态，跨回放必须清零

    # 只装目标场景：别的场景（语音触发等）不该在回放统计里抢事件
    scenes_dir = tmp / "scenes"
    scenes_dir.mkdir(exist_ok=True)
    shutil.copy(scene_yaml, scenes_dir / scene_yaml.name)
    engine = SceneEngine(scenes_dir, st, log, skills=SKILLS)

    # 用落库钩子当探针：技能返回值都会写 action_result 事件（M2 语义）
    results: list = []
    orig_log_event = log.log_event

    def spy(sid, kind, detail):
        if kind == "action_result" and isinstance(detail, dict):
            results.append(detail.get("result"))
        return orig_log_event(sid, kind, detail)
    log.log_event = spy

    occupancy_ons = 0

    async def count_occupancy(e):
        nonlocal occupancy_ons
        if (e.get("payload") or {}).get("new") is True:
            occupancy_ons += 1
    bus.subscribe("state.occupancy", count_occupancy)

    engine.wire_state_triggers()
    engine.wire_event_triggers()

    hist_holder: dict = {}
    orig_register = perception.register_current

    def capture(hist):
        hist_holder["hist"] = hist  # finally 会注销全局，对象本身留下来供统计
        orig_register(hist)
    perception.register_current = capture
    old_src = os.environ.get("VIDEO_SOURCE")
    os.environ["VIDEO_SOURCE"] = str(video)  # 视频源经环境变量注入（规范第八节）
    try:
        await run_perception(
            camera=None,  # 从 settings+VIDEO_SOURCE 构造，回放与实时同一代码路径
            st=st, echo=False)
        await asyncio.sleep(0.3)  # 让 publish_nowait 的派发任务跑完
        if scene_id == "wake_up":
            predicted = wakeup.classify_wakeup(hist=hist_holder.get("hist"))
        else:
            predicted = "none"
            for r in results:
                if r in _KNOWN_RESULTS:
                    predicted = r
                    break
    finally:
        perception.register_current = orig_register
        if old_src is None:
            os.environ.pop("VIDEO_SOURCE", None)
        else:
            os.environ["VIDEO_SOURCE"] = old_src
        log.close()
        bus.clear_subscriptions()
    return {"predicted": predicted,
            "detail": {"decision_results": [str(r) for r in results],
                       "occupancy_ons": occupancy_ons,
                       "pose_history": len(hist_holder.get("hist") or [])}}


# 测试缝隙：单测用假实现替换它，不必为聚合逻辑跑真管线
_RUN_ONCE = _run_once


def run_replay(scene_id: str, videos: list[Path], expect: str,
               repeat: int = 1) -> dict:
    """逐素材 × repeat 次回放（每次独立子进程级隔离：本进程内逐次串行）。

    返回统计 dict：{scene, expect, runs: [{video, run, predicted, correct}], ...}
    """
    scene_yaml = ROOT / "scenes" / f"{scene_id}.yaml"
    if not scene_yaml.exists():
        raise SystemExit(f"场景文件不存在：{scene_yaml}")
    if scene_id not in _DECIDE_SKILL and scene_id != "wake_up":
        raise SystemExit(f"replay.py 目前支持 "
                         f"{sorted(_DECIDE_SKILL) + ['wake_up', 'pillbox']}，"
                         f"收到 {scene_id!r}（pillbox 走单独的检测器回放）")

    # 跨次复用同一个 PoseEstimator（模型常驻，20 次回放只加载一次）
    import sensors.perception as perc
    estimator = PoseEstimator()

    # 回放期间静音：测量目标是「判定准确率」不是发音/录音/推送——用 conftest
    # 同款缝隙替换 tts/stt/notify，否则 20 次回放会让电脑念 20 遍、录 20 遍、
    # 往日志里写 20 遍提醒（fall 场景的二次确认会触发这些）
    from skills import tts as _tts
    from skills import stt as _stt
    from skills import notify as _notify
    orig_runner = _tts._runner
    orig_listen = _stt.listen
    orig_send = _notify.send
    spoken: list[str] = []
    notifications: list[dict] = []

    async def _muted(text: str) -> None:
        spoken.append(text)
    _tts._runner = _muted

    async def _silent_listen(*a, **kw) -> str:
        return ""  # 二次确认听不到回答 → 会走 notify 分支，正好测量该路径
    _stt.listen = _silent_listen

    async def _counting_send(msg, urgent=False, **kw) -> int:
        notifications.append({"msg": msg, "urgent": urgent})
        return 1  # 模拟落库成功（不真写库）
    _notify.send = _counting_send

    runs = []
    t0 = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="replay_") as td:
            tmp = Path(td)
            idx = 0
            for video in videos:
                for r in range(repeat):
                    idx += 1
                    # 把共享姿态估计器注入本次回放（monkeypatch 构造点）
                    orig_cls = perc.PoseEstimator
                    perc.PoseEstimator = lambda: estimator
                    try:
                        outcome = asyncio.run(_RUN_ONCE(
                            scene_id, scene_yaml, video, tmp, idx))
                    finally:
                        perc.PoseEstimator = orig_cls
                    outcome.update({
                        "video": video.name, "run": r + 1,
                        "correct": outcome["predicted"] == expect})
                    runs.append(outcome)
                    mark = "✅" if outcome["correct"] else "❌"
                    print(f"  {mark} {video.name} 第{r + 1}次 → "
                          f"{outcome['predicted']}（期望 {expect}）", flush=True)
    finally:
        _tts._runner = orig_runner
        _stt.listen = orig_listen
        _notify.send = orig_send
    correct = sum(1 for x in runs if x["correct"])
    return {"scene": scene_id, "expect": expect, "runs": runs,
            "total": len(runs), "correct": correct,
            "accuracy": round(correct / len(runs), 4) if runs else 0.0,
            "spoken": len(spoken),
            "notifications": len(notifications),
            "wall_s": round(time.monotonic() - t0, 1)}


# ---------- pillbox 检测器回放（阶段一：药盒在场 5 帧滑窗 ≥3 投票） ----------

def _pillbox_pass_vote(detections: list[bool], vote_frames: int = 5,
                       vote_min: int = 3) -> bool:
    """5 帧滑动窗口 ≥3 命中（与生产 actions/medication 的 _vote_pass 同语义）。"""
    win: deque[bool] = deque(maxlen=vote_frames)
    for hit in detections:
        win.append(hit)
        if len(win) == vote_frames and sum(win) >= vote_min:
            return True
    return False


def run_pillbox_replay(videos: list[Path], expect: str,
                       repeat: int = 1, sample_s: float = 1.0) -> dict:
    """药盒检测器回放：逐秒采样跑检测 + 滑窗投票，判「present / absent」。

    对应生产两阶段的阶段一（先确认药盒在场）；阶段二（手腕近盒）依赖实时
    姿态与构图，以实测为准。需要 models/pillbox.pt（tools/train_pillbox.py）。
    """
    from sensors.camera import Camera
    from sensors.detect import PillboxDetector

    detector = PillboxDetector()
    if not detector.available():
        raise SystemExit(
            "药盒权重 models/pillbox.pt 不存在：先 tools/collect_pillbox_frames.py "
            "采集标注，再 tools/train_pillbox.py 训练产出")

    runs = []
    t0 = time.monotonic()
    for video in videos:
        for r in range(repeat):
            detections: list[bool] = []
            cam = Camera(str(video))   # 同一代码路径：文件按自身帧率推进
            cam.start()
            try:
                last_sample = float("-inf")
                deadline = time.monotonic() + 600
                while time.monotonic() < deadline:
                    latest = cam.latest()
                    if latest is None:
                        if cam.eof:
                            break
                        time.sleep(0.01)
                        continue
                    ts, frame = latest
                    if ts - last_sample >= sample_s:   # 对齐生产 sample_s 采样节奏
                        detections.append(bool(detector.detect(frame)))
                        last_sample = ts
                    if cam.eof:
                        break
            finally:
                cam.stop()
            predicted = "present" if _pillbox_pass_vote(detections) else "absent"
            correct = predicted == expect
            runs.append({"video": video.name, "run": r + 1,
                         "predicted": predicted, "correct": correct,
                         "samples": len(detections),
                         "detail": {"votes": [str(bool(x)) for x in detections]}})
            mark = "✅" if correct else "❌"
            print(f"  {mark} {video.name} 第{r + 1}次 → {predicted}"
                  f"（期望 {expect}，{len(detections)} 个采样）", flush=True)
    n_ok = sum(1 for x in runs if x["correct"])
    return {"scene": "pillbox", "expect": expect, "runs": runs,
            "total": len(runs), "correct": n_ok,
            "accuracy": round(n_ok / len(runs), 4) if runs else 0.0,
            "wall_s": round(time.monotonic() - t0, 1)}


def main() -> int:
    p = argparse.ArgumentParser(description="离线回放：逐场景跑 N 次出准确率统计表")
    p.add_argument("scene", help="场景 id（door_key / get_up / wake_up / fall / pillbox）")
    p.add_argument("videos", nargs="+", type=Path, help="带真实标签的素材文件")
    p.add_argument("--expect", required=True,
                   help="期望判定：door_key=out|in|none；get_up=getup|turn_over|uncertain|none；"
                        "wake_up=in_bed|out_of_bed|unknown；fall=fall|no_fall|uncertain|none；"
                        "pillbox=present|absent")
    p.add_argument("--repeat", type=int, default=1, help="每个素材重复次数")
    args = p.parse_args()

    videos = []
    for v in args.videos:
        if not v.exists():
            raise SystemExit(f"素材不存在：{v}")
        videos.append(v)

    print(f"回放场景 {args.scene}：{len(videos)} 个素材 × {args.repeat} 次，"
          f"期望判定 {args.expect!r}（按文件帧率定速，墙钟≈素材总时长）")
    if args.scene == "pillbox":
        stats = run_pillbox_replay(videos, args.expect, args.repeat)
    else:
        stats = run_replay(args.scene, videos, args.expect, args.repeat)

    # 汇总表：按素材分组
    print("\n================ 回放统计 ================")
    by_video: dict[str, list] = {}
    for x in stats["runs"]:
        by_video.setdefault(x["video"], []).append(x)
    for name, xs in by_video.items():
        ok = sum(1 for x in xs if x["correct"])
        print(f"{name:40s} {ok}/{len(xs)} 正确")
    print("------------------------------------------")
    print(f"总计：{stats['correct']}/{stats['total']} 正确，"
          f"准确率 {stats['accuracy']:.1%}，"
          f"播报 {stats.get('spoken', 0)} 次（已静音），"
          f"通知 {stats.get('notifications', 0)} 次（已打桩），"
          f"墙钟 {stats['wall_s']}s")

    out = ROOT / "data" / f"replay_{args.scene}_" \
        f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(stats, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"明细已存：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
