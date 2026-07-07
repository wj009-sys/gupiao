"""
每日全流程管线 — 一键运行所有 Agent + 知识自动沉淀（Phase 3 闭环）

完整闭环：采集 → 分析 → 决策 → 知识沉淀

流程：
  1. AgentOrchestrator 波次调度（4 Wave × 8 Agent）
  2. 内存提取器自动从报告提取知识（规则 + 可选 LLM）
  3. 更新 knowledge/ 知识库
  4. 最终输出完整闭环摘要

用法：
    # 一键运行（推荐）
    python scripts/run_daily_pipeline.py

    # 指定日期 + LLM 增强提取
    python scripts/run_daily_pipeline.py 20260707 --extract-llm

    # 跳过知识提取
    python scripts/run_daily_pipeline.py --no-extract

    # 串行模式 + 详细输出
    python scripts/run_daily_pipeline.py --mode sequential --extract

输出：
    - 所有 Agent 报告: reports/日报/{agent}/*{date}.md
    - 知识提取: knowledge/复盘记录/提取_{date}.md
    - 策略更新: knowledge/策略/
    - 变更记录: knowledge/CHANGES.md
"""
import os
import sys
import json

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)


def _print_step(num: int, label: str):
    print(f"\n  ═══ 步骤 {num}: {label} ═══")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="每日全流程管线 — 采集→分析→决策→知识沉淀")
    parser.add_argument("date", nargs="?", default="", help="交易日 YYYYMMDD（默认今天）")
    parser.add_argument("--mode", default="auto", choices=["parallel", "sequential", "auto"],
                        help="执行模式")
    parser.add_argument("--dry-run", action="store_true", help="仅显示执行计划")
    parser.add_argument("--json", action="store_true", help="输出 JSON 格式结果")
    parser.add_argument("--wave", type=int, default=0, help="执行指定波次（1-4）")
    parser.add_argument("--agent", default="", help="执行指定 Agent")
    parser.add_argument("--extract", dest="extract", action="store_true", default=None,
                        help="启用知识提取（默认：全流程时启用）")
    parser.add_argument("--no-extract", dest="extract", action="store_false", default=None,
                        help="跳过知识提取")
    parser.add_argument("--extract-llm", action="store_true", default=False,
                        help="启用 LLM 增强知识提取")
    parser.add_argument("--lint", action="store_true", default=False,
                        help="提取后运行 knowledge_lint 检查")
    args = parser.parse_args()

    # ── 导入 ──
    try:
        from scripts.utils.agent_orchestrator import (
            AgentOrchestrator, _today_str, _parse_date,
        )
    except ImportError as e:
        print(f"❌ 调度器导入失败: {e}")
        print("   请确认运行在项目根目录（venv）")
        sys.exit(1)

    try:
        from scripts.utils.memory_extractor import MemoryExtractor
        _HAS_EXTRACTOR = True
    except ImportError:
        _HAS_EXTRACTOR = False

    # ── 日期 ──
    date = args.date
    if date:
        try:
            date = _parse_date(date)
        except ValueError as e:
            print(f"❌ {e}")
            sys.exit(1)
    else:
        date = _today_str()

    # ── 初始化 ──
    bus = None
    try:
        from scripts.utils.message_bus import MessageBus
        bus = MessageBus()
    except Exception:
        pass

    orch = AgentOrchestrator(date=date, mode=args.mode, message_bus=bus)

    print(f"🚀 每日全流程管线 — {date}")
    print(f"   模式: {orch.mode} | MessageBus: {'✅' if bus else '❌'}")
    print(f"   Agent: {len(orch.agents)} | Waves: {len(orch.waves)}")
    print()

    # ── Dry Run ──
    if args.dry_run:
        for wave in orch.waves:
            agents = orch.get_agents_in_wave(wave)
            names = [f"{a.agent_id}({a.name})" for a in agents]
            print(f"  Wave {wave}: {' + '.join(names)}")
            for a in agents:
                cmd = a.build_command(orch.python_path, date)
                report_path = a.report_pattern.replace("{date}", date)
                existing = "✅" if a.check_report(date) else "  "
                print(f"    {existing} {a.agent_id}: python {' '.join(cmd[1:])}")
            print()
        if _HAS_EXTRACTOR:
            print(f"  📚 知识提取: {'规则+LLM' if args.extract_llm else '规则'}")
        print(f"  总计: {len(orch.agents)} Agent / {len(orch.waves)} Waves")
        return

    # ── 执行 Agent ──
    start_time = __import__("time").time()

    if args.agent:
        # 单 Agent
        agent = next((a for a in orch.agents if a.agent_id == args.agent), None)
        if not agent:
            print(f"❌ 未知 Agent: {args.agent}")
            print(f"   可用: {', '.join(a.agent_id for a in orch.agents)}")
            sys.exit(1)
        result = orch.run_agent(agent)
        summary = orch.get_summary({agent.wave: [result]})
        print(summary)
        all_results = {agent.wave: [result]}
    elif args.wave:
        results = orch.run_wave(args.wave)
        summary = orch.get_summary({args.wave: results})
        print(summary)
        all_results = {args.wave: results}
    else:
        _print_step(1, "Agent 波次执行")
        all_results = orch.run_all()
        agent_summary = orch.get_summary(all_results)
        print(agent_summary)

        # 检查决策报告
        decision_path_ymd = f"reports/日报/决策/投资决策_{date}.md"
        decision_path_dash = f"reports/日报/决策/投资决策_{_normalize_date_for_path(date)}.md"
        decision_found = None
        for dp in [decision_path_ymd, decision_path_dash]:
            full = os.path.join(ROOT, dp)
            if os.path.exists(full):
                decision_found = dp
                break
        if decision_found:
            print(f"  📄 最终决策报告: {decision_found} ✅")
        else:
            print(f"  ⚠️  最终决策报告未生成")
            print(f"     预期: {decision_path_ymd}")

    pipeline_duration = __import__("time").time() - start_time

    # ── 知识提取 ──
    should_extract = args.extract if args.extract is not None else (
        not args.agent and not args.wave  # 全流程默认提取
    )

    if should_extract and _HAS_EXTRACTOR:
        _print_step(2, "知识提取与沉淀")
        llm_client = None
        if args.extract_llm:
            try:
                from scripts.utils.llm_client import LLMClient
                llm_client = LLMClient()
                if not llm_client.is_available():
                    print("  ⚠️  LLM 未配置，使用规则提取")
                    llm_client = None
            except Exception as e:
                print(f"  ⚠️  LLM 初始化失败: {e}")

        extractor = MemoryExtractor(llm_client=llm_client)
        extract_start = __import__("time").time()
        result = extractor.extract(date)
        extract_duration = __import__("time").time() - extract_start

        print(f"  {result.summary()}")
        if result.errors:
            for err in result.errors:
                print(f"  ⚠️  {err}")

        # 知识库健康度检查
        if args.lint:
            _print_step(3, "知识库健康度检查")
            try:
                lint_cmd = ["python", "scripts/utils/knowledge_lint.py"]
                import subprocess
                lint_result = subprocess.run(
                    lint_cmd, cwd=ROOT, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=30,
                )
                # 只打印摘要行
                for line in lint_result.stdout.split("\n"):
                    if any(kw in line for kw in ["健康度", "孤页", "断裂", "矛盾", "过时", "🟢", "🟡", "🔴"]):
                        print(f"  {line.strip()}")
            except Exception as e:
                print(f"  ⚠️  knowledge_lint 执行失败: {e}")

        total_duration = pipeline_duration + extract_duration

    else:
        total_duration = pipeline_duration
        if should_extract and not _HAS_EXTRACTOR:
            print("\n  ⚠️  跳过知识提取: memory_extractor 模块未找到")

    # ── 完成 ──
    print()
    print("  " + "=" * 40)
    print(f"  🏁 管线完成 — {date}")
    print(f"  总耗时: {total_duration:.0f}s")
    if not args.agent and not args.wave:
        all_r = [r for wave_res in all_results.values() for r in wave_res]
        success = sum(1 for r in all_r if r.status == "completed")
        total = len(all_r)
        print(f"  Agent: {success}/{total} ✅")
    print("  " + "=" * 40)

    # ── JSON ──
    if args.json:
        output = {
            "date": date,
            "mode": orch.mode,
            "duration_sec": round(total_duration, 1),
            "agent_results": orch.get_json_summary(all_results) if not args.agent and not args.wave else {},
        }
        if should_extract and _HAS_EXTRACTOR:
            output["extraction"] = result.to_dict()
        print(json.dumps(output, ensure_ascii=False, indent=2))


def _normalize_date_for_path(date_str: str) -> str:
    """将 YYYYMMDD → YYYY-MM-DD"""
    d = date_str.replace("-", "")
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}"


if __name__ == "__main__":
    main()
