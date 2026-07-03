"""
Agent 控制路由 — 查看状态、启动 Agent、查看输出
"""
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse

from webui.services import agent_service
from webui.template_helpers import render_template

router = APIRouter()


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def agents_page(request: Request):
    agents = agent_service.get_agent_list()
    html = render_template(
        "agents.html",
        request=request,
        agents=agents,
        page="agents",
    )
    return HTMLResponse(content=html)


@router.post("/run/{agent_key}")
async def run_agent(agent_key: str, args: str = Form("")):
    result = agent_service.run_agent(agent_key, args)
    return JSONResponse(result)


@router.post("/run-batch")
async def run_batch(data: dict):
    keys = data.get("agents", [])
    if not keys:
        return JSONResponse({"error": "请选择至少一个 Agent"})
    results = agent_service.run_batch(keys)
    return JSONResponse({"results": results})


@router.post("/run-pipeline")
async def run_pipeline():
    result = agent_service.run_pipeline()
    return JSONResponse(result)


@router.get("/status/{agent_key}")
async def agent_status(agent_key: str):
    status = agent_service.get_agent_status(agent_key)
    return JSONResponse(status)


@router.get("/status-all")
async def all_status():
    agents = agent_service.get_agent_list()
    return JSONResponse({"agents": agents})


@router.get("/output/{agent_key}")
async def agent_output(agent_key: str):
    output = agent_service.get_agent_output(agent_key)
    return HTMLResponse(content=f"<pre style='white-space:pre-wrap;font-size:13px'>{output}</pre>")


@router.get("/output-view/{agent_key}", response_class=HTMLResponse)
async def output_view(request: Request, agent_key: str):
    agents = agent_service.get_agent_list()
    status = agent_service.get_agent_status(agent_key)
    html = render_template(
        "agent_output.html",
        request=request,
        agent_key=agent_key,
        agents=agents,
        status=status,
        page="agents",
    )
    return HTMLResponse(content=html)
