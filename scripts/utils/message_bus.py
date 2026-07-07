"""
Agent Teams MessageBus — 文件收件箱式异步通信

基于 learn-claude-code s15 Agent Teams 设计，使用 JSONL 文件作为 Agent 间消息通道。
每个 Agent 一个收件箱文件（.jsonl），消息 append-only 写入，读后删除（消费式）。

设计原则：
- 零外部依赖：纯文件 I/O，不需要数据库或消息队列
- 简单可靠：append-only 写入无竞争，read+unlink 消费式读取
- 可观察：inbox 文件是纯文本 JSONL，可直接 cat 查看

消息类型：
  message             — 普通文本消息（通用）
  task_assignment    — Lead→Agent：分配任务
  result             — Agent→Lead：任务完成结果
  progress           — Agent→Lead：进度更新
  question           — 双向：提问
  answer             — 双向：回答
  shutdown_request   — Lead→Agent：请求体面关机
  shutdown_response  — Agent→Lead：回应关机请求（含 approve 字段）
  plan_approval_request  — Agent→Lead：请求审批计划
  plan_approval_response — Lead→Agent：审批结果（含 approve 字段）

用法：
    from scripts.utils.message_bus import MessageBus

    bus = MessageBus()
    bus.send("lead", "agent1", "task_assignment", "采集今日情报", {"priority": "high"})
    msgs = bus.read_inbox("agent1")
    for m in msgs:
        print(f"  [{m['type']}] {m['from']} → {m['to']}: {m['content'][:50]}")

    # 检查收件箱是否有消息（不消费）
    if bus.inbox_exists("agent1"):
        msgs = bus.read_inbox("agent1")

D3异常处理表：
| 触发条件 | 一线修复 | 仍失败兜底 |
|---------|---------|-----------|
| inbox 文件不存在 | 返回空列表 [] | 调用方检查 len>0 |
| inbox 文件损坏（非法 JSON） | 跳过损坏行，读可恢复的行 | 最后报告损坏行数 |
| inbox 文件编码异常 | 先用 utf-8，失败回退到 utf-8-sig | 返回空列表 |
| 发送时目录不存在 | 自动递归创建 MAILBOX_DIR | 抛出 OSError 提示路径 |
| 多线程竞争（同时读写同文件） | 消费式读（read+unlink），append-only 写天然安全 | 极端并发下可能丢消息（Phase 1 不实现文件锁） |

D4 CHECKPOINT:
- CP1-目录就绪：发送前自动确保 MAILBOX_DIR 存在
- CP2-消息完整性：每次 send 写入完整 JSON（json.dumps + ensure_ascii=False）
- CP3-消费确认：read_inbox 后立即 unlink，避免重复消费
- CP4-类型校验：消息类型在定义范围内（硬编码列表校验）
- CP5-编码安全：所有文本操作使用 utf-8 编码

D9反例：
- 不要用内存队列代替文件收件箱（进程崩溃丢消息）
- 不要 read_inbox 后不 unlink（重复消费导致死循环）
- 不要假设备份文件（send 后立刻在另一个进程可读）
- 不要给 MessageBus 加复杂的路由逻辑（保持简单 append+unlink）
"""
import os
import json
import time
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# ===== 常量 =====

# 有效消息类型（硬编码白名单，校验用）
VALID_MESSAGE_TYPES = frozenset({
    "message",
    "task_assignment",
    "result",
    "progress",
    "question",
    "answer",
    "shutdown_request",
    "shutdown_response",
    "plan_approval_request",
    "plan_approval_response",
})

# 默认邮箱目录（相对于项目根）
DEFAULT_MAILBOX_DIR = os.path.join(".claude", "teams", "inboxes")

# 消息元数据默认字段（创建时自动填充）
RESERVED_FIELDS = frozenset({"from", "to", "type", "content", "ts", "msg_id"})


# ===== 工具函数 =====

def _get_project_root() -> str:
    """获取项目根目录（向上找 .git 或 CLAUDE.md）"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    # scripts/utils/ → 项目根 = ../../  （向上2层）
    candidate = os.path.abspath(os.path.join(script_dir, "..", ".."))
    # 验证根目录特征
    if os.path.isdir(os.path.join(candidate, ".git")) or os.path.isfile(os.path.join(candidate, "CLAUDE.md")):
        return candidate
    # fallback: 再往上找一层（特殊情况）
    parent = os.path.dirname(candidate)
    if os.path.isdir(os.path.join(parent, ".git")):
        return parent
    return candidate  # 默认返回


def _ensure_dir(path: str) -> bool:
    """确保目录存在，成功返回 True"""
    try:
        os.makedirs(path, exist_ok=True)
        return True
    except Exception as e:
        logger.error(f"[MessageBus] 创建目录失败 {path}: {e}")
        return False


def _validate_msg_type(msg_type: str) -> bool:
    """校验消息类型是否在白名单内"""
    # message 是通用类型，始终允许
    if msg_type == "message":
        return True
    return msg_type in VALID_MESSAGE_TYPES


def _read_inbox_file(filepath: str) -> list[dict]:
    """读取 inbox 文件，返回解析后的消息列表（不删除文件）

    处理损坏行：跳过非法的 JSON 行，记录警告。
    """
    if not os.path.exists(filepath):
        return []

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except UnicodeDecodeError:
        # try utf-8-sig (带BOM)
        try:
            with open(filepath, "r", encoding="utf-8-sig") as f:
                lines = f.readlines()
        except Exception as e:
            logger.warning(f"[MessageBus] 编码异常 {filepath}: {e}")
            return []
    except Exception as e:
        logger.warning(f"[MessageBus] 读取异常 {filepath}: {e}")
        return []

    messages = []
    corrupt_count = 0
    for i, line in enumerate(lines, 1):
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            if not isinstance(msg, dict):
                corrupt_count += 1
                continue
            messages.append(msg)
        except json.JSONDecodeError:
            corrupt_count += 1
            continue

    if corrupt_count > 0:
        logger.warning(f"[MessageBus] {filepath}: {corrupt_count} 行损坏已跳过")

    return messages


# ===== MessageBus 类 =====

class MessageBus:
    """文件收件箱式 Agent 间消息总线

    每个 Agent 一个 .jsonl 收件箱文件。发消息 append 一行 JSON，
    读消息读完即删（消费式）。不依赖外部进程，纯文件 I/O。

    Attributes:
        mailbox_dir: 收件箱目录的绝对路径
    """

    def __init__(self, mailbox_dir: Optional[str] = None):
        """初始化 MessageBus

        Args:
            mailbox_dir: 收件箱目录路径。默认为 PROJECT_ROOT/.claude/teams/inboxes/
        """
        if mailbox_dir:
            self.mailbox_dir = os.path.abspath(mailbox_dir)
        else:
            self.mailbox_dir = os.path.join(_get_project_root(), DEFAULT_MAILBOX_DIR)
        self._inbox_ready = False

    def _ensure_ready(self) -> bool:
        """确保收件箱目录就绪"""
        if not self._inbox_ready:
            self._inbox_ready = _ensure_dir(self.mailbox_dir)
        return self._inbox_ready

    @staticmethod
    def _validate_agent_name(name: str) -> str:
        """校验 agent 名称合法性

        规则：只允许字母、数字、连字符、下划线，长度 1-64。
        返回规范化的名称（去除首尾空白）。
        """
        name = name.strip()
        if not name:
            raise ValueError("Agent 名称不能为空")
        if len(name) > 64:
            raise ValueError(f"Agent 名称过长 ({len(name)} > 64)")
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
        for c in name:
            if c not in allowed:
                raise ValueError(f"Agent 名称包含非法字符 '{c}'（只允许字母、数字、-、_）")
        return name

    def _inbox_path(self, agent_name: str) -> str:
        """生成收件箱文件路径（调用前已校验名称）"""
        safe_name = self._validate_agent_name(agent_name)
        return os.path.join(self.mailbox_dir, f"{safe_name}.jsonl")

    def send(self, from_agent: str, to_agent: str, msg_type: str = "message",
             content: str = "", metadata: Optional[dict] = None) -> bool:
        """发送消息到指定 Agent 的收件箱

        Args:
            from_agent: 发送方 Agent 名称
            to_agent: 接收方 Agent 名称
            msg_type: 消息类型（message/task_assignment/result/progress 等）
            content: 消息内容（文本）
            metadata: 可选元数据字典

        Returns:
            发送成功返回 True，失败返回 False
        """
        if not self._ensure_ready():
            return False

        # 校验消息类型
        if not _validate_msg_type(msg_type):
            logger.warning(f"[MessageBus] 未知消息类型 '{msg_type}'，仍将发送")

        # 构造消息
        msg = {
            "from": from_agent,
            "to": to_agent,
            "type": msg_type,
            "content": content,
            "metadata": metadata or {},
            "ts": time.time(),
            "msg_id": f"{int(time.time() * 1000)}_{from_agent[:8]}",
        }

        # 写入收件箱（append-only）
        inbox_path = self._inbox_path(to_agent)
        try:
            with open(inbox_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
            logger.debug(f"[MessageBus] {from_agent} → {to_agent} ({msg_type}): {content[:60]}")
            return True
        except Exception as e:
            logger.error(f"[MessageBus] 发送失败 {from_agent}→{to_agent}: {e}")
            return False

    def read_inbox(self, agent_name: str) -> list[dict]:
        """读取并消费指定 Agent 的收件箱

        消费式读取：读完文件全部内容 → 解析 → 删除文件。
        如需检查但不消费，使用 inbox_exists()。

        Args:
            agent_name: Agent 名称

        Returns:
            消息列表（按写入时间排序），空列表表示无消息
        """
        inbox_path = self._inbox_path(agent_name)

        # 读取
        messages = _read_inbox_file(inbox_path)

        # 如果读取成功且有内容，删除文件（消费式）
        if messages:
            try:
                os.unlink(inbox_path)
            except FileNotFoundError:
                pass  # 已被其他进程消费
            except Exception as e:
                logger.warning(f"[MessageBus] 删除收件箱失败 {inbox_path}: {e}")

        return messages

    def inbox_exists(self, agent_name: str) -> bool:
        """检查收件箱是否有消息（不消费）

        只检查文件是否存在且非空，不读取内容。

        Args:
            agent_name: Agent 名称

        Returns:
            有消息返回 True
        """
        inbox_path = self._inbox_path(agent_name)
        if not os.path.exists(inbox_path):
            return False
        try:
            return os.path.getsize(inbox_path) > 0
        except OSError:
            return False

    def peek_inbox(self, agent_name: str, max_messages: int = 5) -> list[dict]:
        """偷看收件箱（读取但不删除）

        用于在不消费的情况下查看消息队列状态。
        注意 peek 后再 read 可能会读到重复消息（极短时间内）。

        Args:
            agent_name: Agent 名称
            max_messages: 最大返回条数

        Returns:
            消息列表（前 max_messages 条）
        """
        inbox_path = self._inbox_path(agent_name)
        all_msgs = _read_inbox_file(inbox_path)
        return all_msgs[:max_messages]

    def clear_inbox(self, agent_name: str) -> bool:
        """清空收件箱（直接删除文件）

        Args:
            agent_name: Agent 名称

        Returns:
            成功返回 True，文件不存在也返回 True
        """
        inbox_path = self._inbox_path(agent_name)
        try:
            if os.path.exists(inbox_path):
                os.unlink(inbox_path)
            return True
        except Exception as e:
            logger.warning(f"[MessageBus] 清空收件箱失败 {inbox_path}: {e}")
            return False

    def list_inboxes(self) -> list[dict]:
        """列出所有非空收件箱

        Returns:
            每个元素: {"agent": agent_name, "size": bytes, "modified": timestamp}
        """
        if not os.path.isdir(self.mailbox_dir):
            return []

        results = []
        try:
            for fname in os.listdir(self.mailbox_dir):
                if not fname.endswith(".jsonl"):
                    continue
                fpath = os.path.join(self.mailbox_dir, fname)
                try:
                    stat = os.stat(fpath)
                    if stat.st_size > 0:
                        results.append({
                            "agent": fname[:-6],  # 去掉 .jsonl
                            "size": stat.st_size,
                            "modified": stat.st_mtime,
                        })
                except OSError:
                    continue
        except Exception as e:
            logger.warning(f"[MessageBus] 列出收件箱失败: {e}")

        return sorted(results, key=lambda x: x["agent"])

    @property
    def mailbox_path(self) -> str:
        """收件箱目录路径"""
        return self.mailbox_dir


# ===== 自测 =====

def _test():
    """MessageBus 自测"""
    import tempfile
    import time

    print("=" * 50)
    print("  MessageBus 自测")
    print("=" * 50)

    # 使用临时目录
    with tempfile.TemporaryDirectory(prefix="message_bus_test_") as tmpdir:
        bus = MessageBus(mailbox_dir=tmpdir)
        assert bus.mailbox_path == os.path.abspath(tmpdir), "mailbox_path 不匹配"
        print(f"  ✅ 初始化: {bus.mailbox_path}")

        # 1. 发送消息
        ok = bus.send("lead", "agent1", "task_assignment", "采集今日情报", {"priority": "high"})
        assert ok, "发送失败"
        print("  ✅ send: lead → agent1")

        # 2. 检查收件箱
        exists = bus.inbox_exists("agent1")
        assert exists, "inbox_exists 应为 True"
        print("  ✅ inbox_exists(agent1) = True")

        # 3. 读取收件箱
        msgs = bus.read_inbox("agent1")
        assert len(msgs) == 1, f"应读取到 1 条消息，实际 {len(msgs)}"
        assert msgs[0]["type"] == "task_assignment"
        assert msgs[0]["content"] == "采集今日情报"
        assert msgs[0]["from"] == "lead"
        assert msgs[0]["to"] == "agent1"
        assert "msg_id" in msgs[0]
        assert "ts" in msgs[0]
        print("  ✅ read_inbox: 1 条消息，字段完整")

        # 4. 读取后收件箱应为空
        exists2 = bus.inbox_exists("agent1")
        assert not exists2, "消费后 inbox_exists 应为 False"
        msgs2 = bus.read_inbox("agent1")
        assert len(msgs2) == 0, "消费后读取应为空"
        print("  ✅ 消费后收件箱为空")

        # 5. 多种消息类型
        bus.send("agent1", "lead", "result", "情报采集完成", {"count": 15})
        bus.send("agent1", "lead", "progress", "50%", {})
        bus.send("lead", "agent2", "task_assignment", "技术分析")
        msgs = bus.read_inbox("lead")
        assert len(msgs) == 2, f"lead 应有 2 条消息，实际 {len(msgs)}"
        types = [m["type"] for m in msgs]
        assert "result" in types
        assert "progress" in types
        print(f"  ✅ 多种类型: {types}")

        # 6. list_inboxes
        bus.send("agent2", "lead", "result", "done")
        all_inboxes = bus.list_inboxes()
        assert len(all_inboxes) > 0
        print(f"  ✅ list_inboxes: {len(all_inboxes)} 非空收件箱")

        # 7. clear_inbox
        bus.send("lead", "clean_test", "message", "to be cleared")
        assert bus.inbox_exists("clean_test")
        bus.clear_inbox("clean_test")
        assert not bus.inbox_exists("clean_test")
        print("  ✅ clear_inbox: 清空成功")

        # 8. peek 不消费
        bus.send("lead", "peek_test", "message", "peek me")
        peeked = bus.peek_inbox("peek_test")
        assert len(peeked) == 1
        assert bus.inbox_exists("peek_test")  # 文件还在
        msgs = bus.read_inbox("peek_test")
        assert len(msgs) == 1  # 仍然能读到
        print("  ✅ peek_inbox: 不消费")

        # 9. 不存在的收件箱
        assert bus.read_inbox("nonexistent") == []
        assert not bus.inbox_exists("nonexistent")
        print("  ✅ 不存在的收件箱返回空列表")

        # 10. 非法 agent 名称应报错
        try:
            bus.send("lead", "bad agent!", "message", "test")
            assert False, "应抛出 ValueError"
        except ValueError:
            pass
        try:
            bus.send("lead", "", "message", "test")
            assert False, "应抛出 ValueError（空名称）"
        except ValueError:
            pass
        try:
            bus._validate_agent_name("a" * 65)
            assert False, "应抛出 ValueError（超长）"
        except ValueError:
            pass
        print("  ✅ 非法 agent 名称校验通过")

        print(f"\n  {'='*40}")
        print(f"  全部 10 项测试通过 ✅")
        print(f"  {'='*40}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    _test()
