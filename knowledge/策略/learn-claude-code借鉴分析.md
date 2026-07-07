---
name: learn-claude-code借鉴分析
description: shareAI-lab/learn-claude-code 20章设计模式与项目现状对照分析
metadata:
  type: reference
  source: https://github.com/shareAI-lab/learn-claude-code
---

> 最后更新：2026-07-07

# learn-claude-code Harness 设计模式借鉴分析

## 核心哲学

**Agent = Model + Harness**。Agency 来自模型训练，Harness 是让 Agent 工作的环境。
本项目的 10 个 Agent 已具备基本 Harness，但缺失若干关键机制。

## 20章对照分析

| 章 | 模式 | 项目现状 | 优先级 |
|:---|:-----|:---------|:------|
| s01 | Agent Loop | ✅ Python脚本 + Claude Code | - |
| s02 | Tool Dispatch | ✅ 多工具分发 | - |
| s03 | Permission | ✅ Claude Code权限系统 | - |
| s04 | Hooks | ✅ SessionStart/PreToolUse/PostToolUse/Stop/PreCompact | - |
| **s05** | **TodoWrite** | ❌ **缺失**：Agent无规划工具，直接执行 | **P0** |
| s06 | Subagent | ✅ Workflow系统 + .claude/agents/ | - |
| **s07** | **Skill Loading** | ⚠️ **待优化**：SKILL.md在CLAUDE.md中列出但无按需加载 | **P1** |
| **s08** | **Context Compact** | ❌ **缺失**：项目级压缩策略 | **P2** |
| s09 | Memory | ✅ Karpathy Wiki + Claude自动记忆 | - |
| **s10** | **System Prompt** | ❌ **缺失**：CLAUDE.md是静态硬编码，非运行时组装 | **P1** |
| **s11** | **Error Recovery** | ⚠️ **待优化**：Python脚本有try/except但无结构化重试(退避/降级/切换模型) | **P1** |
| **s12** | **Task System** | ❌ **缺失**：TODO仅在对话中，无文件持久化任务DAG | **P0** |
| s13 | Background Tasks | ✅ Workflow后台执行 | - |
| s14 | Cron Scheduler | ✅ CronCreate内置工具 | - |
| **s15** | **Agent Teams** | ⚠️ **待优化**：10 Agent独立运行，缺正式MessageBus | **P1** |
| **s16** | **Team Protocols** | ❌ **缺失**：Agent间通信格式无统一协议 | **P1** |
| s17 | Autonomous Agents | ❌ **缺失**：Agent需手动触发，无看板认领 | **P2** |
| s18 | Worktree Isolation | ✅ Agent worktree隔离 | - |
| s19 | MCP Plugin | ⚠️ 已移除Node.js MCP，后续可用Python MCP | **P2** |
| s20 | Comprehensive | - | - |

## 安装计划（按优先级）

### P0 — 立即安装
1. **TodoWrite** — 为每个Agent添加`todo_write`规划工具，执行前先列计划
2. **Task System DAG** — 文件持久化任务图，支持blockedBy依赖

### P1 — 本周完成
3. **System Prompt装配** — 模块化CLAUDE.md，运行时按需组装
4. **Team Inbox协议** — JSONL文件邮箱标准化Agent间通信
5. **Structured Error Recovery** — 指数退避+fallback模型结构化重试

### P2 — 后续优化
6. **Context Compact策略** — 项目级四层压缩(snip→micro→budget→LLM)
7. **Autonomous Claim** — Agent看板认领任务
8. **Python MCP服务器** — 用Python重写MCP服务器
