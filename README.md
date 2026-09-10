# hermes-sync

通过 Syncthing 在两台电脑之间同步 Hermes 的会话记录、配置、记忆和 skills，让对话历史跟随你。

Two-machine Hermes state sync over Syncthing. Mirrors sessions, config, memory, and skills between two computers so conversation history follows you.

---

## 为什么不能直接同步 `state.db` / Why not sync `state.db` directly

Hermes 把会话存在 **WAL 模式** 的 SQLite 数据库里——实时写入进入 `state.db-wal`，而不是 `state.db`。把 `state.db` 交给一个忽略 SQLite 锁的文件同步引擎，会损坏数据库或静默丢失消息。

Hermes stores sessions in a SQLite database in **WAL mode** — live writes go to `state.db-wal`, not `state.db`. Handing `state.db` to a file sync engine that ignores SQLite locks corrupts the database or silently loses messages.

这个脚本用 `VACUUM INTO` 对活数据库做快照，生成一个自包含的 DELETE 模式副本，没有 `-wal`/`-shm` 残留。已验证：快照与活库的逻辑哈希一致。

This script snapshots the live DB with `VACUUM INTO`, producing a self-contained DELETE-mode copy with no `-wal`/`-shm` residue. Verified: the snapshot and the live DB share the same logical hash.

同步判定用**内容哈希**而非 mtime。跨机器时钟偏差会让 mtime 比较不安全——一个偏慢的本地时钟可能"看起来更新"，掩盖真正的分歧。哈希对 `sessions` 和 `messages` 按 `rowid` 排序后再加上 `schema_version` 取指纹，因此与时钟无关。

Sync decisions use a **content hash**, not mtime. Cross-machine clock skew makes mtime comparison unsafe — a slow local clock can look "newer" and mask a real divergence. The hash fingerprints `sessions` and `messages` ordered by `rowid` plus `schema_version`, so it is clock-independent.

---

## 同步判定（8 种状态）/ Sync decisions (8 states)

| 本机 Local | 远程 Remote | 基线 Baseline | 动作 Action |
|---|---|---|---|
| 有数据 data | 空 empty | — | push（初始化 init） |
| 哈希相同 same | 哈希相同 same | — | 无操作 no-op |
| 改过 changed | 未变 unchanged | 有 has | push |
| 未变 unchanged | 未变 unchanged | 有 has | 无操作 no-op |
| 未变 unchanged | 改过 changed | 有 has | **pull** |
| 改过 changed | 改过 changed | 有 has | **冲突 conflict** |
| 无数据库 no DB | 有数据 data | — | pull |
| 有数据 data | 有数据 data | 无基线 no baseline | **冲突 conflict** |

冲突时脚本**不会**自动合并——SQLite 无法在内容级合并而不丢消息。它会备份双方版本到 `hermes-sync-local/conflicts/`，并以退出码 2 结束。

On conflict the script does **not** auto-merge — SQLite can't be merged at the content level without losing messages. It backs up both versions to `hermes-sync-local/conflicts/` and exits with code 2.

---

## 文件结构 / Files

```
hermes-sync/
  hermes-sync.py        主同步脚本 / main sync script
  run-forever.py        5 分钟循环运行器 / 5-minute loop runner
  start-sync.bat        Windows 启动器（双击）/ launcher (double-click)
  setup-task.ps1        计划任务安装器（可选）/ scheduled-task installer
  README.md
  meta.json             提交指针：时间戳+机器+哈希 / commit pointer: ts+machine+hash (gitignored)
  state.db              VACUUM INTO 快照 / snapshot (gitignored)

hermes-sync-local/      本机状态，不同步 / per-machine, NOT synced
  sync.log  .last_seen  conflicts/
```

路径从当前用户主目录自动检测，同一份脚本可在任意账户上运行，无需编辑。

Paths are auto-detected from the current user home, so the same scripts run on any account without editing.

---

## 快速开始 / Quick start

```bash
python hermes-sync/run-forever.py                 # 运行 5 分钟循环 / run the 5-min loop
python hermes-sync/hermes-sync.py --status        # 查看建议动作 / check suggested action
python hermes-sync/hermes-sync.py --push          # 强制推送本机 / force push local
python hermes-sync/hermes-sync.py --pull          # 强制拉取（带冲突检查）/ force pull (conflict-checked)
```

Windows 上双击 `start-sync.bat`，或在 shell 里运行它。

On Windows, double-click `start-sync.bat`, or run it from a shell.

---

## 第二台机器 / Second machine

1. 正常安装 Hermes。/ Install Hermes normally.
2. 两台机器都安装 Syncthing 并配对设备。/ Install Syncthing on both machines and pair the devices.
3. 在 Syncthing 里双向共享 `hermes-sync/` 文件夹。/ Share the `hermes-sync/` folder both ways in Syncthing.
4. **关闭 Hermes**，然后运行 `start-sync.bat`。全新机器首次会提示冲突（没有 `.last_seen` 基线）。完全关闭 Hermes 后重试，基线随后建立。/ **Close Hermes**, then run `start-sync.bat`. A conflict prompt is expected on a fresh machine (no `.last_seen` baseline). Close Hermes fully and retry; the baseline is then established.
5. 让 `run-forever.py` 持续运行以实现自动同步。/ Leave `run-forever.py` running for automatic syncs.

---

## 两条规则 / Two rules

**pull 要求 Hermes 处于关闭状态。** push 在 Hermes 运行时是安全的（只读 + 快照）。pull 会在活进程下替换 `state.db`；后端的 WAL 句柄指向已删除的 inode，下次写入可能损坏数据库。脚本用进程检查硬拦截 pull——是拒绝，不是警告。

**pull requires Hermes to be closed.** push is safe while Hermes runs (read-only + snapshot). pull replaces `state.db` under a live process; the backend's WAL handle points at a deleted inode, so the next write can corrupt the DB. The script hard-gates pull on a process check — it refuses, it does not warn.

**`.env` 和 `auth.json` 默认同步** = 两台机器共享同一套 API keys 和 OAuth tokens。如果不想共享凭据，从 `hermes-sync.py` 顶部的 `SYNC_FILES` 里删掉它们。

**`.env` and `auth.json` are synced by default** = both machines share the same API keys and OAuth tokens. Remove them from `SYNC_FILES` at the top of `hermes-sync.py` if you don't want shared credentials.

---

## 已验证 / Verified

- 18/18 单元测试通过 / 18/18 unit tests pass (`hermes-sync-local/tests/test_sync.py`)
- 覆盖全部 8 种 `decide()` 状态转移 / All 8 `decide()` state transitions covered
- 内容哈希在插入/删除时变化，还原后恢复，与活库一致 / Content hash changes on insert/delete, restores on revert, matches live DB
- Hermes 运行时的 push 保持源数据库（含 FTS5）完好 / push while Hermes runs leaves the source DB (incl. FTS5) intact
- Hermes 运行时的 pull 被正确拦截 / pull while Hermes runs is correctly intercepted
- 冲突时备份双方数据库与文本文件，退出码 2 / conflicts back up both DBs and text files, exit code 2
