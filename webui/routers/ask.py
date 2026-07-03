"""
策略问股路由 — 调用 agent_ask 分析股票
"""
import subprocess, json
from pathlib import Path
from fastapi import APIRouter, Request, Form, Query
from fastapi.responses import HTMLResponse, JSONResponse

from webui.template_helpers import render_template

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
router = APIRouter()

STRATEGIES = [
    {"key": "综合", "label": "综合（默认）", "desc": "多指标综合分析"},
    {"key": "均线", "label": "均线分析", "desc": "MA5/10/20/60排列、金叉死叉、乖离率"},
    {"key": "缠论", "label": "缠论分析", "desc": "顶底分型、笔方向、中枢、背驰"},
    {"key": "波浪理论", "label": "波浪理论", "desc": "5浪推动、3浪调整、波段计数"},
    {"key": "MACD背离", "label": "MACD背离", "desc": "价格-MACD背离、零轴位置"},
    {"key": "量价分析", "label": "量价分析", "desc": "量比、量价关系、天量天价"},
    {"key": "RSI强度", "label": "RSI强度", "desc": "RSI(14)/RSI(6)、强弱分界"},
    {"key": "布林带", "label": "布林带分析", "desc": "上中下轨、带宽、变盘信号"},
    {"key": "KDJ", "label": "KDJ分析", "desc": "金叉死叉、J值极端"},
]


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def ask_page(request: Request):
    html = render_template(
        "ask.html",
        request=request,
        strategies=STRATEGIES,
        page="ask",
    )
    return HTMLResponse(content=html)


@router.post("/analyze")
async def analyze(
    request: Request,
    code: str = Form(...),
    strategy: str = Form("综合"),
    mode: str = Form("standard"),
):
    code = code.strip()
    if code.isdigit() and len(code) == 6:
        if code.startswith(("6", "9")):
            code = f"{code}.SH"
        else:
            code = f"{code}.SZ"

    ask_script = PROJECT_ROOT / "scripts" / "agent_ask" / "ask.py"
    python = PROJECT_ROOT / "venv" / "Scripts" / "python.exe"

    cmd = [str(python), "-X", "utf8", str(ask_script),
           "--code", code, "--strategy", strategy, "--mode", mode]

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        output = proc.stdout or proc.stderr or "无输出"
        return JSONResponse({
            "success": proc.returncode == 0,
            "output": output,
            "code": code,
            "strategy": strategy,
            "mode": mode,
        })
    except subprocess.TimeoutExpired:
        return JSONResponse({"success": False, "output": "分析超时（60秒）"})
    except Exception as e:
        return JSONResponse({"success": False, "output": f"执行错误: {e}"})


@router.get("/codes")
async def search_codes(q: str = Query("")):
    if len(q) < 1:
        return JSONResponse({"results": []})
    try:
        import sqlite3
        db_path = PROJECT_ROOT / "data" / "stocks.db"
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute(
            "SELECT ts_code, name FROM stock_basic WHERE name LIKE ? OR ts_code LIKE ? LIMIT 20",
            (f"%{q}%", f"%{q}%"),
        )
        rows = [{"code": r[0], "name": r[1]} for r in cursor.fetchall()]
        conn.close()
        return JSONResponse({"results": rows})
    except Exception as e:
        return JSONResponse({"results": [], "error": str(e)})
