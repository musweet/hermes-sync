#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Hermes Sync — 在两台电脑之间同步 Hermes 会话记录、配置、记忆、skills

设计目标：
  1. 安全   — 绝不损坏 state.db（SQLite WAL 数据库）
  2. 诚实   — 两边都改时不静默合并，备份双方版本让用户决定
  3. 自动   — 可放入 Windows 计划任务定时运行
  4. 简单   — 只用 Python 标准库，无需安装任何依赖

同步内容（HERMES_HOME 下）：
  state.db            会话记录（通过 VACUUM INTO 生成自包含快照）
  config.yaml         配置
  .env                API keys / tokens（注意：同步了 = 两台机器共享凭据）
  auth.json           OAuth tokens
  SOUL.md             人格定义（如果存在）
  memories/           MEMORY.md, USER.md
  cron/               计划任务
  skills/             用户自定义 skills

不同步：
  hermes-agent/       代码仓库（3.6 GB，每台机器自己装）
  cache/, logs/, sessions/, runtime/    机器本地状态
  backups/, state-snapshots/, checkpoints/, browser-profiles/
  *.lock, *.tmp, *.partial, *-wal, *-shm, *-journal   锁/临时文件

目录布局：
  <user-home>\\hermes-sync\\          Syncthing 共享（同步内容 + 本脚本）
  <user-home>\\hermes-sync-local\\    本机状态（不同步：日志、last_seen、冲突备份）

用法：
  python hermes-sync.py --status     查看本机与远程的状态对比
  python hermes-sync.py              自动判断 push / pull / 冲突
  python hermes-sync.py --push       强制把本机推送到同步文件夹
  python hermes-sync.py --pull       强制从同步文件夹拉取
  python hermes-sync.py --name 本机  指定本机机器名（标识提交来源）
  python hermes-sync.py -v           详细输出

退出码：
  0  成功（push / pull / 无操作）
  1  操作失败（数据库被锁、权限问题等）
  2  检测到冲突，已备份双方版本，需要人工处理
"""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# === 路径配置（自动检测当前用户名，两台机器都能用同一份脚本） ===
# 用 os.path.expanduser("~") 自动获取当前用户的主目录，这样无需手动改路径。
HOME = Path(os.path.expanduser("~"))
HERMES_HOME = HOME / "AppData" / "Local" / "hermes"
SYNC_DIR = HOME / "hermes-sync"         # Syncthing 共享
LOCAL_DIR = HOME / "hermes-sync-local"  # 本机状态，不同步

# 同步文件清单
SYNC_FILES = ["config.yaml", ".env", "auth.json", "SOUL.md"]
SYNC_DIRS = ["memories", "cron", "skills"]

# 不同步的目录名（出现在任意层级都跳过）
EXCLUDE_DIRS = {
    "hermes-agent", "cache", "logs", "sessions", "runtime",
    "backups", "state-snapshots", "checkpoints", "browser-profiles",
    "venv", ".venv", "node_modules", "__pycache__", ".git", ".archive",
}
# 不同步的文件后缀/模式
EXCLUDE_SUFFIXES = (".lock", ".tmp", ".partial")
EXCLUDE_WAL_SUFFIXES = ("-wal", "-shm", "-journal")

VERBOSE = False


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
def log(msg, level="INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level:5s}] {msg}"
    print(line, file=sys.stderr)
    try:
        LOCAL_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOCAL_DIR / "sync.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def vlog(msg):
    if VERBOSE:
        log(msg, "DEBUG")


def now_ms():
    return int(time.time() * 1000)


def fmt_time(ms):
    if not ms:
        return "n/a"
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 机器标识
# ---------------------------------------------------------------------------
def get_machine_id():
    """返回人类可读的机器标识。优先用 install_id 哈希，回退到 COMPUTERNAME。"""
    try:
        p = HERMES_HOME / "install_id"
        if p.exists():
            data = p.read_text(encoding="utf-8", errors="replace").strip()
            return "machine-" + hashlib.md5(data.encode("utf-8")).hexdigest()[:8]
    except Exception:
        pass
    name = os.environ.get("COMPUTERNAME", "unknown")
    return name[:16]


# ---------------------------------------------------------------------------
# meta.json 读写
# ---------------------------------------------------------------------------
def load_meta():
    p = SYNC_DIR / "meta.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            log(f"meta.json 读取失败: {e}", "WARN")
    return {"timestamp": 0, "machine": "", "stats": {}, "schema_version": 1}


def save_meta(meta):
    """原子写入：先写 .tmp 再 os.replace，避免半写状态被读到。"""
    tmp = SYNC_DIR / "meta.json.tmp"
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(SYNC_DIR / "meta.json"))


# ---------------------------------------------------------------------------
# last_seen 读写（本机上次拉取的时间戳，记录在 LOCAL_DIR 下，不同步）
# ---------------------------------------------------------------------------
def load_last_seen():
    """读取本机上次同步点：{"timestamp": ms, "hash": sha256, "machine": str}。

    hash 是当时本机 state.db 的逻辑内容哈希。这是判定"本机自上次同步后
    是否改过"的依据，比 mtime 可靠（mtime 会被 push/pull 复制文件污染）。
    """
    p = LOCAL_DIR / ".last_seen"
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        # 旧格式（纯时间戳数字）
        try:
            ts = int(p.read_text(encoding="utf-8").strip() or "0")
            return {"timestamp": ts, "hash": None, "machine": ""}
        except Exception:
            return {"timestamp": 0, "hash": None, "machine": ""}
    return {"timestamp": 0, "hash": None, "machine": ""}


def save_last_seen(timestamp, db_hash, machine):
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    data = {"timestamp": timestamp, "hash": db_hash, "machine": machine}
    (LOCAL_DIR / ".last_seen").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# 本机最新修改时间
# ---------------------------------------------------------------------------
def get_local_latest_ts():
    """返回本机 state.db 的最后修改时间（毫秒）。

    只用 state.db 的 mtime。不扫描 skills/memories 等目录，因为 push/pull
    复制这些文件会抬高本机 mtime，把 mtime 当成本机修改时间会造成每次
    运行都误判"本机领先"并反复 push。
    """
    db = HERMES_HOME / "state.db"
    if not db.exists():
        return 0
    return int(db.stat().st_mtime * 1000)


def decide(local_hash, remote_hash, remote_ts, last_seen):
    """判定应该执行的动作。

    返回: 'init' | 'push' | 'pull' | 'noop' | 'conflict'

    判定思路（哈希管内容，时间戳管顺序，二者互补）：
      - 内容哈希   → 判定"两边数据是否相同"，与时钟无关，时钟漂移也不会误判
      - last_seen  → 判定"自上次同步后本机是否改过"，用于检测冲突

    状态转移：
      1. 无提交           → init（push 初始化）
      2. 内容已一致        → noop
      3. 远程未变 + 本机变  → push
      4. 远程未变 + 本机未变 → noop
      5. 远程变了 + 本机未变 → pull（安全：本机改动为空）
      6. 远程变了 + 本机变了 → conflict（两边都改了，不能自动合并）
      7. 无基线 + 本机为空  → pull
      8. 无基线 + 本机有数据 → conflict（不猜，宁可让用户决定）
    """
    if remote_ts == 0:
        return "init"

    # 内容一致 → 无需操作（无论时间戳如何）
    if local_hash and remote_hash and local_hash == remote_hash:
        return "noop"

    # 远程提交时间 == 本机上次同步时间 → 远程没变
    if remote_ts == last_seen.get("timestamp", 0):
        if local_hash == last_seen.get("hash"):
            return "noop"
        return "push"

    # 远程变了（或 last_seen 为空）
    if last_seen.get("hash") is None:
        if not local_hash:
            return "pull"        # 本机无数据库，拉取安全
        return "conflict"        # 本机有数据但无基线，不猜

    if local_hash == last_seen.get("hash"):
        return "pull"            # 本机未变，远程变了 → 安全拉取
    return "conflict"            # 两边都改了


# ---------------------------------------------------------------------------
# state.db 操作
# ---------------------------------------------------------------------------
def snapshot_state_db(src, dst):
    """用 VACUUM INTO 生成自包含快照（DELETE 模式，无 -wal/-shm 残留）。

    实测验证：源库 WAL 模式，VACUUM INTO 后目标库为 DELETE 模式，
    sessions/messages 行数一致，目标旁边无 -wal/-shm 文件。
    """
    dst_path = str(dst)
    for suf in EXCLUDE_WAL_SUFFIXES:
        p = dst_path + suf
        if os.path.exists(p):
            os.unlink(p)

    con = sqlite3.connect(str(src), timeout=15)
    try:
        con.execute("VACUUM INTO ?", (dst_path,))
    except sqlite3.OperationalError as e:
        con.close()
        if "locked" in str(e).lower() or "busy" in str(e).lower():
            raise RuntimeError(
                "state.db 被占用，请先关闭 Hermes（CLI/TUI/桌面端）再重试"
            )
        raise
    con.close()

    # 验证快照可用
    chk = sqlite3.connect(dst_path)
    try:
        n = chk.execute("SELECT count(*) FROM sessions").fetchone()[0]
        m = chk.execute("SELECT count(*) FROM messages").fetchone()[0]
    finally:
        chk.close()
    return n, m


def db_stats(db_path):
    """只读方式获取数据库统计。"""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            s = con.execute("SELECT count(*) FROM sessions").fetchone()[0]
            m = con.execute("SELECT count(*) FROM messages").fetchone()[0]
            return {"sessions": s, "messages": m, "ok": True}
        finally:
            con.close()
    except Exception as e:
        return {"ok": False, "error": str(e)}


def db_logical_hash(db_path):
    """计算数据库的"逻辑内容"哈希（SHA-256）。

    按 rowid 稳定排序后取关键列，所以：
      - 同一内容 → 哈希相同（VACUUM INTO 快照与活库一致，已实测）
      - 内容变了（哪怕只多一条消息） → 哈希不同
      - 字节可能不同（page 分配顺序等）但逻辑内容相同 → 哈希仍相同

    这就是"跨机器内容是否一致"的判定基础，与时钟无关。
    """
    if not os.path.exists(db_path):
        return None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            h = hashlib.sha256()
            # 会话主键 + 关键元数据
            rows = con.execute(
                "SELECT id, started_at, ended_at, model, title, message_count "
                "FROM sessions ORDER BY rowid"
            ).fetchall()
            h.update(repr(rows).encode("utf-8"))
            # 消息 id/session_id/role/content 长度（长度而非全文：哈希足够区分，避免超长）
            rows2 = con.execute(
                "SELECT id, session_id, role, length(content) "
                "FROM messages ORDER BY rowid"
            ).fetchall()
            h.update(repr(rows2).encode("utf-8"))
            # schema 版本（跨机器版本不一致时视为不同）
            sv = con.execute("SELECT version FROM schema_version").fetchall()
            h.update(repr(sv).encode("utf-8"))
            return h.hexdigest()
        finally:
            con.close()
    except Exception as e:
        log(f"计算数据库哈希失败: {e}", "WARN")
        return None


# ---------------------------------------------------------------------------
# Hermes 进程检测（pull 安全防线）
# ---------------------------------------------------------------------------
# pull 会 os.replace() 替换 HERMES_HOME/state.db。如果 Hermes 后端还在运行，
# 它持有的 WAL 句柄指向的 inode 已被删除，新数据库文件与旧 WAL 不匹配，
# 下次写入会导致数据损坏或消息丢失。所以 pull 必须在 Hermes 关闭时执行。
# push 不替换本地数据库（只 VACUUM INTO 生成快照到同步文件夹），运行中安全。
def hermes_running():
    """检测 Hermes 后端进程是否正在运行。

    返回 (是否运行, 匹配到的命令行列表)。

    匹配规则（命令行参数精确匹配，避免误杀无关 python.exe）：
      - 'hermes_cli.main serve'   桌面端 gateway 后端（主要的 state.db 持有者）
      - 'hermes_cli.main gateway' CLI gateway
      - 'apps\\desktop\\release'   桌面端 Electron 壳
    """
    patterns = [
        "hermes_cli.main serve",
        "hermes_cli.main gateway",
        "apps\\\\desktop\\\\release",
    ]
    output = ""
    try:
        r = subprocess.run(
            ["wmic", "process", "where",
             "Name='python.exe' or Name='Hermes.exe'",
             "get", "ProcessId,CommandLine", "/format:csv"],
            capture_output=True, text=True, timeout=10,
            errors="replace",
        )
        output = r.stdout or ""
        if not output.strip():
            raise RuntimeError("wmic returned empty")
    except Exception:
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter "
                 "\"Name='python.exe' or Name='Hermes.exe'\" | "
                 "ForEach-Object { $_.ProcessId.ToString() + '|' + $_.CommandLine }"],
                capture_output=True, text=True, timeout=15,
                errors="replace",
            )
            output = r.stdout or ""
        except Exception as e:
            vlog(f"进程检测失败（无法确认 Hermes 是否在运行）: {e}")
            return False, []

    matched = []
    for line in output.splitlines():
        low = line.lower()
        if any(p.lower() in low for p in patterns):
            matched.append(line.strip()[:120])
    return len(matched) > 0, matched


# ---------------------------------------------------------------------------
# 文件复制（带排除规则）
# ---------------------------------------------------------------------------
def copy_file(src, dst):
    try:
        if dst.exists():
            dst.unlink()
        shutil.copy2(str(src), str(dst))
        vlog(f"  文件: {src.name}")
        return True
    except Exception as e:
        log(f"复制文件失败 {src}: {e}", "WARN")
        return False


def copy_dir(src, dst):
    """整目录复制，跳过排除目录和排除文件。"""
    def _ignore(path, names):
        skip = set()
        for n in names:
            if n in EXCLUDE_DIRS:
                skip.add(n)
            elif n.endswith(EXCLUDE_SUFFIXES):
                skip.add(n)
            elif any(n.endswith(s) for s in EXCLUDE_WAL_SUFFIXES):
                skip.add(n)
        return skip
    try:
        if dst.exists():
            shutil.rmtree(str(dst), ignore_errors=True)
        shutil.copytree(str(src), str(dst), ignore=_ignore)
        vlog(f"  目录: {src.name}/")
        return True
    except Exception as e:
        log(f"复制目录失败 {src}: {e}", "WARN")
        return False


# ---------------------------------------------------------------------------
# PUSH：把本机数据写入同步文件夹
# ---------------------------------------------------------------------------
def push(machine, local_latest):
    log(f"开始 push（machine={machine}）")
    SYNC_DIR.mkdir(parents=True, exist_ok=True)

    # 1. 清空同步内容（保留所有需要同步和永久驻留的文件）
    # 注意：keep 集合必须包含所有不应被清理的文件，否则会误删
    # - hermes-sync.py: 主同步脚本
    # - run-forever.py: 常驻运行器
    # - start-sync.bat: 启动器
    # - meta.json: 提交指针
    # - README.md: 使用说明
    # - setup-task.ps1: 计划任务安装脚本（备用）
    # - .stfolder: Syncthing 的文件夹标记文件，删了会导致 folder marker missing 错误
    # - .git: git 版本控制目录，删了会丢失提交历史
    # - .gitignore: git 忽略规则
    keep = {
        "hermes-sync.py",
        "run-forever.py",
        "start-sync.bat",
        "meta.json",
        "README.md",
        "setup-task.ps1",
        ".stfolder",
        ".git",
        ".gitignore",
    }
    for entry in SYNC_DIR.iterdir():
        if entry.name in keep:
            continue
        if entry.is_dir():
            shutil.rmtree(str(entry), ignore_errors=True)
        else:
            try:
                entry.unlink()
            except Exception:
                pass
    log("同步内容已清空")

    # 2. state.db 快照
    src_db = HERMES_HOME / "state.db"
    if not src_db.exists():
        log("state.db 不存在，跳过数据库快照", "WARN")
        return False
    log("生成 state.db 快照（VACUUM INTO）...")
    try:
        n_sess, n_msg = snapshot_state_db(str(src_db), str(SYNC_DIR / "state.db"))
    except RuntimeError as e:
        log(str(e), "ERROR")
        return False
    log(f"state.db 快照完成: {n_sess} 会话, {n_msg} 消息")

    # 3. 单文件
    for f in SYNC_FILES:
        src = HERMES_HOME / f
        if src.exists():
            copy_file(src, SYNC_DIR / f)

    # 4. 目录
    for d in SYNC_DIRS:
        src = HERMES_HOME / d
        if src.exists():
            copy_dir(src, SYNC_DIR / d)

    # 5. 写 meta.json（提交指针，最后写，保证原子性）
    # 记录本机 state.db 的逻辑内容哈希，供另一台机器比对
    local_hash = db_logical_hash(str(HERMES_HOME / "state.db"))
    meta = {
        "timestamp": now_ms(),
        "machine": machine,
        "db_hash": local_hash,
        "source_latest": local_latest,
        "stats": {
            "sessions": n_sess,
            "messages": n_msg,
            "files": [f for f in SYNC_FILES if (HERMES_HOME / f).exists()],
            "dirs": [d for d in SYNC_DIRS if (HERMES_HOME / d).exists()],
        },
        "schema_version": 2,
    }
    save_meta(meta)
    log(f"push 完成: 提交 {fmt_time(meta['timestamp'])}, db_hash={local_hash[:16] if local_hash else 'n/a'}")

    # 建立同步基线：push 后本机内容与同步文件夹一致，
    # 下次运行即可据此判定"本机是否又改过"
    save_last_seen(meta["timestamp"], local_hash, machine)
    return True


# ---------------------------------------------------------------------------
# PULL：从同步文件夹拉取数据
# ---------------------------------------------------------------------------
def pull(machine, local_latest):
    log(f"开始 pull（machine={machine}）")
    if not (SYNC_DIR / "meta.json").exists():
        log("同步文件夹为空，无数据可拉取")
        return "no-data"

    meta = load_meta()
    remote_ts = meta.get("timestamp", 0)
    remote_machine = meta.get("machine", "")
    remote_hash = meta.get("db_hash")
    last_seen = load_last_seen()

    # 冲突检测：pull 会覆盖本机数据，必须先确认本机自上次同步后没改过。
    # 用内容哈希判定，不用 mtime（mtime 会被 push/pull 复制文件污染）。
    #
    # 三种情况：
    #   有基线 + 本机变了   → conflict（本机改动会被覆盖丢失）
    #   无基线 + 本机有数据 → conflict（--pull 强制路径绕过 decide()，这里兜底）
    #   无基线 + 本机无库   → 安全，可拉取
    local_hash = db_logical_hash(str(HERMES_HOME / "state.db"))
    ls_hash = last_seen.get("hash")
    if ls_hash and local_hash != ls_hash:
        log(
            f"检测到冲突：本机自上次同步({fmt_time(last_seen.get('timestamp', 0))})后"
            f"内容已改变，与远程提交({fmt_time(remote_ts)})不同，无法安全覆盖",
            "WARN",
        )
        return "conflict"
    if not ls_hash and local_hash:
        log(
            "本机存在数据库但无同步基线（.last_seen 缺失），"
            "无法确认本机改动是否已同步，拒绝覆盖",
            "ERROR",
        )
        log("如确认要覆盖，请先备份本机数据后手动操作", "ERROR")
        return "conflict"

    log(f"应用远程提交 (from {remote_machine}, {fmt_time(remote_ts)}, hash={remote_hash[:16] if remote_hash else 'n/a'})")

    # 0. 安全防线：pull 会替换 state.db，Hermes 运行中做这个操作会损坏数据
    running, matched = hermes_running()
    if running:
        log("检测到 Hermes 仍在运行，拒绝 pull", "ERROR")
        log("原因：pull 会替换 state.db；Hermes 后端持有的 WAL 句柄会与新文件不匹配，", "ERROR")
        log("      下次写入可能损坏数据库或丢失消息。", "ERROR")
        log("请先关闭 Hermes 桌面端（托盘图标右键退出），再重试 pull。", "ERROR")
        for m in matched[:5]:
            log(f"  检测到进程: {m}", "DEBUG")
        return "hermes-running"

    # 1. 先备份本机 state.db 到 conflicts/，以防 pull 出问题
    conflict_dir = LOCAL_DIR / "conflicts"
    conflict_dir.mkdir(parents=True, exist_ok=True)
    backup_name = f"pre-pull-{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    backup_path = conflict_dir / backup_name
    src_db = HERMES_HOME / "state.db"
    if src_db.exists():
        try:
            con = sqlite3.connect(str(src_db), timeout=15)
            try:
                con.execute("VACUUM INTO ?", (str(backup_path),))
            finally:
                con.close()
            vlog(f"本机 state.db 已备份: {backup_path}")
        except Exception as e:
            log(f"本机备份失败（继续 pull）: {e}", "WARN")
            # 退而求其次用普通复制
            try:
                shutil.copy2(str(src_db), str(backup_path))
            except Exception:
                pass

    # 2. 替换 state.db（假设 Hermes 已关闭）
    remote_db = SYNC_DIR / "state.db"
    applied_db = False
    if remote_db.exists():
        try:
            os.replace(str(remote_db), str(src_db))
            for suf in EXCLUDE_WAL_SUFFIXES:
                p = str(src_db) + suf
                if os.path.exists(p):
                    os.unlink(p)
            applied_db = True
            log("state.db 已替换")
        except PermissionError:
            log("state.db 被占用，无法替换", "ERROR")
            log("请先关闭 Hermes（CLI/TUI/桌面端），再重试 pull", "ERROR")
            return "db-busy"
        except Exception as e:
            log(f"state.db 替换失败: {e}", "ERROR")
            return "db-error"

    # 3. 单文件
    for f in SYNC_FILES:
        src = SYNC_DIR / f
        dst = HERMES_HOME / f
        if src.exists():
            copy_file(src, dst)

    # 4. 目录
    for d in SYNC_DIRS:
        src = SYNC_DIR / d
        dst = HERMES_HOME / d
        if src.exists():
            copy_dir(src, dst)

    # 5. 记录 last_seen（时间戳 + 拉取后的本机内容哈希）
    pulled_hash = db_logical_hash(str(src_db)) or meta.get("db_hash")
    save_last_seen(remote_ts, pulled_hash, remote_machine)
    log(f"pull 完成: 已应用 {fmt_time(remote_ts)} (from {remote_machine}), db={'ok' if applied_db else 'skip'}")
    return "ok"


# ---------------------------------------------------------------------------
# 冲突处理：备份双方版本，不自动合并
# ---------------------------------------------------------------------------
def handle_conflict(machine, local_latest):
    log("=" * 50, "WARN")
    log("检测到冲突：两台机器在相同时间窗内都有修改，无法安全自动合并", "WARN")
    log("=" * 50, "WARN")
    log("正在备份双方版本，请你人工决定保留哪一个。", "WARN")
    log("说明：SQLite 数据库无法做内容级合并，自动合并会丢消息。", "WARN")

    conflict_dir = LOCAL_DIR / "conflicts"
    conflict_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # 1. 本机 state.db 快照
    src_db = HERMES_HOME / "state.db"
    if src_db.exists():
        local_db = conflict_dir / f"local-state-{ts}.db"
        try:
            con = sqlite3.connect(str(src_db), timeout=15)
            try:
                con.execute("VACUUM INTO ?", (str(local_db),))
            finally:
                con.close()
            log(f"本机数据库已备份: {local_db}")
            s = db_stats(str(local_db))
            if s.get("ok"):
                log(f"  本机版本: {s['sessions']} 会话, {s['messages']} 消息", "DEBUG")
        except Exception as e:
            log(f"本机数据库备份失败: {e}", "ERROR")

    # 2. 远程 state.db
    remote_db = SYNC_DIR / "state.db"
    if remote_db.exists():
        remote_copy = conflict_dir / f"remote-state-{ts}.db"
        try:
            shutil.copy2(str(remote_db), str(remote_copy))
            log(f"远程数据库已备份: {remote_copy}")
            s = db_stats(str(remote_copy))
            if s.get("ok"):
                log(f"  远程版本: {s['sessions']} 会话, {s['messages']} 消息", "DEBUG")
        except Exception as e:
            log(f"远程数据库备份失败: {e}", "ERROR")

    # 3. 文本文件两边都备份
    for f in SYNC_FILES:
        local_f = conflict_dir / f"local-{f.lstrip('.')}-{ts}"
        remote_f = conflict_dir / f"remote-{f.lstrip('.')}-{ts}"
        src = HERMES_HOME / f
        if src.exists():
            shutil.copy2(str(src), str(local_f))
        src = SYNC_DIR / f
        if src.exists():
            shutil.copy2(str(src), str(remote_f))

    log(f"冲突版本全部备份到: {conflict_dir}", "WARN")
    log("处理方式：", "WARN")
    log("  方式 A（保留远程）：python hermes-sync.py --pull --force", "WARN")
    log("  方式 B（保留本机）：关闭 Hermes 后从备份恢复，或重跑 --push", "WARN")
    log("  方式 C（人工合并）：手动比较两个 .db 文件后决定", "WARN")
    return "conflict"


# ---------------------------------------------------------------------------
# 状态显示
# ---------------------------------------------------------------------------
def show_status(machine):
    print(f"\n{'=' * 56}")
    print(f"  Hermes Sync 状态")
    print(f"{'=' * 56}")
    print(f"  本机机器名   : {machine}")
    print(f"  HERMES_HOME  : {HERMES_HOME}")
    print(f"  同步文件夹   : {SYNC_DIR}")
    print(f"  本机状态目录 : {LOCAL_DIR}")
    print()

    # 本机
    print(f"--- 本机 ---")
    db = HERMES_HOME / "state.db"
    if db.exists():
        print(f"  state.db     : {db.stat().st_size / 1048576:.2f} MB, mtime={fmt_time(int(db.stat().st_mtime*1000))}")
        s = db_stats(str(db))
        if s.get("ok"):
            print(f"  会话数       : {s['sessions']}")
            print(f"  消息数       : {s['messages']}")
        else:
            print(f"  数据库状态   : 异常 ({s.get('error')})")
    else:
        print("  state.db     : 不存在")

    local_latest = get_local_latest_ts()
    print(f"  本机最新修改 : {fmt_time(local_latest)}")
    print()

    # 远程
    print(f"--- 远程（同步文件夹）---")
    if SYNC_DIR.exists() and (SYNC_DIR / "meta.json").exists():
        meta = load_meta()
        print(f"  提交时间     : {fmt_time(meta.get('timestamp', 0))}")
        print(f"  提交机器     : {meta.get('machine', '?')}")
        s = meta.get("stats", {})
        print(f"  会话数       : {s.get('sessions', '?')}")
        print(f"  消息数       : {s.get('messages', '?')}")
        print(f"  同步文件     : {s.get('files', [])}")
        print(f"  同步目录     : {s.get('dirs', [])}")
    else:
        print("  同步文件夹为空（尚无提交）")
    print()

    # 上次同步
    last_seen = load_last_seen()
    print(f"--- 本机同步记录 ---")
    print(f"  上次同步时间 : {fmt_time(last_seen.get('timestamp', 0))}")
    ls_hash = last_seen.get("hash")
    print(f"  上次同步哈希 : {ls_hash[:16] + '...' if ls_hash else '无（首次同步前）'}")

    # 计算两端内容哈希，判定建议动作
    local_hash = db_logical_hash(str(HERMES_HOME / "state.db"))
    if (SYNC_DIR / "meta.json").exists():
        meta = load_meta()
        remote_ts = meta.get("timestamp", 0)
        remote_machine = meta.get("machine", "")
        remote_hash = meta.get("db_hash")
        print(f"  本机内容哈希 : {local_hash[:16] + '...' if local_hash else 'n/a'}")
        print(f"  远程内容哈希 : {remote_hash[:16] + '...' if remote_hash else 'n/a（旧版提交）'}")

        action = decide(local_hash, remote_hash, remote_ts, last_seen)
        hints = {
            "init": "→ 同步文件夹为空，执行 push 初始化",
            "noop": "✓ 内容已一致，无操作",
            "push": "→ 本机有新修改，应执行 push",
            "pull": "→ 远程有新修改，可执行 pull（pull 前请关闭 Hermes）",
            "conflict": "⚠ 冲突 — 两边都改了不同内容，需人工处理",
        }
        print(f"\n  建议动作     : {hints.get(action, action)}")
    else:
        print(f"\n  建议动作     : → 同步文件夹为空，执行 push 初始化")

    # 冲突文件
    conflict_dir = LOCAL_DIR / "conflicts"
    if conflict_dir.exists():
        files = list(conflict_dir.glob("*"))
        if files:
            print(f"\n  待处理冲突   : {len(files)} 个文件在 {conflict_dir}")
    print(f"{'=' * 56}\n")


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def main():
    global VERBOSE

    parser = argparse.ArgumentParser(
        description="Hermes 会话同步：在两台电脑之间同步会话记录、配置、记忆、skills",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python hermes-sync.py --status        查看状态\n"
            "  python hermes-sync.py                 自动判断 push/pull/冲突\n"
            "  python hermes-sync.py --push          强制推送\n"
            "  python hermes-sync.py --pull          强制拉取\n"
            "  python hermes-sync.py --name 本机     指定机器名\n"
            "  python hermes-sync.py -v              详细输出\n"
        ),
    )
    parser.add_argument("--push", action="store_true", help="强制推送本机数据")
    parser.add_argument("--pull", action="store_true", help="强制从远程拉取")
    parser.add_argument("--status", action="store_true", help="只显示状态，不做操作")
    parser.add_argument("--name", default=None, help="本机机器名（标识提交来源）")
    parser.add_argument("--force", action="store_true", help="强制操作，跳过冲突检查（仅 --pull 生效）")
    parser.add_argument("-v", "--verbose", action="store_true", help="详细输出")

    args = parser.parse_args()
    VERBOSE = args.verbose

    SYNC_DIR.mkdir(parents=True, exist_ok=True)
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)

    machine = args.name or get_machine_id()

    if args.status:
        show_status(machine)
        return 0

    local_latest = get_local_latest_ts()
    local_hash = db_logical_hash(str(HERMES_HOME / "state.db"))

    # 强制 push
    if args.push:
        ok = push(machine, local_latest)
        return 0 if ok else 1

    # 强制 pull
    if args.pull:
        result = pull(machine, local_latest)
        if result == "conflict":
            if args.force:
                log("--force 已指定，但仍检测到冲突，走备份流程", "WARN")
                handle_conflict(machine, local_latest)
                return 2
            handle_conflict(machine, local_latest)
            return 2
        if result == "hermes-running":
            return 1
        return 0 if result not in ("db-busy", "db-error") else 1

    # 自动判定（基于内容哈希 + last_seen，与时钟无关）
    meta = load_meta()
    remote_ts = meta.get("timestamp", 0)
    remote_machine = meta.get("machine", "")
    remote_hash = meta.get("db_hash")
    last_seen = load_last_seen()

    action = decide(local_hash, remote_hash, remote_ts, last_seen)

    if action == "init":
        log("同步文件夹为空，执行初始化 push")
        ok = push(machine, local_latest)
        return 0 if ok else 1

    if action == "noop":
        log("内容已一致，无操作")
        return 0

    if action == "push":
        log("本机有新修改，执行 push")
        ok = push(machine, local_latest)
        return 0 if ok else 1

    if action == "pull":
        log("远程有新修改，执行 pull")
        result = pull(machine, local_latest)
        if result == "conflict":
            handle_conflict(machine, local_latest)
            return 2
        if result == "hermes-running":
            return 1
        return 0 if result not in ("db-busy", "db-error") else 1

    if action == "conflict":
        handle_conflict(machine, local_latest)
        return 2

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        log(f"未捕获异常: {e}", "ERROR")
        import traceback
        traceback.print_exc()
        sys.exit(1)
