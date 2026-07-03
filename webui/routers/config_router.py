"""
配置管理路由 — 查看和编辑配置文件
"""
from fastapi import APIRouter, Request, Form, Query
from fastapi.responses import HTMLResponse, JSONResponse

from webui.services import config_service
from webui.template_helpers import render_template

router = APIRouter()


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def config_page(request: Request):
    configs = config_service.get_config_list()
    db_info = config_service.get_db_info()
    env_info = config_service.get_env_info()
    html = render_template(
        "config.html",
        request=request,
        configs=configs,
        db_info=db_info,
        env_info=env_info,
        page="config",
    )
    return HTMLResponse(content=html)


@router.get("/edit", response_class=HTMLResponse)
async def config_edit(request: Request, path: str = Query(...)):
    content = config_service.get_config(path)
    configs = config_service.get_config_list()
    current = next((c for c in configs if c["path"] == path), None)
    if not content:
        html = render_template(
            "config.html",
            request=request,
            configs=configs,
            error=f"文件不存在: {path}",
            page="config",
        )
        return HTMLResponse(content=html)
    html = render_template(
        "config_edit.html",
        request=request,
        current_config=current,
        config_path=path,
        content=content["raw"],
        page="config",
    )
    return HTMLResponse(content=html)


@router.post("/save")
async def config_save(path: str = Form(...), content: str = Form(...)):
    result = config_service.save_config(path, content)
    return JSONResponse(result)


@router.get("/db", response_class=HTMLResponse)
async def db_info_page(request: Request):
    db_info = config_service.get_db_info()
    html = render_template(
        "db_info.html",
        request=request,
        db_info=db_info,
        page="config",
    )
    return HTMLResponse(content=html)
