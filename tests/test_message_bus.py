"""
测试 MessageBus — message_bus.py

验证文件收件箱式 Agent 间通信的完整生命周期。
"""
import os
import json
import tempfile
import pytest
from unittest.mock import patch, MagicMock


class TestMessageBus:
    """MessageBus 核心功能"""

    def test_send_and_read(self):
        """验证 send → read 完整链路"""
        from scripts.utils.message_bus import MessageBus
        with tempfile.TemporaryDirectory(prefix="test_mbus_") as tmpdir:
            bus = MessageBus(mailbox_dir=tmpdir)

            ok = bus.send("lead", "agent1", "task_assignment",
                          "采集今日情报", {"priority": "high"})
            assert ok

            msgs = bus.read_inbox("agent1")
            assert len(msgs) == 1
            assert msgs[0]["from"] == "lead"
            assert msgs[0]["to"] == "agent1"
            assert msgs[0]["type"] == "task_assignment"
            assert msgs[0]["content"] == "采集今日情报"
            assert "msg_id" in msgs[0]
            assert "ts" in msgs[0]

    def test_read_consumes(self):
        """验证读取后收件箱被消费"""
        from scripts.utils.message_bus import MessageBus
        with tempfile.TemporaryDirectory(prefix="test_mbus_") as tmpdir:
            bus = MessageBus(mailbox_dir=tmpdir)
            bus.send("lead", "agent1", "message", "test")
            bus.read_inbox("agent1")
            # 消费后应该为空
            assert not bus.inbox_exists("agent1")
            assert bus.read_inbox("agent1") == []

    def test_inbox_exists(self):
        """验证 inbox_exists 检测"""
        from scripts.utils.message_bus import MessageBus
        with tempfile.TemporaryDirectory(prefix="test_mbus_") as tmpdir:
            bus = MessageBus(mailbox_dir=tmpdir)

            assert not bus.inbox_exists("agent1")
            bus.send("lead", "agent1", "message", "hello")
            assert bus.inbox_exists("agent1")
            bus.read_inbox("agent1")
            assert not bus.inbox_exists("agent1")

    def test_peek_does_not_consume(self):
        """验证 peek 不消费消息"""
        from scripts.utils.message_bus import MessageBus
        with tempfile.TemporaryDirectory(prefix="test_mbus_") as tmpdir:
            bus = MessageBus(mailbox_dir=tmpdir)
            bus.send("lead", "agent1", "message", "peek test")

            peeked = bus.peek_inbox("agent1")
            assert len(peeked) == 1

            # peek 后 inbox 还在
            assert bus.inbox_exists("agent1")
            msgs = bus.read_inbox("agent1")
            assert len(msgs) == 1  # 仍然能读到

    def test_clear_inbox(self):
        """验证清空收件箱"""
        from scripts.utils.message_bus import MessageBus
        with tempfile.TemporaryDirectory(prefix="test_mbus_") as tmpdir:
            bus = MessageBus(mailbox_dir=tmpdir)
            bus.send("lead", "agent1", "message", "to clear")
            assert bus.inbox_exists("agent1")
            bus.clear_inbox("agent1")
            assert not bus.inbox_exists("agent1")

    def test_list_inboxes(self):
        """验证列出非空收件箱"""
        from scripts.utils.message_bus import MessageBus
        with tempfile.TemporaryDirectory(prefix="test_mbus_") as tmpdir:
            bus = MessageBus(mailbox_dir=tmpdir)
            bus.send("agent1", "lead", "result", "done1")
            bus.send("agent2", "lead", "result", "done2")

            inboxes = bus.list_inboxes()
            agent_names = [i["agent"] for i in inboxes]
            assert "lead" in agent_names
            assert all(i["size"] > 0 for i in inboxes)

    def test_send_fails_on_invalid_name(self):
        """验证非法 Agent 名称抛出异常"""
        from scripts.utils.message_bus import MessageBus
        bus = MessageBus(mailbox_dir=tempfile.mkdtemp())
        with pytest.raises(ValueError):
            bus.send("lead", "bad agent!", "message", "test")
        with pytest.raises(ValueError):
            bus.send("lead", "", "message", "test")

    def test_read_nonexistent_inbox(self):
        """验证读取不存在的收件箱返回空列表"""
        from scripts.utils.message_bus import MessageBus
        bus = MessageBus(mailbox_dir=tempfile.mkdtemp())
        assert bus.read_inbox("nonexistent") == []
        assert not bus.inbox_exists("nonexistent")

    def test_multiple_message_types(self):
        """验证多种消息类型"""
        from scripts.utils.message_bus import MessageBus
        with tempfile.TemporaryDirectory(prefix="test_mbus_") as tmpdir:
            bus = MessageBus(mailbox_dir=tmpdir)
            bus.send("agent1", "lead", "result", "完成", {"count": 15})
            bus.send("agent1", "lead", "progress", "50%", {})

            msgs = bus.read_inbox("lead")
            assert len(msgs) == 2
            types = [m["type"] for m in msgs]
            assert "result" in types
            assert "progress" in types

    def test_validate_agent_name(self):
        """验证 Agent 名称校验规则"""
        from scripts.utils.message_bus import MessageBus
        bus = MessageBus(mailbox_dir=tempfile.mkdtemp())

        # 合法
        assert bus._validate_agent_name("agent1") == "agent1"
        assert bus._validate_agent_name("lead-worker") == "lead-worker"

        # 非法
        with pytest.raises(ValueError):
            bus._validate_agent_name("")
        with pytest.raises(ValueError):
            bus._validate_agent_name("a" * 65)
        with pytest.raises(ValueError):
            bus._validate_agent_name("has space")
        with pytest.raises(ValueError):
            bus._validate_agent_name("chinese中文")
