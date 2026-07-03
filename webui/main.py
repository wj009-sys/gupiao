"""
WebUI 管理界面 — FastAPI 应用入口
运行: uvicorn webui.main:app --reload --host 0.0.0.0 --port 8080
"""
import os, sys
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# 确保 project root 在 path 中
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from webui.routers import dashboard, reports, agents, config_router, ask

# ── FastAPI App ──────────────────────────────────────────────────────
app = FastAPI(
    title="股票投研自动化管理界面",
    description="7 Agent 投研团队 Web 管理界面 - 报告浏览 / Agent 控制 / 配置管理 / 策略问股",
    version="9.0.0",
)

# ── 静态文件 ─────────────────────────────────────────────────────────
static_dir = PROJECT_ROOT / "webui" / "static"
static_dir.mkdir(parents=True, exist_ok=True)
(static_dir / "css").mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

@app.get("/")
async def root_redirect():
    return RedirectResponse(url="/dashboard")

# ── 注册路由 ─────────────────────────────────────────────────────────
app.include_router(dashboard.router, prefix="/dashboard", tags=["dashboard"])
app.include_router(reports.router, prefix="/reports", tags=["reports"])
app.include_router(agents.router, prefix="/agents", tags=["agents"])
app.include_router(config_router.router, prefix="/config", tags=["config"])
app.include_router(ask.router, prefix="/ask", tags=["ask"])

# ── 健康检查 ────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "project": "stock-research", "version": "9.0.0"}


# ── 直接运行入口 ─────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("webui.main:app", host="0.0.0.0", port=8080, reload=True)
