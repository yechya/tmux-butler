---
name: tmux-butler
description: >-
  Coordinate coding agents across tmux windows with a SQLite task board, Git worktrees, terminal messages, and local status checks.
  Use when the user asks to coordinate parallel coding agents, inspect stalled tmux windows, relay task results, or manage temporary worktrees.
---

# tmux-butler：tmux 编程 Agent 协作工具

## 开始前

1. 按 [`README.md`](README.md) 检查 Linux、Python、tmux、Git 和固定配置目录；确认配置文件有效
2. 确认目标仓库、tmux 会话、窗口及用户授权范围；角色标签不表示模型能力
3. 保持 `auto_confirm_safe_prompts: false`，先用单次只读检查。配置缺失或解析失败时，代码可能回退到启用自动确认的默认值
4. worktree 只分离工作目录，不隔离权限；工具不能替代人工授权、代码评审或任务验收

以下命令使用安装目录；将会话、窗口和仓库变量换成实际目标：

```bash
BUTLER="$HOME/.gemini/config/skills/tmux-butler"
SESSION="butler-demo"
TARGET="$SESSION:worker-1"
REPO="/path/to/repo"
```

## 1. 检查窗口，不自动解锁

```bash
python3 "$BUTLER/scripts/tmux_bridge.py" status --session "$SESSION"
python3 "$BUTLER/scripts/daemon.py" check --session "$SESSION" --read-only --json
python3 "$BUTLER/scripts/tmux_bridge.py" capture --target "$TARGET" --lines 30
```

判据：目标窗口与进程符合预期，疑似停滞原因能从实际输出中说明。`--read-only` 不发送按键，仍写状态和日志；静默或 CPU 活动不足以证明死锁、失败或完成。

需要用户批准的操作，报告原命令、目标与影响，不代替用户确认。可用的提醒模板见 [`references/prompts.md`](references/prompts.md)。

## 2. 创建并认领任务

```bash
python3 "$BUTLER/scripts/blackboard.py" create --title "检查示例项目" --issuer planner

# 使用 create 输出的实际 ID，下面仅以 task-001 为例
python3 "$BUTLER/scripts/blackboard.py" claim task-001 --assignee worker-1
python3 "$BUTLER/scripts/blackboard.py" update task-001 --status RUNNING
python3 "$BUTLER/scripts/blackboard.py" show task-001
```

判据：任务由 `PENDING` 转为 `CLAIMED`，再转为 `RUNNING`；不要重复认领别人的任务。验证后才更新为 `DONE`，并记录实际产出；有阻塞时记录 `BLOCKED` 和原因。

```bash
python3 "$BUTLER/scripts/blackboard.py" update task-001 --status DONE --note "实际产出与验证结果"
python3 "$BUTLER/scripts/blackboard.py" summary
```

摘要按活跃任务增长，没有固定 token 上限。

## 3. 使用独立 worktree

```bash
python3 "$BUTLER/scripts/workspace_manager.py" create --task-id task-001 --repo "$REPO"
```

在返回的目录中修改、验证并提交；确认主仓库和 worktree 无未提交改动，再合并：

```bash
python3 "$BUTLER/scripts/workspace_manager.py" merge --task-id task-001 --repo "$REPO" \
  --message "Complete task-001"
```

判据：合并命令成功，检查合入内容与验证结果。冲突时保留 worktree，人工处理，不自动宣称任务完成。

确认没有新增的未提交文件后，显式清理：

```bash
python3 "$BUTLER/scripts/workspace_manager.py" cleanup --task-id task-001 --repo "$REPO"
```

不要用 `--force` 掩盖未合并或未保存的工作。合并不会自动清理 worktree，也不会自动推送远端。

## 4. 预览并发送任务消息

```bash
python3 "$BUTLER/scripts/protocol.py" send \
  --type TASK_ASSIGN --sender planner --recipient worker-1 \
  --task-id task-001 --content "检查示例项目并汇报实际结果" \
  --target "$TARGET" --dry-run
```

判据：预览中任务与目标正确，目标窗口运行预期 Agent，而不是普通 shell。满足这些条件后才移除 `--dry-run`；不要用 `--allow-shell` 或 `--include-shell` 绕过多行文本保护。

发送后通过 `capture` 检查对方输出。消息送达不等于任务已完成，文本解析也不保证所有字段无损往返。

## 5. 连续巡检

确认安装目录下的配置有效、自动确认关闭后：

```bash
python3 -u "$BUTLER/scripts/daemon.py" run --session "$SESSION" --interval 15
```

巡检脚本不调用模型 API，但 Agent 本身可能产生费用。告警是启发式结果；不要将危险关键词黑名单当作授权系统或安全保证。
