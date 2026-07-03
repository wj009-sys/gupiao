"""
模板渲染辅助 — 使用原生 Jinja2 Environment，绕过 Starlette Jinja2Templates 的 cache 问题
"""
from pathlib import Path
from jinja2 import Environment, FileSystemLoader

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
loader = FileSystemLoader(str(TEMPLATES_DIR))
env = Environment(loader=loader, autoescape=False)


def render_template(name: str, **context) -> str:
    """渲染模板并返回 HTML 字符串"""
    template = env.get_template(name)
    return template.render(**context)
