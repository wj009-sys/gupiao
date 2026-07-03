"""
仪表盘路由 — 项目概览、最新报告摘要、环境状态
"""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from webui.services import report_service, config_service, agent_service
from webui.template_helpers import render_template

router = APIRouter()


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    dates = report_service.get_available_dates()
    latest_date = dates[0] if dates else "暂无"

    agent_statuses = agent_service.get_agent_list()
    latest_reports = report_service.get_reports_for_date(latest_date) if dates else []
    db_info = config_service.get_db_info()
    env_info = config_service.get_env_info()
    all_reports = report_service.list_markdown_files()
    total_reports = len(all_reports)
    total_dates = len(dates)

    html = render_template(
        "dashboard.html",
        request=request,
        latest_date=latest_date,
        total_reports=total_reports,
        total_dates=total_dates,
        latest_reports=latest_reports,
        agent_statuses=agent_statuses,
        db_info=db_info,
        env_info=env_info,
        dates=dates[:10],
        page="dashboard",
    )
    return HTMLResponse(content=html)
