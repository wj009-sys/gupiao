"""
报告浏览服务 — 扫描 reports/ 目录，读取和解析各类报告
"""
import os, re, json
from datetime import datetime, date
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
REPORTS_DIR = PROJECT_ROOT / "reports"

# ── Agent 类型配置 ───────────────────────────────────────────────────
AGENT_TYPES = {
    "情报": {"dir": "情报", "label": "🕵️ 情报员", "icon": "📰"},
    "分析": {"dir": "分析", "label": "📊 分析师", "icon": "📊"},
    "选股": {"dir": "选股", "label": "🔍 选股机器人", "icon": "🎯"},
    "风控": {"dir": "风控", "label": "🛡️ 风控官", "icon": "🛡️"},
    "操盘": {"dir": "操盘", "label": "🎯 操盘手", "icon": "📝"},
    "决策": {"dir": "决策", "label": "🏆 投资领导", "icon": "🏆"},
    "复盘": {"dir": "复盘", "label": "🔄 复盘师", "icon": "🔄"},
}


def get_available_dates() -> list[str]:
    """获取所有有报告的交易日期（去重排序）"""
    dates = set()
    for agent_key, cfg in AGENT_TYPES.items():
        agent_dir = REPORTS_DIR / "日报" / cfg["dir"]
        if not agent_dir.exists():
            continue
        for fname in os.listdir(str(agent_dir)):
            # 匹配 YYYY-MM-DD 日期
            m = re.search(r"(\d{4}-\d{2}-\d{2})", fname)
            if m:
                dates.add(m.group(1))
    return sorted(dates, reverse=True)


def get_reports_for_date(target_date: str) -> list[dict]:
    """获取指定日期的所有 Agent 报告"""
    reports = []
    for agent_key, cfg in AGENT_TYPES.items():
        agent_dir = REPORTS_DIR / "日报" / cfg["dir"]
        if not agent_dir.exists():
            continue
        for fname in sorted(os.listdir(str(agent_dir)), reverse=True):
            if target_date in fname and fname.endswith(".md"):
                fpath = agent_dir / fname
                stats = fpath.stat()
                reports.append({
                    "agent_key": agent_key,
                    "agent_label": cfg["label"],
                    "agent_icon": cfg["icon"],
                    "filename": fname,
                    "filepath": str(fpath),
                    "size": stats.st_size,
                    "mtime": datetime.fromtimestamp(stats.st_mtime),
                    "rel_path": f"日报/{cfg['dir']}/{fname}",
                })
                break  # 每个 agent 只取最新一份
    return reports


def get_report_content(rel_path: str) -> tuple[Optional[str], Optional[str]]:
    """读取报告内容，返回 (html_content, raw_markdown)"""
    fpath = PROJECT_ROOT / "reports" / rel_path
    if not fpath.exists():
        return None, None
    try:
        raw = fpath.read_text("utf-8", errors="replace")
        html = _md_to_html(raw)
        return html, raw
    except Exception as e:
        return f"<p>读取错误: {e}</p>", None


def get_latest_report_for_agent(agent_key: str) -> Optional[dict]:
    """获取某 Agent 的最新报告"""
    cfg = AGENT_TYPES.get(agent_key)
    if not cfg:
        return None
    agent_dir = REPORTS_DIR / "日报" / cfg["dir"]
    if not agent_dir.exists():
        return None
    files = sorted(
        [f for f in os.listdir(str(agent_dir)) if f.endswith(".md")],
        reverse=True,
    )
    if not files:
        return None
    fpath = agent_dir / files[0]
    raw = fpath.read_text("utf-8", errors="replace")
    return {
        "agent_label": cfg["label"],
        "agent_icon": cfg["icon"],
        "filename": files[0],
        "content": _md_to_html(raw),
        "raw": raw,
        "date": _extract_date(files[0]),
    }


def list_markdown_files(agent_key: str = None) -> list[dict]:
    """列出所有报告文件"""
    results = []
    agents = [agent_key] if agent_key else AGENT_TYPES.keys()
    for ak in agents:
        cfg = AGENT_TYPES.get(ak)
        if not cfg:
            continue
        agent_dir = REPORTS_DIR / "日报" / cfg["dir"]
        if not agent_dir.exists():
            continue
        for fname in sorted(os.listdir(str(agent_dir)), reverse=True):
            if not fname.endswith(".md"):
                continue
            fpath = agent_dir / fname
            results.append({
                "agent_key": ak,
                "agent_label": cfg["label"],
                "agent_icon": cfg["icon"],
                "filename": fname,
                "date": _extract_date(fname),
                "size": fpath.stat().st_size,
                "rel_path": f"日报/{cfg['dir']}/{fname}",
            })
    return results


def _extract_date(fname: str) -> str:
    m = re.search(r"(\d{4}-\d{2}-\d{2})", fname)
    return m.group(1) if m else "未知"


def _md_to_html(md_text: str) -> str:
    """将 Markdown 转为简单 HTML"""
    import markdown
    import re

    extensions = [
        "markdown.extensions.fenced_code",
        "markdown.extensions.tables",
        "markdown.extensions.codehilite",
    ]
    try:
        html = markdown.markdown(md_text, extensions=extensions)
        # 嵌入样式
        html = html.replace("<table>", '<table class="table table-striped table-bordered">')
        html = html.replace("<code>", '<code class="bg-light px-1 rounded">')
        return html
    except Exception:
        # 降级: 基本行处理
        lines = []
        for line in md_text.split("\n"):
            if line.startswith("# "):
                lines.append(f"<h1>{line[2:]}</h1>")
            elif line.startswith("## "):
                lines.append(f"<h2>{line[3:]}</h2>")
            elif line.startswith("### "):
                lines.append(f"<h3>{line[4:]}</h3>")
            elif line.startswith("|"):
                lines.append(f"<pre>{line}</pre>")
            elif line.startswith("- ") or line.startswith("* "):
                lines.append(f"<li>{line[2:]}</li>")
            else:
                lines.append(f"<p>{line}</p>")
        return "\n".join(lines)
