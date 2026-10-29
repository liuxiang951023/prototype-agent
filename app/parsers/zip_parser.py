"""zip 压缩包解析器（标准库 zipfile，无第三方依赖）。

把 zip 内的需求文档合并为带层级的纯文本，支持：
  - .docx：解压后复用 docx_parser
  - .txt / .md：直接读取文本（utf-8，失败回退 gbk）
其他类型文件忽略；不支持嵌套压缩包。

安全防护：
  - 路径穿越（zip slip）：拒绝绝对路径 / 含 .. 的条目，并二次校验解压目标；
  - 压缩炸弹：限制条目数、单文件大小与解压总大小。
"""
from __future__ import annotations

import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from app.parsers.docx_parser import DocxParseResult, parse_docx

_TEXT_EXTS = {".txt", ".md"}

_MAX_ENTRIES = 50
_MAX_SINGLE_BYTES = 20 * 1024 * 1024       # 单文件 20MB
_MAX_TOTAL_BYTES = 50 * 1024 * 1024        # 解压总量 50MB


def _safe_entry_name(raw: str) -> str | None:
    """返回安全的条目相对路径；含路径穿越/绝对路径时返回 None。"""
    norm = raw.replace("\\", "/")
    if norm.startswith("/") or ":" in norm:
        return None
    parts = PurePosixPath(norm).parts
    if ".." in parts:
        return None
    return norm


def _is_within(base: Path, target: Path) -> bool:
    """二次校验 target 解析后仍位于 base 内。"""
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def _read_text(data: bytes) -> str:
    """优先 utf-8，兼容 gbk，仍失败则忽略非法字节。"""
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


def parse_zip(file_path: str | Path) -> DocxParseResult:
    """解析 zip 压缩包，合并其中所有可识别文档为纯文本。"""
    if not zipfile.is_zipfile(file_path):
        raise ValueError("不是有效的 zip 压缩包")

    parts: list[str] = []
    files_parsed = 0
    table_count = 0
    total_inflate = 0

    with zipfile.ZipFile(file_path) as zf, tempfile.TemporaryDirectory(
        prefix="reqzip_"
    ) as td:
        base = Path(td)
        entries = [i for i in zf.infolist() if not i.is_dir()]
        if len(entries) > _MAX_ENTRIES:
            raise ValueError(f"压缩包内文件过多（上限 {_MAX_ENTRIES} 个）")

        for info in entries:
            safe_name = _safe_entry_name(info.filename)
            if safe_name is None:
                # 可疑条目：跳过不解压
                continue
            if info.file_size > _MAX_SINGLE_BYTES:
                continue
            total_inflate += info.file_size
            if total_inflate > _MAX_TOTAL_BYTES:
                raise ValueError("压缩包解压后总大小超过 50MB 上限")

            ext = PurePosixPath(safe_name).suffix.lower()
            if ext not in (".docx", *_TEXT_EXTS):
                continue

            target = base / safe_name
            if not _is_within(base, target):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)

            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)

            if ext == ".docx":
                r = parse_docx(target)
                parts.append(f"# 文件：{safe_name}\n{r.text}")
                table_count += r.table_count
            else:
                parts.append(
                    f"# 文件：{safe_name}\n" + _read_text(target.read_bytes())
                )
            files_parsed += 1

    if not files_parsed:
        raise ValueError("压缩包内没有可识别的需求文档（支持 .docx / .txt / .md）")

    text = "\n\n---\n\n".join(parts)
    return DocxParseResult(
        text=text,
        page_count=files_parsed,
        table_count=table_count,
        char_count=len(text),
    )
