#!/usr/bin/env python3
"""安装或卸载当前用户的 macOS launchd 定时任务。"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape


APP_DIR = Path(__file__).resolve().parent
LABEL = "com.wca.new-competition-notifier"
TARGET = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
TEMPLATE = APP_DIR / f"{LABEL}.plist.template"
RUNTIME_DIR = Path.home() / "Library" / "Application Support" / "WCA Watch"


def launchctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["launchctl", *args], text=True, check=check, capture_output=True)


def uninstall(quiet: bool = False) -> None:
    domain = f"gui/{subprocess.check_output(['id', '-u'], text=True).strip()}"
    launchctl("bootout", domain, str(TARGET), check=False)
    if TARGET.exists():
        TARGET.unlink()
    if not quiet:
        print(f"[OK] 已卸载 {LABEL}")


def install(skip_test_email: bool = False) -> None:
    env_path = APP_DIR / ".env"
    if not env_path.exists():
        raise RuntimeError("请先复制 .env.example 为 .env，并填写邮件配置")

    # launchd 对外置卷可能没有访问权限，将最小运行副本放到用户资源库。
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    for name in ("wca.py", "run.sh", ".env"):
        shutil.copy2(APP_DIR / name, RUNTIME_DIR / name)
    (RUNTIME_DIR / "run.sh").chmod(0o755)
    (RUNTIME_DIR / ".env").chmod(0o600)
    runtime_state = RUNTIME_DIR / ".wca_seen.json"
    if not runtime_state.exists() and (APP_DIR / ".wca_seen.json").exists():
        shutil.copy2(APP_DIR / ".wca_seen.json", runtime_state)

    # 安装前先验证配置；正常检查会保留已有游标，首次使用时才建立基线。
    if not skip_test_email:
        subprocess.run([sys.executable, str(RUNTIME_DIR / "wca.py"), "--test-email"], check=True)
    subprocess.run([sys.executable, str(RUNTIME_DIR / "wca.py")], check=True)

    replacements = {
        "__RUN_SCRIPT__": str(RUNTIME_DIR / "run.sh"),
        "__WORK_DIR__": str(RUNTIME_DIR),
        "__STDOUT_LOG__": str(RUNTIME_DIR / "wca-watch.log"),
        "__STDERR_LOG__": str(RUNTIME_DIR / "wca-watch-error.log"),
    }
    content = TEMPLATE.read_text(encoding="utf-8")
    for key, value in replacements.items():
        content = content.replace(key, escape(value))

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    uninstall(quiet=True)
    TARGET.write_text(content, encoding="utf-8")

    domain = f"gui/{subprocess.check_output(['id', '-u'], text=True).strip()}"
    launchctl("bootstrap", domain, str(TARGET))
    print("[OK] 已安装：每 30 分钟检查一次 WCA 新公告。")
    print(f"[INFO] 任务文件：{TARGET}")
    print(f"[INFO] 运行目录：{RUNTIME_DIR}")


def main() -> int:
    parser = argparse.ArgumentParser(description="安装 WCA Watch 的 macOS 定时任务")
    parser.add_argument("--uninstall", action="store_true", help="卸载定时任务")
    parser.add_argument("--skip-test-email", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        uninstall() if args.uninstall else install(skip_test_email=args.skip_test_email)
        return 0
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
