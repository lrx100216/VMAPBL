"""生成并安装 launchd 配置（LaunchAgent）。

必须使用 LaunchAgent（用户 GUI 会话）而不是 LaunchDaemon：AVFoundation /
麦克风在系统会话中不可用，主进程会在 import 阶段直接失败。

用法：
    python tools/install_launchd.py --dry-run
    python tools/install_launchd.py
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPLOY_DIR = ROOT / "deploy"
TEMPLATES = ("com.local.butler.guardian.plist",
             "com.local.butler.watchdog.plist")


def render_plist(template: str, values: dict[str, object]) -> str:
    """替换 __KEY__ 占位符；有残留即报错，防止装出一份坏配置。"""
    text = template
    for key, value in values.items():
        text = text.replace(f"__{key}__", str(value))
    leftover = sorted(set(re.findall(r"__[A-Z_]+__", text)))
    if leftover:
        raise ValueError(f"未替换的占位符：{leftover}")
    return text


def build_plists(project_dir: Path, python: Path, log_dir: Path) -> dict[str, str]:
    """返回 {plist 文件名: 内容}，不写盘、不加载。"""
    values = {
        "PROJECT_DIR": Path(project_dir).resolve(),
        "PYTHON": str(Path(python).resolve()),
        "LOG_DIR": Path(log_dir).resolve(),
    }
    result = {}
    for name in TEMPLATES:
        template = (DEPLOY_DIR / f"{name}.template").read_text(encoding="utf-8")
        result[name] = render_plist(template, values)
    return result


def install(plists: dict[str, str], dest: Path) -> list[Path]:
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    written = []
    for name, content in plists.items():
        path = dest / name
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="安装 AI 管家 launchd 配置")
    parser.add_argument("--project-dir", type=Path, default=ROOT)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--log-dir", type=Path, default=ROOT / "logs")
    parser.add_argument("--dest", type=Path,
                        default=Path.home() / "Library" / "LaunchAgents")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    plists = build_plists(args.project_dir, args.python, args.log_dir)
    if args.dry_run:
        for name, content in plists.items():
            print(f"===== {name} =====")
            print(content)
        return 0

    written = install(plists, args.dest)
    print("已写入：")
    for path in written:
        print(f"  {path}")
    print("\n加载/重载（在当前用户会话）：")
    print("  launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.local.butler.guardian.plist 2>/dev/null || true")
    print("  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.local.butler.guardian.plist")
    print("  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.local.butler.watchdog.plist")
    return 0


if __name__ == "__main__":
    sys.exit(main())
