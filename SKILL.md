---
name: tmux-butler
description: >-
  Supervise and orchestrate heterogeneous multi-agent teams running across tmux windows (Claude Code, Codex, Devin, Pi, AGY).
  Use this skill when running unattended multi-agent pipelines to coordinate tasks via a shared blackboard, isolate work in temporary git worktrees,
  send structured ACOM messages, auto-unblock prompts with speed-first safety guards, and maintain continuous progress with linear git history.
---

# Tmux Butler (异构多 Agent 协同巡检操作系统)

`tmux-butler` 专为多模型、多窗口的 tmux 异构 Agent 协作网络设计。系统秉持 **“开发速度第一，安全底线兜底” (Velocity over Caution)** 的工程哲学，在保障系统不被不可逆毁灭的前提下，最大化无人值守流水线的吞吐效率：

1. **速度优先的安全底线 (Pragmatic Safety Guard)**：仅拦截 `rm -rf /`、强制推送生产分支等毁灭性操作。普通开发操作（文件删改、kill 进程、代码格式化、正常 force-with-lease）一律全自动确认通过，绝不拖慢流水线。
2. **0-Token 巡检与 300s 去重降噪**：本地轻量轮询监控进程状态与 CPU，5 分钟内同一事件不重复告警，历史日志自动轮转清理，零 Token 消耗、零告警轰炸。
3. **临时 Git Worktree 沙箱与严格线性历史**：为并发 Worker 提供隔离写目录，任务完成后通过 `squash` 或 `rebase` 自动合回主干并立即销毁临时分支，杜绝分支膨胀与多 Agent 写冲突。
4. **轻量 SQLite 任务黑板 (Blackboard)**：提供原子化认领 (`claim`)、状态流转 (`PENDING -> RUNNING -> TESTING -> DONE`) 与 <100 Token 极简摘要。
5. **ACOM 结构化跨窗口通信协议**：采用终端自适应渲染的结构化协议规范跨 Agent 任务下发、进度更新、完成交付与管家 Nudge。

---

## 模型能力与分工矩阵 (Model Hierarchy & Division of Labor)

| 模型 / 工具 | 智力等级与核心优势 | 成本与配额特性 | 最佳任务分工 |
| :--- | :--- | :--- | :--- |
| **Codex** (`GPT-6.1-Sol`) | ⭐⭐⭐⭐⭐ **高智商**<br>空间几何推理、3D 点云与视觉感知、位姿估计、运动规划 | 付费/高价值模型 | **空间感知与规划核心**<br>处理空间几何坐标变换、SAM3/Stereo 点云复现、位姿漂移归因与核心算法 |
| **Claude / Opus** (`Claude Code`) | ⭐⭐⭐⭐⭐ **高智商**<br>系统架构设计、全景代码审查、复杂边界推演 | 付费/高价值模型 | **总架构师与评审者**<br>负责全局方案设计、复杂漏洞挖掘、跨模块契约审查与最终技术把关 |
| **Devin** (`SWE-2 Max`) | ⭐⭐⭐☆ **中等智力**<br>工程实现能力合格，持久运行稳定性好 | 🎁 **免费不限量**<br>SWE-2 Max 无上限 | **高并发苦力与攻坚主力**<br>跑耗时测试套件、大批量代码机械重构、长任务执行、试错打杂（充分压榨免费配额） |
| **AGY / Butler** (`Gemini 3.8 Flash`) | ⭐⭐⭐☆ **中等智力**<br>极速响应、本地工具与 tmux 交互原生集成 | 低成本 / 极速 | **调度管家与环境桥梁**<br>执行 0-Token 巡检、黑板任务调度、工作区隔离、消息中继与破死锁引导 |

---

## 核心工具链与工作流

```
~/.gemini/config/skills/tmux-butler/scripts/
├── tmux_bridge.py        # 底层 Tmux I/O 与窗口生命周期管理
├── daemon.py             # 0-Token 守护进程与去重报警
├── blackboard.py         # SQLite 轻量任务黑板
├── workspace_manager.py  # Git Worktree 沙箱隔离与线性合并
└── protocol.py           # ACOM 结构化消息协议与终端渲染
```

### 1. 任务协同黑板 (Blackboard)

管理多 Agent 之间的任务派发与认领，避免任务冲突与遗忘：

```bash
# 创建任务
python3 ~/.gemini/config/skills/tmux-butler/scripts/blackboard.py create --title "重构点云配准异常处理" --issuer claude --assignee devin

# 查看所有待办与进行中任务
python3 ~/.gemini/config/skills/tmux-butler/scripts/blackboard.py list

# Worker 原子认领任务
python3 ~/.gemini/config/skills/tmux-butler/scripts/blackboard.py claim task-001 --assignee devin

# 更新状态与产出
python3 ~/.gemini/config/skills/tmux-butler/scripts/blackboard.py update task-001 --status DONE --note "12 项单元测试全部通过"

# 获取紧凑摘要（适合注入 LLM 上下文，< 100 Tokens）
python3 ~/.gemini/config/skills/tmux-butler/scripts/blackboard.py summary
```

### 2. 工作区隔离与线性 Git 历史 (Workspace Manager)

在多 Agent 并发开发时，既杜绝文件冲突踩踏，又保持 Git 主干提交历史清爽：

```bash
# 1. 为任务创建隔离沙箱（位于 ~/.tmux-butler-workspaces/task-001）
python3 ~/.gemini/config/skills/tmux-butler/scripts/workspace_manager.py create --task-id task-001 --repo /path/to/repo

# 2. Worker 在该独立目录下完成开发与验证后，自动 squash 合并回主干（保持线性历史）
python3 ~/.gemini/config/skills/tmux-butler/scripts/workspace_manager.py merge --task-id task-001 --message "feat: 完成点云配准重构"

# 3. 销毁临时 worktree 并删除临时分支（零分支残留）
python3 ~/.gemini/config/skills/tmux-butler/scripts/workspace_manager.py cleanup --task-id task-001

# 一键清理所有已合并的历史临时沙箱
python3 ~/.gemini/config/skills/tmux-butler/scripts/workspace_manager.py cleanup --all
```

### 3. ACOM 跨窗口结构化通信 (Protocol)

将上下文与任务元数据规范化传递给目标 Agent：

```bash
# 下发任务给 Devin
python3 ~/.gemini/config/skills/tmux-butler/scripts/protocol.py send \
  --type TASK_ASSIGN \
  --sender claude \
  --recipient devin \
  --task-id task-001 \
  --content "重构点云配准异常处理并补齐边界测试" \
  --metadata '{"working_dir": "/home/test/.tmux-butler-workspaces/task-001", "test_cmd": "pytest -v"}' \
  --target 0:devin

# 向卡住的 Agent 注入破死锁引导 (Nudge)
python3 ~/.gemini/config/skills/tmux-butler/scripts/protocol.py nudge --target 0:codex --reason "检测到静默超时且 CPU 空闲"

# 全局架构决议广播
python3 ~/.gemini/config/skills/tmux-butler/scripts/protocol.py broadcast --sender claude --content "全局约定：统一升级使用 NumPy 2.0 接口"
```

### 4. 无人值守守护进程 (Daemon)

```bash
# 后台启动守护进程（每 15 秒轮询，5 分钟同类去重抑制，自动清理旧日志）
# 必须加 -u（unbuffered），否则 stdout 块缓冲会让 daemon.log 长时间为空
nohup python3 -u ~/.gemini/config/skills/tmux-butler/scripts/daemon.py run --interval 15 > ~/.gemini/config/skills/tmux-butler/daemon.log 2>&1 &

# 快速查看当前巡检健康度（< 20 Tokens）
python3 ~/.gemini/config/skills/tmux-butler/scripts/daemon.py check --compact
```
