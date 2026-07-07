#!/usr/bin/env python3
"""
task_manager.py — 源自 learn-claude-code s12 Task System 模式

文件持久化的任务图（DAG），支持 blockedBy 依赖检查。
s12格言: "大目标拆成小任务, 排好序, 持久化 — 多 Agent 协作的基础。"

用法:
    # 创建任务
    python scripts/utils/task_manager.py create --subject "分析报告" --description "生成今日分析" --blocked-by task_abc

    # 认领任务
    python scripts/utils/task_manager.py claim TASK_ID --owner Agent2

    # 更新状态
    python scripts/utils/task_manager.py update TASK_ID --status completed

    # 列出所有任务
    python scripts/utils/task_manager.py list

    # 查看 DAG 依赖
    python scripts/utils/task_manager.py dag TASK_ID
"""

import sys
import json
import os
import time
import secrets
from datetime import datetime

TASKS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", ".tasks")


def _ensure_dir():
    os.makedirs(TASKS_DIR, exist_ok=True)


def _task_path(task_id):
    return os.path.join(TASKS_DIR, f"{task_id}.json")


def random_hex(n=4):
    return secrets.token_hex(n)


def create_task(subject, description="", blocked_by=None):
    _ensure_dir()
    task_id = f"task_{int(time.time())}_{random_hex(4)}"
    task = {
        "id": task_id,
        "subject": subject,
        "description": description,
        "status": "pending",
        "owner": None,
        "blockedBy": blocked_by or [],
        "createdAt": datetime.now().isoformat(),
        "updatedAt": datetime.now().isoformat(),
        "worktree": None,
    }
    save_task(task)
    return task


def save_task(task):
    with open(_task_path(task["id"]), "w", encoding="utf-8") as f:
        json.dump(task, f, ensure_ascii=False, indent=2)


def load_task(task_id):
    path = _task_path(task_id)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def list_tasks(status=None):
    _ensure_dir()
    tasks = []
    for fname in os.listdir(TASKS_DIR):
        if fname.endswith(".json"):
            task = load_task(fname[:-5])
            if task:
                if status is None or task["status"] == status:
                    tasks.append(task)
    tasks.sort(key=lambda t: t.get("createdAt", ""))
    return tasks


def can_start(task_id):
    """检查 blockedBy 依赖是否全部 completed"""
    task = load_task(task_id)
    if not task:
        return False, "任务不存在"
    for dep_id in task.get("blockedBy", []):
        dep = load_task(dep_id)
        if dep is None:
            return False, f"依赖任务 {dep_id} 不存在"
        if dep["status"] != "completed":
            return False, f"依赖任务 {dep_id} 状态为 {dep['status']}，需先完成"
    return True, "可以开始"


def claim_task(task_id, owner):
    """认领任务（检查依赖后）"""
    task = load_task(task_id)
    if not task:
        return f"❌ 任务不存在: {task_id}"
    ok, msg = can_start(task_id)
    if not ok:
        return f"❌ 无法认领: {msg}"
    if task["status"] != "pending":
        return f"❌ 任务状态为 {task['status']}，无法认领"
    task["owner"] = owner
    task["status"] = "in_progress"
    task["updatedAt"] = datetime.now().isoformat()
    save_task(task)
    return f"✅ {owner} 已认领任务 {task_id}: {task['subject']}"


def update_task(task_id, status):
    if status not in ("pending", "in_progress", "completed"):
        return f"❌ 无效状态: {status}"
    task = load_task(task_id)
    if not task:
        return f"❌ 任务不存在: {task_id}"
    task["status"] = status
    task["updatedAt"] = datetime.now().isoformat()
    save_task(task)
    return f"✅ 任务 {task_id} 状态已更新为 {status}"


def show_dag(task_id):
    """显示任务依赖图"""
    task = load_task(task_id)
    if not task:
        return f"❌ 任务不存在: {task_id}"
    lines = [f"\n📊 任务依赖图: {task['id']}"]
    lines.append(f"  📌 {task['subject']} ({task['status']})")

    if task.get("blockedBy"):
        lines.append(f"\n  ⬆️ 依赖 (blockedBy):")
        for dep_id in task["blockedBy"]:
            dep = load_task(dep_id)
            if dep:
                lines.append(f"     ├─ {dep['id']}: {dep['subject']} [{dep['status']}]")
            else:
                lines.append(f"     ├─ {dep_id}: [不存在]")

    # 查找哪些任务依赖此任务
    blocked = [t for t in list_tasks() if task_id in t.get("blockedBy", [])]
    if blocked:
        lines.append(f"\n  ⬇️ 被依赖 (blocks):")
        for b in blocked:
            lines.append(f"     └─ {b['id']}: {b['subject']} [{b['status']}]")

    return "\n".join(lines)


def show_tasks(tasks):
    if not tasks:
        return "📋 当前无任务"
    lines = ["\n📋 任务列表:"]
    for t in tasks:
        icon = {"pending": "⬜", "in_progress": "🔄", "completed": "✅"}.get(t["status"], "⬜")
        owner = f" ({t['owner']})" if t.get("owner") else ""
        deps = f" 依赖: {t['blockedBy']}" if t.get("blockedBy") else ""
        lines.append(f"  {icon} {t['id']}: {t['subject']}{owner} {deps}")
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Task System DAG 任务管理器")
    sub = parser.add_subparsers(dest="command")

    # create
    p_create = sub.add_parser("create", help="创建任务")
    p_create.add_argument("--subject", required=True)
    p_create.add_argument("--description", default="")
    p_create.add_argument("--blocked-by", nargs="*", default=[])

    # claim
    p_claim = sub.add_parser("claim", help="认领任务")
    p_claim.add_argument("task_id")
    p_claim.add_argument("--owner", default="unknown")

    # update
    p_update = sub.add_parser("update", help="更新任务状态")
    p_update.add_argument("task_id")
    p_update.add_argument("--status", choices=["pending", "in_progress", "completed"], required=True)

    # list
    sub.add_parser("list", help="列出所有任务")

    # dag
    p_dag = sub.add_parser("dag", help="查看依赖图")
    p_dag.add_argument("task_id")

    args = parser.parse_args()

    if args.command == "create":
        task = create_task(args.subject, args.description, args.blocked_by)
        print(f"✅ 创建任务: {task['id']}")
        print(f"   📌 {task['subject']}")
        print(f"   ⬆️  blockedBy: {task['blockedBy']}")
    elif args.command == "claim":
        print(claim_task(args.task_id, args.owner))
    elif args.command == "update":
        print(update_task(args.task_id, args.status))
    elif args.command == "list":
        tasks = list_tasks()
        print(show_tasks(tasks))
    elif args.command == "dag":
        print(show_dag(args.task_id))
    else:
        parser.print_help()
