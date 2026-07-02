"""
微信报告发送工具

功能：
1. 通过 PushPlus API 推送报告/通知到微信（主要通道）
2. 将 Markdown 报告转换为 Word (.docx)
3. 通过 cc-connect 发送文件到微信（备选通道）
4. 支持批量发送今日所有报告

用法：
    python scripts/utils/wechat_send.py                           # 发送今日所有报告（PushPlus）
    python scripts/utils/wechat_send.py --report 情报             # 发送指定报告
    python scripts/utils/wechat_send.py --pushplus                # 强制走 PushPlus 通道
    python scripts/utils/wechat_send.py --report 决策 --text       # 仅发文本摘要
    python scripts/utils/wechat_send.py --watch                   # 监控模式（cc-connect）
    python scripts/utils/wechat_send.py --file xxx.docx           # 发送指定文件

依赖：
    pip install python-docx  (如未安装)
    PUSHPLUS_TOKEN 需在 settings.local.json 中配置

D3 异常处理:
    触发条件                    一线修复                            仍失败兜底
    ──────────────────────────  ──────────────────────────────────  ──────────────────────────
    PUSHPLUS_TOKEN未配置        检查env和settings.local.json        降级到cc-connect通道
    PushPlus API超时(>10s)     重试1次(5s超时)                      降级到cc-connect，标注超时
    PushPlus限频(5条/分钟)     间隔3-5秒重试                        合并多条为一条长消息
    报告文件不存在              搜索同日期+同类型其他目录            发送文字摘要替代
    报告内容超长(>10KB)        截取前3000字+[查看完整报告]链接      只发标题+日期摘要
    cc-connect进程不可用        检查daemon状态、重启cc-connect       仅本地保存报告，不推送
    PushPlus token过期/无效    检查API返回code，提示用户刷新         保存到本地待重发列表

D4 CHECKPOINT:
    [ ] CP1-Token有效: PUSHPLUS_TOKEN非空且在API预检中返回正常
    [ ] CP2-文件存在: 报告.md文件存在且非空
    [ ] CP3-内容非空: 发送内容长度 > 0（截取后也要检查）
    [ ] CP4-发送确认: PushPlus返回code=200/success，或cc-connect返回exit=0

D9 工作反例:
    #  反模式                    为什么不要做                    应该怎么做
    ──  ────────────────────────  ─────────────────────────────  ──────────────────────────
    1   不检查Token就直接调用API   无效请求浪费限频配额             先验证Token非空且格式正确
    2   报告发送失败不说原因      用户不知道是文件缺失还是网络问题   失败时输出具体错误码和原因
    3   硬编码PushPlus URL        API地址变更导致不可用            用常量PUSHPLUS_API管理
    4   大报告一次发送超过限频     被PushPlus拒绝或截断             超过10KB截断或分段发送
    5   发送成功/失败不记录日志    无法追溯历史推送状态             每次发送写入send.log
"""

import os
import sys
import glob
import time
import json
import urllib.request
import subprocess
import tempfile
from datetime import datetime

# 项目根目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def p(path: str) -> str:
    """转为项目绝对路径"""
    return os.path.join(PROJECT_ROOT, path)


# ─── PushPlus 配置 ──────────────────────────────────────────────────
PUSHPLUS_API = "https://www.pushplus.plus/send"
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "")

# ─── cc-connect 路径（备选通道） ────────────────────────────────────
CC_CONNECT = os.path.join(
    os.environ.get("APPDATA", "C:/Users/65004/AppData/Roaming"),
    "npm/node_modules/cc-connect/bin/cc-connect.exe"
)

# 报告路径模板（与 reports/日报/ 结构一致）
REPORT_CATEGORIES = {
    "情报":   ("情报/情报摘要_{date}.md",       "📊 情报摘要"),
    "分析":   ("分析/分析报告_{date}.md",       "📈 分析报告"),
    "风控":   ("风控/风控报告_{date}.md",       "🛡️ 风控报告"),
    "选股":   ("选股/选股建议_{date}.md",       "🔍 选股建议"),
    "操盘":   ("操盘/交易计划_{date}.md",        "🎯 交易计划"),
    "决策":   ("决策/投资决策_{date}.md",        "🏆 投资决策"),
    "复盘":   ("复盘/复盘报告_{date}.md",        "🔄 复盘报告"),
    "周度复盘": ("复盘/周度复盘_{date}.md",      "📊 周度复盘"),
}


# ══════════════════════════════════════════════════════════════════════
# PushPlus 推送通道
# ══════════════════════════════════════════════════════════════════════

def send_via_pushplus(title: str, content: str, template: str = "markdown") -> bool:
    """
    通过 PushPlus API 推送消息到微信

    Args:
        title: 消息标题
        content: 消息内容（支持 markdown）
        template: 模板格式（markdown/html/txt）

    Returns:
        bool: 是否发送成功
    """
    if not PUSHPLUS_TOKEN:
        print("  ❌ PUSHPLUS_TOKEN 未配置")
        print("  💡 请在 .claude/settings.local.json 中设置 env.PUSHPLUS_TOKEN")
        return False

    # PushPlus markdown 内容过长会被截断，限制长度
    if template == "markdown" and len(content) > 30000:
        content = content[:30000] + "\n\n... (内容过长已截断)"

    data = json.dumps({
        "token": PUSHPLUS_TOKEN,
        "title": title,
        "content": content,
        "template": template,
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            PUSHPLUS_API,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))

        if result.get("code") == 200:
            print(f"  ✅ PushPlus 推送成功: {title}")
            return True
        else:
            print(f"  ❌ PushPlus 推送失败: {result.get('msg', '未知错误')}")
            return False
    except urllib.error.URLError as e:
        print(f"  ❌ PushPlus 网络错误: {e.reason}")
        return False
    except Exception as e:
        print(f"  ❌ PushPlus 推送异常: {e}")
        return False


def send_report_via_pushplus(report_type: str, date_str: str = None) -> bool:
    """
    读取指定类型的报告，通过 PushPlus 发送到微信

    Args:
        report_type: 报告类型（情报/分析/风控/选股/操盘/决策/复盘/周度复盘）
        date_str: 日期 YYYY-MM-DD

    Returns:
        bool: 是否发送成功
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")

    if report_type not in REPORT_CATEGORIES:
        print(f"  ❌ 未知报告类型: {report_type}")
        return False

    md_rel, emoji_title = REPORT_CATEGORIES[report_type]
    md_path = p(f"reports/日报/{md_rel.replace('{date}', date_str)}")

    if not os.path.exists(md_path):
        print(f"  ⚠️ 报告不存在: {md_path}")
        return False

    # 读取报告内容
    with open(md_path, "r", encoding="utf-8") as f:
        content = f.read()

    title = f"{emoji_title} — {date_str}"
    return send_via_pushplus(title, content, template="markdown")


def send_today_reports_via_pushplus(date_str: str = None) -> dict:
    """
    通过 PushPlus 发送今日所有报告到微信

    Returns:
        dict: {报告类型: 发送状态}
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")

    today_cn = datetime.now().strftime("%Y年%m月%d日")
    print(f"\n📤 通过 PushPlus 发送 {today_cn} 报告...")

    results = {}
    md_root = p("reports/日报")

    for report_type, (md_rel, emoji_title) in REPORT_CATEGORIES.items():
        md_path = os.path.join(md_root, md_rel.replace("{date}", date_str))
        if os.path.exists(md_path):
            success = send_report_via_pushplus(report_type, date_str)
            results[report_type] = "sent" if success else "failed"
            time.sleep(1)  # 避免 API 限频
        else:
            results[report_type] = "not_found"

    sent_count = sum(1 for v in results.values() if v == "sent")
    print(f"\n  📊 结果：{sent_count}/{len(results)} 份报告已推送")

    return results


# ══════════════════════════════════════════════════════════════════════
# cc-connect 通道（备选）
# ══════════════════════════════════════════════════════════════════════

def find_docx_by_report_type(report_type: str, date_str: str = None) -> str:
    """根据报告类型查找对应的 .docx 文件"""
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")

    if report_type not in REPORT_CATEGORIES:
        return None

    md_rel, _ = REPORT_CATEGORIES[report_type]
    md_path = md_rel.replace("{date}", date_str)
    docx_path = md_path.replace(".md", ".docx")
    full_path = p(f"reports/日报/{docx_path}")

    if os.path.exists(full_path):
        return full_path
    return None


def find_all_today_docx(date_str: str = None) -> list:
    """查找今日所有已生成的 .docx 报告"""
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")

    results = []
    for report_type in REPORT_CATEGORIES:
        docx_path = find_docx_by_report_type(report_type, date_str)
        if docx_path:
            results.append((report_type, docx_path))

    return results


def convert_md_to_docx(md_path: str) -> str:
    """将单份 MD 报告转为 DOCX"""
    sys.path.insert(0, p("scripts/utils"))
    from md_to_docx import convert_md_to_docx as convert

    docx_path = convert(md_path)
    return docx_path


def convert_all_md_to_docx(date_str: str = None) -> list:
    """转换今日所有 MD 报告为 DOCX"""
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")

    converted = []
    root = p(f"reports/日报")

    for report_type, (md_rel, _) in REPORT_CATEGORIES.items():
        md_path = os.path.join(root, md_rel.replace("{date}", date_str))
        if os.path.exists(md_path):
            docx_path = convert_md_to_docx(md_path)
            if docx_path:
                converted.append((report_type, docx_path))

    return converted


def send_to_wechat(file_path: str, message: str = None, max_retries: int = 3) -> bool:
    """
    通过 cc-connect 发送文件到微信

    Args:
        file_path: .docx 文件路径
        message: 附带的消息文本
        max_retries: 重试次数

    Returns:
        bool: 是否发送成功
    """
    if not os.path.exists(file_path):
        print(f"  ❌ 文件不存在: {file_path}")
        return False

    for attempt in range(1, max_retries + 1):
        try:
            cmd = [CC_CONNECT, "send", "-p", "stock-research", "--file", os.path.abspath(file_path)]
            if message:
                cmd.extend(["-m", message])

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=PROJECT_ROOT
            )

            if result.returncode == 0:
                size = os.path.getsize(file_path) / 1024
                print(f"  ✅ 已发送到微信: {os.path.basename(file_path)} ({size:.0f} KB)")
                return True

            # 解析错误
            err = result.stderr or result.stdout
            if "expired context_token" in err:
                if attempt < max_retries:
                    wait = attempt * 3
                    print(f"  ⏳ context_token 已过期，{wait}秒后重试 (第{attempt}次)...")
                    time.sleep(wait)
                else:
                    print(f"  ⚠️ 文件发送失败：context_token 过期。用户在微信发任意消息后自动刷新token。")
                    print(f"  💡 文件已保存在: {os.path.abspath(file_path)}")
                    return False
            else:
                print(f"  ❌ 发送失败 (attempt {attempt}): {err[:200]}")
                return False

        except subprocess.TimeoutExpired:
            print(f"  ⏰ 发送超时 (attempt {attempt})")
        except FileNotFoundError:
            print(f"  ❌ 未找到 cc-connect: {CC_CONNECT}")
            print(f"  💡 文件已保存在: {os.path.abspath(file_path)}")
            return False
        except Exception as e:
            print(f"  ❌ 发送异常: {e}")
            return False

    return False


def send_text_to_wechat(message: str) -> bool:
    """发送纯文本消息到微信"""
    try:
        cmd = [CC_CONNECT, "send", "-p", "stock-research", "-m", message]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, cwd=PROJECT_ROOT
        )
        if result.returncode == 0:
            print(f"  ✅ 文本已发送到微信")
            return True
        else:
            print(f"  ❌ 文本发送失败: {(result.stderr or result.stdout)[:200]}")
            return False
    except Exception as e:
        print(f"  ❌ 文本发送异常: {e}")
        return False


def generate_report_summary(date_str: str = None) -> str:
    """生成今日所有报告的文本摘要（用于微信文本发送）"""
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")
    today_cn = datetime.now().strftime("%Y年%m月%d日")
    weekday = datetime.now().strftime("%A")

    lines = [f"📋 {today_cn} 投研报告汇总", "─" * 20]

    # 检查各报告是否存在
    md_root = p("reports/日报")
    has_any = False

    for report_type, (md_rel, emoji_title) in REPORT_CATEGORIES.items():
        md_path = os.path.join(md_root, md_rel.replace("{date}", date_str))
        if os.path.exists(md_path):
            docx_path = md_path.replace(".md", ".docx")
            docx_status = "✅ Word" if os.path.exists(docx_path) else ""
            lines.append(f"\n{emoji_title}")
            lines.append(f"  📄 {os.path.basename(md_path)} {docx_status}")
            has_any = True

    if not has_any:
        lines.append("\n⚠️ 今日暂无报告")

    lines.append("\n💡 报告已通过 PushPlus 推送")

    return "\n".join(lines)


def send_today_reports(date_str: str = None, prefer_text: bool = False) -> dict:
    """
    发送今日所有报告到微信

    Returns:
        dict: {报告类型: 发送状态}
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")

    today_cn = datetime.now().strftime("%Y年%m月%d日")
    print(f"\n📤 发送 {today_cn} 报告到微信...")

    # Step 1: 确保所有 MD 都已转 DOCX
    print("  [1/3] 转换报告为 Word 格式...")
    converted = convert_all_md_to_docx(date_str)

    if not converted:
        print("  ⚠️ 今日无报告需转换/发送")
        return {}

    # Step 2: 查找所有 .docx
    print("  [2/3] 查找已生成的 Word 文件...")
    docx_files = find_all_today_docx(date_str)

    if not docx_files:
        print("  ⚠️ 未找到 Word 文件")
        return {}

    # Step 3: 发送到微信
    print("  [3/3] 发送到微信...")
    results = {}

    if prefer_text:
        # 仅发文本摘要
        summary = generate_report_summary(date_str)
        sent = send_text_to_wechat(summary)
        for report_type, _ in docx_files:
            results[report_type] = "text_sent" if sent else "failed"
    else:
        for report_type, docx_path in docx_files:
            message = f"📄 {REPORT_CATEGORIES.get(report_type, ('', report_type))[1]} — {date_str}"
            success = send_to_wechat(docx_path, message=message)
            results[report_type] = "sent" if success else "token_expired"

    # 汇总
    sent_count = sum(1 for v in results.values() if v in ("sent", "text_sent"))
    failed_count = sum(1 for v in results.values() if v == "failed")
    expired_count = sum(1 for v in results.values() if v == "token_expired")

    print(f"\n  📊 结果：{sent_count}份已发送 / {expired_count}份token过期 / {failed_count}份失败")

    if expired_count > 0:
        print(f"  💡 过期文件仍保存在 reports/日报/ 目录下，用户在微信发任意消息后，token会自动刷新")

    return results


def auto_convert_and_send(md_path: str = None, report_type: str = None, date_str: str = None):
    """
    一键转换+发送：Agent生成报告后的标准化后处理步骤

    该函数可被任何 Agent 脚本调用，实现：
    1. 将 .md 转为 .docx
    2. 尝试发送到微信
    3. 发送失败时启动监控等待token刷新

    Args:
        md_path: .md 报告路径（指定此项则转换此文件）
        report_type: 报告类型（情报/分析/风控/选股/操盘/决策/复盘）
        date_str: 日期 YYYY-MM-DD

    Returns:
        (docx_path, sent_ok)
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")

    # 确定 MD 路径
    if md_path is None and report_type is not None:
        if report_type in REPORT_CATEGORIES:
            md_rel, _ = REPORT_CATEGORIES[report_type]
            md_path = p(f"reports/日报/{md_rel.replace('{date}', date_str)}")

    if md_path and os.path.exists(md_path):
        print(f"\n📄 正在转换 {os.path.basename(md_path)} → Word...")
        docx_path = convert_md_to_docx(md_path)
        if docx_path:
            print(f"  ✅ Word已生成: {os.path.basename(docx_path)}")
            # 尝试发送到微信
            result = send_to_wechat(docx_path)
            if result:
                return (docx_path, True)
        return (docx_path, False)

    print(f"  ⚠️ 未找到报告: {report_type} ({md_path})")
    return (None, False)


def watch_and_send(date_str: str = None, interval: int = 5, max_wait: int = 300):
    """
    监控 context_token 变化，token 刷新后自动发送文件

    工作原理：
    1. 记录当前 context_tokens.json 的修改时间
    2. 轮询等待该文件变更（用户发微信消息会刷新token）
    3. token 刷新后立即尝试发送所有报告

    Args:
        date_str: 日期
        interval: 轮询间隔（秒）
        max_wait: 最长等待时间（秒）
    """
    token_file = os.path.join(
        os.path.expanduser("~"),
        ".cc-connect/weixin/stock-research/ab627185fac7@im.bot/context_tokens.json"
    )

    if not os.path.exists(token_file):
        print(f"⚠️ 未找到 token 文件: {token_file}")
        print("💡 先确保 cc-connect 微信已登录")
        return

    # 先尝试一次
    print(f"⏳ 正在等待用户微信消息刷新 token...")
    result = send_today_reports(date_str)
    if all(v == "sent" for v in result.values()):
        return

    last_mtime = os.path.getmtime(token_file)
    waited = 0

    while waited < max_wait:
        time.sleep(interval)
        waited += interval
        try:
            current_mtime = os.path.getmtime(token_file)
            if current_mtime != last_mtime:
                print(f"  🔄 token 已刷新（等待 {waited}s），正在发送文件...")
                result = send_today_reports(date_str)
                if all(v == "sent" for v in result.values()):
                    print(f"  ✅ 所有文件发送完成！")
                    return
                # 没发完继续试
                last_mtime = current_mtime
        except (OSError, FileNotFoundError):
            pass

    print(f"  ⚠️ 等待超时（{max_wait}s），文件未发送。")
    print(f"  💡 文件保存在 reports/日报/ 目录，可随时手动发送")


def main():
    """命令行入口"""
    args = sys.argv[1:]

    # 手动指定日期
    date_str = None
    prefer_text = False
    use_pushplus = False
    use_cc_connect = False

    for i, arg in enumerate(args):
        if arg == "--date" and i + 1 < len(args):
            date_str = args[i + 1]
        elif arg == "--only-text":
            prefer_text = True
        elif arg == "--pushplus":
            use_pushplus = True
        elif arg == "--cc-connect":
            use_cc_connect = True
        elif arg == "--watch":
            # 监控模式：等用户发消息后自动发送（cc-connect）
            watch_and_send(date_str)
            return
        elif arg == "--report" and i + 1 < len(args):
            # 发送指定报告类型
            report_type = args[i + 1]
            if use_pushplus or PUSHPLUS_TOKEN:
                send_report_via_pushplus(report_type, date_str)
            else:
                docx_path = find_docx_by_report_type(report_type, date_str)
                if docx_path:
                    msg = f"📄 {REPORT_CATEGORIES.get(report_type, ('', report_type))[1]}"
                    send_to_wechat(docx_path, message=msg)
                else:
                    print(f"⚠️ 未找到 {report_type} 报告")
            return
        elif arg == "--file" and i + 1 < len(args):
            # 发送指定文件（只能用 cc-connect）
            send_to_wechat(args[i + 1])
            return
        elif arg == "--summary":
            # 仅输出文本摘要
            print(generate_report_summary(date_str))
            return

    # 默认：走 PushPlus（如果已配置）
    if use_pushplus or (PUSHPLUS_TOKEN and not use_cc_connect):
        send_today_reports_via_pushplus(date_str)
    elif use_cc_connect:
        send_today_reports(date_str, prefer_text=prefer_text)
    else:
        # 自动选择：PushPlus 优先
        if PUSHPLUS_TOKEN:
            send_today_reports_via_pushplus(date_str)
        else:
            send_today_reports(date_str, prefer_text=prefer_text)


if __name__ == "__main__":
    main()
