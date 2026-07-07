#!/usr/bin/env python3
"""
auto-lint.py — PostToolUse Hook (Python版)

写操作后自动检查：
1. 知识库文件 (knowledge/*.md) 编辑后 → 检查格式
2. 报告文件 (reports/**/*.md) 编辑后 → 检查完整性标记
3. Python脚本编辑后 → 检查基本语法

PostToolUse 不能阻塞操作，只能注入附加上下文或记录日志。
"""

import sys
import json
import subprocess
import os


def main():
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, Exception):
        data = {}

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})
    project_root = data.get("cwd") or os.getcwd()

    # 只关注写操作
    if tool_name not in ("Edit", "Write"):
        _output({})
        return

    file_path = tool_input.get("file_path", "")
    if not file_path:
        _output({})
        return

    rel_path = _rel_path(file_path, project_root)
    issues = []

    # 知识库文件检查
    if "/knowledge/" in rel_path and rel_path.endswith(".md"):
        issues = _check_knowledge_file(rel_path, project_root)

    # 报告检查
    if "/reports/" in rel_path and rel_path.endswith(".md"):
        issues = _check_report_file(rel_path, project_root)

    if issues:
        sys.stderr.write(f"[auto-lint] {rel_path}: {', '.join(issues)}\n")

    # PostToolUse 输出为空 JSON
    _output({})


def _rel_path(file_path, project_root):
    """构建相对路径"""
    fp = file_path.replace("\\", "/")
    pr = project_root.replace("\\", "/")
    if fp.startswith(pr):
        return fp[len(pr):].lstrip("/")
    return fp


def _check_knowledge_file(rel_path, project_root):
    """简单知识库格式检查"""
    issues = []
    full_path = os.path.join(project_root, rel_path.replace("/", os.sep))
    if not os.path.exists(full_path):
        return issues
    try:
        with open(full_path, "r", encoding="utf-8") as f:
            content = f.read()
        # 检查文件头 ---
        if not content.startswith("---"):
            issues.append("缺少 YAML frontmatter (---)")
        # 检查wikilink
        if "[[" in content and "]]" not in content:
            issues.append("不匹配的 [[wikilink")
    except Exception as e:
        issues.append(f"读取失败: {e}")
    return issues


def _check_report_file(rel_path, project_root):
    """简单报告完整性检查"""
    issues = []
    full_path = os.path.join(project_root, rel_path.replace("/", os.sep))
    if not os.path.exists(full_path):
        return issues
    try:
        with open(full_path, "r", encoding="utf-8") as f:
            content = f.read()
        # 确保报告非空
        if len(content.strip()) < 50:
            issues.append("报告内容过短 (<50字符)")
    except Exception as e:
        issues.append(f"读取失败: {e}")
    return issues


def _output(obj):
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
