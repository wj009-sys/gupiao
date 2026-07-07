---
name: Tushare-Pro-API参考
description: Tushare Pro 14大类225接口的完整参考手册（参数/返回字段/积分要求/注意事项/项目使用对照/MCP Server/Skills）
metadata:
  type: reference
  source: https://tushare.pro/document/2
---

# Tushare Pro API 参考手册

> 编译时间：2026-07-07 | 来源：Tushare Pro 官方文档中心 https://tushare.pro/document/2
> 用途：供所有Agent参考的数据接口速查手册
> 原则：原始文档不可变（本文是编译知识），接口细节以官网为准

---

## 通用规则

| 项目 | 规则 |
|:----|:-----|
| 日期格式 | **YYYYMMDD**（如 `20260107`），无分隔符 |
| 积分制 | 基础积分用户~500次/分钟，每次最多6000条 |
| 数据入库 | 交易日 **15:00~16:00** |
| 复权说明 | `daily` 接口返回**未复权**行情，需乘 `adj_factor` 或使用 `pro_bar(adj='qfq')` |
| 单位说明 | `vol`=**手**（1手=100股），`amount`=**千元**，`pct_chg`=**百分比**（%） |
| 股票代码 | `000001.SZ`（6位数字 + `.SZ` 深交所 / `.SH` 上交所 / `.BJ` 北交所） |
| 指数代码 | `000001.SH` 上证指数、`399001.SZ` 深证成指、`000688.SH` 科创50、`399006.SZ` 创业板 |
| THS概念代码 | `885823.TI`（同花顺概念指数，8开头.TI后缀） |

---

## 平台概述与演进历史

> 来源：https://tushare.pro/document/1（介绍分类）

### 核心使命
> **让普通大众用上专业数据，让 AI 更智能**

### 数据生产模式
```
社区协同采集 → 标准化字段定义 → 入库归档 → 多层级质检 → API 统一输出
```
从「网络采集数据」到「自主生产标准化数据」的底层逻辑重塑。全链路治理体系从源头剔除脏数据、缺失值，契合机器学习/策略开发对低噪声数据源的要求。

### 发展历程

| 阶段 | 时间 | 说明 |
|:----|:----|:------|
| **Org 版** | 2014 年 | Python+Pandas 开源 API，网络采集模式，GitHub 星标全球前列 |
| **Pro 版** | 2017 年+ | 全面自研数据生产，建立全链路质检，数据品类大幅扩展 |
| **AI 阶段** | 2024 年起 | 发布 Skills/MCP 支持，适配 AI 智能体与批量数据调取需求 |

### 商业化定位
- 平台无资本风投，靠积分与少量付费支撑
- 基础数据对普通用户**免费开放**
- 商业化收益全部反哺：扩充采编团队、迭代质控体系、优化 API 架构
- 形成 **"商业造血 → 数据提质 → 普惠开放"** 正向循环

### 全品类覆盖
225 个接口，14+ 大类：股票（107）、指数（16）、基金（8）、ETF（7）、债券（15）、期货（14）、期权（3）、港股（11）、美股（9）、外汇（2）、宏观经济（22）、另类数据（2）、大模型语料（5）、财富管理（2）

### 三种调用方式

| 方式 | 推荐场景 | 文档 |
|:----|:--------|:----|
| **Python SDK** `pro.api_name()` | 量化策略、数据分析 | doc_id=131 |
| **HTTP RESTful** `POST api.tushare.pro` | 语言无关、非 Python 环境 | doc_id=130 |
| **MCP Server** `streamableHttp` | AI 智能体直接调用 | doc_id=463（含 MCP 配置） |

---

## 一、股票日线行情 — `daily`（doc_id=5 / 2-27）

### 输入参数

| 参数 | 类型 | 必选 | 描述 |
|:----|:----|:----:|:------|
| `ts_code` | str | N | 股票代码（支持逗号分隔多只） |
| `trade_date` | str | N | 交易日期 |
| `start_date` | str | N | 开始日期 |
| `end_date` | str | N | 结束日期 |

### 返回字段

| 字段 | 类型 | 描述 |
|:----|:----:|:------|
| `ts_code` | str | **股票代码** |
| `trade_date` | str | 交易日期 |
| `open` | float | 开盘价 |
| `high` | float | 最高价 |
| `low` | float | 最低价 |
| `close` | float | **收盘价** |
| `pre_close` | float | **昨收价（除权价）** |
| `change` | float | 涨跌额 |
| `pct_chg` | float | **涨跌幅（%）** — 基于除权后昨收计算 |
| `vol` | float | **成交量（手）** |
| `amount` | float | **成交额（千元）** |

### 调用示例

```python
pro.daily(ts_code='000001.SZ', start_date='20260101', end_date='20260131')
pro.daily(ts_code='000001.SZ,600000.SH', start_date='20260101', end_date='20260131')
pro.daily(trade_date='20260107')  # 某天全部股票
```

### ⚠️ 项目注意事项
- **未复权数据**：`daily` 返回的 `open/high/low/close` 是未复权价
- **`pre_close` 是除权价**：导致分红/送股后 `pct_chg` 基于除权前收计算
- 需要用 `adj_factor` 计算复权价格，或改用 `pro_bar(adj='qfq')`
- 本项目 DB 中 `daily_price` 表存的是未复权行情，`adj_close_f/adj_close_b` 列存复权价

---

## 二、通用行情接口 — `pro_bar`（doc_id=6）

### 输入参数

| 参数 | 类型 | 必选 | 描述 |
|:----|:----|:----:|:------|
| `ts_code`| str | Y | 证券代码（**不支持多值**） |
| `freq` | str | N | `1min`/`5min`/`15min`/`30min`/`60min`/`D`/`W`/`M`，默认`D` |
| `start_date` | str | N | 开始日期（分钟线格式 `2019-09-01 09:00:00`） |
| `end_date` | str | N | 结束日期 |
| `asset` | str | N | `E`股票 / `I`指数 / `FT`期货 / `FD`基金 / `O`期权 / `CB`可转债 |
| `adj` | str | N | 复权：`None`未复权 / `qfq`前复权 / `hfq`后复权（**仅日线支持**） |
| `ma` | list | N | 均线，如 `[5,10,20]` 返回 `ma5/ma10/ma20` |
| `factors` | list | N | 股票因子：`tor`换手率、`vr`量比 |
| `adjfactor` | bool | N | 是否返回复权因子 |

### 返回字段
同 `daily` + 可选 `ma5`/`ma10`/`ma20`/`ma_v_5`/`ma_v_10` + `turnover_rate`/`volume_ratio`

### 调用示例

```python
# 股票前复权日线（推荐替代 daily + adj_factor 组合）
ts.pro_bar(ts_code='000001.SZ', freq='D', adj='qfq', start_date='20260101', end_date='20260131')

# 指数
ts.pro_bar(ts_code='000001.SH', freq='D', asset='I', start_date='20260101', end_date='20260131')

# 1分钟线
ts.pro_bar(ts_code='000001.SZ', freq='1min', start_date='2026-01-07 09:00:00', end_date='2026-01-07')

# 期货
ts.pro_bar(ts_code='CU2501.SHF', freq='15min', asset='FT', start_date='2026-01-01', end_date='2026-01-07')
```

### 实时分钟行情（`rt_min` / `rt_idx_min` / `rt_etf_min` / `rt_fut_min`）

| 接口 | 适用 |
|:----|:-----|
| `rt_min` | 个股实时分钟 |
| `rt_idx_min` | 指数实时分钟 |
| `rt_etf_min` | ETF实时分钟 |
| `rt_fut_min` | 期货实时分钟（含`oi`持仓量） |

```python
pro.rt_min(ts_code='600000.SH', freq='1MIN')   # 注意是大写 1MIN/5MIN/15MIN/30MIN/60MIN
pro.rt_idx_min(ts_code='000001.SH', freq='1MIN')
```

---

## 三、指数日线行情 — `index_daily`（doc_id=29 / 2-128）

### 输入参数

| 参数 | 类型 | 必选 | 描述 |
|:----|:----|:----:|:------|
| `ts_code` | str | **Y** | 指数代码 |
| `trade_date` | str | N | 交易日期 |
| `start_date` | str | N | 开始日期 |
| `end_date` | str | N | 结束日期 |

### 返回字段

| 字段 | 类型 | 描述 |
|:----|:----:|:------|
| `ts_code` | str | 指数代码 |
| `trade_date` | str | 交易日期 |
| `close/open/high/low` | float | 收盘/开/高/低点位 |
| `pre_close` | float | 昨日收盘点位 |
| `change` | float | 涨跌点 |
| `pct_chg` | float | 涨跌幅（%） |
| `vol` | float | 成交量（手） |
| `amount` | float | 成交额（千元） |

### 性能限制
- 单次最多返回 **8000 条**记录
- 积分要求：**200 积分**起

---

## 四、复权因子 — `adj_factor`（doc_id=7 / 2-199）

### 输入参数

| 参数 | 类型 | 必选 | 描述 |
|:----|:----|:----:|:------|
| `ts_code` | str | N | 股票代码 |
| `trade_date` | str | N | 交易日期 |
| `start_date` | str | N | 开始日期 |
| `end_date` | str | N | 结束日期 |

### 返回字段（仅3个）

| 字段 | 类型 | 描述 |
|:----|:----:|:------|
| `ts_code` | str | 股票代码 |
| `trade_date` | str | 交易日期 |
| `adj_factor` | float | **复权因子** |

### 复权计算

| 类型 | 公式 | 参数 |
|:----|:-----|:----:|
| 不复权 | 原始价 | None |
| **前复权** | 当日收盘价 × (当日复权因子 / 最新复权因子) | `qfq` |
| **后复权** | 当日收盘价 × 当日复权因子 | `hfq` |

### 性能限制
- 单次最多 **3000 条**
- 积分要求：**2000** 积分可用，**5000** 高频
- 更新时间：每天 **9:30**

---

## 五、停复牌信息 — `suspend` / `suspend_d`（doc_id=8）

### `suspend`（通用停复牌）

| 参数 | 类型 | 必选 | 描述 |
|:----|:----|:----:|:------|
| `ts_code` | str | N | 股票代码（三选一） |
| `suspend_date` | str | N | 停牌日期 |
| `resume_date` | str | N | 复牌日期 |

| 返回字段 | 类型 | 描述 |
|:---------|:----:|:------|
| `ts_code` | str | 股票代码 |
| `suspend_date` | str | 停牌日期 |
| `resume_date` | str | 复牌日期 |
| `reason` | str | **停牌原因** |
| `suspend_days` | float | 停牌天数 |
| `resume_reason` | str | 复牌原因 |

### `suspend_d`（每日停复牌明细）

本项目当前使用 `pro.suspend_d()`。较 `suspend` 多了每日级别的具体停复牌状态。

---

## 六、分红送股 — `dividend`（doc_id=9 / 2-103）

### 输入参数

| 参数 | 类型 | 必选 | 描述 |
|:----|:----|:----:|:------|
| `ts_code` | str | N | 股票代码 |
| `ann_date` | str | N | 公告日 |
| `record_date` | str | N | 股权登记日 |
| `ex_date` | str | N | **除权除息日** |
| `imp_ann_date` | str | N | 实施公告日 |

### 返回字段

| 字段 | 类型 | 描述 |
|:----|:----:|:------|
| `ts_code` | str | 股票代码 |
| `end_date` | str | **分红年度** |
| `ann_date` | str | 预案公告日 |
| `div_proc` | str | **实施进度**：预案/实施/股东大会通过/停止实施 |
| `stk_div` | float | **每股送转**（送股+转增合计） |
| `stk_bo_rate` | float | 每股送股比例 |
| `stk_co_rate` | float | 每股转增比例 |
| `cash_div` | float | **每股分红（税后）** |
| `cash_div_tax` | float | 每股分红（税前） |
| `record_date` | str | 股权登记日 |
| `ex_date` | str | **除权除息日** |
| `pay_date` | str | 派息日 |
| `div_listdate` | str | 红股上市日 |
| `imp_ann_date` | str | 实施公告日 |
| `base_share` | float | 基准股本（万） |

### 积分要求：300 积分起

---

## 七、同花顺概念板块 — `ths_index` / `ths_daily` / `ths_member`（doc_id=473）

### `ths_index` — 板块列表

| 参数 | 类型 | 必选 | 描述 |
|:----|:----|:----:|:------|
| `ts_code` | str | N | 指数代码 |
| `exchange` | str | N | `A` A股 / `HK` 港股 / `US` 美股 |
| `type` | str | N | `N`概念指数 / `I`行业指数 / `R`地域指数 |

| 返回 | 类型 | 描述 |
|:----|:----:|:------|
| `ts_code` | str | 概念代码 `885823.TI` |
| `name` | str | 板块名称 |
| `count` | int | 成分股数量 |

### `ths_daily` — 板块日线行情

| 参数 | 类型 | 必选 | 描述 |
|:----|:----|:----:|:------|
| `ts_code` | str | N | 指数代码 |
| `trade_date` | str | N | 日期 |
| `start_date` | str | N | 开始 |
| `end_date` | str | N | 结束 |

| 返回 | 类型 | 描述 |
|:----|:----:|:------|
| `ts_code`| str | 指数代码 |
| `trade_date`| str | 日期 |
| `open/ high/ low/ close` | float | 开/高/低/收 |
| `pct_chg` | float | 涨跌幅(%) |
| `vol/ amount` | float | 量/额 |

### `ths_member` — 板块成分股

| 参数 | 类型 | 必选 | 描述 |
|:----|:----|:----:|:------|
| `ts_code` | str | N | 板块代码（查成分股） |
| `con_code` | str | N | 股票代码（查所属板块） |

| 返回 | 类型 | 描述 |
|:----|:----:|:------|
| `trade_date`| str | 日期 |
| `ts_code`| str | 板块代码 |
| `con_code`| str | 成分股代码 |
| `name`| str | 成分股名称 |

### ✅ 本项目当前状态
- `ths_daily` 已通过 `get_ths_index(daily=True)` 实现，有多级 fallback（DB→东财→AkShare）
- `ths_index` 已通过 `get_ths_index(daily=False)` 获取板块列表
- `ths_member` **缺失** — 待添加

---

## 八、限售股解禁 — `share_float`（doc_id=450 / 2-160）

### 输入参数

| 参数 | 类型 | 必选 | 描述 |
|:----|:----|:----:|:------|
| `ts_code` | str | N | 股票代码 |
| `ann_date` | str | N | 公告日期 |
| `float_date` | str | N | **解禁日期** |
| `start_date` | str | N | 解禁开始日期 |
| `end_date` | str | N | 解禁结束日期 |

### 返回字段

| 字段 | 类型 | 描述 |
|:----|:----:|:------|
| `ts_code` | str | 股票代码 |
| `ann_date` | str | 公告日期 |
| `float_date` | str | **解禁日期** |
| `float_share` | float | **流通股份（股）** |
| `float_ratio` | float | 流通股份占总股本比率（%） |
| `holder_name` | str | **股东名称** |
| `share_type` | str | 股份类型 |

### 性能
- 单次最大 **6000 条**
- 积分要求：**120** 积分起

---

## 九、个股资金流向 — `moneyflow`（doc_id=463 / 2-170）

### 输入参数

| 参数 | 类型 | 必选 | 描述 |
|:----|:----|:----:|:------|
| `ts_code` | str | N | 股票代码 |
| `trade_date` | str | N | 交易日期 |
| `start_date` | str | N | 开始 |
| `end_date` | str | N | 结束 |

### 返回字段（核心）

| 字段 | 类型 | 描述 |
|:----|:----:|:------|
| `ts_code` | str | 股票代码 |
| `trade_date` | str | 日期 |
| `buy_sm_vol` / `buy_sm_amount` | int/float | 小单买入量（手）/ 买入金额（万元） |
| `sell_sm_vol` / `sell_sm_amount` | int/float | 小单卖出 |
| `buy_md_vol` / `buy_md_amount` | int/float | 中单买入 |
| `sell_md_vol` / `sell_md_amount` | int/float | 中单卖出 |
| **`buy_lg_vol` / `buy_lg_amount`** | int/float | **大单买入量/金额** |
| **`sell_lg_vol` / `sell_lg_amount`** | int/float | **大单卖出量/金额** |
| **`buy_elg_vol` / `buy_elg_amount`** | int/float | **特大单买入量/金额** |
| **`sell_elg_vol` / `sell_elg_amount`** | int/float | **特大单卖出量/金额** |
| `net_mf_vol` | int | 净流入量（手） |
| `net_mf_amount` | float | 净流入额（万元） |

### 订单规模分类

| 类别 | 范围 | 前缀 |
|:----|:-----|:----:|
| 小单（sm） | ≤ 5万 | `sm` |
| 中单（md） | 5万 ~ 20万 | `md` |
| **大单（lg）** | 20万 ~ 100万 | **`lg`** |
| **特大单（elg）** | ≥ 100万 | **`elg`** |

### ⚠️ 项目当前问题修复
- `hot_money_tracker.py` 中 `net_lg_amount` 目前错误地用 `net_amount`（总净流入）代替
- **正确做法**：`net_lg_amount = buy_lg_amount - sell_lg_amount`
- 积分要求：**2000** 积分起

### `moneyflow_dc`（东方财富版 — 更简洁）

```python
pro.moneyflow_dc(trade_date='20260107')  # 当日全部股票
pro.moneyflow_dc(ts_code='000001.SZ', start_date='20260101', end_date='20260107')
```

额外返回：`name`（股票名称）、`pct_change`（涨跌幅）、`close`（最新价）

---

## 十、沪深港通资金流向 — `moneyflow_hsgt`（doc_id=40 / 2-47）

### 输入参数

| 参数 | 类型 | 必选 | 描述 |
|:----|:----|:----:|:------|
| `trade_date` | str | N | 日期（与`start_date`二选一） |
| `start_date` | str | N | 开始日期 |
| `end_date` | str | N | 结束日期 |

### 返回字段

| 字段 | 类型 | 描述 |
|:----|:----:|:------|
| `trade_date` | str | 交易日期 |
| `ggt_ss` | float | **港股通（上海）** — 南向（百万元） |
| `ggt_sz` | float | **港股通（深圳）** — 南向（百万元） |
| `hgt` | float | **沪股通** — 北向（百万元） |
| `sgt` | float | **深股通** — 北向（百万元） |
| `north_money` | float | **北向资金合计** = hgt + sgt（百万元） |
| `south_money` | float | **南向资金合计** = ggt_ss + ggt_sz（百万元） |

### 单位转换
- 所有数值单位 **百万元**（÷10 = 亿元）
- `north_money` = hgt + sgt（北向外资流入A股）
- `south_money` = ggt_ss + ggt_sz（南向内地资金流入港股）

### ✅ 本项目当前状态
`get_moneyflow_hsgt()` 已实现，可直接调用。

---

## 十一、龙虎榜 — `limit_list` / `top_list`（doc_id=230 / 2-106）

### `limit_list` — 龙虎榜榜单

| 参数 | 类型 | 必选 | 描述 |
|:----|:----|:----:|:------|
| `trade_date` | str | N | 交易日期 |
| `ts_code` | str | N | 股票代码 |
| `start_date` | str | N | 开始日期 |
| `end_date` | str | N | 结束日期 |

### 返回字段

| 字段 | 类型 | 描述 |
|:----|:----:|:------|
| `trade_date` | str | 交易日期 |
| `ts_code` / `name` | str | 股票代码/名称 |
| `close` | float | 收盘价 |
| `pct_chg` | float | 涨跌幅（%） |
| `amount` | float | 成交额（元） |
| `buy` | float | **龙虎榜买入额（元）** |
| `sell` | float | **龙虎榜卖出额（元）** |
| `net` | float | **净买入额（元）** |
| `buy_count` | int | 买入席位数 |
| `sell_count` | int | 卖出席位数 |
| `direction` | str | 方向 |

### `top_list` — 龙虎榜机构/营业部交易明细

| 参数 | 类型 | 必选 | 描述 |
|:----|:----|:----:|:------|
| `trade_date` | str | N | 交易日期 |
| `ts_code` | str | N | 股票代码 |
| `start_date` | str | N | 开始 |
| `end_date` | str | N | 结束 |

### 返回字段

| 字段 | 类型 | 描述 |
|:----|:----:|:------|
| `trade_date` | str | 日期 |
| `ts_code` / `name` | str | 股票代码/名称 |
| `buy` | float | **买入金额（元）** |
| `sell` | float | **卖出金额（元）** |
| `buy_rate` | float | 买入占总成交比（%） |
| `sell_rate` | float | 卖出占总成交比（%） |
| `exalter` | str | **营业部/机构名称** |
| `exalter_type` | str | 类型：机构专用 / 券商营业部 |

### ✅ 本项目当前状态
`get_limit_list()` 已实现。`top_list`（机构明细）缺失，Agent9 需要时需补充。

---

## 十二、股票基础信息 — `stock_basic`（doc_id=122 / 2-25）

### 输入参数

| 参数 | 类型 | 必选 | 描述 |
|:----|:----|:----:|:------|
| `ts_code` | str | N | 股票代码 |
| `name` | str | N | 名称 |
| `market` | str | N | 市场：主板/创业板/科创板/CDR/北交所 |
| `list_status` | str | N | 上市状态：`L`上市 `D`退市 `P`暂停，默认`L` |
| `exchange` | str | N | 交易所：`SSE`上交所 `SZSE`深交所 `BSE`北交所 |
| `is_hs` | str | N | 沪深港通：`N`否 `H`沪股通 `S`深股通 |

### 返回字段（核心）

| 字段 | 类型 | 描述 |
|:----|:----:|:------|
| `ts_code` | str | **TS代码** |
| `symbol` | str | 股票代码（纯数字） |
| `name` | str | **股票名称** |
| `area` | str | 所在地域 |
| `industry` | str | **所属行业** |
| `fullname` | str | 股票全称 |
| `market` | str | **市场类型** |
| `exchange` | str | 交易所代码 |
| `list_status` | str | 上市状态 |
| `list_date` | str | **上市日期** |
| `delist_date` | str | 退市日期 |
| `is_hs` | str | 沪深港通标的 |
| `act_name` | str | 实控人名称 |
| `act_ent_type` | str | 实控人企业性质 |

### ✅ 本项目当前状态
`data_provider.py` 的 `TushareProvider.stock_basic()` 已实现。`tushare_client.py` 中**缺失** `get_stock_basic()` 直接封装。

---

## 十三、Tushare MCP Server — AI 智能体本地数据接入

> Tushare 官方已推出 MCP Server，支持 Streamable HTTP 协议，适配 Claude Code、Cursor、Trae、Cline 等主流 AI 工具。

### 配置方式

```json
{
  "mcpServers": {
    "tushareMcp": {
      "url": "https://api.tushare.pro/mcp/token=你的Token"
    }
  }
}
```

### 社区方案对比

| 方案 | 语言 | 工具数 | 安装 | 适用场景 |
|:----|:----:|:------:|:-----|:---------|
| **FinanceMCP** | Node.js | 14 个核心工具 | `npm install -g finance-mcp` | 快速上手，Claude 深度集成 |
| **tushare_MCP** | Python | 52 个专业工具 | `git clone + pip install` | 量化分析，数据全面性需求 |

### FinanceMCP 配置（推荐 — Node.js）

```json
{
  "mcpServers": {
    "finance-mcp": {
      "command": "npx",
      "args": ["-y", "finance-mcp"],
      "env": {
        "TUSHARE_TOKEN": "你的Token"
      }
    }
  }
}
```

### tushare_MCP 配置（Python）

```bash
git clone https://github.com/zhewenzhang/tushare_MCP.git
cd tushare_MCP
pip install -r requirements.txt
echo "TUSHARE_TOKEN=你的Token" > .env
python server.py   # 运行在 http://localhost:8000/mcp
```

### Skills vs MCP 选型

| 维度 | Tushare Skills | Tushare MCP Server |
|:----|:--------------|:------------------|
| 配置难度 | 零配置，一键即用 | 一次配置，永久复用 |
| 自动化策略联动 | 较难嵌入量化闭环 | 全流程自动化 |
| 适配AI环境 | 主流AI平台 | 公有+私有+内网+本地 |
| 定位 | 日常临时查数 | 数量分析、批量建模 |

### ⚠️ 本项目当前状态
- 当前 `.mcp.json` 已清空（使用内置 Cron 工具替代）
- 如后续需要 AI 对话中直接查询 Tushare 实时数据，可配置 FinanceMCP
- 建议：仅在需要实时数据交叉验证时启用，日常流水线仍走 auto_sync DB 同步

---

## 十四、Tushare Skills — AI 助手金融数据能力包

Tushare 官方提供 Skills（AI 助手的"超能力包"），每个 Skill 面向特定金融数据场景，包含领域知识库和最佳实践。

### 安装方式

```bash
# OpenClaw 环境
clawhub install tushare-data

# AI 编程环境
npx skills add https://github.com/waditu-tushare/skills --skill tushare

# 离线安装：下载 https://tushare.pro/files/pro/tushare-data.zip 手动导入
```

### 与本项目关系
- 本项目已自建 10 个 Agent 专用 Skill（`skills/agent*-*/SKILL.md`）
- Tushare Skills 可作外部数据补充参考，但不建议替代项目自有 Skill
- 若未来接入更多 Tushare 数据源，可参考其 Skills 定义模式

---

## 十五、HTTP RESTful API（doc_id=130）— 语言无关的降级方案

### 接口地址
`POST http://api.tushare.pro`（旧地址 `http://api.waditu.com` 也可用）

### 请求格式（JSON Body）

| 参数 | 类型 | 说明 |
|:----|:----|:------|
| `api_name` | string | 接口名称，如 `daily`、`stock_basic` |
| `token` | string | 用户 Token |
| `params` | object | 接口参数，如 `{"ts_code":"000001.SZ"}` |
| `fields` | string | 字段列表，逗号分隔，如 `"ts_code,trade_date,close"` |

### 返回格式

| 字段 | 说明 |
|:----|:------|
| `code` | 0=成功，2002=权限问题 |
| `msg` | 错误信息 |
| `data.fields` | 字段名列表 |
| `data.items` | 数据内容（二维数组） |

### 调用示例

```bash
# cURL
curl -X POST -d '{
  "api_name": "daily",
  "token": "xxx",
  "params": {"ts_code": "000001.SZ", "start_date": "20240101"},
  "fields": "ts_code,trade_date,close,pct_chg"
}' http://api.tushare.pro
```

```python
# Python urllib（无需 pip install tushare）
import json, urllib.request
req = urllib.request.Request('http://api.tushare.pro',
    json.dumps({"api_name":"daily","token":"xxx","params":{}}).encode())
res = json.loads(urllib.request.urlopen(req).read().decode())
import pandas as pd
df = pd.DataFrame(res['data']['items'], columns=res['data']['fields'])
```

### ⚠️ 本项目应用
- `tushare_client.py` 当前仅使用 Python SDK 方式调用
- 若 SDK 版本不支持某些新接口，可降级到 HTTP API 调用
- 若其他 Agent（非 Python 环境）需要数据，HTTP API 是语言无关选项

---

## 十六、多语言 SDK 支持

| SDK | doc_id | 说明 |
|:----|:-----:|:------|
| **Python**（推荐） | 131 | `pip install tushare`，功能最完整 |
| **HTTP API** | 130 | 语言无关，任何语言只需 HTTP POST |
| **Matlab** | 132 | Matlab 金融分析场景 |
| **R** | 133 | R 语言统计分析场景 |
| **Rust**（社区） | — | `tushare-rs-pro` crate，社区维护 |

> 本项目基于 Python 生态，Python SDK 已是首选。其余 SDK 仅供未来拓展参考。

---

## 十七、Web 在线调试工具

Tushare 提供在线调试工具：**[https://tushare.pro/webclient/](https://tushare.pro/webclient/)**

功能：
- ✅ 无需编码，可视化选择接口和参数
- ✅ 直接查看返回结果
- ✅ 自动生成调用代码（Python 示例）
- ✅ 支持 CSV 导出

> 适合快速验证接口返回字段、测试参数组合，再写入正式脚本。

---

## 积分层级速查

| 积分 | 可调用的关键接口 |
|:---:|:----------------|
| **基础** | `daily`, `index_daily`, `stock_basic`, `suspend`, `daily_basic` |
| **120** | `share_float`（限售股解禁） |
| **200** | `index_daily` 正式使用 |
| **300** | `dividend`（分红送股） |
| **2000** | `adj_factor`, `moneyflow`（个股资金流）, `ths_index/ths_daily/ths_member` |
| **5000** | `adj_factor` 高频调用, `moneyflow` 高频, `limit_list`（龙虎榜） |

---

## 关键规则速查

| 场景 | 正确做法 | 错误做法 |
|:----|:--------|:---------|
| 计算涨跌幅 | 用 `pct_chg` 字段（已基于除权后前收计算） | 用 `(close-pre_close)/pre_close*100` |
| 获取前复权价 | `pro_bar(ts_code, adj='qfq')` | 在未复权 `daily` 上直接算指标 |
| 取北向资金 | `north_money = hgt + sgt`（百万元） | 只取 `hgt` 忽略 `sgt` |
| 资金流向分析 | `buy_lg_amount - sell_lg_amount` 算大单净流入 | 用 `net_mf_amount` 当大单 |
| 龙虎榜净买额 | 用 `limit_list.net` 字段 | 手动 `buy - sell`（存在不一致） |
| 复权因子计算 | `close * (adj_factor / latest_adj_factor)` 前复权 | 直接 `close * adj_factor`（那是后复权） |

---

## 📋 Tushare Pro 完整API目录（14大类225个接口）

> 数据来源：https://tushare.pro/document/2 完整接口分类（含5个大模型语料类）
> ✅ = 项目中已使用 | ➕ = 已封装待用 | ⚡ = 建议接入（高价值）

### 1️⃣ 股票数据（39接口）

| API方法 | 说明 | 项目状态 | 建议优先级 |
|:--------|:-----|:--------:|:---------:|
| `stock_basic` | 股票列表（含行业/市场/实控人） | ✅ | — |
| `daily` | 日线行情（未复权） | ✅ | — |
| `weekly` | 周线行情 | ➕ 待封装 | ⭐⭐ |
| `monthly` | 月线行情 | ➕ 待封装 | ⭐ |
| **`adj_factor`** | **复权因子** | ✅ | — |
| **`daily_basic`** | **每日指标（PE/PB/换手率/市值）** | ✅ | — |
| `stk_limit` | 涨跌停价格 | ✅ | — |
| `suspend_d` | 停复牌信息 | ✅ | — |
| `new_share` | IPO新股上市 | — | ⭐ |
| `namechange` | 股票曾用名 | — | ⭐ |
| `st_stock` | ST股票列表 | — | ⭐ |
| **`share_float`** | **限售股解禁** | ✅ | — |
| `block_trade` | 大宗交易 | — | ⭐⭐⭐ |
| `stk_holdernumber` | 股东人数变化 | — | ⭐⭐ |
| `stk_holdchange` | 股东增减持 | — | ⭐⭐ |
| `pledge_stat` | 股权质押统计 | — | ⭐ |
| `repurchase` | 股票回购 | — | ⭐ |
| **`top_list`** | **龙虎榜明细** | ✅ | — |
| `top_inst` | 龙虎榜机构交易 | — | ⭐⭐⭐ |
| **`limit_list`** | **龙虎榜（涨跌停板）** | ✅ | — |
| `limit_list_d` | 涨停连板天梯 | — | ⭐⭐⭐ |
| `margin` | 融资融券汇总 | — | ⭐⭐⭐ |
| `margin_detail` | 融资融券明细 | — | ⭐⭐ |
| **`moneyflow`** | **个股资金流向** | ✅ | — |
| **`moneyflow_dc`** | **个股资金流（东财版）** | ✅ | — |
| `top10_holders` | 前十大股东 | ✅ | — |
| `top10_floatholders` | 前十大流通股东 | — | ⭐⭐ |
| `hk_hold` | 沪深港通持股明细 | — | ⭐⭐⭐ |
| `hsgt_top10` | 沪深港通十大成交 | — | ⭐⭐ |
| `cyq_chips` | 筹码分布 | — | ⭐⭐⭐ |
| `stk_factor` | 技术面因子 | — | ⭐⭐⭐ |
| `stock_company` | 上市公司基本信息 | — | ⭐⭐ |
| `stk_managers` | 管理层信息 | — | ⭐ |
| `broker_monthly` | 券商月度金股 | — | ⭐⭐ |
| `ah_price` | AH股比价 | — | ⭐ |
| `hs_const` | 沪深港通股票列表 | — | ⭐⭐ |
| `stk_openlimit` | 开盘集合竞价 | — | ⭐ |
| `stk_closelimit` | 收盘集合竞价 | — | ⭐ |
| `stk_nine` | 神奇九转指标 | — | ⭐ |

### 2️⃣ 指数数据（18接口）

| API方法 | 说明 | 项目状态 | 建议优先级 |
|:--------|:-----|:--------:|:---------:|
| `index_basic` | 指数基本信息 | ✅ | — |
| **`index_daily`** | **指数日线行情** | ✅ | — |
| `index_weekly` | 指数周线 | ➕ 待封装 | ⭐ |
| `index_monthly` | 指数月线 | ➕ 待封装 | ⭐ |
| `index_weight` | 指数成分和权重 | — | ⭐⭐⭐ |
| **`index_dailybasic`** | **指数每日指标（PE/PB）** | — | ⭐⭐⭐ |
| `index_classify` | 申万行业分类 | — | ⭐⭐ |
| `index_member` | 申万行业成分 | — | ⭐⭐ |
| `sw_daily` | 申万行业指数日行情 | — | ⭐⭐⭐ |
| `ci_member` | 中信行业成分 | — | ⭐ |
| `ci_daily` | 中信行业指数日行情 | — | ⭐ |
| **`ths_daily`** | **同花顺概念指数** | ✅ | — |
| **`ths_member`** | **同花顺概念成分** | ✅ | — |
| `ths_index` | 同花顺概念板块列表 | ✅ | — |
| `dc_daily` | 东财概念指数行情 | — | ⭐⭐ |
| `dc_member` | 东财概念成分 | — | ⭐⭐ |
| `index_global` | 全球主要指数 | — | ⭐⭐⭐ |
| `index_realtime` | 实时指数 | — | ⭐ |

### 3️⃣ 基金数据（11接口）

| API方法 | 说明 | 项目状态 |
|:--------|:-----|:--------:|
| `fund_basic` | 基金基本信息 | ✅ |
| `fund_daily` | ETF日线行情 | ✅ |
| `fund_nav` | 基金净值 | — |
| `fund_div` | 基金分红 | — |
| `fund_portfolio` | 基金持仓 | — |
| `fund_manager` | 基金管理人 | — |
| `fund_adj` | ETF复权因子 | ⭐⭐ |
| `fund_share_etf` | ETF份额规模 | ⭐⭐⭐ |
| `fund_scale` | 基金规模 | — |

### 4️⃣ 财务数据（9接口）

| API方法 | 说明 | 项目状态 |
|:--------|:-----|:--------:|
| `income` | 利润表 | ⭐⭐⭐ **建议接入** |
| `balancesheet` | 资产负债表 | ⭐⭐ |
| `cashflow` | 现金流量表 | ⭐⭐ |
| **`fina_indicator`** | **财务指标（ROE/ROA）** | ✅ |
| `fina_mainbz` | 主营业务构成 | ⭐⭐⭐ |
| **`forecast`** | **业绩预告** | ⭐⭐⭐ **建议接入** |
| `express` | 业绩快报 | ⭐⭐⭐ |
| **`dividend`** | **分红送股** | ✅ |
| `fina_audit` | 审计意见 | ⭐ |

### 5️⃣ 宏观数据（10接口）

| API方法 | 说明 | 项目状态 |
|:--------|:-----|:--------:|
| `cn_gdp` | GDP | ⭐⭐ |
| `cn_cpi` | CPI | ⭐⭐ |
| `cn_ppi` | PPI | ⭐⭐ |
| `cn_pmi` | PMI | ⭐⭐⭐ |
| `cn_m` | 货币供应M1/M2 | ⭐⭐⭐ |
| `shibor` | Shibor利率 | ⭐⭐ |
| `cn_lpr` | LPR利率 | ⭐⭐ |
| `cn_sf` | 社融增量 | ⭐⭐⭐ |
| `libor` | Libor利率 | ⭐ |
| `wz_index` | 温州民间借贷利率 | ⭐ |

### 6️⃣ 通用行情（核心接口）

| API方法 | 说明 | 项目状态 | 建议优先级 |
|:--------|:-----|:--------:|:---------:|
| **`pro_bar`** | **通用行情接口（最强最灵活）** | ➕ 待封装 | ⭐⭐⭐ |
| `rt_min` | 个股实时分钟 | — | ⭐ |
| `rt_idx_min` | 指数实时分钟 | — | ⭐ |
| `rt_etf_min` | ETF实时分钟 | — | ⭐ |

> **`pro_bar` 是本项目目前最缺失的核心接口** — 支持：
> - 所有资产类型：E股票/I指数/FT期货/FD基金/O期权/CB可转债
> - 所有频度：D日/W周/M月/1min/5min/15min/30min/60min
> - 内置复权：qfq前复权/hfq后复权（**仅日线支持**）
> - 内置均线：`ma=[5,10,20]` 返回 ma5/ma10/ma20
> - 内置因子：`factors=['tor','vr']` 返回换手率/量比

---

## 项目API覆盖度总结

| 类别 | 总数 | 已用 | 覆盖率 | 建议接入 |
|:----|:---:|:----:|:------:|:--------|
| 股票数据（含财务/参考） | 107 | 15 | 14% | `pro_bar`、`margin`、`block_trade`、`hk_hold`、`limit_list_d`、`cyq_chips`、`stk_factor` |
| 宏观经济 | 22 | 0 | 0% | `cn_pmi`PMI、`cn_sf`社融、`cn_m`M1/M2（Agent8政策分析急需）|
| 指数 | 16 | 4 | 25% | `index_dailybasic`PE/PB、`index_global`、`index_weight` |
| 基金 | 8 | 2 | 25% | `fund_share_etf`ETF份额 |
| ETF | 7 | 0 | 0% | — |
| 财务指标 | 9 | 2 | 22% | `forecast`业绩预告、`fina_mainbz`主营构成 |
| 大模型语料 | 5 | 0 | 0% | 新闻舆情、公告分类（Agent1参考） |
| 其余（债券/期货/期权/港股/美股/通用/另类/外汇/现货/财富） | 51 | 1 | 2% | — |
| **合计** | **225** | **24** | **~11%** | **7个高优先级新增建议** |

---

## 建议优先接入的API

| 优先级 | API | 用途 | 对接Agent |
|:------:|:----|:-----|:----------|
| 🔴 P0 | `pro_bar` | 统一行情接口（复权/均线/频度/因子） | 全Agent（替代部分daily调用） |
| 🔴 P0 | `limit_list_d` | 涨停连板天梯（连板高度/梯队） | Agent9游资追踪师 |
| 🟡 P1 | `margin` | 融资融券汇总（市场杠杆情绪） | Agent1情报员/Agent2分析师 |
| 🟡 P1 | `hk_hold` | 沪深港通持股明细（外资具体买了谁） | Agent1情报员/Agent8政策 |
| 🟡 P1 | `forecast` | 业绩预告（选股核心基本面因子） | Agent5选股机器人 |
| 🟡 P1 | `index_global` | 全球主要指数（外围市场联动） | Agent1情报员/Agent8政策 |
| 🟢 P2 | `block_trade` | 大宗交易（机构/折价信号） | Agent9游资追踪师 |
| 🟢 P2 | `cyq_chips` | 筹码分布（成本/支撑/压力） | Agent6操盘手 |
| 🟢 P2 | `stk_factor` | 技术面因子（丰富技术指标维度） | Agent2分析师 |
| 🟢 P2 | `index_dailybasic` | 指数PE/PB（估值水位） | Agent2分析师 |
| 🟢 P2 | `cn_pmi` | PMI（制造业景气度） | Agent8政策分析师 |
| 🟢 P2 | `cn_sf` | 社融增量（宏观流动性） | Agent8政策分析师 |

---

*本文由 LLM 基于 Tushare Pro 官方文档中心编译而成（2026-07-07）。*
*接口细节更新请以 https://tushare.pro/document/2 为准。*
*项目API覆盖度统计截止于2026-07-07，已使用24/225接口。*
