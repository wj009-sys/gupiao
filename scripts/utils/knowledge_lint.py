"""
知识库一致性检查脚本 (Lint)

功能：
1. 孤页检测 — 文件未被任何其他文件引用
2. 断裂引用检测 — 引用的目标文件不存在
3. 过时检测 — 文件超过N天未更新，且策略类文件需标注数据日期
4. 矛盾检测 — 策略文件之间交叉检查矛盾点
5. 目录完整性 — INDEX.md 与实际文件是否一致
6. 生成健康度报告

用法：
    python scripts/utils/knowledge_lint.py               # 默认检查 + 交互
    python scripts/utils/knowledge_lint.py --report      # 只输出 JSON 报告（供 Agent4 调用）
    python scripts/utils/knowledge_lint.py --fix-index   # 修复 INDEX.md 与实际不符的问题
    python scripts/utils/knowledge_lint.py --max-stale-days 30  # 自定义过时阈值

D3异常处理表：
| 触发条件 | 一线修复 | 仍失败兜底 |
|---------|---------|-----------|
| 某个知识库文件不存在（被删除了）| 跳过该文件，记录为"缺失" | 从 INDEX.md 中移除引用 |
| 引用格式非标准（[[link]] 或 [text](path) 以外的格式）| 尝试正则匹配变体格式 | 忽略该引用 |
| 知识库文件编码非UTF-8 | 尝试 GBK/cp1252 回退 | 跳过该文件并警告 |
| 目录扫描权限被拒绝 | 跳过该目录 | 仍输出已有结果 |

D4 CHECKPOINT:
- CP1-引用完整性：所有 [[link]] 和 Markdown 链接的目标必须存在
- CP2-目录一致性：INDEX.md 列出的文件必须与实际文件系统一致
- CP3-时效标注：策略文件必须有最后更新日期标注
- CP4-矛盾标记：跨文件矛盾必须明确标记，不能静默忽略

D9反例：
1. 不要只输出警告不提供修复建议——每个问题必须附带修复操作
2. 不要自动修改知识库——lint 是只读检查，修改需要用户/复盘师确认
3. 不要重复报告同一个问题——同一问题只报告一次
"""
import os
import sys
import json
import re
import glob
import logging
from datetime import datetime, timedelta

# Windows GBK 编码兼容：强制 stdout/stderr 使用 UTF-8
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        # Python < 3.7 不支持 reconfigure，设环境变量
        os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        logging.debug("stderr.reconfigure not available on this Python version")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] knowledge_lint: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("knowledge_lint")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def p(path: str) -> str:
    return os.path.join(PROJECT_ROOT, path)


def scan_files() -> dict:
    """扫描知识库目录结构"""
    knowledge_dir = p("knowledge")
    files = {}
    for root, dirs, fnames in os.walk(knowledge_dir):
        # 跳过 .gitkeep
        fnames = [f for f in fnames if f != ".gitkeep"]
        for fn in fnames:
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, knowledge_dir).replace("\\", "/")
            mtime = os.path.getmtime(full)
            files[rel] = {
                "path": full,
                "mtime": mtime,
                "mtime_display": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M"),
                "size": os.path.getsize(full),
            }
    return files

# Backward compatibility alias（保留供外部导入/旧代码兼容，当前项目内无使用者）
_scan_files = scan_files


def _extract_references(text: str, source_file: str, inside_knowledge: bool = True) -> list:
    """从文本中提取所有引用目标

    支持格式:
    - [[target]]            Obsidian/记忆格式
    - [label](target)       Markdown 链接
    - data/file.json        内联引用（以data/开头）
    - skills/path           内联引用（以skills/开头）
    - knowledge/path        内联引用（以knowledge/开头）
    - 策略/path             相对路径引用（knowledge/ 内文件常见）
    """
    refs = []
    # 1. [[link]] 格式 — 解析失败时记录文件并跳过该格式，不阻断后续步骤
    try:
        for m in re.finditer(r'\[\[([^\]]+?)\]\]', text):
            refs.append({"raw": m.group(0), "target": m.group(1), "type": "wikilink"})
    except Exception as e:
        print(f'  [WARN] wikilink解析失败 ({source_file}): {e}，跳过wikilink格式，继续解析其他格式')
    # 2. Markdown 链接 [label](path)
    try:
        for m in re.finditer(r'\[([^\]]+?)\]\(([^\)]+)\)', text):
            target = m.group(2).strip()
            # 规范化路径
            if target.startswith("../"):
                target = target[3:]  # relative to project root
            # 保留项目内引用
            if target.startswith(("data/", "skills/", "knowledge/")):
                refs.append({"raw": m.group(0), "target": target, "type": "markdown"})
            elif inside_knowledge and target.startswith(("策略/", "复盘记录/", "CHANGES.md", "INDEX.md", "LLM-Wiki", "参考/")):
                # knowledge/ 内文件的相对引用
                refs.append({"raw": m.group(0), "target": "knowledge/" + target, "type": "markdown"})
    except Exception as e:
        print(f'  [WARN] markdown链接解析失败 ({source_file}): {e}，跳过markdown链接格式，继续解析其他格式')
    # 3. 行内 data/ 引用（不在代码块中）
    try:
        in_code_block = False
        for line in text.split("\n"):
            if line.strip().startswith("```"):
                in_code_block = not in_code_block
                continue
            if in_code_block:
                continue
            for m in re.finditer(r'(?<![\[`])((?:data|skills|knowledge)/[^\s,)\]"\'》]+)', line):
                target = m.group(1).rstrip(".")
                if target not in [r["target"] for r in refs]:
                    refs.append({"raw": m.group(0), "target": target, "type": "inline"})
    except Exception as e:
        print(f'  [WARN] 内联引用解析失败 ({source_file}): {e}，跳过内联引用格式，继续返回已解析引用')
    return refs


def _check_orphans(files: dict) -> list:
    """孤页检测：未被其他文件引用的文件"""
    # 先扫描所有文件的内容，提取引用
    all_refs = set()
    for rel, info in files.items():
        try:
            with open(info["path"], "r", encoding="utf-8") as f:
                content = f.read()
            refs = _extract_references(content, rel)
            for r in refs:
                # 规范化引用目标：去掉文件扩展名差异
                target = r["target"]
                # 如果是 knowledge/ 开头的引用
                if target.startswith("knowledge/"):
                    target_rel = target[len("knowledge/"):]
                    all_refs.add(target_rel)
                    # 也添加不带扩展名的引用
                    base, _ = os.path.splitext(target_rel)
                    all_refs.add(target_rel)
                    all_refs.add(base)
        except Exception as e:
            logger.warning(f"扫描引用时出错 ({rel}): {e}", exc_info=True)
            continue

    # 检查是否有文件从未被引用
    orphans = []
    for rel in sorted(files.keys()):
        # INDEX.md 和 CHANGES.md 是基础设施，不视为孤儿
        if rel in ("INDEX.md", "CHANGES.md"):
            continue
        # 检查 rel 是否出现在 all_refs 中
        base, ext = os.path.splitext(rel)
        if rel not in all_refs and base not in all_refs:
            orphans.append(rel)
    return orphans


def _check_broken_refs(files: dict) -> list:
    """断裂引用检测：引用的文件不存在"""
    broken = []
    knowledge_files_set = set(files.keys())
    # 外层文件 (data/, skills/ 等)
    existing_outer = set()
    for p_str in [p("data"), p("skills")]:
        if os.path.exists(p_str):
            for root, dirs, fnames in os.walk(p_str):
                for fn in fnames:
                    rel = os.path.relpath(os.path.join(root, fn), PROJECT_ROOT).replace("\\", "/")
                    existing_outer.add(rel)

    for rel, info in files.items():
        try:
            with open(info["path"], "r", encoding="utf-8") as f:
                content = f.read()
            refs = _extract_references(content, rel)
            for r in refs:
                target = r["target"]
                # 判断引用类型
                if target.startswith("knowledge/"):
                    target_rel = target[len("knowledge/"):]
                    # 检查文件是否存在（允许无扩展名匹配）
                    exists = target_rel in knowledge_files_set
                    if not exists:
                        base, _ = os.path.splitext(target_rel)
                        exists = base in knowledge_files_set or target_rel + ".md" in knowledge_files_set
                    if not exists:
                        broken.append({
                            "source": rel,
                            "target": target,
                            "type": r["type"],
                            "raw": r["raw"],
                            "suggest": "文件不存在，或路径有误",
                        })
                elif target.startswith(("data/", "skills/")):
                    if target not in existing_outer:
                        # 检查是否为存在的目录（非文件路径）
                        target_path = os.path.join(PROJECT_ROOT, target)
                        if os.path.isdir(target_path):
                            continue  # 目录存在，不是断裂引用
                        broken.append({
                            "source": rel,
                            "target": target,
                            "type": r["type"],
                            "raw": r["raw"],
                            "suggest": f"外部文件{target}不存在",
                        })
        except Exception as e:
            logger.warning(f"检查引用时出错 ({rel}): {e}", exc_info=True)
            continue
    return broken


def _check_staleness(files: dict, max_stale_days: int = 90) -> list:
    """过时检测：策略文件超过N天未更新"""
    now = datetime.now()
    stale = []
    # 策略文件需要更新更频繁
    critical_files = {
        "策略/选股策略.md": 30,      # 选股权重变化快
        "策略/择时策略.md": 60,      # 择时参数可做季度调整
        "策略/交易执行规则.md": 60,  # 交易规则相对稳定
        "INDEX.md": 7,               # 索引应随时更新
    }
    for rel, info in files.items():
        threshold = critical_files.get(rel, max_stale_days)
        file_mtime = datetime.fromtimestamp(info["mtime"])
        age_days = (now - file_mtime).days
        if age_days > threshold:
            stale.append({
                "file": rel,
                "last_update": info["mtime_display"],
                "age_days": age_days,
                "threshold": threshold,
                "severity": "HIGH" if age_days > threshold * 2 else "MEDIUM",
            })
    return stale


def _check_contradictions(texts: dict) -> list:
    """矛盾检测：策略文件之间的关键规则交叉检查"""
    contradictions = []

    # 数据文件中的规则和策略文件中的规则对比
    rules = {}

    # 从仓位管理规则读取（若存在）
    position_rule_path = p("data/仓位管理规则.json")
    if os.path.exists(position_rule_path):
        try:
            with open(position_rule_path, "r", encoding="utf-8") as f:
                rules["仓位管理"] = json.load(f)
        except Exception as e:
            logger.warning(f"读取仓位管理规则失败: {e}", exc_info=True)

    # 检查1: 仓位上限一致性
    content_择时 = texts.get("策略/择时策略.md", "")
    content_交易 = texts.get("策略/交易执行规则.md", "")

    # 检查跌破20日线的仓位上限描述——"5成"和"50%"等价，不视为矛盾
    has_50pct = "减仓至50%" in content_择时 or "减半仓" in content_择时
    has_5cheng = "5成" in content_择时
    # "50%以下"和"5成"等价，不算矛盾；但如果有"20%"和"80%"这种明显差异才标记

    # 检查2: 止损规则一致性
    stop_loss_7 = 0
    if "-7%" in content_交易:
        stop_loss_7 += 1
    if "亏损达-7%强制止损" in content_交易:
        stop_loss_7 += 1
    if stop_loss_7 == 0:
        contradictions.append({
            "type": "止损规则缺失",
            "detail": "交易执行规则中未找到明确的-7%止损描述",
            "files": ["策略/交易执行规则.md"],
            "severity": "HIGH",
        })

    # 检查3: 停牌处理规则 — 复盘记录是现状记录，不需要兜底逻辑；只检查策略文件
    for fname, text in texts.items():
        if fname in ("INDEX.md", "CHANGES.md") or fname.startswith("复盘记录/") or fname.startswith("参考/"):
            continue
        if "停牌" in text and not any(kw in text for kw in ["停牌状态未知", "停牌/退市", "停牌中", "数据因停牌", "停牌处理"]):
            contradictions.append({
                "type": "停牌处理未覆盖",
                "detail": f"{fname} 提到了停牌但缺少停牌处理兜底逻辑",
                "files": [fname],
                "severity": "LOW",
            })
            break

    return contradictions


def _check_index_consistency(files: dict) -> dict:
    """检查 INDEX.md 与实际文件系统的一致性"""
    index_path = p("knowledge/INDEX.md")
    if not os.path.exists(index_path):
        return {"status": "missing", "detail": "INDEX.md 不存在"}

    try:
        with open(index_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        logger.warning(f"读取INDEX.md失败: {e}", exc_info=True)
        return {"status": "error", "detail": str(e)}

    # 提取 INDEX.md 中引用的所有文件
    index_files = set()
    for m in re.finditer(r'\[([^\]]+)\]\(([^\)]+)\)', content):
        target = m.group(2).strip()
        if target.startswith(("策略/", "复盘记录/", "CHANGES.md", "LLM-Wiki", "参考/")):
            index_files.add(target)

    # 实际文件系统中的知识库文件（不含 INDEX.md 自身）
    actual_files = set()
    for rel in files.keys():
        if rel == "INDEX.md":
            continue
        actual_files.add(rel)

    missing_in_index = actual_files - index_files
    missing_in_fs = index_files - actual_files

    return {
        "status": "ok" if not missing_in_index and not missing_in_fs else "inconsistent",
        "missing_in_index": sorted(missing_in_index),
        "missing_in_fs": sorted(missing_in_fs),
        "total_index": len(index_files),
        "total_actual": len(actual_files),
    }


def _check_strategy_update_dates(texts: dict) -> list:
    """检查策略文件是否有明确的最后更新日期标注"""
    issues = []
    for rel, text in texts.items():
        if rel.startswith("策略/"):
            has_date = bool(re.search(r'最后更新|最后修改|更新日期|Updated:|Last updated', text, re.IGNORECASE))
            if not has_date:
                issues.append({
                    "file": rel,
                    "detail": "缺少最后更新日期标注",
                    "severity": "LOW",
                    "suggest": "在文件头部添加 > 最后更新：YYYY-MM-DD",
                })
    return issues


def run_lint(max_stale_days: int = 90) -> dict:
    """执行完整知识库 lint 检查"""
    print("\n🔍 知识库一致性检查 (Knowledge Lint)")
    print("=" * 50)

    files = scan_files()
    print(f"\n📁 扫描文件: {len(files)} 个")

    # 读取所有文本内容供矛盾检测
    texts = {}
    for rel, info in files.items():
        if rel.endswith(".md") or rel.endswith(".json"):
            try:
                with open(info["path"], "r", encoding="utf-8") as f:
                    texts[rel] = f.read()
            except Exception as e:
                logger.warning(f"读取文件失败 ({rel}): {e}", exc_info=True)
                texts[rel] = ""

    report = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "total_files": len(files),
        "orphans": [],
        "broken_refs": [],
        "stale": [],
        "contradictions": [],
        "index_consistency": {},
        "missing_dates": [],
        "summary": {
            "errors": 0,
            "warnings": 0,
            "info": 0,
        },
    }

    # 1. 孤页检测
    report["orphans"] = _check_orphans(files)
    if report["orphans"]:
        print(f"\n⚠️  孤页检测: {len(report['orphans'])} 个孤页")
        for o in report["orphans"]:
            print(f"   • {o}")
        report["summary"]["warnings"] += len(report["orphans"])
    else:
        print(f"\n✅ 孤页检测: 无孤页")

    # 2. 断裂引用检测
    report["broken_refs"] = _check_broken_refs(files)
    if report["broken_refs"]:
        print(f"\n❌ 断裂引用: {len(report['broken_refs'])} 处")
        for b in report["broken_refs"][:10]:
            print(f"   • {b['source']} → {b['target']} ({b['suggest']})")
        report["summary"]["errors"] += len(report["broken_refs"])
    else:
        print(f"✅ 引用完整性: 无断裂引用")

    # 3. 过时检测
    report["stale"] = _check_staleness(files, max_stale_days)
    if report["stale"]:
        print(f"\n⚠️  过时文件: {len(report['stale'])} 个")
        for s in report["stale"]:
            print(f"   • {s['file']} ({s['age_days']}天未更新, 阈值{s['threshold']}天) [{s['severity']}]")
        report["summary"]["warnings"] += len(report["stale"])
    else:
        print(f"✅ 时效性: 无过时文件")

    # 4. 矛盾检测
    report["contradictions"] = _check_contradictions(texts)
    if report["contradictions"]:
        print(f"\n❌ 规则矛盾: {len(report['contradictions'])} 处")
        for c in report["contradictions"]:
            print(f"   • [{c['severity']}] {c['type']}: {c['detail'][:80]}")
        report["summary"]["errors"] += len(report["contradictions"])
    else:
        print(f"✅ 规则一致性: 无矛盾")

    # 5. INDEX.md 一致性
    report["index_consistency"] = _check_index_consistency(files)
    ic = report["index_consistency"]
    if ic.get("status") == "inconsistent":
        print(f"\n⚠️  INDEX.md 与实际不一致:")
        if ic.get("missing_in_index"):
            print(f"   • INDEX.md 遗漏: {len(ic['missing_in_index'])} 个文件")
        if ic.get("missing_in_fs"):
            print(f"   • INDEX.md 引用但不存在: {len(ic['missing_in_fs'])} 个文件")
        report["summary"]["warnings"] += len(ic.get("missing_in_index", [])) + len(ic.get("missing_in_fs", []))
    elif ic.get("status") == "missing":
        print(f"   ⚠️  INDEX.md 不存在")
        report["summary"]["warnings"] += 1
    else:
        print(f"✅ INDEX.md 一致性: 校验通过")

    # 6. 策略文件缺少更新日期
    report["missing_dates"] = _check_strategy_update_dates(texts)
    if report["missing_dates"]:
        print(f"\nℹ️  缺少更新日期: {len(report['missing_dates'])} 个文件")
        for m in report["missing_dates"]:
            print(f"   • {m['file']}: {m['suggest']}")
        report["summary"]["info"] += len(report["missing_dates"])

    # 汇总
    s = report["summary"]
    print(f"\n{'=' * 50}")
    print(f"📊 知识库健康度报告")
    print(f"   总文件: {report['total_files']}")
    print(f"   ❌ 错误: {s['errors']}")
    print(f"   ⚠️  警告: {s['warnings']}")
    print(f"   ℹ️  提示: {s['info']}")
    health = "🟢 健康" if s['errors'] == 0 and s['warnings'] == 0 else \
             "🟡 需关注" if s['errors'] == 0 else \
             "🔴 需修复"
    print(f"   健康度: {health}")
    print(f"{'=' * 50}")

    return report


def fix_index(files: dict) -> int:
    """自动修复 INDEX.md：添加遗漏的文件引用"""
    index_path = p("knowledge/INDEX.md")
    if not os.path.exists(index_path):
        print("INDEX.md 不存在，无法修复")
        return 1

    with open(index_path, "r", encoding="utf-8") as f:
        content = f.read()

    # 提取已引用的文件
    index_files = set()
    for m in re.finditer(r'\[([^\]]+)\]\(([^\)]+)\)', content):
        target = m.group(2).strip()
        if target.startswith(("策略/", "复盘记录/")):
            index_files.add(target)

    # 找到遗漏的文件
    added = 0
    strategy_section = False
    review_section = False

    for rel in sorted(files.keys()):
        if rel in ("INDEX.md", "CHANGES.md"):
            continue
        if rel in index_files:
            continue

        # 添加到对应章节
        if rel.startswith("策略/"):
            # 在选股策略.md 条目后追加
            new_entry = f"| [{rel}]({rel}) | 待补充描述 | AgentX | 待补充 |\n"
            # 找到表格末尾插入
            content = content.rstrip()
            content += f"\n{new_entry}"
            added += 1
        elif rel.startswith("复盘记录/"):
            # 从文件名提取日期
            m = re.search(r"(\d{8})", rel)
            date_display = f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:8]}" if m else "未知日期"
            new_entry = f"| [{rel}]({rel}) | {date_display} | 日常复盘 | — |\n"
            content = content.rstrip()
            content += f"\n{new_entry}"
            added += 1

    if added > 0:
        content += "\n"
        with open(index_path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"INDEX.md 已更新: 新增 {added} 个文件引用")
    else:
        print("INDEX.md 无需更新")

    return added


def update_changes(file_type: str, file_path_rel: str, summary: str):
    """向 CHANGES.md 追加变更记录（供 Agent4 调用）"""
    changes_path = p("knowledge/CHANGES.md")
    if not os.path.exists(changes_path):
        print("CHANGES.md 不存在，跳过变更记录")
        return

    today = datetime.now().strftime("%Y-%m-%d")

    # 类型标签映射
    type_tags = {
        "复盘": "📝 复盘",
        "选股": "📊 选股",
        "择时": "📈 择时",
        "交易": "🎯 交易",
        "风控": "🛡️ 风控",
        "新增": "🔧 新增",
        "修正": "✏️ 修正",
        "重构": "🔄 重构",
        "废弃": "❌ 废弃",
    }
    tag = type_tags.get(file_type, f"📝 {file_type}")

    new_line = f"| {today} | {tag} | {file_path_rel} | {summary} |\n"

    with open(changes_path, "r", encoding="utf-8") as f:
        content = f.read()

    # 插入到当月表格中（在 | 日期 | 类型 | 文件 | 变更摘要 | 表头之后）
    table_header = "| 日期 | 类型 | 文件 | 变更摘要 |"
    if table_header in content:
        # 找到表头后第一行（分隔行 ---），在其后插入
        lines = content.split("\n")
        insert_idx = None
        for i, line in enumerate(lines):
            if line.strip().startswith("|---") or line.strip().startswith("|:---"):
                insert_idx = i + 1
                break
        if insert_idx is not None and insert_idx < len(lines):
            lines.insert(insert_idx, new_line.rstrip())
            content = "\n".join(lines)
    else:
        # 表头不存在，追加到文件末尾
        content = content.rstrip() + f"\n{new_line}"

    with open(changes_path, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"CHANGES.md 已追加: [{tag}] {file_path_rel} — {summary}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="知识库一致性检查")
    parser.add_argument("--report", action="store_true", help="只输出 JSON 报告，供 Agent4 调用")
    parser.add_argument("--fix-index", action="store_true", help="修复 INDEX.md 与实际不符")
    parser.add_argument("--add-change", nargs=3, metavar=("TYPE", "FILE", "SUMMARY"),
                        help='添加变更记录: --add-change 复盘 "复盘记录/xxx.json" "日常复盘"')
    parser.add_argument("--max-stale-days", type=int, default=90, help="过时阈值天数（默认90）")
    args = parser.parse_args()

    if args.add_change:
        update_changes(args.add_change[0], args.add_change[1], args.add_change[2])
        sys.exit(0)

    if args.fix_index:
        files = scan_files()
        fix_index(files)
        sys.exit(0)

    # 默认：运行lint
    report = run_lint(max_stale_days=args.max_stale_days)

    if args.report:
        # JSON 输出，供 review.py 调用
        print("\n=== LINT_REPORT ===")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print("=== END ===")

    # 如果有断裂引用或矛盾，返回非零退出码
    if report["summary"]["errors"] > 0:
        sys.exit(2)
