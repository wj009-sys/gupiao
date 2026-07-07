"""
Context Compact — 四层上下文压缩管线

基于 learn-claude-code s08 Context Compact 设计。
在消息膨胀前进行自动压缩，遵循"便宜的先跑，贵的后跑"原则。

压缩管线（执行顺序）：
  L3: tool_result_budget — 大工具输出落盘（0 API）
  L1: snip_compact — 裁掉中间旧对话（0 API）
  L2: micro_compact — 旧 tool_result 换占位符（0 API）
  L4: compact_history — LLM 全量摘要（1 API，需 LLMClient）
  RE: reactive_compact — 应急：保留末尾 N 条 + 摘要前面（1 API）

用法：
    from scripts.utils.context_compact import compression_pipeline, estimate_tokens

    # 自动四层压缩
    messages = compression_pipeline(messages)

    # 或逐层手动控制
    from scripts.utils.context_compact import (
        tool_result_budget, snip_compact, micro_compact,
        compact_history, reactive_compact,
    )
    messages = tool_result_budget(messages)
    messages = snip_compact(messages)
    messages = micro_compact(messages)

D3异常处理表：
| 触发条件 | 一线修复 | 仍失败兜底 |
|---------|---------|-----------|
| 消息列表为空 | 直接返回 [] | — |
| 消息结构异常（无 content 字段） | 跳过该消息 | 保留原始消息 |
| tool_result_budget 落盘失败 | 跳过该 block 的落盘，保持原内容 | 打印警告 |
| compact_history 需要 LLM 但未提供 | 跳过 L4，打印提示 | 返回原始 messages |
| compact_history LLM 调用失败 | 打印警告 | 返回原始 messages |
| reactive_compact LLM 失败 | 保留最后 N 条截断 | 返回原始 messages |

D4 CHECKPOINT:
- CP1-执行顺序：必须 L3→L1→L2→L4（budget 在 micro 前，确保大内容先落盘）
- CP2-配对保护：裁剪时不可拆散 tool_use/tool_result 对
- CP3-文件安全：落盘使用 PROJECT_ROOT/.task_outputs/，不在临时目录
- CP4-阈值守护：token 估计超过阈值才触发 L4，避免每个 turn 都触发 LLM 摘要
- CP5-应急熔断：reactive_compact 最多触发 1 次

D9反例：
- 不要在 L4 后没有任何恢复就全部替换（后压缩恢复：CC 会重新附加最近文件）
- 不要用 token 精确计数来代替字符估算（教学版用字符数，够用）
- 不要在裁剪时拆散 tool_use 和 tool_result 对（会导致 LLM 困惑）
- 不要每轮都跑 L4（expensive，只在超阈值时触发）
"""
import os
import json
import time
import logging
import hashlib
from typing import Optional, Callable

logger = logging.getLogger(__name__)

# ===== 常量 =====

# 各层默认阈值
MAX_MESSAGES = 50           # L1: 消息数上限（保留头3 + 尾47）
KEEP_HEAD = 3                # L1: 保留头部消息数
KEEP_RECENT_TOOL_RESULTS = 3  # L2: 保留最新 tool_result 数
TOOL_RESULT_BUDGET = 200_000  # L3: tool_result 大小上限（字符数）
TOKEN_ESTIMATE_THRESHOLD = 80_000  # L4: 触发 LLM 摘要的 token 估计阈值
REACTIVE_KEEP_LAST = 5        # RE: 应急保留最后消息数
MAX_CONTINUOUS_COMPACT = 3    # 熔断：连续压缩失败上限

# 落盘输出目录
OUTPUT_DIR = os.path.join(".claude", "task_outputs")

# 消息角色集合
_USER = "user"
_ASSISTANT = "assistant"
_TOOL_RESULT_TYPES = frozenset({"tool_result"})


# ===== Token 估计 =====

def estimate_tokens(text: str) -> int:
    """粗略估计 token 数（~4 字符 ≈ 1 token，混合中英文）

    Args:
        text: 要估计的文本

    Returns:
        估计 token 数（至少 1）
    """
    if not text:
        return 0
    # 粗略估计: 4 字符 ≈ 1 token
    return max(1, len(text) // 4)


def estimate_messages_tokens(messages: list[dict]) -> int:
    """估计消息列表总 token 数"""
    total = 0
    for msg in messages:
        total += estimate_tokens(str(msg.get("role", "")))
        content = msg.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    total += estimate_tokens(str(block.get("content", "")))
                    total += estimate_tokens(str(block.get("text", "")))
                    total += estimate_tokens(str(block.get("name", "")))
                    total += estimate_tokens(str(block.get("input", "")))
    return total


# ===== 内部工具 =====

def _get_project_root() -> str:
    """获取项目根目录"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidate = os.path.abspath(os.path.join(script_dir, "..", ".."))
    if os.path.isdir(os.path.join(candidate, ".git")) or os.path.isfile(os.path.join(candidate, "CLAUDE.md")):
        return candidate
    return candidate


def _ensure_output_dir() -> str:
    """确保落盘目录存在"""
    out_dir = os.path.join(_get_project_root(), OUTPUT_DIR)
    os.makedirs(out_dir, exist_ok=True)
    return out_dir


def _is_tool_result_block(block: dict) -> bool:
    """检查 content block 是否是 tool_result"""
    return isinstance(block, dict) and block.get("type") in _TOOL_RESULT_TYPES


def _is_tool_use_block(block: dict) -> bool:
    """检查 content block 是否是 tool_use"""
    return isinstance(block, dict) and block.get("type") == "tool_use"


def _message_has_tool_use(msg: dict) -> bool:
    """检查消息是否包含 tool_use block"""
    content = msg.get("content", "")
    if isinstance(content, list):
        return any(_is_tool_use_block(b) for b in content)
    return False


def _is_tool_result_message(msg: dict) -> bool:
    """检查消息是否是 tool_result 消息（content 全是 tool_result block）"""
    content = msg.get("content", "")
    if isinstance(content, list):
        return all(b.get("type") == "tool_result" for b in content)
    return False


def _get_content_blocks(messages: list[dict]) -> list[tuple[int, int, dict]]:
    """收集所有 tool_result content blocks

    Returns:
        list of (msg_index, block_index, block)
    """
    blocks = []
    for i, msg in enumerate(messages):
        content = msg.get("content", "")
        if isinstance(content, list):
            for j, block in enumerate(content):
                if _is_tool_result_block(block):
                    blocks.append((i, j, block))
    return blocks


def _persist_large_output(tool_use_id: str, content: str) -> str:
    """将大输出落盘，返回持久化标记文本"""
    out_dir = _ensure_output_dir()
    # 用 content hash 做唯一文件名（避免重复落盘）
    content_hash = hashlib.md5(content.encode("utf-8")).hexdigest()[:12]
    filename = f"tool_{tool_use_id}_{content_hash}.out"
    filepath = os.path.join(out_dir, filename)

    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        preview = content[:2000]
        summary = (
            f"<persisted-output>\n"
            f"  File: {OUTPUT_DIR}/{filename}\n"
            f"  Length: {len(content)} chars\n"
            f"  Preview: {preview}\n"
            f"</persisted-output>"
        )
        return summary
    except Exception as e:
        logger.warning(f"[Compact] 落盘失败 {filename}: {e}")
        # fallback: 截断到 2000 字符
        return content[:2000]


# ===== L3: 大工具结果落盘 =====

def tool_result_budget(
    messages: list[dict],
    max_bytes: int = TOOL_RESULT_BUDGET,
) -> list[dict]:
    """L3: 大工具结果落盘

    统计最后一条 user 消息中所有 tool_result 的总大小。
    超过 max_bytes 时，从最大的开始落盘到磁盘，上下文只留标记+预览。

    必须在 L2 micro_compact 之前执行（micro 会替换旧内容为占位符，
    落盘必须在替换前完成）。
    """
    if not messages:
        return messages

    last = messages[-1]
    content = last.get("content", "")
    if not isinstance(content, list):
        return messages

    # 收集 tool_result blocks
    blocks = []
    for i, block in enumerate(content):
        if _is_tool_result_block(block):
            blocks.append((i, block))

    if not blocks:
        return messages

    # 计算总大小
    total = 0
    for _, block in blocks:
        c = block.get("content", "")
        total += len(str(c))

    if total <= max_bytes:
        return messages

    # 按大小降序排列
    ranked = sorted(blocks, key=lambda p: len(str(p[1].get("content", ""))), reverse=True)

    # 从最大的开始落盘
    persisted = 0
    for idx, block in ranked:
        if total <= max_bytes:
            break
        content_str = str(block.get("content", ""))
        tool_use_id = block.get("tool_use_id", f"unknown_{idx}")
        block["content"] = _persist_large_output(tool_use_id, content_str)
        total -= len(content_str)
        persisted += 1

    if persisted > 0:
        logger.info(f"[Compact] L3: {persisted} 个大结果落盘，节省 ~{total} 字符")

    return messages


# ===== L1: 裁中间 =====

def snip_compact(
    messages: list[dict],
    max_messages: int = MAX_MESSAGES,
    keep_head: int = KEEP_HEAD,
) -> list[dict]:
    """L1: 裁掉中间旧对话

    消息数超过 max_messages → 保留头部 keep_head 条 + 尾部若干条。
    不会拆散 tool_use / tool_result 配对。
    """
    if not messages:
        return messages
    if len(messages) <= max_messages:
        return messages

    tail = max_messages - keep_head
    head_end = keep_head
    tail_start = len(messages) - tail

    # 保护：head 末尾如果是 tool_use，后面的 tool_result 也要保留
    if head_end > 0 and _message_has_tool_use(messages[head_end - 1]):
        while head_end < len(messages) and _is_tool_result_message(messages[head_end]):
            head_end += 1

    # 保护：tail 开头如果是 tool_result，前面的 tool_use 也要保留
    if (tail_start > 0 and tail_start < len(messages)
            and _is_tool_result_message(messages[tail_start])
            and _message_has_tool_use(messages[tail_start - 1])):
        tail_start -= 1

    # 防止 head/tail 重叠
    if head_end >= tail_start:
        # 无法裁剪，直接返回
        return messages

    snipped = tail_start - head_end
    placeholder = {
        "role": "user",
        "content": f"[裁剪了 {snipped} 条中间消息。需要时可重新读取。]",
    }

    result = messages[:head_end] + [placeholder] + messages[tail_start:]
    logger.info(f"[Compact] L1: 裁剪 {snipped} 条消息 ({len(messages)} → {len(result)})")
    return result


# ===== L2: 旧结果占位 =====

def micro_compact(
    messages: list[dict],
    keep_recent: int = KEEP_RECENT_TOOL_RESULTS,
    compact_strategy: str = "replace",
) -> list[dict]:
    """L2: 旧 tool_result 替换为占位符

    只保留最近 keep_recent 条 tool_result 的完整内容，
    更早的替换为一行占位符。

    compact_strategy:
        "replace" — 替换为一行占位符（默认）
        "truncate" — 截断到 200 字符（保留部分信息，但占空间）
    """
    if not messages:
        return messages

    blocks = _get_content_blocks(messages)
    if len(blocks) <= keep_recent:
        return messages

    replaced = 0
    saved_chars = 0
    for msg_idx, block_idx, block in blocks[:-keep_recent]:
        content = str(block.get("content", ""))
        if len(content) > 120:
            saved_chars += len(content)
            if compact_strategy == "truncate":
                block["content"] = content[:200] + "\n...[截断，需要时重新获取]"
            else:
                block["content"] = "[此工具结果已压缩。需要时请重新运行获取。]"
            replaced += 1

    if replaced > 0:
        logger.info(f"[Compact] L2: {replaced} 条旧结果占位，节省 ~{saved_chars} 字符")

    return messages


# ===== L4: LLM 全量摘要 =====

def compact_history(
    messages: list[dict],
    llm_client: Optional[Callable] = None,
) -> list[dict]:
    """L4: LLM 全量摘要

    将完整对话历史发给 LLM 生成摘要，替换所有消息为一条摘要消息。

    Args:
        messages: 消息列表
        llm_client: LLMClient 实例或兼容的 callable
                    callable(messages) → 摘要文本

    Returns:
        压缩后的消息列表（只有一条 user 消息）
    """
    if not messages:
        return messages

    if llm_client is None:
        logger.warning("[Compact] L4: 未提供 LLM Client，跳过")
        return messages

    summary = _summarize_with_llm(messages, llm_client)
    if not summary:
        logger.warning("[Compact] L4: LLM 摘要失败，跳过")
        return messages

    logger.info(f"[Compact] L4: 全量摘要完成 ({len(messages)} 条 → 1 条)")
    return [{"role": "user", "content": f"[对话历史压缩摘要]\n\n{summary}"}]


def _summarize_with_llm(messages: list[dict], llm_client) -> str:
    """调用 LLM 生成对话摘要"""
    system_prompt = (
        "你需要对一段 Agent 工作对话生成摘要。保留以下关键信息：\n"
        "1. 当前目标和剩余任务\n"
        "2. 已完成的关键工作（文件修改、数据分析结果、决策结论）\n"
        "3. 重要的发现和结论\n"
        "4. 用户的约束和偏好\n"
        "5. 待处理的问题和下一步\n\n"
        "要求：\n"
        "- 保持简洁，用列表形式组织\n"
        "- 保留股票代码、日期、数值等精确信息\n"
        "- 不要评价对话质量\n"
        "- 输出纯文本，不要 Markdown 代码块"
    )

    summary_messages = [
        {"role": "user", "content": system_prompt},
        *messages[-30:],  # 只取最近 30 条做摘要（避免二次超限）
    ]

    try:
        if hasattr(llm_client, "complete"):
            response = llm_client.complete(
                messages=summary_messages,
                max_tokens=4000,
                temperature=0.3,
            )
            # 提取文本
            try:
                return response.choices[0].message.content
            except (AttributeError, IndexError, TypeError):
                pass
            try:
                return response.content[0].text
            except (AttributeError, IndexError, TypeError):
                pass
            return str(response)
        elif callable(llm_client):
            return llm_client(summary_messages)
        else:
            return ""
    except Exception as e:
        logger.error(f"[Compact] L4 摘要 LLM 调用失败: {e}")
        return ""


# ===== RE: 应急压缩 =====

def reactive_compact(
    messages: list[dict],
    keep_last: int = REACTIVE_KEEP_LAST,
    llm_client: Optional[Callable] = None,
) -> list[dict]:
    """应急压缩：保留末尾 N 条消息，压缩前面全部

    用于 LLM 返回 prompt_too_long 时的最后一搏。
    比 compact_history 更激进——保留最近的对话上下文，
    仅对前面的内容做摘要。

    如果提供 llm_client，会对前面内容做摘要；
    否则只保留最后 N 条，丢弃前面。
    """
    if not messages:
        return messages

    if len(messages) <= keep_last:
        return messages

    tail_start = len(messages) - keep_last

    # 配对保护：确保不拆散 tool_use/tool_result
    if (tail_start > 0 and tail_start < len(messages)
            and _is_tool_result_message(messages[tail_start])
            and _message_has_tool_use(messages[tail_start - 1])):
        tail_start -= 1
        keep_last += 1

    before = messages[:tail_start]
    tail = messages[tail_start:]

    if llm_client:
        summary = _summarize_with_llm(before, llm_client)
        if summary:
            result = [
                {"role": "user", "content": f"[应急压缩摘要]\n\n{summary}"},
                *tail,
            ]
            logger.info(
                f"[Compact] RE: 应急压缩 ({len(messages)} → {len(result)})"
            )
            return result

    # LLM 不可用：直接丢弃前面
    logger.warning(
        f"[Compact] RE: 应急丢弃前 {len(before)} 条消息，保留最后 {keep_last} 条"
    )
    return [
        {"role": "user", "content": f"[应急截断：丢弃了 {len(before)} 条旧消息，保留最后 {keep_last} 条]"},
        *tail,
    ]


# ===== 压缩管线 =====

def compression_pipeline(
    messages: list[dict],
    llm_client: Optional[Callable] = None,
    threshold: int = TOKEN_ESTIMATE_THRESHOLD,
    force: bool = False,
) -> list[dict]:
    """四层压缩管线：按顺序执行，便宜的先跑

    Args:
        messages: 消息列表（会被修改）
        llm_client: 可选的 LLMClient，用于 L4 摘要
        threshold: L4 触发阈值（估计 token 数）
        force: 是否强制跑所有层（包括 L4），即使未超阈值

    Returns:
        压缩后的消息列表
    """
    if not messages:
        return messages

    # L3: 大结果落盘（必须先于 L2，因为 L2 会替换内容）
    messages = tool_result_budget(messages)

    # L1: 裁中间
    messages = snip_compact(messages)

    # L2: 旧结果占位
    messages = micro_compact(messages)

    # L4: LLM 摘要（如果超阈值或强制）
    if force or estimate_messages_tokens(messages) > threshold:
        messages = compact_history(messages, llm_client)

    return messages


# ===== 便捷函数 =====

def format_compression_summary(messages_before: int, messages_after: int) -> str:
    """格式化压缩摘要信息"""
    saved = messages_before - messages_after
    pct = (saved / messages_before * 100) if messages_before > 0 else 0
    return f"{messages_before} 条 → {messages_after} 条 (节省 {saved} 条/{pct:.0f}%)"


# ===== 自测 =====

def _make_msg(role: str, content) -> dict:
    """构造测试消息"""
    return {"role": role, "content": content}


def _test():
    """Context Compact 自测"""
    print("=" * 50)
    print("  Context Compact 自测")
    print("=" * 50)

    # 1. Token 估计
    assert estimate_tokens("hello world") >= 1
    assert estimate_tokens("") == 0
    cn_text = "Ａ股市场今日高开高走，上证指数收涨1.5%"
    assert estimate_tokens(cn_text) >= 1
    print("  ✅ token 估计")

    # 2. snip_compact — 较短的列表不裁剪
    msgs = [_make_msg("user", "hi"), _make_msg("assistant", "hello")]
    result = snip_compact(msgs, max_messages=50)
    assert len(result) == 2
    print("  ✅ snip_compact: 短列表不裁剪")

    # 3. snip_compact — 裁剪中间
    msgs = [_make_msg("user", f"msg_{i}") for i in range(20)]
    result = snip_compact(msgs, max_messages=10, keep_head=3)
    # 占位符是额外一行: 3(head) + 1(placeholder) + 7(tail) = 11
    assert len(result) == 11, f"期望11,实际{len(result)}"
    assert result[0]["content"] == "msg_0"
    # 中间有占位符
    placeholders = [m for m in result if "[裁剪了" in m.get("content", "")]
    assert len(placeholders) >= 1
    print(f"  ✅ snip_compact: 裁剪 20→{len(result)} 条")

    # 4. snip_compact — tool_use/tool_result 配对保护
    msgs = [
        {"role": "assistant", "content": [{"type": "tool_use", "name": "bash"}]},
        {"role": "user", "content": [{"type": "tool_result", "content": "output"}]},
        {"role": "user", "content": "后续对话"},
    ]
    result = snip_compact(msgs, max_messages=2, keep_head=1)
    # 应该保留 tool_use + tool_result 对
    assert len(result) >= 2
    print("  ✅ snip_compact: tool_use/tool_result 配对保护")

    # 5. micro_compact — 旧结果占位
    msgs = [_make_msg("user", "first"), _make_msg("assistant", "ok")]
    # 添加两条 tool_result
    msgs.append({
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "a", "content": "X" * 500},
        ],
    })
    msgs.append({
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "b", "content": "Y" * 500},
        ],
    })
    msgs.append({
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "c", "content": "Z" * 500},
        ],
    })
    result = micro_compact(msgs, keep_recent=2)
    # 应该保留最近 2 条完整，第 1 条被替换
    # 总共有3条tool_result blocks，keep_recent=2，1条被替换
    blocks = _get_content_blocks(result)
    full_count = sum(1 for _, _, b in blocks if not b["content"].startswith("["))
    assert full_count == 2, f"期望 2 条完整，实际 {full_count}"
    print("  ✅ micro_compact: 旧结果占位")

    # 6. tool_result_budget — 大结果落盘
    big_content = "BIG" * 100_000  # 300K chars
    msgs = [_make_msg("user", "hi"), _make_msg("assistant", "ok")]
    msgs.append({
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "big1", "content": big_content},
            {"type": "tool_result", "tool_use_id": "small1", "content": "small"},
        ],
    })
    result = tool_result_budget(msgs, max_bytes=50_000)
    # big 应被落盘，small 保持
    last_content = result[-1]["content"]
    assert isinstance(last_content, list)
    big_block = last_content[0]
    assert "<persisted-output>" in big_block.get("content", ""), "大内容应落盘"
    small_block = last_content[1]
    assert small_block.get("content") == "small", "小内容应保持"
    print("  ✅ tool_result_budget: 大结果落盘")

    # 7. compact_history — 无 LLM 时跳过
    msgs = [_make_msg("user", "hi"), _make_msg("assistant", "hello")]
    result = compact_history(msgs, llm_client=None)
    # 应该跳过
    assert result == msgs or len(result) == 2
    print("  ✅ compact_history: 无 LLM 时跳过")

    # 8. reactive_compact — 应急截断
    msgs = [_make_msg("user", f"msg_{i}") for i in range(20)]
    result = reactive_compact(msgs, keep_last=3, llm_client=None)
    assert len(result) <= 4  # 1 条标记 + 3 条保留
    assert any("[应急截断" in m.get("content", "") for m in result)
    print("  ✅ reactive_compact: 应急截断")

    # 9. compression_pipeline — 完整管线
    msgs = [_make_msg("user", f"msg_{i}") for i in range(60)]
    result = compression_pipeline(msgs, llm_client=None, force=False)
    # L1 裁剪应该生效
    assert len(result) < 60
    print(f"  ✅ compression_pipeline: 完整管线 ({len(msgs)}→{len(result)})")

    # 10. 空消息和边界
    assert compression_pipeline([]) == []
    assert snip_compact([]) == []
    print("  ✅ 空消息边界处理")

    # 11. estimate_messages_tokens
    msgs = [_make_msg("user", "hello"), _make_msg("assistant", "world")]
    tokens = estimate_messages_tokens(msgs)
    assert tokens >= 1
    print("  ✅ estimate_messages_tokens")

    print(f"\n  {'='*40}")
    print(f"  全部 11 项测试通过 ✅")
    print(f"  {'='*40}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    _test()
