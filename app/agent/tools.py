"""Agent 工具集。

每个 docx 上传对应一个 PrototypeSession，工具方法持有状态，
避免不同会话之间的数据混淆。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Callable

from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from app.agent.prompts import (
    EXTRACT_PAGES_PROMPT,
    GENERATE_PAGE_PROMPT,
    SITE_SUMMARY_PROMPT,
)
from app.generator.logic_runtime import normalize_logic_runtime
from app.generator.prototype import (
    assemble_project,
    extract_json_from_llm_output,
    render_page_html,
)


def init_llm(temperature: float = 0.4, max_tokens: int = 4096) -> ChatOpenAI:
    """从环境变量初始化 LLM。streaming=True 以支持思考过程 token 流。"""
    return ChatOpenAI(
        api_key=os.environ["LLM_API_KEY"],
        base_url=os.environ.get(
            "LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"
        ),
        model=os.environ.get("LLM_MODEL", "glm-4-plus"),
        temperature=temperature,
        max_tokens=max_tokens,
        streaming=True,
    )


class PrototypeSession:
    """一次 docx/站点 → 原型 的会话状态。"""

    def __init__(
        self,
        session_id: str,
        output_root: Path,
        on_event: Callable[[dict], None] | None = None,
    ):
        self.session_id = session_id
        self.output_dir = output_root / session_id
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.pages_html: dict[str, str] = {}  # key -> html 片段
        self.llm = init_llm()
        # 页面 HTML 输出体量大，单独用更高 token 上限，避免截断；
        # 网关拒绝大 max_tokens 时按阶梯降级重试
        self.page_max_tokens = int(os.environ.get("PAGE_MAX_TOKENS", 16384))
        self._page_llm_cache = {}  # max_tokens -> ChatOpenAI

        # ---- 思考过程事件 ----
        self.on_event = on_event or (lambda ev: None)
        # builder 注入的 LangChain callback handlers
        self.cb_handlers: list = []
        # 当前阶段人类可读标签 + 输出类型（text/json/html）
        self.current_phase: tuple[str, str] = ("模型思考中", "text")
        # 逐页生成进度
        self.total_pages = 0
        self.gen_index = 0

    def _page_llm(self, max_tokens: int) -> ChatOpenAI:
        """按 max_tokens 惰性创建并缓存页面生成模型。"""
        llm = self._page_llm_cache.get(max_tokens)
        if llm is None:
            llm = init_llm(max_tokens=max_tokens)
            self._page_llm_cache[max_tokens] = llm
        return llm

    def _set_phase(
        self,
        label: str,
        kind: str = "text",
        step: int | None = None,
        index: int | None = None,
        total: int | None = None,
    ) -> None:
        """切换当前阶段，并推送 phase 事件驱动前端步骤条。"""
        self.current_phase = (label, kind)
        ev: dict[str, Any] = {"type": "phase", "label": label, "kind": kind}
        if step is not None:
            ev["step"] = step
        if index is not None:
            ev["index"] = index
        if total is not None:
            ev["total"] = total
        self.on_event(ev)

    def _llm_config(self, run_name: str) -> dict[str, Any]:
        """组装 LangChain 调用配置，挂上流式 callback。"""
        return {"callbacks": self.cb_handlers, "run_name": run_name}

    # ---------- Tool 0: 站点采集结果 → 系统功能需求说明 ----------

    def summarize_site(
        self, site_pages: list[dict[str, Any]], extra_requirements: str = ""
    ) -> str:
        """把浏览器采集到的页面摘要交给 LLM 汇总成功能需求说明。"""
        self._set_phase(
            "正在分析系统功能，梳理页面与业务流程…",
            kind="text",
            step=2,
        )
        # 控制送入 LLM 的体量：每页只保留关键字段
        compact: list[dict[str, Any]] = []
        for p in site_pages:
            compact.append({
                "title": p.get("title", ""),
                "url": p.get("url", ""),
                "headings": p.get("headings", []),
                "buttons": p.get("buttons", []),
                "tableHeaders": p.get("tableHeaders", []),
                "labels": p.get("labels", []),
                "navLinks": p.get("navLinks", []),
                "bodyText": (p.get("bodyText") or "")[:800],
            })
        site_json = json.dumps(compact, ensure_ascii=False)[:12000]
        prompt = SITE_SUMMARY_PROMPT.format(
            site_json=site_json,
            extra_requirements=extra_requirements.strip() or "（用户未补充）",
        )
        resp = self.llm.invoke(prompt, config=self._llm_config("analyze_site"))
        summary = (resp.content or "").strip()
        (self.output_dir / "site_summary.md").write_text(
            summary, encoding="utf-8"
        )
        self.on_event({
            "type": "progress",
            "msg": f"系统功能分析完成，输出 {len(summary)} 字需求说明",
        })
        return summary

    # ---------- Tool 1: 提取页面清单 ----------

    def extract_pages_spec(self, doc_text: str) -> str:
        """从需求文档提取页面清单（JSON 字符串）。"""
        self._set_phase(
            "正在规划页面清单（页面名称、标识、描述）…",
            kind="json",
            step=2,
        )
        prompt = EXTRACT_PAGES_PROMPT.format(doc_text=doc_text[:8000])
        resp = self.llm.invoke(
            prompt, config=self._llm_config("extract_pages_spec")
        )
        pages_json = extract_json_from_llm_output(resp.content)
        # 落地一份 spec，便于排查
        (self.output_dir / "pages_spec.json").write_text(
            pages_json, encoding="utf-8"
        )
        try:
            self.total_pages = len(json.loads(pages_json).get("pages", []))
        except Exception:
            self.total_pages = 0
        self.gen_index = 0
        self.on_event({
            "type": "progress",
            "msg": f"页面规划完成，共识别 {self.total_pages} 个页面，开始逐个生成",
        })
        return pages_json

    # ---------- Tool 2: 生成单个页面 HTML ----------

    def generate_page_html(
        self,
        page_name: str,
        page_description: str,
        page_key: str = "",
    ) -> str:
        """生成单个页面的 HTML 并缓存。

        page_key 是 extract_pages_spec 返回的英文标识，用作缓存 key。
        若 Agent 没传，则用 _safe_key(page_name) 兜底（中文页面名仍可能
        被归一化为 page，因此强烈建议传 page_key）。
        """
        self.gen_index += 1
        idx, total = self.gen_index, self.total_pages or 0
        self._set_phase(
            f"正在生成页面「{page_name}」的高保真交互原型…",
            kind="html",
            step=3,
            index=idx,
            total=total,
        )
        # GENERATE_PAGE_PROMPT 含大量 CSS/JS 花括号，必须用 replace 注入变量
        prompt = GENERATE_PAGE_PROMPT.replace(
            "{page_name}", page_name
        ).replace("{page_description}", page_description)
        # 从高到低尝试多个 token 上限：网关拒绝时逐级降级，尽量保住输出完整
        candidates: list[int] = []
        for mt in (self.page_max_tokens, 8192, 4096):
            if mt not in candidates:
                candidates.append(mt)
        resp = None
        for i, mt in enumerate(candidates):
            try:
                resp = self._page_llm(mt).invoke(
                    prompt, config=self._llm_config(f"generate_page:{page_name}")
                )
                if i > 0:
                    self.on_event({
                        "type": "progress",
                        "msg": f"页面生成使用 max_tokens={mt}（更大取值被网关拒绝）",
                    })
                break
            except Exception as e:
                self.on_event({
                    "type": "progress",
                    "msg": f"max_tokens={mt} 生成失败（{str(e)[:80]}），降低输出上限重试",
                })
        if resp is None:
            raise RuntimeError("页面生成失败：所有 max_tokens 档位均被拒绝")
        html = _strip_code_fence(resp.content or "")

        # 确定性后处理：清除历史遗留的气泡运行时/坏脚本（无气泡页面原样返回）
        html, stats = normalize_logic_runtime(html, page_name=page_name)

        # 优先用 page_key，避免中文 page_name 都归一化成 "page"
        key = page_key or _safe_key(page_name)
        self.pages_html[key] = html
        self.on_event({
            "type": "progress",
            "msg": f"页面「{page_name}」已生成（{len(html)} 字符）",
        })
        return f"[已生成页面] {page_name} -> {key}（{len(html)} chars）"

    # ---------- Tool 3: 组装项目 ----------

    def save_prototype(self, pages_json: str) -> str:
        """把所有页面 HTML 组装成可访问的项目。"""
        self._set_phase("正在组装原型项目（菜单、iframe 入口）…", step=4)
        try:
            data = json.loads(pages_json)
        except json.JSONDecodeError:
            # 容错：从字符串里抠出 JSON
            data = json.loads(extract_json_from_llm_output(pages_json))

        pages = data.get("pages", []) if isinstance(data, dict) else data
        if not pages:
            return "[错误] pages_json 解析失败或为空"

        # 对每个 spec 页面，若没有缓存 HTML，从 spec 重新生成
        final_pages: list[dict[str, str]] = []
        for p in pages:
            name = p.get("name", "未命名")
            key = _safe_key(p.get("key") or name)
            desc = p.get("description", "")
            html = self.pages_html.get(key)
            if not html:
                # 兜底：补一次生成
                self.generate_page_html(name, desc)
                html = self.pages_html.get(key, render_page_html(name, desc))
            final_pages.append({
                "key": key,
                "name": name,
                "description": desc,
                "html": html,
            })

        entry = assemble_project(final_pages, self.output_dir)
        return f"PROTOTYPE_READY:{entry}"


# ---------- 输入 Schema ----------

class ExtractInput(BaseModel):
    doc_text: str = Field(..., description="需求文档全文（带层级）")


class GeneratePageInput(BaseModel):
    page_name: str = Field(..., description="页面名称（中文）")
    page_description: str = Field(..., description="页面描述，包括模块/字段/操作")
    page_key: str = Field(
        "",
        description="页面的英文标识（来自 extract_pages_spec 返回的 key 字段），"
        "用作缓存键，避免中文页面名归一化冲突。强烈建议传入。",
    )


class SaveInput(BaseModel):
    pages_json: str = Field(
        ...,
        description="extract_pages_spec 返回的完整 JSON 字符串",
    )


def build_tools(session: PrototypeSession) -> list[StructuredTool]:
    """把 session 的方法包装成 LangChain 工具。"""
    return [
        StructuredTool.from_function(
            session.extract_pages_spec,
            name="extract_pages_spec",
            description=(
                "从需求文档全文中提取页面清单。"
                "输入 doc_text（需求文档全文），返回 JSON 字符串。"
                "必须在 generate_page_html 之前调用一次。"
            ),
            args_schema=ExtractInput,
        ),
        StructuredTool.from_function(
            session.generate_page_html,
            name="generate_page_html",
            description=(
                "生成单个页面的 HTML 片段并缓存。"
                "输入 page_name 和 page_description。"
                "每个页面都需要调用一次。"
            ),
            args_schema=GeneratePageInput,
        ),
        StructuredTool.from_function(
            session.save_prototype,
            name="save_prototype",
            description=(
                "把所有页面 HTML 组装成可访问的原型项目。"
                "输入 pages_json（extract_pages_spec 返回的 JSON）。"
                "所有页面都已通过 generate_page_html 生成后再调用。"
            ),
            args_schema=SaveInput,
        ),
    ]


# ---------- 工具函数 ----------

def _strip_code_fence(text: str) -> str:
    """去掉 LLM 输出里的 ```html ... ``` 包裹。"""
    text = text.strip()
    if text.startswith("```"):
        # 去掉首行 fence
        text = re.sub(r"^```(?:html)?\s*\n", "", text)
        text = re.sub(r"\n```\s*$", "", text)
    return text.strip()


def _safe_key(name: str) -> str:
    """把中文/特殊字符页面名转成安全的文件名 key。"""
    if not name:
        return "page"
    # ASCII 字母数字下划线保留
    safe = re.sub(r"[^A-Za-z0-9_]", "_", name)
    safe = re.sub(r"_+", "_", safe).strip("_").lower()
    return safe or "page"
