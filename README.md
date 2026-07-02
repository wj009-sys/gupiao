# 股票投研自动化 — A股 AI Agent 团队

构建 A 股自动化投研团队，**7 个 AI Agent** 每天自动完成情报采集→技术分析→选股推荐→风控检查→交易计划→复盘迭代的完整闭环，由投资领导统筹管理。

---

## 🏗️ 架构总览

```
07:00 Agent1 情报员 ──► 情报摘要（政策/公告/资金流向/龙虎榜）
08:30 Agent2 分析师 ──► 技术分析（MACD/KDJ/RSI/板块强度排名）
09:00 Agent5 早盘选股 ──► 早盘候选池（pre_market模式）
12:00 Agent5 午盘选股 ──► 午盘速评（noon模式）
21:00 Agent4 复盘师 ──► 复盘报告（偏差分析/知识库更新）
21:30 Agent5 晚间选股 ──► 选股建议（evening模式）
按需 Agent3 风控官 ──► 风控检查/操盘审查/冲突标记
按需 Agent6 操盘手 ──► 交易计划（买入/卖出/持有清单）
按需 Agent7 投资领导 ──► 质量审核/冲突仲裁/最终决策
```

## 🔄 双向反馈闭环

```
投资领导 ──审核──► Agent1-6
复盘师   ──审核──► 投资领导
           ↻ 持续进化
```

## 📁 目录结构

| 目录 | 用途 |
|------|------|
| `data/` | 持仓/自选/规则配置 |
| `reports/` | 日报/周报/月报输出 |
| `scripts/` | 7个Agent的Python脚本 + 工具函数 |
| `skills/` | 7个Agent的AI Skill指令集 |
| `knowledge/` | 策略知识库 + 复盘记录 |
| `.claude/` | MCP服务器、定时任务、凭据 |

## 📊 Agent 角色

| Agent | 角色 | 脚本 | Darwin评分 | 定时 |
|-------|------|------|-----------|------|
| 🕵️ Agent1 | 情报员 | `fetch_all.py` | **96** | 07:00 |
| 📊 Agent2 | 分析师 | `analyze.py` | **97** | 08:30 |
| 🛡️ Agent3 | 风控官 | `risk_check.py` | **97** | 按需 |
| 🔄 Agent4 | 复盘师 | `review.py` | **97** | 21:00 |
| 🔍 Agent5 | 选股机器人 | `stock_picker.py` | **100** | 09:00/12:00/21:30 |
| 🎯 Agent6 | 操盘手 | `trader.py` | **97** | 按需 |
| 🏆 Agent7 | 投资领导 | `leader.py` | **96** | 按需 |

## 🚀 快速开始

```bash
cd 股票投资
source venv/Scripts/activate

# 运行全部Agent（按顺序）
python -X utf8 scripts/agent1-情报采集/fetch_all.py
python -X utf8 scripts/agent2-技术分析/analyze.py
python -X utf8 scripts/agent3-风控/risk_check.py --portfolio data/portfolio.json
python -X utf8 scripts/agent5-选股/stock_picker.py --mode evening --top-n 5
python -X utf8 scripts/agent6-操盘/trader.py
python -X utf8 scripts/agent7-决策/leader.py
python -X utf8 scripts/agent4-复盘/review.py

# 或在Claude Code中使用Slash命令：
# /情报员  → 情报采集
# /分析师  → 技术分析
# /选股    → 选股（晚间模式）
# /决策    → 最终决策
```

## 🔧 环境要求

- Python 3.14+
- Tushare Pro Token（数据源）
- 依赖：`pip install tushare pandas numpy ta openpyxl`

## 📤 通知通道

1. **微信**（主通道）— PushPlus API → PushPlus 公众号推送
2. **微信**（备选）— cc-connect + ilink（已不稳定，建议用 PushPlus）
3. **QQ**（可选）— QQ 邮箱 SMTP / QQ MCP 推送

## 📜 详细文档

- [CLAUDE.md](CLAUDE.md) — 完整项目说明
- [skills/](skills/) — 各Agent Skill指令集
- [data/](data/) — 规则配置和持仓数据

---

> *Powered by Darwin Optimization — 7个Agent平均评分 97.1（五星）*
