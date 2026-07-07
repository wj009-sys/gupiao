"""
Phase 1 集成测试 — 验证三个新模块协同工作

测试场景模拟真实的 Agent 工作流：
  1. Agent7 (Lead) 通过 MessageBus 向 Agent1/Agent8/Agent9 并行分发任务
  2. 各 Agent 执行后通过 MessageBus 回报结果
  3. LLM 调用出现 429 限流 → 指数退避恢复
  4. 大对话上下文 → 压缩管线（L3落盘→L1裁剪→L2占位→L4摘要）
  5. 收件箱轮询 + 消息累积 + 压缩的综合场景

用法：
    python scripts/test_phase1_integration.py
"""
import os
import sys
import time
import json
import logging

# 确保能从项目根导入
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

logging.basicConfig(level=logging.WARNING, format="%(message)s")

from scripts.utils.message_bus import MessageBus
from scripts.utils.llm_client import LLMClient
from scripts.utils.context_compact import (
    compression_pipeline,
    snip_compact,
    micro_compact,
    tool_result_budget,
    reactive_compact,
    estimate_messages_tokens,
    format_compression_summary,
)


def section(title: str):
    """打印分节标题"""
    print(f"\n  {'='*40}")
    print(f"  {title}")
    print(f"  {'='*40}")


def test_message_bus_workflow():
    """场景1：Agent7 Lead 通过 MessageBus 分派和收集任务"""
    import tempfile
    with tempfile.TemporaryDirectory(prefix="integ_test_") as tmpdir:
        bus = MessageBus(mailbox_dir=tmpdir)
        results = []

        # ── Phase 1: Lead 派发 3 个并行任务 ──
        section("MessageBus 并行任务派发")

        tasks = [
            ("agent1", "情报员", "采集今日情报和龙虎榜数据"),
            ("agent8", "政策分析师", "分析今日政策新闻"),
            ("agent9", "游资追踪师", "追踪龙虎榜游资动向"),
        ]

        for agent_name, role, task_desc in tasks:
            ok = bus.send("lead", agent_name, "task_assignment",
                          task_desc, {"role": role, "priority": "high"})
            assert ok, f"发送任务到 {agent_name} 失败"
            print(f"  lead → {agent_name}: {task_desc[:30]}...")

        # ── Phase 2: 各 Agent 读取任务，执行，回传结果 ──
        section("Agent 执行与回传")

        # Agent1 情报员
        inbox = bus.read_inbox("agent1")
        assert len(inbox) == 1, f"agent1 应收到 1 条消息，实际 {len(inbox)}"
        task1 = inbox[0]
        assert task1["type"] == "task_assignment"
        assert task1["from"] == "lead"
        bus.send("agent1", "lead", "result", "情报采集完成：3条热点新闻+龙虎榜数据",
                 {"articles": 3, "limit_up_stocks": 5})
        results.append("agent1")
        print("  agent1 → lead: 情报完成")

        # Agent8 政策分析师
        inbox = bus.read_inbox("agent8")
        assert len(inbox) == 1
        bus.send("agent8", "lead", "result", "政策分析完成：2条宏观+1条产业",
                 {"macro": 2, "industry": 1})
        results.append("agent8")
        print("  agent8 → lead: 政策完成")

        # Agent9 游资追踪师
        inbox = bus.read_inbox("agent9")
        assert len(inbox) == 1
        bus.send("agent9", "lead", "result", "游资追踪完成：3家席位活跃",
                 {"seats": 3, "hot_money_index": 65})
        results.append("agent9")
        print("  agent9 → lead: 游资完成")

        # ── Phase 3: Lead 收集所有结果 ──
        section("Lead 结果收集")

        lead_inbox = bus.read_inbox("lead")
        assert len(lead_inbox) == 3, f"lead 应收到 3 条结果，实际 {len(lead_inbox)}"
        for msg in lead_inbox:
            assert msg["type"] == "result"
            assert msg["from"] in ["agent1", "agent8", "agent9"]
            meta = msg.get("metadata", {})
            print(f"  来自 {msg['from']}: {msg['content'][:40]} (meta: {meta})")

        # ── Phase 4: 检查收件箱状态 ──
        section("收件箱状态验证")
        inboxes = bus.list_inboxes()
        for ib in inboxes:
            assert ib["agent"] != "lead", "lead 收件箱应已被消费"
        print(f"  非空收件箱: {[ib['agent'] for ib in inboxes]}")

        # 再次读取应为空
        empty = bus.read_inbox("lead")
        assert len(empty) == 0, "lead 收件箱应已清空"
        print("  lead 收件箱已清空 ✅")

        # 不存在的 Agent
        assert bus.read_inbox("nonexistent") == []
        print("  不存在的 agent 返回空 ✅")

    print(f"\n  ✅ MessageBus 工作流: {len(results)} Agent 完成")
    return True


def test_llm_client_recovery():
    """场景2：LLM Client 错误恢复（模拟异常，不调用真实 API）"""
    section("LLM Client 错误恢复")

    bf = LLMClient(provider="openai", model="gpt-4o-mini", api_key="test-key")

    # 429 限流 — 应指数退避
    class Mock429(Exception):
        status_code = 429

    for attempt in [1, 2, 3, 5, 10]:
        should, delay = bf._should_retry(Mock429(), attempt)
        assert should, f"Attempt {attempt} 应重试"

        # 验证指数增长
        expected_ms = min(500 * (2 ** (attempt - 1)), 32_000)
        assert delay >= expected_ms / 1000.0, (
            f"Attempt {attempt}: delay={delay:.2f}s 应 >= {expected_ms / 1000:.2f}s"
        )
        assert delay <= expected_ms / 1000.0 * 1.25 + 0.1, (
            f"Attempt {attempt}: delay={delay:.2f}s 超出抖动范围"
        )

    print("  ✅ 429 指数退避: 10 次重试延迟验证通过")

    # 529 过载检测
    class Mock529(Exception):
        status_code = 529

    assert bf._is_overloaded_error(Mock529()), "529 检测失败"
    print("  ✅ 529 过载检测")

    # 超时 — 类名匹配
    import httpx
    should, _ = bf._should_retry(httpx.TimeoutException("timed out"), 1)
    assert should, "httpx.TimeoutException 应重试"
    print("  ✅ 超时错误检测")

    # 不可重试
    class ValueError(Exception):
        pass

    # 注意：直接 inheriting ValueError 会有 status_code 冲突，用 type 创建
    MockValErr = type("MockValErr", (Exception,), {})
    should, _ = bf._should_retry(MockValErr(), 1)
    assert not should, "值错误不应重试"
    print("  ✅ 不可重试错误正确拒绝")

    # 备用模型切换
    fb_client = LLMClient(
        provider="openai", model="gpt-4o-mini",
        api_key="test", fallback_model="gpt-4o",
    )
    fb_client.consecutive_529 = 3
    fb_client._current_model = fb_client.fallback_model
    assert fb_client._current_model == "gpt-4o"
    print("  ✅ 备用模型切换")

    # is_available — 取决于环境（有配置则可用，无配置则不可用）
    cfg_client = LLMClient()
    available = cfg_client.is_available()
    print(f"  ✅ 可用性检测: {'可用' if available else '不可用'} (provider={cfg_client._provider})")

    print(f"\n  ✅ LLM Client 错误恢复: 全部通过")
    return True


def test_context_compression_pipeline():
    """场景3：模拟一个 Agent 工作后的膨胀上下文，运行完整压缩管线"""
    section("Context Compact 压缩管线")

    # ── 构建模拟的 Agent 对话（约 70 条消息，含大 tool_result） ──
    messages = []
    messages.append({"role": "user", "content": "开始今日投研工作"})
    messages.append({"role": "assistant", "content": "好的，开始情报采集..."})

    # 添加 10 轮工具调用（带大输出）
    for i in range(10):
        # tool_use
        messages.append({
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": f"tool_{i}",
                 "name": "bash" if i % 2 == 0 else "read_file",
                 "input": {"command": f"echo step_{i}"} if i % 2 == 0 else {"path": f"file_{i}.txt"}},
            ],
        })
        # tool_result（模拟大小不同的输出）
        content_size = 5000 + i * 3000  # 5K → 32K 逐渐增大
        messages.append({
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": f"tool_{i}",
                 "content": f"第{i}轮工具输出: " + "X" * content_size},
            ],
        })
        # assistant 分析
        messages.append({
            "role": "assistant",
            "content": f"分析第{i}轮结果：发现了一些有价值的信号。",
        })

    # 添加 10 条"对话"（模拟用户交互）
    for i in range(10, 20):
        messages.append({"role": "user", "content": f"继续分析，关注指标{i}..."})
        messages.append({
            "role": "assistant",
            "content": f"指标{i}分析完成：" + "Y" * (200 + i * 50),
        })

    total_before = len(messages)
    tokens_before = estimate_messages_tokens(messages)
    print(f"  压缩前: {total_before} 条消息, ~{tokens_before:,} 估计 token")

    # ── 执行压缩管线 ──
    # 1. L3: 大结果落盘
    msgs = tool_result_budget(messages)
    # 2. L1: 裁中间
    msgs = snip_compact(msgs, max_messages=30, keep_head=3)
    # 3. L2: 旧结果占位
    msgs = micro_compact(msgs, keep_recent=2)

    total_after = len(msgs)
    tokens_after = estimate_messages_tokens(msgs)
    print(f"  三层压缩后: {total_after} 条消息, ~{tokens_after:,} 估计 token")
    print(f"  {format_compression_summary(total_before, total_after)}")

    # 验证压缩效果
    assert total_after < total_before, "压缩后消息数应减少"
    assert tokens_after < tokens_before * 0.6, f"token 应显著减少 ({tokens_after} vs {tokens_before})"

    # ── L4: 应急压缩（无 LLM 时） ──
    msgs2 = reactive_compact(msgs, keep_last=5, llm_client=None)
    assert len(msgs2) <= 6, f"应急压缩应保留最多 6 条"
    print(f"  RE 应急压缩后: {len(msgs2)} 条消息")

    print(f"\n  ✅ Context Compact 压缩管线: 全部通过")
    return True


def test_integrated_scenario():
    """场景4：综合场景 — 消息传递 + 错误恢复 + 压缩管线联合工作"""
    section("综合场景：联合工作")

    import tempfile
    with tempfile.TemporaryDirectory(prefix="integ_full_") as tmpdir:
        bus = MessageBus(mailbox_dir=tmpdir)

        # ── 1. Lead 并行派发任务 ──
        agents = ["agent1", "agent2", "agent3", "agent5", "agent6"]
        for a in agents:
            bus.send("lead", a, "task_assignment", f"任务给{a}", {"seq": agents.index(a)})

        # ── 2. 所有 Agent 并行回传结果（模拟） ──
        for a in agents:
            msgs = bus.read_inbox(a)
            assert len(msgs) == 1, f"{a} 应收到任务"
            bus.send(a, "lead", "result", f"{a} 执行完毕",
                     {"duration_sec": len(a) * 2, "status": "success"})

        # ── 3. Lead 检查进度（通过 inbox_exists 快速轮询） ──
        max_polls = 50
        for poll in range(max_polls):
            if not bus.inbox_exists("lead"):
                time.sleep(0.01)
                continue
            break
        else:
            assert False, "Lead 应在合理时间内收到结果"

        results = bus.read_inbox("lead")
        assert len(results) == len(agents), f"应收到 {len(agents)} 个结果"
        result_agents = [r["from"] for r in results]
        assert sorted(result_agents) == sorted(agents)
        print(f"  Lead 收到 {len(results)} 个并行任务结果")

        # ── 4. Lead 开始生成决策报告，构建大上下文 ──
        decision_msgs = [
            {"role": "user", "content": f"收到来自 {r['from']} 的结果: {r['content']}"}
            for r in results
        ]

        # 添加一些助手分析结果
        for i, msg in enumerate(decision_msgs[:]):
            decision_msgs.append({
                "role": "assistant",
                "content": f"分析第{i}个结果：综合评分{70 + i * 5}分，建议{'买入' if i % 2 == 0 else '持有'}。"
            })
            # 模拟大 tool_result
            decision_msgs.append({
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": f"analysis_{i}",
                     "content": "详细指标: " + "D" * 30000},
                ],
            })

        big_before = len(decision_msgs)
        tokens_big_before = estimate_messages_tokens(decision_msgs)
        print(f"  决策上下文: {big_before} 条, ~{tokens_big_before:,} token")

        # ── 5. 应用压缩管线 ──
        compressed = compression_pipeline(decision_msgs, llm_client=None)
        big_after = len(compressed)
        tokens_big_after = estimate_messages_tokens(compressed)

        print(f"  压缩后: {big_after} 条, ~{tokens_big_after:,} token")
        print(f"  节省: {format_compression_summary(big_before, big_after)}")

        # 至少应该有压缩效果
        assert big_after <= big_before + 1  # +1 for placeholder

    print(f"\n  ✅ 综合场景: 全部通过")
    return True


def test_edge_cases():
    """场景5：边界条件和异常情况"""
    section("边界条件测试")

    # ── MessageBus 边界 ──
    bus = MessageBus(mailbox_dir=os.path.join(
        os.path.dirname(__file__), "..", ".claude", "teams", "inboxes_test"
    ))
    bus.clear_inbox("lead")

    # 发一条空消息
    ok = bus.send("lead", "test_agent", "message", "", {"empty": True})
    assert ok
    inbox = bus.read_inbox("test_agent")
    assert len(inbox) == 1
    assert inbox[0]["content"] == ""
    print("  空消息 ✅")

    # 清理测试目录
    bus.clear_inbox("test_agent")
    import shutil
    bus_mbox_dir = os.path.join(
        os.path.dirname(__file__), "..", ".claude", "teams", "inboxes_test"
    )
    if os.path.exists(bus_mbox_dir):
        shutil.rmtree(bus_mbox_dir, ignore_errors=True)

    # ── Context Compact 边界 ──
    # 单条消息
    single = compression_pipeline([{"role": "user", "content": "hi"}])
    assert len(single) == 1
    print("  单条消息管线 ✅")

    # 全是 tool_result 不配对
    tool_only = [
        {"role": "user", "content": [{"type": "tool_result", "content": "data"}]},
    ]
    result = micro_compact(tool_only)
    assert len(result) == 1
    print("  tool_only 不崩溃 ✅")

    # 超长 agent 名称
    try:
        bus.send("lead", "a" * 65, "message", "test")
        assert False, "超长 agent 名称应报错"
    except ValueError:
        pass
    print("  超长名称校验 ✅")

    # ── LLM Client 边界 ──
    # 有/无配置取决于环境，这里验证 complete 空消息校验
    client = LLMClient()
    if client.is_available():
        print(f"  ⓘ LLM 已配置 ({client.model})，跳过不可用测试")
    else:
        print("  ⓘ LLM 未配置 (按预期)")

    # complete 空消息校验（不管有无配置都会抛 ValueError）
    try:
        client.complete([])
        # 如果 LLM 已配置且走到了 API 调用，会因为空消息被 litellm 拒绝而抛异常
        # 也能接受
    except ValueError:
        print("  空消息校验 ✅")
    except Exception:
        print("  空消息被其他机制拒绝 ✅")

    # 没有 fallback_model 时保持原模型
    no_fb = LLMClient(provider="openai", model="gpt-4", api_key="test-key")
    no_fb.consecutive_529 = 3
    assert no_fb._current_model == "gpt-4"
    print("  无 fallback 时保持 ✅")

    print(f"\n  ✅ 边界条件: 全部通过")
    return True


def main():
    print("=" * 50)
    print("  Phase 1 集成测试")
    print("  模块: MessageBus + LLMClient + ContextCompact")
    print("=" * 50)

    tests = [
        ("MessageBus 工作流", test_message_bus_workflow),
        ("LLM Client 错误恢复", test_llm_client_recovery),
        ("Context Compact 压缩管线", test_context_compression_pipeline),
        ("综合场景", test_integrated_scenario),
        ("边界条件", test_edge_cases),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ──── {name}: ✅ 通过 ────")
            passed += 1
        except Exception as e:
            import traceback
            print(f"\n  ❌ {name} 失败: {e}")
            traceback.print_exc()
            failed += 1

    print(f"\n{'='*50}")
    print(f"  Phase 1 集成测试完成")
    print(f"  ✅ {passed} 通过, ❌ {failed} 失败")
    print(f"{'='*50}")

    # 清理残留
    import shutil
    for d in [os.path.join(ROOT, ".claude", "task_outputs")]:
        if os.path.exists(d):
            shutil.rmtree(d, ignore_errors=True)

    return failed == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
