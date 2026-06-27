"""读取Excel持仓明细，生成portfolio.json
用法: python scripts/utils/build_portfolio.py [Excel文件路径]
默认: C:\Users\65004\Desktop\持仓明细YYYY-MM-DD.xlsx
"""
import pandas as pd
import json
import os
import sys
import glob
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from scripts.utils.tushare_client import get_daily, pro

# === 1. 读取Excel ===
if len(sys.argv) > 1:
    path = sys.argv[1]
else:
    # 自动找桌面最新的持仓明细文件
    candidates = glob.glob(os.path.expanduser(r'~\Desktop\持仓明细*.xlsx'))
    path = max(candidates, key=os.path.getmtime) if candidates else r'C:\Users\65004\Desktop\持仓明细2026-6-27.xlsx'
print(f'读取: {path}')
df = pd.read_excel(path, header=0)
df['代码'] = df['证券代码'].apply(lambda c: str(c).zfill(6))

def market(c):
    s = str(c).zfill(6)
    if s.startswith('118'): return 'SH'  # 可转债
    if s.startswith(('6','5','7')): return 'SH'
    return 'SZ'

df['全代码'] = df.apply(lambda r: r['代码'] + '.' + market(r['代码']), axis=1)

# 合并重复代码（加权平均成本）
merged = df.groupby(['代码','全代码','证券名称']).agg(
    数量=('持仓数量','sum'),
    成本价=('参考成本价', lambda x: round(
        (x * df.loc[x.index,'持仓数量']).sum() / df.loc[x.index,'持仓数量'].sum(), 3))
).reset_index()

# === 2. 获取最新行情 ===
prices = {}

# 股票日线
stock_codes = ['002352.SZ','002930.SZ','600111.SH','600388.SH',
               '600930.SH','600970.SH','603072.SH','603799.SH']
for c in stock_codes:
    d = get_daily(c, '20260626', '20260626')
    if d is not None and len(d) > 0:
        prices[c] = float(d.iloc[0]['close'])

# ETF基金
fund_codes = ['159755.SZ','159915.SZ','510300.SH','511010.SH','511260.SH',
              '511360.SH','511380.SH','512890.SH','518880.SH','588050.SH']
for c in fund_codes:
    f = pro.fund_daily(ts_code=c, trade_date='20260626')
    if f is not None and len(f) > 0:
        prices[c] = float(f.iloc[0]['close'])

print(f'已获取 {len(prices)} 个品种行情')

# === 3. 构建持仓列表 ===
def classify(code, name):
    if code.startswith(('718','733')): return '待上市发债'
    if code.startswith('400'): return '老三板'
    if code.startswith('118'): return '可转债'
    if code.startswith(('15','51','58')) and 'ETF' in name: return 'ETF'
    if code.startswith('68'): return '科创板股票'
    return '股票'

items = []
total_mv = 0

for _, row in merged.iterrows():
    code = row['代码']
    full = row['全代码']
    name = row['证券名称']
    qty = int(row['数量'])
    cost = float(row['成本价'])
    typ = classify(code, name)
    cur = prices.get(full, cost)  # 无行情则用成本价

    # 处理负成本（分红除权导致）
    note = None
    if cost < 0:
        note = f"原始成本{cost}元（分红除权导致负成本），已调整为名义成本0.01"
        cost = 0.01

    mv = round(qty * cur, 2)
    pl = round((cur - cost) / cost * 100, 2) if cost > 0 else 0.0
    if abs(pl) < 0.01:
        pl = 0.0

    entry = {
        '代码': full, '名称': name, '类型': typ,
        '成本价': round(cost, 3), '当前价': cur,
        '持股数量': qty, '市值': mv,
        '盈亏比例': pl
    }
    if note:
        entry['备注'] = note
    items.append(entry)
    total_mv += mv

# 按市值排序
items.sort(key=lambda x: x['市值'], reverse=True)
total_mv = round(total_mv, 2)
total_cost = round(sum(it['成本价'] * it['持股数量'] for it in items), 2)
total_pnl = round(sum((it['当前价'] - it['成本价']) * it['持股数量'] for it in items), 2)

for it in items:
    it['仓位比例'] = round(it['市值'] / total_mv * 100, 2)

# === 4. 风控警示 ===
warnings = []
for it in items:
    if it['类型'] not in ('股票', '科创板股票'):
        continue
    if it['盈亏比例'] < -7:
        warnings.append(f"⚠️ {it['名称']}({it['代码']}) {it['盈亏比例']}% 已触发-7%止损线")
    if it['仓位比例'] > 20:
        warnings.append(f"⚠️ {it['名称']}({it['代码']}) {it['仓位比例']}%仓位 超过震荡市单票上限20%")

# 行业集中度检查
stock_items = [it for it in items if it['类型'] in ('股票', '科创板股票')]
stock_mv = sum(it['市值'] for it in stock_items)
for it in stock_items:
    if stock_mv > 0 and it['仓位比例'] > 0:
        ratio_in_stocks = it['市值'] / stock_mv * 100
        if ratio_in_stocks > 40:
            warnings.append(f"🔴 {it['名称']}({it['代码']}) 占股票仓位{ratio_in_stocks:.1f}%，集中度过高")

# 总体信息
warnings.append(f"ℹ️ 持仓总市值: {total_mv:,.0f} 元 | 总盈亏: {total_pnl:+,.0f} 元 ({round(total_pnl/total_cost*100,2) if total_cost else 0}%)")
warnings.append(f"ℹ️ 股票仓位合计: {round(sum(it['仓位比例'] for it in stock_items),1)}% | ETF/债券仓位: {round(sum(it['仓位比例'] for it in items if it['类型']=='ETF'),1)}%")
warnings.append(f"⚠️ 提示: 总资产和可用余额请手动填写（本数据不含现金）")

# === 5. 输出 ===
portfolio = {
    '说明': '持仓信息 — 来自Excel真实持仓',
    '数据来源': 'C:/Users/65004/Desktop/持仓明细2026-6-27.xlsx',
    '更新日期': '2026-06-27',
    '总资产_含现金': None,
    '可用余额': None,
    '持仓总市值': total_mv,
    '总成本': total_cost,
    '总盈亏': total_pnl,
    '持仓数量': len(items),
    '持仓列表': items,
    '风控警示': warnings
}

# 写入文件（项目根目录 data/）
base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
data_dir = os.path.join(base_dir, 'data')
out_path = os.path.join(data_dir, 'portfolio.json')

with open(out_path, 'w', encoding='utf-8') as f:
    json.dump(portfolio, f, ensure_ascii=False, indent=2)

print(f'\n✅ 已写入: {out_path}')
print(f'持仓总市值: {total_mv:,.2f}')
print(f'总成本: {total_cost:,.2f}')
print(f'总盈亏: {total_pnl:+,.2f}')
