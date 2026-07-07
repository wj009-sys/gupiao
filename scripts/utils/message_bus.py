#!/usr/bin/env python3
"""
message_bus.py -- 源自 learn-claude-code s15/s16 Agent Teams + Protocols 模式

JSONL 文件邮箱系统，用于 Agent 间异步通信。
s15格言: "一个搞不定, 组队来 -- 文件收件箱 + 队友线程"
s16格言: "队友之间要有约定 -- 用固定的请求-回复格式沟通"
"""

import sys
import json
import os
import time

TEAMS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", ".claude", "teams")

AGENTS = {
    "Agent1": "情报员", "Agent2": "分析师", "Agent3": "风控官",
    "Agent4": "复盘师", "Agent5": "选股机器人", "Agent6": "操盘手",
    "Agent7": "投资领导", "Agent8": "政策分析师", "Agent9": "游资追踪师",
    "AgentQ": "问股", "orchestrator": "调度器", "lead": "Lead (Agent7 别名)",
}


def _inbox_path(agent_id):
    return os.path.join(TEAMS_DIR, "inboxes", f"{agent_id}.jsonl")


def _ensure_inbox(agent_id):
    inbox = _inbox_path(agent_id)
    os.makedirs(os.path.dirname(inbox), exist_ok=True)
    return inbox


def send(from_agent, to_agent, content, msg_type="message"):
    msg = {
        "from": from_agent, "to": to_agent,
        "type": msg_type, "content": content, "ts": time.time(),
    }
    inbox = _ensure_inbox(to_agent)
    with open(inbox, "a", encoding="utf-8") as f:
        f.write(json.dumps(msg, ensure_ascii=False) + "\n")
    return f"{from_agent} -> {to_agent}: {msg_type}"


def read_inbox(agent_id, consume=True):
    inbox = _inbox_path(agent_id)
    if not os.path.exists(inbox):
        return []
    with open(inbox, "r", encoding="utf-8") as f:
        lines = f.readlines()
    msgs = []
    for line in lines:
        line = line.strip()
        if line:
            try:
                msgs.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    if consume:
        os.remove(inbox)
    return msgs


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="MessageBus Agent 通信")
    sub = parser.add_subparsers(dest="command")

    p_send = sub.add_parser("send")
    p_send.add_argument("--from", dest="from_agent", required=True)
    p_send.add_argument("--to", required=True)
    p_send.add_argument("--content", required=True)
    p_send.add_argument("--type", dest="msg_type", default="message")

    p_read = sub.add_parser("read")
    p_read.add_argument("agent_id")
    p_read.add_argument("--keep", action="store_true")

    sub.add_parser("list")

    args = parser.parse_args()
    if args.command == "send":
        print(send(args.from_agent, args.to, args.content, args.msg_type))
    elif args.command == "read":
        msgs = read_inbox(args.agent_id, consume=not args.keep)
        if not msgs:
            print(f"  {args.agent_id} 收件箱为空")
        else:
            print(f"  {args.agent_id} 收件箱 ({len(msgs)} 条):")
            for m in msgs:
                print(f"  [{m['type']}] {m['from']}: {m['content'][:100]}")
    elif args.command == "list":
        for aid, role in AGENTS.items():
            print(f"  {aid:12s} - {role}")
    else:
        parser.print_help()
