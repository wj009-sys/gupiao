#!/usr/bin/env python3
"""
protect-knowledge.py — PreToolUse Hook (Python版)

知识库安全保护钩子。
阻止对知识库核心文件的意外修改，确保只有知识库维护流程可以修改它们。

保护策略:
  1. CLAUDE.md — 项目指令，修改需确认(ask)
  2. knowledge/* — 知识库，确保格式正确
  3. memory/*.md — 持久记忆，仅限记忆系统修改(deny)
  4. .claude/settings.json — 共享配置，修改需确认
  5. .claude/agents/* — Agent定义，修改需确认
  6. data/*.json — 策略规则数据，修改需确认

安装: 在 .claude/settings.json 中添加
  "PreToolUse": [{
    "matcher": "Write|Edit",
    "hooks": [{
      "type": "command",
      "command": "python .claude/hooks/protect-knowledge.py"
    }]
  }]
"""

import sys
import json
import re


def main():
    # 读取 stdin JSON
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, Exception) as e:
        _error_exit(f"JSON 解析失败: {e}")

    file_path = (data.get("tool_input") or {}).get("file_path") or ""
    cwd = data.get("cwd") or ""

    # 构建相对路径
    rel_path = file_path.replace("\\", "/")
    if cwd and rel_path.startswith(cwd.replace("\\", "/")):
        rel_path = rel_path[len(cwd.replace("\\", "/")):]
    if cwd and not rel_path.startswith("/"):
        rel_path = "/" + rel_path

    # 保护规则: (路径模式, 决策, 原因)
    rules = [
        # ⛔ 持久记忆 — 只能通过记忆系统管理
        (r'/memory/.*\.md$', 'deny', 'memory/*.md 只能通过记忆系统修改 — 使用 /remember 命令'),
        # ⚠️ CLAUDE.md — 需确认
        (r'/CLAUDE\.md$', 'ask', 'CLAUDE.md 是项目核心指令，确认是否要修改？'),
        # ⚠️ 知识库文件
        (r'/knowledge/.*\.md$', 'ask', '知识库文件修改需确认，确保格式符合 Karpathy Wiki 规范'),
        (r'/knowledge/INDEX\.md$', 'ask', 'INDEX.md 是知识库索引，确保变更后同步更新'),
        # ⚠️ 共享配置
        (r'/\.claude/settings\.json$', 'ask', '项目共享配置修改会影响所有团队成员，确认？'),
        (r'/\.claude/agents/.*\.md$', 'ask', 'Agent 定义修改会影响子代理行为，确认？'),
        (r'/\.claude/workflows/.*\.js$', 'ask', 'Workflow 脚本修改会影响审计流程，确认？'),
        # ⚠️ 策略数据
        (r'/data/.*\.json$', 'ask', '策略规则数据修改会影响 Agent 决策，确认？'),
    ]

    for pattern, decision, reason in rules:
        if re.search(pattern, rel_path, re.IGNORECASE):
            _output({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": decision,
                    "permissionDecisionReason": reason,
                }
            })
            return

    # 无匹配 — 放行
    _output({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": "文件不在保护列表中",
        }
    })


def _output(obj):
    """输出 JSON 到 stdout"""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.flush()


def _error_exit(msg):
    """错误时放行，不阻塞会话"""
    sys.stderr.write(f"[protect-knowledge] Error: {msg}\n")
    _output({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": "Hook 错误，安全放行",
        }
    })
    sys.exit(0)


if __name__ == "__main__":
    main()
