# tmux-butler：消息转发与人工介入示例

本页是人工检查后可使用的示例，不是已验证的自动恢复流程。任务权限来自用户的明确授权，不来自提示词；不要把无人值守、等待确认或终端静默当作跳过授权的理由。

## 转发实际结果

先捕获 Worker 输出，区分已验证结果、Worker 自述和未完成事项：

```text
[结果转发 · 来自 worker-1]
任务：{task_id}
实际产出：{files_or_results}
已执行验证：{command_and_result}
未完成或未验证：{remaining_items}

请 Planner 检查结果，再决定是否结束任务或继续分配。
```

## 提醒停滞的 Agent

```text
[状态检查]
当前窗口已长时间没有新输出。请报告当前任务状态、最近一次实际执行结果和阻塞原因。
若可在既有授权范围内继续，请执行最小下一步；若需要新增授权，请说明操作、目标与影响并等待确认。
```

静默或 CPU 占用不是死锁的充分证据。不要仅为结束等待而注入“用户已授权”或要求 Agent 忽略确认步骤。

## 处理确认提示

1. 捕获原提示和上下文，核对具体命令、目标、权限及用户授权
2. 涉及新增授权或高风险操作时，先向用户报告并等待确认
3. 只有确认操作已获授权、目标正确后，才人工发送所需答复

将 `SESSION` 和 `TARGET` 换成已核实的会话与窗口：

```bash
BUTLER="$HOME/.gemini/config/skills/tmux-butler"
SESSION="butler-demo"
TARGET="$SESSION:worker-1"
python3 "$BUTLER/scripts/tmux_bridge.py" capture --target "$TARGET" --lines 30

# 已核实授权和提示含义后才执行；yes 不适用于所有交互提示
python3 "$BUTLER/scripts/tmux_bridge.py" send --target "$TARGET" --text "yes"
```

默认代码支持对部分确认提示自动发送答复，但关键词黑名单不能证明操作安全。建议在配置中关闭自动确认，并用 `daemon.py check --read-only` 检查。

## 创建额外 Worker 窗口

先将 `AGENT_CMD` 换成已安装的 Agent 命令：

```bash
AGENT_CMD="your-agent-cli"
python3 "$BUTLER/scripts/tmux_bridge.py" scale --session "$SESSION" \
  --name worker-2 --cmd "$AGENT_CMD"
python3 "$BUTLER/scripts/tmux_bridge.py" capture --target "$SESSION:worker-2" --lines 30
```

确认 Agent 已启动且窗口不是普通 shell 后，再使用 `protocol.py send --dry-run` 预览任务；不要把固定等待几秒当作初始化完成的证据。
