#!/usr/bin/env python3
"""
save-session-summary.py — Stop Hook (Python版)

每次 Claude 响应完成后执行：
1. 运行 knowledge_lint 检查知识库健康度
2. 检查 git 状态，报告未提交变更

安装: 在 .claude/settings.json 中添加
  "Stop": [{
    "matcher": "",
    "hooks": [{
      "type": "command",
      "command": "python .claude/hooks/save-session-summary.py"
    }]
  }]
"""

import sys
import json
import subprocess
import os


def main():
    # 读取 stdin（可能为空）
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, Exception):
        data = {}

    project_root = data.get("cwd") or os.getcwd()

    # 1. 运行知识库健康度检查
    run_knowledge_lint(project_root)

    # 2. 检查 git 状态
    check_git_status(project_root)

    # Stop hook 输出空 JSON
    _output({})


def run_knowledge_lint(project_root):
    """运行 knowledge_lint，只输出健康度摘要"""
    try:
        my_env = os.environ.copy()
        my_env["PYTHONIOENCODING"] = "utf-8"
        result = subprocess.run(
            [sys.executable, "scripts/utils/knowledge_lint.py"],
            cwd=project_root,
            timeout=15,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=my_env,
        )
        stdout = result.stdout or ""
        for line in stdout.split("\n"):
            if "健康度" in line or "HEALTHY" in line or "ERROR" in line or "WARNING" in line:
                sys.stderr.write(f"[session-summary] 知识库: {line.strip()}\n")
                break
        else:
            sys.stderr.write("[session-summary] 知识库检查完成\n")
    except subprocess.TimeoutExpired:
        sys.stderr.write("[session-summary] knowledge_lint 超时，跳过\n")
    except Exception as e:
        sys.stderr.write(f"[session-summary] knowledge_lint 跳过: {e}\n")


def check_git_status(project_root):
    """检查未提交变更"""
    try:
        result = subprocess.run(
            ["git", "status", "--short"],
            cwd=project_root,
            timeout=5,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        output = (result.stdout or "").strip()
        if output:
            lines = output.split("\n")
            sys.stderr.write(f"[session-summary] ⚠️ {len(lines)} 个未提交变更\n")
        else:
            sys.stderr.write("[session-summary] ✅ 工作区干净\n")
    except Exception:
        pass


def _output(obj):
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
