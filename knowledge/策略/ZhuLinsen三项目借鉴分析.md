# 🏗️ ZhuLinsen 三项目架构借鉴分析

> 最后更新：2026-07-04（v1：首版综合深度分析）
> 创建：2026-07-04
> 来源：
> - [daily_stock_analysis](https://github.com/ZhuLinsen/daily_stock_analysis) — LLM驱动的多市场股票智能分析系统（38K+ Stars）
> - [AlphaSift](https://github.com/ZhuLinsen/alphasift) — 多因子选股与全市场扫描（L1→L2→L3三级管线）
> - [AlphaEvo](https://github.com/ZhuLinsen/alphaevo) — 策略回测与自我进化实验
> 目的：对比我们现有7 Agent架构，提取可借鉴的设计模式并落地改进
> 关联：[[TradingAgents借鉴分析]]、[[选股策略]]、[[择时策略]]

---

## 一、三项目总览 vs 我们的架构

### ZhuLinsen 生态（3项目矩阵）

```
┌───────────────────────────────────────────────────────────────────┐
│                    ZhuLinsen 量化投研生态                          │
│                                                                   │
│  daily_stock_analysis              AlphaSift        AlphaEvo      │
│  ┌────────────────────────┐  ┌────────────────┐  ┌────────────┐  │
│  │ LLM决策仪表盘           │  │ L1全市场快照   │  │ 策略回测    │  │
│  │ 多市场覆盖(A/H/US/JP/KR)│  │ L2 LLM重排    │  │ 参数搜索    │  │
│  │ 15+推送渠道             │  │ L3后置分析     │  │ 自我进化    │  │
│  │ GitHub Actions部署      │  │ 8因子评分体系  │  │ 策略迭代    │  │
│  │ 15+内置Agent策略问股    │  │ 评分曲线配置   │  │             │  │
│  │ 多数据源自动fallback    │  │ 风险叠加层     │  │             │  │
│  └────────────────────────┘  └────────────────┘  └────────────┘  │
│                          ↑                                                  │
│                    共用技术栈: Python + LiteLLM + YAML配置                   │
└───────────────────────────────────────────────────────────────────┘
```

### 我们的架构（7 Agent串行）

```
Agent1 情报员 → Agent2 分析师 → Agent5 选股 →
Agent3 风控官 ↔ Agent6 操盘手(制衡) →
Agent4 复盘师(反馈闭环) → Agent7 投资领导(统筹)
```

---

## 二、核心差距分析

### 2.1 选股管线架构

| 维度 | 我们(当前) | AlphaSift | 差距 |
|:-----|:----------|:----------|:-----|
| 筛选层级 | 单层评分 | **L1→L2→L3** 三级管线 | 缺L2 LLM重排和L3后置分析 |
| 因子数量 | 5因子 | **8因子** (含流动性/稳定/主题热度) | 缺流动性、稳定性、主题热度 |
| 评分曲线 | 硬编码 | **YAML配置scoring_profile** | 不可配置 |
| 风险控制 | 内嵌在脚本 | **独立Risk Overlay层** | 紧耦合 |
| 行业约束 | 简单百分比 | **风险桶(Risk Bucket)** 软约束 | 不够精细 |
| LLM参与 | 无 | **L2候选排序+候选级上下文** | 未用LLM提升排序 |

### 2.2 部署与数据可靠性

| 维度 | 我们(当前) | daily_stock_analysis | 差距 |
|:-----|:----------|:--------------------|:-----|
| 部署方式 | 本地Cron+claw MCP | **GitHub Actions+Docker+本地** | 缺零成本云端方案 |
| 数据源 | Tushare单源 | **多源自动fallback**(AkShare等6+) | 单点故障风险 |
| 推送渠道 | PushPlus+QQ | **15+渠道**(Telegram/企微/飞书/邮件等) | 通道单一 |
| 交互方式 | 定时报告 | **Agent策略问股**(15+策略模板) | 无交互能力 |
| WebUI | 无 | **FastAPI+React**前端 | 无可视化管理 |

### 2.3 策略进化

| 维度 | 我们(当前) | AlphaEvo | 差距 |
|:-----|:----------|:---------|:-----|
| 回测验证 | 有(含退市股) | **系统性回测** | 需规范化 |
| 参数优化 | 手动调整 | **自动参数搜索** | 无自动化 |
| 自我进化 | 人工复盘 | **策略迭代实验** | 非系统化 |

---

## 三、daily_stock_analysis 深度分析

### 3.1 核心架构

```
┌─ 调度层 ───────────────────────────────────────┐
│ GitHub Actions / Docker / 本地Cron / FastAPI    │
└────────────────────┬──────────────────────────┘
                     ▼
┌─ 数据采集层 ────────────────────────────────────┐
│  行情: AkShare / Tushare / YFinance / Longbridge│
│  新闻: Anspire / SerpAPI / Tavily / Bocha/Brave │
│  社交: Stock Sentiment API (Reddit/X/Polymarket)│
└────────────────────┬──────────────────────────┘
                     ▼
┌─ 分析决策层 ────────────────────────────────────┐
│  Prompt工程: 结构化模板+金融知识注入             │
│  LLM调用: Gemini/Claude/DeepSeek/GPT等          │
│  规则引擎: 乖离率/量比/筹码分布阈值检查          │
│  Agent策略: 15+内置(均线/波浪/缠论等)          │
└────────────────────┬──────────────────────────┘
                     ▼
┌─ 输出层 ────────────────────────────────────────┐
│  Markdown决策仪表盘(🟢🟡🔴信号)                │
│  15+推送渠道(企微/飞书/Telegram/Discord/邮件等)  │
│  WebUI(FastAPI+React)                           │
└───────────────────────────────────────────────┘
```

### 3.2 关键设计亮点

#### (1) 决策仪表盘结构
```
🎯 2026-07-04 决策仪表盘
共分析5只股票 | 🟢买入:1 🟡观望:3 🔴卖出:1

⚪ 股票A(000XXX): 买入 | 评分 75 | 看多
⚪ 股票B(600XXX): 观望 | 评分 55 | 震荡
...
```
**可借鉴**: 先结论→再证据→再风险清单的结构化报告

#### (2) Agent策略问股
- 自然语言触发: `"用缠论分析茅台"` `"用波浪理论看宁德时代"`
- 15+内置策略: 均线金叉、多头趋势、缠论(笔/线段/中枢)、波浪理论、热点题材、事件驱动、成长质量等
- 自动调用实时行情+K线+技术指标+新闻舆情
- 多轮对话支持追问和会话导出

#### (3) 多数据源自动fallback设计模式
```python
# DSA的抽象层模式（简化示意）
class DataProvider:
    providers = [AkShareProvider(), TushareProvider(), YFinanceProvider()]
    
    def get_data(self, code):
        for provider in self.providers:
            try:
                return provider.fetch(code)
            except:
                continue
        raise AllProvidersFailed()
```

### 3.3 可借鉴改进点

| 优先级 | 改进点 | 预估工作 | 影响范围 |
|:------|:-------|:--------|:--------|
| 🔴 P0 | 多数据源fallback | 中等 | data层可靠性 |
| 🔴 P0 | GitHub Actions云端部署 | 小 | 部署兜底 |
| 🟠 P1 | 推送渠道扩展(Telegram) | 小 | 通知覆盖 |
| 🟠 P1 | 报告格式优化(决策仪表盘) | 小 | 可读性 |
| 🟡 P2 | Agent策略问股 | 大 | 交互方式 |
| 🟡 P2 | WebUI管理界面 | 大 | 管理体验 |

---

## 四、AlphaSift 深度分析

### 4.1 L1→L2→L3 三级筛选管线

```
全市场~5500只股票
    │
    ▼ L1: 快照硬筛 + 横向评分 (全量)
    │  ├─ 硬筛条件: ST排除/市值/成交额/PE区间
    │  ├─ screen_score: 8因子加权评分
    │  │   估值/流动性/动量/反转/资金活跃/稳定/容量/主题热度
    │  └─ 日K增强: Top N候选补充60日技术指标
    │
    ▼ L2: LLM相对排序 (Top K ~50只)
    │  ├─ 输入: screen_score + 全市场快照摘要 + 新闻/情报
    │  ├─ 输出: 全局market_view + 重排候选+thesis/风险/催化
    │  └─ 约束: 不能推荐候选池外，不能覆写硬筛条件
    │
    ▼ L3: 后置分析器 (Top N ~5-10只)
       ├─ scorecard(默认): 本地规则加减分
       ├─ dsa(可选): 单股深度LLM分析
       └─ external_http(可选): 外部系统集成
```

### 4.2 8因子评分体系 (screen_score)

| 因子 | 含义 | 我们有无 | 对应我们因子 |
|:-----|:-----|:--------|:-----------|
| `value` | PE/PB估值吸引力 | ✅ | 估值因子 |
| `liquidity` (新增) | 成交额代表的可交易性 | ❌ | — |
| `momentum` | 建设性涨幅,避免追高 | ✅ | 动量因子 |
| `reversal` | 控制回撤后的修复价值 | ❌ (隐含) | 部分在技术面 |
| `activity` (新增) | 量比/换手率资金活跃度 | ❌ | 部分在情绪 |
| `stability` (新增) | 惩罚极端波动/过热换手/负PE | ❌ | — |
| `size` | 总市值与容量 | ✅ | 筛选条件中有 |
| `theme_heat` (新增) | 板块/概念热度(惩罚过热) | ❌ | 部分在情绪 |

**可借鉴**: 增加流动性、稳定性、主题热度3个因子，使评分更全面

### 4.3 评分曲线可配置化 (scoring_profile)

AlphaSift的评分因子使用**非线性曲线**而非简单线性映射：

```yaml
# 示例: scoring_profile 配置
momentum_chase_start_pct: 12       # 涨幅超12%开始惩罚追高
activity_ideal_volume_ratio: 1.8   # 理想量比1.8
activity_ideal_turnover_rate: 5    # 理想换手率5%
reversal_ideal_change_pct: -3      # 偏好-3%的回撤
stability_hot_change_pct: 9        # 超9%开始惩罚过热
theme_heat_overheat_score: 80      # 热度超80惩罚
```

每个因子使用分段函数或正态分布映射到0-100分，而非简单的线性归一化。

### 4.4 风险叠加层 (Risk Overlay Layer)

独立于因子评分的惩罚/否决机制：

| 检查项 | 动作 |
|:-------|:-----|
| 当日涨跌过大 | 惩罚/可选否决 |
| 量比异常/换手过高 | 惩罚/可选否决 |
| 负PE/高PB | 惩罚 |
| MACD弱势/RSI超买 | 惩罚 |
| LLM风险标签 | 影响最终分 |
| DSA分析风险标签 | 影响最终分 |

**可借鉴**: 将风控逻辑从因子评分中解耦，独立叠加

### 4.5 组合分散化 (Portfolio Diversification)

- 使用风险桶(Risk Bucket)机制: 银行/保险/证券→金融桶
- 同桶超限触发 `PORTFOLIO_CONCENTRATION_PENALTY` 软约束
- 比我们的"同行业≤40%"更精细

### 4.6 可借鉴改进点

| 优先级 | 改进点 | 预估工作 | 影响范围 |
|:------|:-------|:--------|:--------|
| 🔴 P0 | 8因子评分体系升级 | 中 | stock_picker核心 |
| 🔴 P0 | 评分曲线可配置化 | 中 | 选股规则.json |
| 🔴 P0 | L1→L2→L3管线重构 | 大 | stock_picker架构 |
| 🟠 P1 | 风险叠加层 | 中 | 新模块 |
| 🟠 P1 | 风险桶分散化 | 小 | 行业约束增强 |
| 🟡 P2 | LLM参与L2排序 | 大 | Agent7增强 |

---

## 五、AlphaEvo 深度分析

### 5.1 核心定位

> 策略回测与自我进化实验 — 验证交易规则的有效性，迭代优化策略参数与组合

### 5.2 关键设计

```
策略定义(YAML)
    │
    ▼
历史数据回测 → 回测报告(收益率/夏普/胜率/最大回撤)
    │
    ▼
参数搜索 → 最优参数组合
    │
    ▼
策略迭代 → 进化后的策略
```

### 5.3 我们的回测现状

- 已有: `knowledge/策略/含退市股全策略回测报告.md` (327只退市股修正)
- 已有: `knowledge/策略/MA96-RSI策略分析.md` (MA96+RSI回测)
- 缺: 系统化的参数自动搜索、策略自动迭代

### 5.4 可借鉴改进点

| 优先级 | 改进点 | 预估工作 | 影响范围 |
|:------|:-------|:--------|:--------|
| 🟡 P2 | 策略参数YAML化 | 中 | 策略规则.json |
| 🟡 P2 | 参数自动搜索 | 大 | 新模块 |
| 🟢 P3 | 回测框架规范化 | 中 | 复盘流程 |

---

## 六、综合改进计划

### 6.1 优先级矩阵

```
高影响 ┼───────────────────────────────────────────────
       │  🔴P0: 多数据源fallback                     │
       │  🔴P0: 8因子评分体系升级                     │
       │  🔴P0: 评分曲线可配置化                      │
       │  🔴P0: GitHub Actions部署                    │
       │                                              │
       │  🟠P1: L1→L2→L3管线重构                     │
       │  🟠P1: 风险叠加层                            │
       │  🟠P1: 推送渠道扩展                          │
       │  🟠P1: 风险桶分散化                          │
       │                                              │
       │  🟡P2: Agent策略问股                         │
       │  🟡P2: LLM参与L2排序                        │
       │  🟡P2: WebUI管理界面                         │
       │  🟢P3: 回测框架规范化                         │
低影响 ┼───────────────────────────────────────────────
        低努力                      高努力→
```

### 6.2 实施路线

| 阶段 | 内容 | 依赖 |
|:----|:-----|:-----|
| **Phase 1** | 多数据源fallback + 因子体系升级 + 评分曲线配置 | 无 |
| **Phase 2** | 风险叠加层 + GitHub Actions部署 + 推送扩展 | Phase 1 |
| **Phase 3** | L1→L2→L3管线重构 + 风险桶 | Phase 1+2 |
| **Phase 4** | LLM参与L2排序 + Agent策略问股 | Phase 3 |
| **Phase 5** | WebUI + 回测框架 + 自我进化 | Phase 4 |

---

## 七、关键技术方案

### 7.1 多数据源抽象层设计

```python
# data_provider/  — 新目录
# 抽象层 + 3个Provider + 自动fallback
class DataProvider:
    """多数据源抽象层，单源失败自动切换"""
    def __init__(self):
        self._providers = [TushareProvider(), AkshareProvider()]
    
    def daily(self, code, start, end):
        return self._try_all("daily", code, start=start, end=end)
    
    def _try_all(self, method, *args, **kwargs):
        errors = []
        for p in self._providers:
            try:
                return getattr(p, method)(*args, **kwargs)
            except Exception as e:
                errors.append(f"{p.name}: {e}")
                continue
        raise DataUnavailableError(f"所有数据源都失败: {errors}")
```

### 7.2 screen_score 配置化方案

```yaml
# 在 data/选股规则.json 中扩展 scoring_profile
"scoring_profile": {
  "momentum_chase_start_pct": 12,
  "activity_ideal_volume_ratio": 1.8,
  "reversal_ideal_change_pct": -3,
  "stability_hot_change_pct": 9,
  "theme_heat_overheat_score": 80,
  "theme_heat_persistence_min_score": 60
}
```

### 7.3 风险叠加层设计

```python
class RiskOverlay:
    """独立于因子评分的风险惩罚/否决机制"""
    checks = [
        DailyChangeCheck(threshold=9.5),      # 涨跌>9.5%否决
        VolumeRatioCheck(max_ratio=5),         # 量比>5否决  
        NegativePECheck(),                     # 负PE惩罚
        MacdWeakCheck(),                       # MACD弱势惩罚
    ]
    
    def apply(self, stock_data) -> dict:
        """返回 {penalty: float, veto: bool, reasons: []}"""
```

---

## 八、总结

ZhuLinsen 三项目代表了当前开源A股AI投研领域的最高水平之一。核心启发：

1. **架构思维**: L1→L2→L3 管线化设计比我们的单层评分更严谨
2. **配置驱动**: YAML配置化比硬编码更灵活可维护
3. **可靠性优先**: 多数据源fallback比单源依赖更稳健
4. **用户体验**: Agent策略问股+多渠道推送覆盖更广
5. **零成本部署**: GitHub Actions方案让项目可持续运行

> **关联**: 详见 [[选股策略]] 的因子配置、[[择时策略]] 的入场规则、[[TradingAgents借鉴分析]] 的多Agent制衡设计
