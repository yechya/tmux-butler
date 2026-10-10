# tmux-butler

> **Heterogeneous Multi-Agent Swarm Orchestration System for tmux**  
> 专为多模型、多窗口的 tmux 异构 Agent 协作网络设计的无人值守协同操作系统。

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.8+](https://img.shields.io/badge/python-3.8+-brightgreen.svg)](https://www.python.org/)
[![Zero-Token](https://img.shields.io/badge/LLM%20Token-Zero%20Cost%20Inspection-orange.svg)](#)

---

## 核心设计哲学 (Design Philosophy)

1. **速度优先，安全底线兜底 (Velocity over Caution)**：
   - 杜绝阻碍研发流程的过度防御。仅拦截致命的不可逆破坏操作（如 `rm -rf /`、强制推送保护分支、生产库 DROP、`mkfs/dd`）。
   - 普通日常开发操作（文件删改、kill 进程、代码格式化、常规 force-with-lease）一律自动确认放行。
2. **严格保持 Git 线性历史 (Linear History Enforcement)**：
   - 为并发 Agent 提供临时 Git Worktree 沙箱，解决多模型写同一代码库的冲突踩踏问题。
   - 任务完成后默认采用 `squash` 或 `rebase` 合并回主干，并立即销毁临时分支与沙箱，杜绝分支膨胀乱象。
3. **0-Token 基础巡检与 300s 去重降噪**：
   - 本地轻量轮询监控进程状态与 CPU 负载，零 LLM Token 消耗。
   - 跨执行持久化的 Cooldown 机制（同一窗口同类异常 5 分钟内静默抑制），最多保留 50 份事件历史，避免磁盘洪泛。
4. **轻量 SQLite 任务黑板 (Blackboard)**：
   - 无任何第三方外部依赖，基于 SQLite (WAL 模式) 实现原子化认领 (`claim`)、状态流转与 `< 100 Token` 极简摘要。
5. **ACOM 结构化跨窗口通信 (Protocol)**：
   - 跨 Agent 结构化协议（`TASK_ASSIGN`, `TASK_UPDATE`, `TASK_COMPLETE`, `RELAY`, `NUDGE`, `BROADCAST`），终端自适应排版。

---

## 目录结构

```
tmux-butler/
├── SKILL.md                 # Agent 技能定义文件
├── config.json              # 异构模型算力层级与窗口角色映射
├── references/
│   └── prompts.md           # 干预与 Nudge 提示词模板
└── scripts/
    ├── tmux_bridge.py       # Tmux 底层 I/O 与终端状态检测
    ├── daemon.py            # 0-Token 巡检守护进程与去重报警
    ├── blackboard.py        # SQLite 任务协同黑板
    ├── workspace_manager.py # Git Worktree 沙箱隔离与线性合并
    └── protocol.py          # ACOM 结构化消息协议
```

---

## 模型算力层级与分工矩阵

| 模型 / 角色 | 智力等级与核心优势 | 成本特性 | 最佳任务分工 |
| :--- | :--- | :--- | :--- |
| **Codex** (`GPT-6.1-Sol`) | ⭐⭐⭐⭐⭐ 空间几何推理、3D 点云与视觉感知、运动规划 | 付费/高价值 | **空间感知与规划核心**：处理复杂几何算法、位姿漂移归因 |
| **Claude / Opus** (`Claude Code`) | ⭐⭐⭐⭐⭐ 系统架构设计、全景代码审查、复杂边界推演 | 付费/高价值 | **总架构师与评审者**：全局方案设计、接口契约终审 |
| **Devin** (`SWE-2 Max`) | ⭐⭐⭐☆ 工程实现能力合格，持久运行稳定性好 | 🎁 免费不限量 | **高并发苦力与攻坚主力**：全套测试回归、大工程批量机械重构 |
| **AGY / Butler** (`Gemini 3.8 Flash`) | ⭐⭐⭐☆ 极速响应、本地工具与 tmux 交互原生集成 | 极低成本/极速 | **调度管家与环境桥梁**：0-Token 巡检、黑板任务调度、安全阻断 |

---

## 快速使用

### 1. 任务协同黑板 (Blackboard)

```bash
# 创建任务
python3 scripts/blackboard.py create --title "重构点云配准异常处理" --issuer claude --assignee devin

# 查看待办与进行中任务
python3 scripts/blackboard.py list

# Worker 原子认领任务
python3 scripts/blackboard.py claim task-001 --assignee devin

# 更新状态与产出
python3 scripts/blackboard.py update task-001 --status DONE --note "12 项单元测试全部通过"

# 获取紧凑摘要（适合注入 LLM 上下文，< 100 Tokens）
python3 scripts/blackboard.py summary
```

### 2. 工作区沙箱隔离与线性 Git 历史 (Workspace Manager)

```bash
# 1. 为任务创建隔离沙箱
python3 scripts/workspace_manager.py create --task-id task-001 --repo /path/to/repo

# 2. Worker 在独立目录下开发完成后，squash 合并回主干（保持线性历史）
python3 scripts/workspace_manager.py merge --task-id task-001 --message "feat: 完成点云配准重构"

# 3. 销毁临时 worktree 并删除临时分支（零分支残留）
python3 scripts/workspace_manager.py cleanup --task-id task-001
```

### 3. ACOM 跨窗口结构化通信 (Protocol)

```bash
# 下发任务给 Devin
python3 scripts/protocol.py send \
  --type TASK_ASSIGN \
  --sender claude \
  --recipient devin \
  --task-id task-001 \
  --content "重构点云配准异常处理并补齐边界测试" \
  --metadata '{"working_dir": "/home/test/.tmux-butler-workspaces/task-001", "test_cmd": "pytest -v"}' \
  --target 0:devin

# 注入破死锁引导 (Nudge)
python3 scripts/protocol.py nudge --target 0:codex --reason "检测到静默超时且 CPU 空闲"
```

### 4. 守护巡检 (Daemon)

```bash
# 后台启动守护进程（每 15 秒轮询，5 分钟同类去重抑制，自动清理旧日志）
nohup python3 scripts/daemon.py run --interval 15 > daemon.log 2>&1 &

# 快速单次超压缩巡检（< 20 Tokens）
python3 scripts/daemon.py check --compact
```

---

## License

MIT License.
