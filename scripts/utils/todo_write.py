#!/usr/bin/env python3
"""
todo_write.py — 源自 learn-claude-code s05 TodoWrite 模式

Agent 执行任务前先用此工具列出计划步骤，追踪进度。
s05格言: "没有计划的 agent 走哪算哪 — 先列步骤再动手，完成率翻倍。"

用法:
    python scripts/utils/todo_write.py --create "我的计划" --step "步骤1:做A" --step "步骤2:做B" --step "步骤3:做C"
    python scripts/utils/todo_write.py --update 1 --status completed
    python scripts/utils/todo_write.py --show
    python scripts/utils/todo_write.py --nag    # 连续3次未更新时注入提醒
"""

import sys
import json
import os

TODO_FILE = os.path.join(os.path.dirname(__file__), "..", "..", ".current_todos.json")


def load_todos():
    if os.path.exists(TODO_FILE):
        with open(TODO_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_todos(todos):
    os.makedirs(os.path.dirname(TODO_FILE), exist_ok=True)
    with open(TODO_FILE, "w", encoding="utf-8") as f:
        json.dump(todos, f, ensure_ascii=False, indent=2)


def show_todos(todos):
    if not todos:
        return "📋 当前无计划"
    lines = ["\n📋 当前任务计划:"]
    for i, t in enumerate(todos, 1):
        icon = {"pending": "⬜", "in_progress": "🔄", "completed": "✅"}.get(t["status"], "⬜")
        lines.append(f"  {icon} [{i}] {t['content']} ({t['status']})")
    return "\n".join(lines)


def create_todos(steps):
    todos = [{"content": s, "status": "pending"} for s in steps]
    save_todos(todos)
    return show_todos(todos)


def update_step(index, status):
    todos = load_todos()
    if 0 <= index < len(todos):
        todos[index]["status"] = status
        save_todos(todos)
        return show_todos(todos)
    return f"❌ 步骤索引 {index} 超出范围 (共 {len(todos)} 步)"


def nag_check(todos, rounds_since_update):
    if rounds_since_update >= 3 and todos:
        return "📢 <reminder>请更新任务进度 — 连续3轮未更新 todo_write</reminder>"
    return None


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="TodoWrite 规划工具")
    parser.add_argument("--create", help="创建新计划")
    parser.add_argument("--step", action="append", help="步骤描述 (可多次)")
    parser.add_argument("--update", type=int, help="更新步骤索引 (1-based)")
    parser.add_argument("--status", choices=["pending", "in_progress", "completed"])
    parser.add_argument("--show", action="store_true", help="显示当前计划")
    parser.add_argument("--nag", type=int, help="距上次更新轮数, 触发提醒")

    args = parser.parse_args()

    if args.create:
        if not args.step:
            print("❌ --create 需要至少一个 --step")
            sys.exit(1)
        print(create_todos(args.step))
    elif args.update is not None and args.status:
        print(update_step(args.update - 1, args.status))
    elif args.show:
        todos = load_todos()
        print(show_todos(todos))
    elif args.nag is not None:
        todos = load_todos()
        msg = nag_check(todos, args.nag)
        if msg:
            print(msg)
    else:
        parser.print_help()
