#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Hermes Sync 常驻运行脚本

用法：在终端里运行本脚本，它会每 5 分钟自动执行一次 hermes-sync.py。
关闭窗口或按 Ctrl+C 即停止。

    python hermes-sync/run-forever.py

优点：
  - 不依赖 Windows 计划任务（计划任务的 Enabled 属性在某些环境下打不开）
  - 实时看到同步日志，出问题一目了然
  - 随时可停，Ctrl+C 干净退出

如果希望开机自动启动，把这个命令行加到
"设置 -> 应用 -> 启动" 或放在 shell:startup 文件夹里：

    pythonw hermes-sync/run-forever.py

（pythonw 不弹窗口，静默运行）
"""
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# 路径配置（用 __file__ 自动定位，两台机器都能用同一份脚本）
# 脚本自己就在 hermes-sync/ 下，所以 SCRIPT 和 SYNC_DIR 直接取它所在目录
SYNC_DIR = Path(__file__).resolve().parent
SCRIPT = SYNC_DIR / "hermes-sync.py"
LOCAL_DIR = SYNC_DIR.parent / "hermes-sync-local"
PYTHON = sys.executable  # 用当前解释器，保证环境一致
INTERVAL_SECONDS = 5 * 60  # 5 分钟
LOG_FILE = LOCAL_DIR / "sync.log"

BANNER = r"""
============================================================
  Hermes Sync -- 常驻同步
============================================================
  脚本   : {script}
  解释器 : {python}
  间隔   : {interval} 秒
  日志   : {log}
============================================================
  按 Ctrl+C 停止
============================================================
""".format(script=SCRIPT, python=PYTHON, interval=INTERVAL_SECONDS, log=LOG_FILE)


def log_line(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def run_sync():
    """执行一次同步，返回退出码。"""
    cmd = [PYTHON, str(SCRIPT)]
    log_line(f"开始同步: {' '.join(cmd)}")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        rc = r.returncode
        # 把子进程的输出写入日志
        for line in (r.stdout or "").splitlines():
            log_line("  sync> " + line)
        for line in (r.stderr or "").splitlines():
            log_line("  sync> " + line)
        rc_map = {0: "OK", 1: "失败", 2: "冲突"}
        log_line(f"同步结束: exit={rc} ({rc_map.get(rc, '未知')})")
        return rc
    except subprocess.TimeoutExpired:
        log_line("同步超时（>120s），强制中断")
        return 1
    except Exception as e:
        log_line(f"同步异常: {e}")
        return 1


def main():
    print(BANNER, flush=True)
    log_line("常驻脚本启动")

    # 首次立即运行一次，不等 5 分钟
    run_sync()

    while True:
        log_line(f"等待 {INTERVAL_SECONDS} 秒后再次同步...")
        # 分段睡眠，方便 Ctrl+C 快速响应
        for _ in range(INTERVAL_SECONDS):
            time.sleep(1)
        run_sync()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已停止（Ctrl+C）", flush=True)
        log_line("常驻脚本已停止")
        sys.exit(0)
