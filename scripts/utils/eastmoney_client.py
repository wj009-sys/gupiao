"""
东方财富板块数据客户端 — 替代 Tushare THS API 获取概念板块行情

用法:
    from scripts.utils.eastmoney_client import get_sector_data
    df = get_sector_data()  # 获取当日实时板块数据

说明:
    - 数据源: push2.eastmoney.com API (fs=m:90)
    - 无需 Tushare 权限，免费公开接口
    - 返回 DataFrame 格式与 pro.ths_daily() 兼容 (ts_code, name, pct_chg 等)
    - 仅支持实时数据（当日），不支持历史日期查询

D3 异常处理:
    触发条件                        一线修复                          仍失败兜底
    ─────────────────────────────  ────────────────────────────────  ──────────────────────────
    East Money API 返回空/超时      等待2秒重试1次                    返回空 DataFrame
    网络连接失败                    检查代理配置                      降级为返回空数据
    JSON 解析失败                   检查返回内容格式                  返回空 DataFrame
    非交易日（数据全为0或无变化）    正常，标记 anomaly=0              继续，数据仍写入 DB

D4 CHECKPOINT:
    [ ] CP1-数量检查: 返回板块数应在 400-1200 之间（概念+行业板块）
    [ ] CP2-字段完整性: f2/f3/f12/f14 必须全部非空
    [ ] CP3-涨跌幅合理性: pct_chg 绝对值不应超过 11%（A股涨跌停限制外可能是新股/异常）
    [ ] CP4-代码格式: 板块代码以 BK 开头

D9 工作反例:
    #  反模式                          为什么不要做                        应该怎么做
    ──  ─────────────────────────────  ────────────────────────────────  ──────────────────────
    1   在循环中频繁调用（不限频）       可能被东方财富临时封IP              批量获取，缓存结果
    2   硬编码字段索引                   字段编号可能随东方财富升级变化      用字段名映射
    3   不检查返回数据就写入DB           空数据会覆盖有效历史                先检查 df 非空
    4   用历史日期参数调用               此API不支持历史查询                 仅用于当日数据
"""

import time
import pandas as pd
import requests
import json
from datetime import datetime
from typing import Optional

# 东方财富API配置
EASTMONEY_API = "https://push2.eastmoney.com/api/qt/clist/get"
EASTMONEY_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/center/boardlist.html",
}

# 字段映射: 东方财富 f_xxx → 输出列名
# f2=最新价(指数点位), f3=涨跌幅%, f4=涨跌额, f12=代码, f14=名称, f20=总市值
# f104=上涨家数, f105=下跌家数, f124=交易日期(时间戳)
FIELD_MAP = {
    "f2": "close",          # 最新价/指数点位
    "f3": "pct_chg",        # 涨跌幅 (%)
    "f4": "change",         # 涨跌额
    "f12": "ts_code",       # 板块代码 (BKxxxx)
    "f14": "name",          # 板块名称
    "f20": "total_mv",      # 总市值
    "f104": "up_count",     # 上涨家数
    "f105": "down_count",   # 下跌家数
    "f124": "timestamp",    # 交易时间戳
}


def _load_proxy() -> Optional[dict]:
    """从 settings.local.json 加载 SOCKS 代理配置"""
    import os
    settings_paths = [
        os.path.join(os.path.dirname(__file__), "..", "..", ".claude", "settings.local.json"),
    ]
    for sp in settings_paths:
        if os.path.exists(sp):
            try:
                with open(sp, "r", encoding="utf-8") as f:
                    s = json.load(f)
                    proxy_url = s.get("env", {}).get("SOCKS_PROXY", "")
                    if proxy_url:
                        return {"http": proxy_url, "https": proxy_url}
            except Exception as e:
                print(f"  [WARN] 读取SOCKS_PROXY配置失败: {e}")
    return None


def get_sector_data(retry: int = 2) -> pd.DataFrame:
    """
    从东方财富获取当日概念板块+行业板块行情数据

    Args:
        retry: 失败重试次数

    Returns:
        DataFrame with columns: ts_code, trade_date, name, pct_chg, close, change,
                                total_mv, up_count, down_count, strength, anomaly
        失败时返回空 DataFrame
    """
    # 先获取总数
    params = {
        "pn": "1",
        "pz": "5",
        "po": "1",
        "np": "1",
        "fltt": "2",
        "invt": "2",
        "fid": "f3",
        "fs": "m:90",
        "fields": "f2,f3,f4,f12,f14,f20,f104,f105,f124",
    }

    proxies = _load_proxy()
    today_str = datetime.now().strftime("%Y%m%d")

    for attempt in range(retry + 1):
        try:
            # 第一次请求：获取总数
            resp = requests.get(
                EASTMONEY_API, params=params,
                headers=EASTMONEY_HEADERS, proxies=proxies,
                timeout=15
            )
            data = resp.json()

            if data.get("rc") != 0 or data.get("data") is None:
                print(f"[EastMoney] API返回异常 rc={data.get('rc')}", flush=True)
                if attempt < retry:
                    time.sleep(2)
                    continue
                return pd.DataFrame()

            total = data["data"].get("total", 0)
            if total == 0:
                print("[EastMoney] 板块数据为空（可能非交易日）", flush=True)
                return pd.DataFrame()

            # 分页拉取全部板块（每页最多100条）
            PAGE_SIZE = 100
            total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
            all_rows = []

            for page in range(1, total_pages + 1):
                params["pn"] = str(page)
                params["pz"] = str(PAGE_SIZE)

                if page > 1:
                    time.sleep(0.15)  # 翻页间隔，避免限频
                    resp = requests.get(
                        EASTMONEY_API, params=params,
                        headers=EASTMONEY_HEADERS, proxies=proxies,
                        timeout=15
                    )
                    page_data = resp.json()
                    if page_data.get("rc") != 0:
                        print(f"[EastMoney] 第{page}页拉取失败 rc={page_data.get('rc')}，跳过", flush=True)
                        continue
                    diff_list = page_data["data"].get("diff", [])
                else:
                    diff_list = data["data"].get("diff", [])

                for item in diff_list:
                    pct_chg = item.get("f3")
                    if pct_chg is None or pct_chg == "-":
                        continue
                    try:
                        pct_chg_val = float(pct_chg)
                    except (ValueError, TypeError):
                        continue

                    ts_code = str(item.get("f12", ""))
                    if not ts_code.startswith("BK"):
                        continue

                    close_val = item.get("f2")
                    try:
                        close_val = float(close_val) if close_val is not None and close_val != "-" else 0.0
                    except (ValueError, TypeError):
                        close_val = 0.0

                    total_mv = item.get("f20")
                    try:
                        total_mv = float(total_mv) if total_mv is not None and total_mv != "-" else 0.0
                    except (ValueError, TypeError):
                        total_mv = 0.0

                    up_count = int(item.get("f104", 0) or 0)
                    down_count = int(item.get("f105", 0) or 0)

                    # strength = 综合强度（基于涨跌幅+上涨占比）
                    total_stocks = up_count + down_count
                    up_ratio = up_count / total_stocks if total_stocks > 0 else 0.5
                    strength = pct_chg_val * (0.5 + 0.5 * up_ratio)

                    # anomaly: 涨跌幅异常标记
                    anomaly = 1 if abs(pct_chg_val) > 10.5 else 0

                    all_rows.append({
                        "ts_code": ts_code,
                        "trade_date": today_str,
                        "name": str(item.get("f14", "")),
                        "pct_chg": pct_chg_val,
                        "close": close_val,
                        "total_mv": total_mv,
                        "up_count": up_count,
                        "down_count": down_count,
                        "strength": round(strength, 2),
                        "anomaly": anomaly,
                    })

                if page % 3 == 0 or page == total_pages:
                    print(f"[EastMoney] 分页进度: {page}/{total_pages} ({len(all_rows)}个板块)", flush=True)

            df = pd.DataFrame(all_rows)

            # D4-CP1: 数量检查
            if len(df) < 400:
                print(f"[EastMoney] WARN: 板块数量偏少 ({len(df)}/{total}), 可能数据不完整", flush=True)

            # D4-CP3: 涨跌幅合理性
            extreme = df[df["pct_chg"].abs() > 11]
            if len(extreme) > 0:
                print(f"[EastMoney] WARN: {len(extreme)}个板块涨跌幅超11%（已标记anomaly）", flush=True)

            up_count = len(df[df['pct_chg'] > 0])
            down_count = len(df[df['pct_chg'] < 0])
            print(f"[EastMoney] 获取 {len(df)}/{total} 个板块 (涨:{up_count} 跌:{down_count})", flush=True)
            return df

        except requests.exceptions.Timeout:
            print(f"[EastMoney] 请求超时 (attempt {attempt+1}/{retry+1})", flush=True)
            if attempt < retry:
                time.sleep(2)
        except requests.exceptions.ConnectionError as e:
            print(f"[EastMoney] 连接失败: {e}", flush=True)
            if attempt < retry:
                time.sleep(2)
        except json.JSONDecodeError as e:
            print(f"[EastMoney] JSON解析失败: {e}", flush=True)
            if attempt < retry:
                time.sleep(2)
        except Exception as e:
            print(f"[EastMoney] 未知错误: {e}", flush=True)
            if attempt < retry:
                time.sleep(2)

    return pd.DataFrame()


def get_ths_daily_fallback(trade_date: Optional[str] = None) -> pd.DataFrame:
    """
    Tushare ths_daily 的东方财富替代实现

    优先使用 Tushare pro.ths_daily()，失败时回退到此函数。
    返回格式兼容 pro.ths_daily() 的主要字段。

    Args:
        trade_date: 目标日期 (YYYYMMDD)，仅当日数据有效

    Returns:
        DataFrame with columns: ts_code, trade_date, name, pct_chg, strength, anomaly
        (与 pro.ths_daily() 返回格式兼容)
    """
    df = get_sector_data()

    if df.empty:
        return pd.DataFrame()

    # 如果指定了非当日日期，标记数据延后
    if trade_date and trade_date != datetime.now().strftime("%Y%m%d"):
        print(f"[EastMoney] 注意: 仅支持当日数据，请求日期{trade_date}与数据日期不匹配", flush=True)

    # 返回与 pro.ths_daily() 兼容的列
    compat_cols = ["ts_code", "trade_date", "name", "pct_chg", "strength", "anomaly"]
    return df[compat_cols].copy()


if __name__ == "__main__":
    # 测试
    print("=" * 60)
    print("  东方财富板块数据测试")
    print("=" * 60)
    df = get_sector_data()
    if not df.empty:
        print(f"\n板块总数: {len(df)}")
        print(f"涨: {(df['pct_chg']>0).sum()} | 跌: {(df['pct_chg']<0).sum()} | 平: {(df['pct_chg']==0).sum()}")
        print(f"\n涨幅前5:")
        for _, row in df.nlargest(5, "pct_chg").iterrows():
            print(f"  {row['ts_code']} {row['name']:<8} {row['pct_chg']:>+7.2f}% 强度:{row['strength']:.1f}")
        print(f"\n跌幅前5:")
        for _, row in df.nsmallest(5, "pct_chg").iterrows():
            print(f"  {row['ts_code']} {row['name']:<8} {row['pct_chg']:>+7.2f}% 强度:{row['strength']:.1f}")
    else:
        print("获取板块数据失败")
