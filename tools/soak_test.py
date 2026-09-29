"""2 小时稳压测试（规范 M8）：循环跑感知管线，监控稳定性与资源。

用法：
    python tools/soak_test.py --video tests/media/smoke_none_01.mp4 --minutes 120
    python tools/soak_test.py --minutes 5              # 不传 --video = 实时摄像头

循环回放素材（或实时感知）直到时间预算耗尽，逐轮记录：轮次、单轮墙钟、
异常数、每轮结束后的进程 RSS（resource 高水位采样，KB）与事件循环任务数。
验收标准（规范 M8「2 小时稳压」）：全程零异常、RSS 无明显单调增长
（<20MB 漂移）、无卡死（每轮都能完成）。

姿态模型全程只加载一次（与 replay 同款注入）；每轮全新 StateStore/日志库，
隔离跨轮状态。测量代码不编造结论——跑完打报告，是否达标由你判。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import resource
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # 允许从任意 cwd 运行

from core import bus, config  # noqa: E402
from sensors.pose import PoseEstimator  # noqa: E402


def _rss_kb() -> int:
    """当前进程 RSS（KB）。

    优先用标准库 resource.getrusage(RUSAGE_SELF).ru_maxrss：无子进程依赖、
    在任何 Python 环境都可用，比 ps 采样可靠（ps 在受限环境可能缺失/被禁）。
    注意 ru_maxrss 单位随平台不同：macOS 是字节，Linux 是 KB，必须按平台
    换算，否则报告里的 MB 数值会差 1024 倍。
    ps 采样降级为 fallback，供极端环境（resource 被改写等）兜底。
    两级都失败才返回 0——0 表示「采样失败」，报告打印时不应当作真实 RSS
    参与泄漏判断（漂移基线/峰值都会失真）。
    """
    try:
        kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform == "darwin":
            return kb // 1024  # macOS 单位是字节，换算成 KB
        return kb  # Linux 单位已是 KB
    except Exception:
        pass
    try:
        out = subprocess.check_output(["ps", "-o", "rss=", "-p", str(os.getpid())])
        return int(out.strip())
    except Exception:
        return 0


async def _cycle(video: str | None, tmp: Path, idx: int) -> dict:
    """单轮：全新 StateStore/日志/感知回路跑一个视频（或实时直到被取消）。"""
    from core.log import EventLog
    from core.state import StateStore
    from sensors.perception import run_perception

    bus.clear_subscriptions()
    st = StateStore()
    log = EventLog(tmp / f"cycle{idx}.db")
    old_src = os.environ.get("VIDEO_SOURCE")
    if video is not None:
        os.environ["VIDEO_SOURCE"] = video
    try:
        stats = await run_perception(st=st, echo=False)
    finally:
        if old_src is None:
            os.environ.pop("VIDEO_SOURCE", None)
        else:
            os.environ["VIDEO_SOURCE"] = old_src
        log.close()
        bus.clear_subscriptions()
    return {"frames": stats.get("frames", 0),
            "pose_calls": stats.get("pose_calls", 0)}


def main() -> int:
    p = argparse.ArgumentParser(description="稳压测试：循环感知直到时间预算耗尽")
    p.add_argument("--video", default=None,
                   help="素材文件路径（缺省=实时摄像头，需 TCC 授权）")
    p.add_argument("--minutes", type=float, default=120.0,
                   help="运行时长（分钟），默认 120 = 规范 2 小时")
    p.add_argument("--max_cycles", type=int, default=0,
                   help="最多轮数（0=不限，默认按时间停）")
    args = p.parse_args()

    if args.video and not Path(args.video).exists():
        raise SystemExit(f"素材不存在：{args.video}")

    # 姿态模型只加载一次（循环复用）
    import sensors.perception as perc
    estimator = PoseEstimator()
    orig_cls = perc.PoseEstimator
    perc.PoseEstimator = lambda: estimator

    t_end = time.monotonic() + args.minutes * 60
    cycles = errors = 0
    cycle_times: list[float] = []
    rss_samples: list[int] = []
    err_samples: list[str] = []
    t_start = time.monotonic()

    print(f"稳压测试开始：video={args.video or '摄像头'} "
          f"预算 {args.minutes:.0f} 分钟（Ctrl+C 提前结束）")
    try:
        with tempfile.TemporaryDirectory(prefix="soak_") as td:
            tmp = Path(td)
            idx = 0
            while time.monotonic() < t_end:
                if args.max_cycles and idx >= args.max_cycles:
                    break
                idx += 1
                t0 = time.monotonic()
                try:
                    await_cycle = _cycle(args.video, tmp, idx)
                    info = asyncio.run(await_cycle)
                    cycles += 1
                    dt = time.monotonic() - t0
                    cycle_times.append(dt)
                    rss = _rss_kb()
                    rss_samples.append(rss)
                    # 0 = 采样失败（见 _rss_kb 注释），如实打印，不冒充 0MB
                    rss_txt = f"RSS {rss // 1024}MB" if rss > 0 else "RSS 采样失败"
                    print(f"  轮 {idx}: {dt:.1f}s，{info['frames']} 帧，"
                          f"姿态 {info['pose_calls']} 次，{rss_txt}",
                          flush=True)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    errors += 1
                    err_samples.append(f"{type(e).__name__}: {e}")
                    print(f"  ✗ 轮 {idx} 异常：{type(e).__name__}: {e}", flush=True)
    except KeyboardInterrupt:
        print("\n提前结束。")
    finally:
        perc.PoseEstimator = orig_cls

    elapsed = time.monotonic() - t_start
    print("\n================ 稳压报告 ================")
    print(f"运行 {elapsed / 60:.1f} 分钟，完成 {cycles} 轮，异常 {errors} 次")
    if cycle_times:
        print(f"单轮耗时：中位 {sorted(cycle_times)[len(cycle_times) // 2]:.1f}s，"
              f"最长 {max(cycle_times):.1f}s，最短 {min(cycle_times):.1f}s")
    # 0 值 = 采样失败（见 _rss_kb 注释），必须剔除：否则 rss_samples[0]=0
    # 时 drift = peak - 0 会把正常进程虚报成「可能泄漏」
    valid_rss = [r for r in rss_samples if r > 0]
    if len(valid_rss) >= 2:
        # 基线取首个有效样本（模型已常驻）：一次性模型加载不误报为泄漏
        base, peak = valid_rss[0], max(valid_rss)
        drift = peak - base
        print(f"RSS：首轮后 {base // 1024}MB，峰值 {peak // 1024}MB，"
              f"漂移 {drift / 1024:.1f}MB（{'⚠ 可能泄漏' if drift > 20 * 1024 else '正常'}）")
    elif cycles:
        print("RSS：采样不可用（有效样本不足 2 次），跳过泄漏判断")
    if err_samples:
        print("异常样本：")
        for s in err_samples[:5]:
            print(f"  - {s}")
    print("验收：异常 0 次 + RSS 漂移 <20MB + 每轮都完成 = 2 小时稳压通过")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
