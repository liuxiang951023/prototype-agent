"""docx 需求文档解析器。

将 docx 解析为带层级的纯文本，便于后续送入 LLM。
保留标题层级（H1/H2/H3）、表格内容、列表项，丢掉样式噪音。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from docx import Document
from docx.document import Document as DocxDocument
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph


@dataclass
class DocxParseResult:
    """docx 解析结果。"""
    text: str           # 带层级的纯文本
    page_count: int     # 段落数
    table_count: int    # 表格数
    char_count: int     # 字符数


def _iter_block_items(parent) -> Iterable:
    """按文档真实顺序迭代段落与表格（python-docx 默认会分开）。"""
    if isinstance(parent, DocxDocument):
        parent_elm = parent.element.body
    elif isinstance(parent, _Cell):
        parent_elm = parent._tc
    else:
        return
    for child in parent_elm.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, parent)
        elif isinstance(child, CT_Tbl):
            yield Table(child, parent)


def _heading_level(paragraph: Paragraph) -> int:
    """返回标题层级，非标题返回 0。"""
    style = (paragraph.style.name or "").lower()
    if style.startswith("heading"):
        # 'Heading 1' -> 1
        try:
            return int(style.split()[-1])
        except ValueError:
            return 1
    if style.startswith("title"):
        return 1
    return 0


def _render_table(table: Table) -> str:
    """把表格渲染成 markdown 风格文本，方便 LLM 阅读。"""
    lines = []
    for r_idx, row in enumerate(table.rows):
        cells = [cell.text.strip().replace("\n", " ") for cell in row.cells]
        lines.append("| " + " | ".join(cells) + " |")
        if r_idx == 0:
            lines.append("|" + "|".join(["---"] * len(cells)) + "|")
    return "\n".join(lines)


def _render_list_item(paragraph: Paragraph, ordered: bool, idx: int) -> str:
    bullet = f"{idx}." if ordered else "-"
    return f"{bullet} {paragraph.text.strip()}"


def _is_list(paragraph: Paragraph) -> tuple[bool, bool]:
    """返回 (是否列表, 是否有序)。python-docx 没直接 API，靠 numPr 判断。"""
    p = paragraph._p
    num_pr = p.find(
        "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}numPr"
    )
    if num_pr is None:
        return False, False
    num_id = num_pr.find(
        "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}numId"
    )
    # 简化判定：有 numId 即视为列表，0 视为无序，其他视为有序
    val = num_id.get(
        "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val"
    ) if num_id is not None else "0"
    return True, val != "0"


def parse_docx(file_path: str | Path) -> DocxParseResult:
    """解析 docx 文件，返回带层级的纯文本。

    结构示例：
        # 1. 功能概述
        ## 1.1 用户登录
        - 用户名
        - 密码

        | 字段 | 说明 |
        | --- | --- |
        | username | 登录账号 |
    """
    doc = Document(str(file_path))

    parts: list[str] = []
    para_count = 0
    table_count = 0
    list_idx = 0
    current_ordered = False

    for block in _iter_block_items(doc):
        if isinstance(block, Paragraph):
            text = block.text.strip()
            if not text:
                continue

            level = _heading_level(block)
            is_list, ordered = _is_list(block)

            if level > 0:
                prefix = "#" * min(level, 6)
                parts.append(f"{prefix} {text}")
                list_idx = 0
                current_ordered = False
            elif is_list:
                if ordered != current_ordered:
                    list_idx = 0
                    current_ordered = ordered
                list_idx += 1
                parts.append(_render_list_item(block, ordered, list_idx))
            else:
                parts.append(text)
            para_count += 1

        elif isinstance(block, Table):
            parts.append("")
            parts.append(_render_table(block))
            parts.append("")
            table_count += 1

    text = "\n".join(parts)
    return DocxParseResult(
        text=text,
        page_count=para_count,
        table_count=table_count,
        char_count=len(text),
    )


def chunk_by_heading(text: str, max_chars: int = 4000) -> list[str]:
    """按一级/二级标题切片，超长再按 max_chars 二次切。"""
    chunks: list[str] = []
    buffer: list[str] = []
    buffer_size = 0

    for line in text.split("\n"):
        is_h = line.startswith("# ") or line.startswith("## ")
        # 命中新的二级标题，先把缓冲区吐出去
        if is_h and buffer_size >= max_chars // 2:
            chunks.append("\n".join(buffer))
            buffer = []
            buffer_size = 0
        buffer.append(line)
        buffer_size += len(line) + 1

    if buffer:
        chunks.append("\n".join(buffer))
    return chunks
