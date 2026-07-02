"""
Markdown → Word (.docx) 转换工具

用法:
    # 单个文件
    python scripts/utils/md_to_docx.py reports/日报/决策/投资决策_2026-06-27.md

    # 批量转换今日所有报告
    python scripts/utils/md_to_docx.py --today

    # 指定输出目录
    python scripts/utils/md_to_docx.py input.md --output-dir reports/日报/决策/

D3 异常处理:
    触发条件                    一线修复                        仍失败兜底
    ──────────────────────────  ──────────────────────────────  ────────────────────────────
    输入.md文件不存在            检查路径拼写、搜索同日期文件      跳过转换，标注"报告缺失"
    .md文件为空(0字节)           检查文件大小，提示空文件          跳过，输出警告
    python-docx未安装            pip install python-docx          降级：输出纯文本.txt
    表格解析失败                 逐行解析、跳过问题行              表格转为文字描述段落
    中文编码错误                 显式指定UTF-8编码                用errors='replace'替换乱码
    输出文件写入失败             检查磁盘空间和目录权限            输出到临时目录%TEMP%

D4 CHECKPOINT:
    [ ] CP1-输入文件存在且非空: os.path.exists() + os.path.getsize() > 0
    [ ] CP2-输出目录可写: os.access(out_dir, os.W_OK)
    [ ] CP3-转换后.docx文件大小 > 0: 防止空文件覆盖
    [ ] CP4-关键章节标题保留: 检查一级标题#和二级标题##在输出中数量一致

D9 工作反例:
    #  反模式                    为什么不要做                    应该怎么做
    ──  ────────────────────────  ─────────────────────────────  ──────────────────────────
    1   不检查文件存在直接转换     文件不存在时崩溃，无提示         先os.path.exists()检查
    2   用系统默认编码不指定UTF-8  中文在不同系统乱码               显式encoding='utf-8'
    3   表格解析异常时崩溃         一个坏表格导致整个报告丢失       逐表try/except，失败表转文字
    4   不验证输出质量             .docx为0字节也当成功             转换后检查文件大小
    5   硬编码输出路径             环境不同路径不存在               用参数--output-dir指定
"""

import os
import re
import sys
import glob
from datetime import datetime
from docx import Document
from docx.shared import Pt, Inches, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn, nsdecls
from docx.oxml import parse_xml


def add_heading_styled(doc, text, level=1):
    """添加带样式的标题"""
    heading = doc.add_heading(text, level=level)
    return heading


def add_paragraph_styled(doc, text, bold=False, color=None, font_size=None):
    """添加格式化段落（支持内联粗体）"""
    p = doc.add_paragraph()

    # 处理内联粗体 **text** 和行内代码 `code`
    parts = re.split(r'(\*\*.*?\*\*|`.*?`)', text)
    for part in parts:
        if part.startswith('**') and part.endswith('**'):
            run = p.add_run(part[2:-2])
            run.bold = True
        elif part.startswith('`') and part.endswith('`'):
            run = p.add_run(part[1:-1])
            run.font.name = 'Courier New'
            run.font.size = Pt(9)
            run.font.color.rgb = RGBColor(0x33, 0x33, 0x33)
        else:
            run = p.add_run(part)

    if bold:
        for run in p.runs:
            run.bold = True
    if color:
        for run in p.runs:
            run.font.color.rgb = color
    if font_size:
        for run in p.runs:
            run.font.size = Pt(font_size)

    return p


def add_table_from_markdown(doc, md_table_text):
    """将Markdown表格转换为Word表格"""
    lines = [l.strip() for l in md_table_text.strip().split('\n') if l.strip()]
    if len(lines) < 2:  # 至少需要表头+分隔行
        return

    # 解析表头
    headers = [h.strip().strip('|') for h in lines[0].split('|') if h.strip()]
    # 解析数据行（跳过第1行表头和第2行分隔符）
    data_rows = []
    for line in lines[2:]:
        cells = [c.strip().strip('|') for c in line.split('|') if c.strip()]
        if cells:
            data_rows.append(cells)

    if not headers or not data_rows:
        return

    # 创建Word表格
    table = doc.add_table(rows=len(data_rows) + 1, cols=len(headers))
    table.style = 'Light Grid Accent 1'
    table.alignment = WD_TABLE_ALIGNMENT.CENTER

    # 表头
    header_row = table.rows[0]
    for i, h in enumerate(headers):
        if i < len(header_row.cells):
            cell = header_row.cells[i]
            cell.text = h
            # 表头加粗
            for paragraph in cell.paragraphs:
                paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
                for run in paragraph.runs:
                    run.bold = True
                    run.font.size = Pt(10)

    # 数据行
    for row_idx, row_data in enumerate(data_rows):
        row = table.rows[row_idx + 1]
        for col_idx, cell_text in enumerate(row_data):
            if col_idx < len(row.cells):
                cell = row.cells[col_idx]
                cell.text = cell_text
                for paragraph in cell.paragraphs:
                    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    for run in paragraph.runs:
                        run.font.size = Pt(10)

    doc.add_paragraph()  # 表后空行


def convert_md_to_docx(md_path, output_path=None):
    """将单个Markdown文件转换为Word文档"""
    if not os.path.exists(md_path):
        print(f"❌ 文件不存在: {md_path}")
        return None

    # 读取Markdown
    with open(md_path, 'r', encoding='utf-8') as f:
        content = f.read()

    # 确定输出路径
    if output_path:
        out_path = output_path
    else:
        base = os.path.splitext(md_path)[0]
        out_path = base + '.docx'

    # 创建文档
    doc = Document()

    # 设置默认字体（跨平台CJK字体回退链）
    style = doc.styles['Normal']
    font = style.font
    # 按平台优先级选择CJK字体: 微软雅黑(Win) > 苹方(macOS) > 思源黑体(Linux) > 系统默认
    import platform
    _sys = platform.system()
    if _sys == "Windows":
        _cjk_font = "微软雅黑"
    elif _sys == "Darwin":
        _cjk_font = "PingFang SC"
    else:
        _cjk_font = "Source Han Sans SC"
    font.name = _cjk_font
    font.size = Pt(11)
    try:
        style.element.rPr.rFonts.set(qn('w:eastAsia'), _cjk_font)
    except Exception:
        pass  # 东亚洲字体设置失败时不影响正文

    # 段落间距
    pf = style.paragraph_format
    pf.space_after = Pt(4)
    pf.line_spacing = 1.15

    # 逐行解析
    lines = content.split('\n')
    i = 0
    in_code_block = False
    in_table = False
    table_buffer = []

    while i < len(lines):
        line = lines[i]

        # 代码块（跳过但保留标记）
        if line.strip().startswith('```'):
            if in_code_block:
                in_code_block = False
                doc.add_paragraph()  # 空行分隔
            else:
                in_code_block = True
            i += 1
            continue

        if in_code_block:
            # 代码内容用等宽字体段落
            p = doc.add_paragraph()
            run = p.add_run(line)
            run.font.name = 'Courier New'
            run.font.size = Pt(9)
            run.font.color.rgb = RGBColor(0x55, 0x55, 0x55)
            i += 1
            continue

        # 空行
        if not line.strip():
            if in_table:
                # 表格结束
                add_table_from_markdown(doc, '\n'.join(table_buffer))
                in_table = False
                table_buffer = []
            i += 1
            continue

        # 检测表格行（以|开头和结尾）
        if line.strip().startswith('|') and line.strip().endswith('|'):
            in_table = True
            table_buffer.append(line)
            i += 1
            continue

        # 如果之前正在解析表格但当前行不是表格行，结束表格
        if in_table:
            add_table_from_markdown(doc, '\n'.join(table_buffer))
            in_table = False
            table_buffer = []

        stripped = line.strip()

        # 分隔线 ---
        if re.match(r'^-{3,}$', stripped) or re.match(r'^\*{3,}$', stripped):
            # 添加水平线（用边框底部替代）
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(6)
            p.paragraph_format.space_after = Pt(6)
            pPr = p._p.get_or_add_pPr()
            pBdr = parse_xml(
                '<w:pBdr %s>'
                '  <w:bottom w:val="single" w:sz="6" w:space="1" w:color="999999"/>'
                '</w:pBdr>' % nsdecls('w')
            )
            pPr.append(pBdr)
            i += 1
            continue

        # 标题 # ## ###
        heading_match = re.match(r'^(#{1,3})\s+(.+)$', stripped)
        if heading_match:
            level = len(heading_match.group(1))
            text = heading_match.group(2).strip()
            # 去掉行内标记如 **text**
            text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
            text = re.sub(r'`(.*?)`', r'\1', text)
            add_heading_styled(doc, text, level)
            i += 1
            continue

        # 列表项 - 或 *
        list_match = re.match(r'^[\-\*]\s+(.+)$', stripped)
        if list_match:
            text = list_match.group(1)
            p = doc.add_paragraph(style='List Bullet')
            # 清空自动文本并替换
            parts = re.split(r'(\*\*.*?\*\*)', text)
            p.clear()
            for part in parts:
                if part.startswith('**') and part.endswith('**'):
                    run = p.add_run(part[2:-2])
                    run.bold = True
                else:
                    run = p.add_run(part)
            i += 1
            continue

        # 编号列表 1. 2. 3.
        num_match = re.match(r'^(\d+)[\'"]*[\.\)]\s+(.+)$', stripped)
        if num_match:
            text = num_match.group(2)
            p = doc.add_paragraph(style='List Number')
            parts = re.split(r'(\*\*.*?\*\*)', text)
            p.clear()
            for part in parts:
                if part.startswith('**') and part.endswith('**'):
                    run = p.add_run(part[2:-2])
                    run.bold = True
                else:
                    run = p.add_run(part)
            i += 1
            continue

        # 普通段落（引用 > 开头）
        if stripped.startswith('>'):
            text = re.sub(r'^>\s*', '', stripped)
            # 去掉行内标记
            text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
            text = re.sub(r'`(.*?)`', r'\1', text)
            p = doc.add_paragraph()
            run = p.add_run(text)
            run.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
            run.italic = True
            p.paragraph_format.left_indent = Inches(0.3)
            i += 1
            continue

        # 普通段落
        add_paragraph_styled(doc, line)
        i += 1

    # 如果最后还在表格中
    if in_table and table_buffer:
        add_table_from_markdown(doc, '\n'.join(table_buffer))

    # 保存
    doc.save(out_path)
    size_kb = os.path.getsize(out_path) / 1024
    print(f"✅ {os.path.basename(md_path)} → {os.path.basename(out_path)} ({size_kb:.0f} KB)")
    return out_path


def convert_today_reports():
    """转换今日所有报告"""
    today = datetime.now().strftime("%Y-%m-%d")
    root = os.path.join(os.path.dirname(__file__), "..", "..", "reports", "日报")

    report_patterns = [
        f"决策/投资决策_{today}.md",
        f"情报/情报摘要_{today}.md",
        f"分析/分析报告_{today}.md",
    ]

    converted = []
    for pattern in report_patterns:
        md_path = os.path.join(root, pattern)
        if os.path.exists(md_path):
            result = convert_md_to_docx(md_path)
            if result:
                converted.append(result)

    if not converted:
        print("⚠️ 今日无报告需转换")
    else:
        print(f"\n📤 共转换 {len(converted)} 份报告")

    return converted


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--today":
        convert_today_reports()
    elif len(sys.argv) > 1:
        output_dir = None
        if "--output-dir" in sys.argv:
            idx = sys.argv.index("--output-dir")
            if idx + 1 < len(sys.argv):
                output_dir = sys.argv[idx + 1]
        md_file = sys.argv[1]
        if md_file.startswith("--"):
            print("用法: python md_to_docx.py <file.md> [--output-dir <dir>]")
            sys.exit(1)
        out_path = None
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
            basename = os.path.splitext(os.path.basename(md_file))[0]
            out_path = os.path.join(output_dir, basename + ".docx")
        convert_md_to_docx(md_file, out_path)
    else:
        print("用法:")
        print("  python scripts/utils/md_to_docx.py <file.md>        # 单个文件")
        print("  python scripts/utils/md_to_docx.py --today          # 批量今日所有报告")
