# tmux-butler

面向 tmux 中多个编程 Agent 的轻量协作工具：用 SQLite 跟踪任务，用 Git worktree 分离工作目录，用终端文本传递消息，并通过本地脚本检查窗口状态。

巡检脚本不调用模型 API；运行中的 Agent 仍可能产生模型费用。

[MIT License](LICENSE)

## 已实现的功能

| 工具 | 功能 |
| --- | --- |
| [`blackboard.py`](scripts/blackboard.py) | 任务创建、条件更新认领、状态流转、事件记录；SQLite WAL 存储 |
| [`workspace_manager.py`](scripts/workspace_manager.py) | 创建 worktree，显式执行 squash / rebase 合并与清理；遇到合并冲突保留工作目录 |
| [`protocol.py`](scripts/protocol.py) | 渲染、尽力解析和发送任务、进度、完成、转发、提醒与广播消息 |
| [`tmux_bridge.py`](scripts/tmux_bridge.py) | 捕获终端输出、识别部分提示符、发送文本、重命名窗口和启动新窗口 |
| [`daemon.py`](scripts/daemon.py) | 根据输出变化、提示符和进程活动报告疑似停滞；空闲类告警默认按窗口与类型去重 300 秒，保留最多 50 份历史报告 |

这些工具提供协作原语，任务拆分、模型选择和结果验收仍由使用者负责。

## 环境与安装

- Linux；可在 WSL2 的 Linux 环境中使用，需自行验证完整工作流
- Python 3.8 或更新版本；Python 代码只使用标准库，最低版本尚无 CI 覆盖
- tmux、Git；巡检使用 `ps`，安装 `pstree` 可帮助检查子进程活动
- Agent CLI 需另行安装。`daemon.py` 使用 `fcntl` 和 Linux 进程信息，原生 Windows 不支持该巡检流程

当前代码固定读取 `~/.gemini/config/skills/tmux-butler/` 下的配置和状态，**不会自动读取任意克隆目录里的 `config.json`**。以下命令假设目标目录不存在；已有安装时先检查内容，不要覆盖。

```bash
mkdir -p "$HOME/.gemini/config/skills"
git clone https://github.com/yechya/tmux-butler.git "$HOME/.gemini/config/skills/tmux-butler"
cd "$HOME/.gemini/config/skills/tmux-butler"
```

使用前编辑该目录的 `config.json`。建议新安装从以下配置开始：

```json
{
  "session_name": "butler-demo",
  "worker_prefix": "worker-",
  "idle_timeout_seconds": 60,
  "incident_cooldown_seconds": 300,
  "worker_launch_cmd": "bash",
  "auto_confirm_safe_prompts": false,
  "role_mappings": {}
}
```

`session_name` 应指向自己的 tmux 会话；`worker_launch_cmd` 换成已安装的 Agent 命令。角色名是窗口标签，不表示模型能力。建议对工作区命令显式传入 `--repo`，不沿用示例中的机器路径。

**自动确认默认值是 `true`，配置缺失或解析失败时也可能回退到该默认值。** 首次检查使用 `--read-only`；连续巡检前确认配置正确且自动确认已关闭。

## 快速使用

以下命令在安装目录执行。任务黑板、消息渲染不需要运行中的 tmux 会话。

### 1. 任务状态流转

```bash
python3 scripts/blackboard.py create --title "验证任务状态流转" --issuer planner

# 将 task-001 替换为 create 输出的实际任务 ID
python3 scripts/blackboard.py claim task-001 --assignee worker-1
python3 scripts/blackboard.py update task-001 --status RUNNING
python3 scripts/blackboard.py update task-001 --status DONE --note "示例任务结束"
python3 scripts/blackboard.py show task-001
python3 scripts/blackboard.py summary
```

重复认领会失败。常用路径为 `PENDING → CLAIMED → RUNNING → DONE`，也可使用 `TESTING`、`REVIEWING`、`BLOCKED` 等状态。`summary` 按活跃任务输出紧凑文本，长度随任务数量增长，没有固定 token 上限。

### 2. 消息预览与发送

```bash
# 仅预览，不向任何窗口发送按键
python3 scripts/protocol.py send \
  --type TASK_ASSIGN --sender planner --recipient worker-1 \
  --task-id demo-001 --content "检查示例项目并汇报结果" \
  --target butler-demo:worker-1 --dry-run
```

确认目标窗口正在运行预期的 Agent 后，移除 `--dry-run` 才会发送消息。不要向普通 shell 粘贴多行任务文本；脚本会拒绝常见 shell 的多行注入，但这不是完整的安全隔离。

### 3. 工作目录隔离

```bash
# 将 /path/to/repo 替换为实际的 Git 仓库路径
python3 scripts/workspace_manager.py create --task-id demo-001 --repo /path/to/repo

# 在返回的 worktree 中修改、验证并提交；确认两个工作目录均无未提交改动
python3 scripts/workspace_manager.py merge --task-id demo-001 --repo /path/to/repo \
  --message "Complete demo task"

# 合并成功、检查结果并确认没有新增的未提交文件后，再清理
python3 scripts/workspace_manager.py cleanup --task-id demo-001 --repo /path/to/repo
```

worktree 位于 `~/.tmux-butler-workspaces/<task-id>/`。默认 squash 合并，可用 `--strategy rebase`；清理是独立命令，不会在合并后自动执行。worktree 共享 Git 数据和用户权限，不是安全沙箱。

### 4. 检查已有 tmux 会话

```bash
python3 scripts/tmux_bridge.py status --session butler-demo
python3 scripts/daemon.py check --session butler-demo --read-only --compact

# 确认 config.json 中 auto_confirm_safe_prompts 为 false 后，连续巡检
python3 -u scripts/daemon.py run --session butler-demo --interval 15
```

`--read-only` 表示不向窗口发送按键，仍会写入巡检状态和事件日志。巡检只报告疑似异常；CPU 活动或终端静默不能单独证明任务成功、失败或死锁。

## 使用边界

- 提示符与危险操作识别依赖文本匹配，可能漏报或误报；不应作为生产系统的授权或安全边界
- 自动确认可能批准文件修改、进程终止等操作。仅在明确授权、可恢复的测试环境中考虑启用；部署、删除、推送等操作另行核实权限
- 消息解析为尽力恢复，不保证所有字段和类型无损往返；发送成功也不代表对方已执行任务
- 巡检面向每个 tmux 窗口的当前 pane，不是对所有 pane、Agent 或模型的完整监控
- 仓库目前没有端到端测试或性能基准，不承诺固定 token 开销、吞吐量或全面的 Agent CLI 兼容性

Agent 使用指引见 [`SKILL.md`](SKILL.md)，消息与人工介入示例见 [`references/prompts.md`](references/prompts.md)。
