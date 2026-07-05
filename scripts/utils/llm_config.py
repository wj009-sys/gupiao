"""
LLM 配置管理工具 — 持久化配置 L2 LLM 排序参数

用法：
    python scripts/utils/llm_config.py                      # 查看当前配置
    python scripts/utils/llm_config.py --set                 # 交互式设置
    python scripts/utils/llm_config.py --set provider=openai model=gpt-4o-mini api_key=sk-xxx
    python scripts/utils/llm_config.py --check               # 检查LLM是否可用
    python scripts/utils/llm_config.py --unset               # 清空配置（恢复规则排序）

配置文件路径: data/llm_config.json
"""
import os, sys, json, argparse

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CONFIG_PATH = os.path.join(PROJECT_ROOT, "data", "llm_config.json")


def load() -> dict:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"❌ 读取失败: {e}")
    return {"provider": "", "model": "", "api_key": "", "api_base": ""}


def save(cfg: dict):
    cfg["更新日期"] = __import__("datetime").datetime.now().strftime("%Y-%m-%d")
    cfg["说明"] = "L2 LLM排序配置"
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print(f"✅ 配置已保存: {CONFIG_PATH}")


def show(cfg: dict):
    print("=" * 50)
    print("  LLM 当前配置")
    print("=" * 50)
    for k in ("provider", "model", "api_key", "api_base"):
        v = cfg.get(k, "")
        masked = v[:8] + "****" if v and len(v) > 12 else v
        print(f"  {k:<12} = {masked or '✗ (空)'}")
    # 检查可用性
    available = all(cfg.get(k) for k in ("provider", "model", "api_key"))
    print(f"\n  LLM可用: {'✅ 是' if available else '❌ 否（将降级到规则排序）'}")
    if not available:
        print(f"  缺失: {', '.join(k for k in ('provider', 'model', 'api_key') if not cfg.get(k))}")


def interactive_setup():
    """交互式配置"""
    cfg = load()
    print("=" * 50)
    print("  LLM 交互式配置")
    print("=" * 50)
    print("  可用provider: openai / deepseek / gemini / claude")
    print("  直接回车跳过（保持原值），输入 . 清空该项\n")

    for k in ("provider", "model", "api_key", "api_base"):
        current = cfg.get(k, "")
        prompt = f"  {k:<12} [{current or '空'}]: "
        val = input(prompt).strip()
        if val == ".":
            cfg[k] = ""
            print(f"    → 已清空")
        elif val:
            cfg[k] = val
            print(f"    → 已设置")

    save(cfg)
    print()
    show(cfg)


def cli_set(args):
    """命令行设置 key=value 对"""
    cfg = load()
    valid_keys = {"provider", "model", "api_key", "api_base"}
    for kv in args.set:
        if "=" not in kv:
            print(f"  ⚠️ 忽略无效格式: {kv}（需要 key=value）")
            continue
        k, v = kv.split("=", 1)
        if k not in valid_keys:
            print(f"  ⚠️ 忽略未知key: {k}（有效: {', '.join(valid_keys)}）")
            continue
        cfg[k] = v
        print(f"  ✓ {k} = {v[:12]}{'****' if len(v) > 12 else ''}")
    save(cfg)


def main():
    parser = argparse.ArgumentParser(description="LLM 配置管理工具")
    parser.add_argument("--set", nargs="*", default=None,
                        help="设置: 交互式(无参数) 或 key=value 对")
    parser.add_argument("--check", action="store_true", help="检查LLM是否可用")
    parser.add_argument("--unset", action="store_true", help="清空所有LLM配置")
    args = parser.parse_args()

    if args.unset:
        cfg = {"provider": "", "model": "", "api_key": "", "api_base": ""}
        save(cfg)
        print("  已清空，将降级到规则排序")
        return

    if args.set is not None:
        if len(args.set) == 0:
            interactive_setup()
        else:
            cli_set(args)
        return

    if args.check:
        sys.path.insert(0, PROJECT_ROOT)
        from scripts.utils.l2_rerank import is_llm_available, get_llm_config
        cfg = get_llm_config()
        show(cfg)
        return

    # 默认：显示配置
    cfg = load()
    show(cfg)


if __name__ == "__main__":
    main()
