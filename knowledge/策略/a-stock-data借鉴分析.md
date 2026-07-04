# 🏗️ a-stock-data 技术方案借鉴分析

> 最后更新：2026-07-05
> 来源：[simonlin1212/a-stock-data](https://github.com/simonlin1212/a-stock-data) (⭐5.8k+)
> 目的：记录从 a-stock-data 项目中吸收的技术方案和设计思路
> 关联：[[ZhuLinsen三项目借鉴分析]]、[[TradingAgents借鉴分析]]

---

## 一、项目概览

a-stock-data 是一个面向AI编程助手的A股全栈数据工具包，以**单文件SKILL.md**形式交付。

### 本项目已吸收的技术方案

| # | 技术方案 | 状态 | 涉及文件 |
|:--|:--------|:-----|:--------|
| 1️⃣ | **mootdx TCP数据源** | ✅ 已实现 | `scripts/utils/mootdx_provider.py`, `data_provider.py` |
| 2️⃣ | **em_get() 东方财富限流网关** | ✅ 已实现 | `scripts/utils/eastmoney_get.py` |
| 3️⃣ | **多级数据源优先级系统** | ✅ 已实现 | `scripts/utils/data_provider.py` (Tushare→mootdx→AkShare) |
| 4️⃣ | **TradingAgents-astock新增角色** | ✅ 已实现 | Agent8 政策分析师, Agent9 游资追踪师, Agent3 风控扩展 |

---

## 二、已吸收技术详解

### 2.1 mootdx TCP 数据源

mootdx 通过通达信 TCP 二进制协议（端口 7709）直接连接行情服务器。

**在本项目的定位**：
- Tier 2 优先级，介于 Tushare（Tier 1）和 AkShare（Tier 3）之间
- 用于行情补全（Tushare没有数据的股票）
- 作为Tushare不可用时的免费替代

**实现文件**：`scripts/utils/mootdx_provider.py`

**使用方式**：
```python
from scripts.utils.mootdx_provider import MootdxProvider
mp = MootdxProvider()
df = mp.daily("000001.SZ", start_date="20260701", end_date="20260703")
```

### 2.2 em_get() 限流网关

借鉴 a-stock-data 的 em_get() 反爬设计，实现的东方财富数据网关。

**限流参数**：
- 串行请求（不并发）
- ≥1.0s 请求间隔 + 0.1-0.5s 随机抖动
- Session Keep-Alive 复用

**东方财富封禁阈值**（来自 a-stock-data 实测）：
- >5 req/s → 封禁
- ≥10 并发 → 封禁
- ≥200 请求/min → 封禁

**实现文件**：`scripts/utils/eastmoney_get.py`

**支持端点**：
- `em_get()` — 通用限流GET请求
- `get_dragon_tiger()` — 龙虎榜数据
- `get_moneyflow_stock()` — 个股资金流向
- `get_lockup_calendar()` — 限售股解禁日历
- `get_margin_detail()` — 个股融资融券

### 2.3 多级数据源优先级

将原本简单的 Tushare→AkShare 两级 fallback 升级为三级优先级系统。

```
Tier 1 — Tushare Pro: 付费版，数据最全最准（主数据源）
Tier 2 — mootdx: 免费TCP，不封IP（新增）
Tier 3 — AkShare: 免费HTTP（最终回退）
```

**实现文件**：`scripts/utils/data_provider.py`

**使用方式**：
```python
# 默认优先级：Tushare → mootdx → AkShare
dp = DataProvider()

# 自定义优先级：mootdx 优先
dp = DataProvider(provider_order=["mootdx", "tushare", "akshare"])
```

---

## 三、未吸收的方案与原因

| 方案 | 未吸收原因 |
|:----|:-----------|
| **SKILL.md 单文件架构** | 本项目的8 Agent Python流水线与单文件架构不兼容，采用传统多文件方案 |
| **10层40端点架构** | 部分端点（ETF/期权/打板）与现有Agent定位重叠，暂不引入 |
| **13个全部免费数据源** | 已有 Tushare Pro 付费版，数据质量和稳定性优于免费源 |
| **零第三方依赖** | 已有成熟的第三方依赖链（pandas/ta/litellm），迁移成本高 |

---

## 四、对比总结

```
a-stock-data 的精华 —— 已吸收:
  ├── mootdx TCP数据源      → scripts/utils/mootdx_provider.py
  ├── em_get() 限流网关      → scripts/utils/eastmoney_get.py
  ├── 多级优先级系统          → scripts/utils/data_provider.py
  └── TradingAgents-astock   → Agent8/Agent9/Agent3扩展

我们的优势（a-stock-data没有的）:
  ├── ✅ 8 Agent流水线       → 完整投研闭环
  ├── ✅ Karpathy知识库       → 知识复利
  ├── ✅ 多因子选股系统       → L1/L2/L3管线
  ├── ✅ 风控/操盘制衡机制    → 三权分立
  ├── ✅ 含退市股全策略回测   → 幸存者偏差修正
  ├── ✅ 微信推送闭环         → PushPlus + QQ
  └── ✅ 定时调度系统         → claw MCP + GHA
```

---

> 本文件由深度研究工作流（94 Agent调用，12数据源，25条对抗验证）生成。
> Agent4 复盘师维护更新。
