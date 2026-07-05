"""
配置管理服务 — 读写 data/ 目录下的 JSON 配置文件
"""
import os, json
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data"

# ── 可编辑的配置文件清单 ─────────────────────────────────────────────
CONFIG_FILES = {
    "选股规则": {
        "path": "data/选股规则.json",
        "desc": "选股权重配置（4种模式独立权重 + scoring_profile + 热点板块关键词）",
        "type": "json",
    },
    "仓位管理规则": {
        "path": "data/仓位管理规则.json",
        "desc": "仓位上限配置（4种市场环境 + 3种风控风格）",
        "type": "json",
    },
    "止损规则": {
        "path": "data/止损规则.json",
        "desc": "止损量化触发条件配置",
        "type": "json",
    },
    "策略规则": {
        "path": "data/策略规则.json",
        "desc": "买入/卖出策略信号定义",
        "type": "json",
    },
    "持仓信息": {
        "path": "data/portfolio.json",
        "desc": "当前持仓和自选股信息",
        "type": "json",
    },
}


def get_config_list() -> list[dict]:
    """获取配置列表"""
    result = []
    for name, cfg in CONFIG_FILES.items():
        fpath = PROJECT_ROOT / cfg["path"]
        exists = fpath.exists()
        size = fpath.stat().st_size if exists else 0
        result.append({
            "name": name,
            "path": cfg["path"],
            "desc": cfg["desc"],
            "type": cfg["type"],
            "exists": exists,
            "size": size,
        })
    return result


def get_config(rel_path: str) -> Optional[dict]:
    """读取配置文件内容"""
    fpath = PROJECT_ROOT / rel_path
    if not fpath.exists():
        return None
    try:
        raw = fpath.read_text("utf-8", errors="replace")
        if rel_path.endswith(".json"):
            # 格式化 JSON
            try:
                parsed = json.loads(raw)
                formatted = json.dumps(parsed, ensure_ascii=False, indent=2)
                return {"raw": formatted, "parsed": parsed}
            except json.JSONDecodeError:
                return {"raw": raw, "parsed": None}
        return {"raw": raw, "parsed": None}
    except Exception as e:
        return {"raw": f"读取错误: {e}", "parsed": None}


def save_config(rel_path: str, content: str) -> dict:
    """保存配置文件"""
    fpath = PROJECT_ROOT / rel_path
    if not fpath.exists():
        return {"success": False, "error": "文件不存在"}
    try:
        # 验证 JSON 合法性
        if rel_path.endswith(".json"):
            json.loads(content)
        fpath.write_text(content, encoding="utf-8")
        return {"success": True, "message": "保存成功"}
    except json.JSONDecodeError as e:
        return {"success": False, "error": f"JSON 格式错误: {e}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_config_meta(name: str) -> Optional[dict]:
    """按名称获取配置元信息"""
    return CONFIG_FILES.get(name)


# ── DB 信息 ──────────────────────────────────────────────────────────
def get_db_info() -> dict:
    """获取数据库基本信息（含同步状态和覆盖度）"""
    db_path = DATA_DIR / "stocks.db"
    if not db_path.exists():
        return {"exists": False, "size": 0, "tables": [], "message": "数据库文件不存在"}
    try:
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [row[0] for row in cursor.fetchall()]

        # 各表行数
        table_info = {}
        total_rows = 0
        for t in tables:
            try:
                cursor.execute(f"SELECT COUNT(*) FROM [{t}]")
                cnt = cursor.fetchone()[0]
                table_info[t] = cnt
                total_rows += cnt
            except Exception:
                table_info[t] = -1

        # 股票总数
        cursor.execute("SELECT COUNT(DISTINCT ts_code) FROM daily_price")
        stock_count = cursor.fetchone()[0]

        # OBV 覆盖度
        cursor.execute("SELECT COUNT(*), COUNT(obv) FROM daily_indicator")
        obv_total, obv_have = cursor.fetchone()
        obv_pct = round(obv_have * 100 / obv_total, 1) if obv_total else 0

        # 同步状态（关键表）
        sync_tables = [
            ("daily_price", "日线行情"),
            ("daily_basic", "每日估值"),
            ("adj_factor", "复权因子"),
            ("daily_indicator", "技术指标"),
            ("fina_indicator", "财报数据"),
            ("dividend", "分红数据"),
            ("ths_daily", "概念板块"),
            ("moneyflow_hsgt", "北向资金"),
            ("moneyflow_stock", "个股资金"),
            ("moneyflow_mkt", "大盘资金"),
            ("policy_events", "政策事件"),
        ]
        sync_status = []
        for tname, tlabel in sync_tables:
            if tname in table_info:
                rows = table_info[tname]
                cursor.execute(f"SELECT MAX(trade_date) FROM [{tname}]")
                row = cursor.fetchone()
                latest = row[0] if row and row[0] else "—"
                if tname in ("fina_indicator", "dividend"):
                    cursor.execute(f"SELECT MAX(end_date) FROM [{tname}]")
                    row = cursor.fetchone()
                    latest = row[0] if row and row[0] else "—"
                    pct = 100 if rows > 1000 else int(rows / 5000 * 100) if rows else 0
                elif tname == "daily_price":
                    cursor.execute("SELECT MAX(trade_date), COUNT(DISTINCT ts_code) FROM daily_price")
                    r2 = cursor.fetchone()
                    pct = int(r2[1] / stock_count * 100) if stock_count else 0
                elif tname == "daily_indicator":
                    pct = obv_pct
                elif tname == "moneyflow_hsgt":
                    pct = 100 if rows > 100 else int(rows / 2000 * 100)
                elif tname in ("moneyflow_stock", "moneyflow_mkt"):
                    pct = min(int(rows / 50000 * 100), 100) if rows > 100 else int(rows / 100 * 100)
                elif tname == "policy_events":
                    pct = 100 if rows > 50 else 0
                else:
                    pct = 100 if rows > 100 else int(rows / 100 * 100) if rows else 0

                if rows == 0:
                    status = "empty"
                    pct = 0
                elif pct >= 90:
                    status = "fresh"
                elif pct >= 10:
                    status = "partial"
                else:
                    status = "empty"

                sync_status.append({
                    "name": f"{tname} ({tlabel})", "rows": rows,
                    "latest_date": latest, "pct": pct, "status": status,
                })

        conn.close()
        return {
            "exists": True,
            "size": db_path.stat().st_size,
            "size_mb": round(db_path.stat().st_size / 1024 / 1024, 1),
            "tables": tables,
            "table_count": len(tables),
            "total_rows": total_rows,
            "stock_count": stock_count,
            "obv_pct": obv_pct,
            "table_info": table_info,
            "sync_status": sync_status,
        }
    except Exception as e:
        return {"exists": True, "size": db_path.stat().st_size, "error": str(e)}


# ── 环境变量信息 ─────────────────────────────────────────────────────
def get_env_info() -> dict:
    """获取当前环境变量状态（不显示实际值）"""
    checks = {
        "TUSHARE_TOKEN": bool(os.environ.get("TUSHARE_TOKEN")),
        "PUSHPLUS_TOKEN": bool(os.environ.get("PUSHPLUS_TOKEN")),
        "LLM_PROVIDER": os.environ.get("LLM_PROVIDER", "未配置"),
        "LLM_MODEL": os.environ.get("LLM_MODEL", "未配置"),
    }
    return checks
