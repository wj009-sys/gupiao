"""
报告浏览路由 — 按日期/Agent 浏览报告
"""
from fastapi import APIRouter, Request, Query
from fastapi.responses import HTMLResponse

from webui.services import report_service
from webui.template_helpers import render_template

router = APIRouter()


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def reports_list(
    request: Request,
    date: str = Query(None, alias="date"),
    agent: str = Query(None, alias="agent"),
):
    dates = report_service.get_available_dates()
    selected_date = date or (dates[0] if dates else None)
    all_reports = report_service.list_markdown_files()

    if selected_date:
        all_reports = [r for r in all_reports if r["date"] == selected_date]
    if agent:
        all_reports = [r for r in all_reports if r["agent_key"] == agent]

    html = render_template(
        "reports.html",
        request=request,
        dates=dates,
        selected_date=selected_date,
        selected_agent=agent or "",
        reports=all_reports,
        page="reports",
        agent_types=report_service.AGENT_TYPES,
    )
    return HTMLResponse(content=html)


@router.get("/view", response_class=HTMLResponse)
async def report_view(request: Request, path: str = Query(...)):
    html_content, raw_md = report_service.get_report_content(path)
    html = render_template(
        "report_detail.html",
        request=request,
        path=path,
        content=html_content or "<p>报告未找到</p>",
        raw=raw_md or "",
        page="reports",
    )
    return HTMLResponse(content=html)


@router.get("/raw", response_class=HTMLResponse)
async def report_raw(request: Request, path: str = Query(...)):
    _, raw_md = report_service.get_report_content(path)
    return HTMLResponse(content=f"<pre>{raw_md}</pre>")
