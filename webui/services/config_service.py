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
    """获取数据库基本信息"""
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
        for t in tables:
            try:
                cursor.execute(f"SELECT COUNT(*) FROM [{t}]")
                table_info[t] = cursor.fetchone()[0]
            except Exception:
                table_info[t] = -1
        conn.close()
        return {
            "exists": True,
            "size": db_path.stat().st_size,
            "size_mb": round(db_path.stat().st_size / 1024 / 1024, 1),
            "tables": tables,
            "table_info": table_info,
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
