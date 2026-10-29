"""Agent 构建与运行入口。

事件协议（SSE 推给前端，全部为结构化事件，思考流与工具流分开）：
  phase        阶段切换（step 1-4，index/total 表示逐页进度）
  progress     一句话进度
  browser      浏览器动作（start/login/page/nav/skip/done）
  tool_call    Agent 调用工具
  tool_result  工具返回
  thought_start  一次 LLM 调用开始输出（id/label/kind：text|json|html|reasoning）
  thought_delta  LLM 输出增量（按 id 聚合，节流推送）
  thought_end    一次 LLM 调用结束
  done / error
"""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.callbacks.base import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel

from app.agent.prompts import build_agent_prompt
from app.agent.tools import PrototypeSession, build_tools

OUTPUT_ROOT = Path(__file__).resolve().parent.parent.parent / "output"

# delta 推送最小间隔（秒），避免 SSE 被 token 级事件打爆
_FLUSH_INTERVAL = 0.1


def build_agent_executor(
    session: PrototypeSession, llm: BaseChatModel
) -> AgentExecutor:
    """构建一次会话对应的 AgentExecutor。"""
    prompt = build_agent_prompt()
    tools = build_tools(session)
    agent = create_tool_calling_agent(llm, tools, prompt)
    return AgentExecutor(
        agent=agent,
        tools=tools,
        verbose=True,
        max_iterations=24,        # 留足空间逐个生成页面
        max_execution_time=900,   # 15 分钟超时（含站点浏览）
        handle_parsing_errors=True,
        return_intermediate_steps=False,
    )


class ThinkingCallbackHandler(BaseCallbackHandler):
    """把 LangChain 运行时事件转成前端 SSE 结构化事件。

    设计要点：
    - 思考流走独立通道（thought_start/delta/end），绝不拼进工具/进度文本；
    - 同一次 LLM 调用内，正文与 reasoning（思考模型，如 GLM-4.5/DeepSeek-R）
      使用不同 channel id，仅在确有 reasoning_content 字段时才开启，不伪造；
    - delta 按时间节流聚合，end 时强制 flush，数据完整不截断。
    """

    def __init__(
        self,
        on_event: Callable[[dict], None],
        get_phase: Callable[[], tuple[str, str]],
    ):
        self._on_event = on_event
        self._get_phase = get_phase
        self._call_seq = 0
        self._cur_id: str | None = None
        self._reasoning_started = False
        self._buffers: dict[str, str] = {}
        self._last_flush = 0.0

    # ---------- LLM ----------

    def on_llm_start(self, serialized, prompts, **kwargs) -> None:
        self._call_seq += 1
        self._cur_id = f"llm-{self._call_seq}"
        self._reasoning_started = False
        self._buffers = {"main": "", "reasoning": ""}
        self._last_flush = time.time()
        label, kind = self._get_phase()
        self._on_event({
            "type": "thought_start",
            "id": self._cur_id,
            "label": label or "模型思考中",
            "kind": kind,
        })

    def on_llm_new_token(self, token: str, *, chunk=None, **kwargs) -> None:
        if not self._cur_id:
            return

        # 思考模型的推理链：仅在协议字段真实存在时分流
        reasoning = ""
        if chunk is not None:
            ak = getattr(chunk, "additional_kwargs", {}) or {}
            reasoning = ak.get("reasoning_content") or ak.get("reasoning") or ""

        if reasoning:
            if not self._reasoning_started:
                self._on_event({
                    "type": "thought_start",
                    "id": f"{self._cur_id}-r",
                    "label": "模型推理过程",
                    "kind": "reasoning",
                })
                self._reasoning_started = True
            self._buffers["reasoning"] += reasoning
        if token:
            self._buffers["main"] += token
        self._maybe_flush()

    def on_llm_end(self, response, **kwargs) -> None:
        self._flush()
        if self._reasoning_started:
            self._on_event({"type": "thought_end", "id": f"{self._cur_id}-r"})
        if self._cur_id:
            self._on_event({"type": "thought_end", "id": self._cur_id})
        self._cur_id = None
        self._buffers = {}

    def on_llm_error(self, error, **kwargs) -> None:
        self._flush()
        if self._cur_id:
            self._on_event({"type": "thought_end", "id": self._cur_id})
        self._cur_id = None
        self._buffers = {}

    # ---------- Tool（Agent 层工具调用） ----------

    def on_tool_start(self, serialized, input_str, **kwargs) -> None:
        name = (serialized or {}).get("name") or kwargs.get("name", "")
        preview = str(input_str)
        # 工具入参可能含整篇文档，只展示开头
        if len(preview) > 300:
            preview = preview[:300] + "…（已截断）"
        self._on_event({
            "type": "tool_call",
            "tool": name,
            "input": preview,
        })

    def on_tool_end(self, output, **kwargs) -> None:
        preview = str(output)
        if len(preview) > 300:
            preview = preview[:300] + "…（已截断）"
        self._on_event({
            "type": "tool_result",
            "output": preview,
        })

    # ---------- 内部 ----------

    def _maybe_flush(self) -> None:
        now = time.time()
        if now - self._last_flush >= _FLUSH_INTERVAL:
            self._flush()
            self._last_flush = now

    def _flush(self) -> None:
        if not self._cur_id:
            return
        if self._buffers.get("reasoning"):
            self._on_event({
                "type": "thought_delta",
                "id": f"{self._cur_id}-r",
                "delta": self._buffers["reasoning"],
            })
            self._buffers["reasoning"] = ""
        if self._buffers.get("main"):
            self._on_event({
                "type": "thought_delta",
                "id": self._cur_id,
                "delta": self._buffers["main"],
            })
            self._buffers["main"] = ""


def run_pipeline_streaming(
    on_event: Callable[[dict], None],
    doc_text: str = "",
    site_url: str = "",
    site_username: str = "",
    site_password: str = "",
    extra_requirements: str = "",
    output_root: Path | None = None,
) -> dict[str, Any]:
    """流式运行 pipeline：站点浏览/文档读取 → 功能分析 → 规划 → 逐页生成 → 组装。

    返回 {"session_id", "output_dir", "entry", "log", "preview_url"}。
    """
    session_id = uuid.uuid4().hex[:12]
    root = output_root or OUTPUT_ROOT
    root.mkdir(parents=True, exist_ok=True)

    on_event({"type": "progress", "msg": "正在初始化 Agent 会话…"})

    session = PrototypeSession(
        session_id=session_id, output_root=root, on_event=on_event
    )
    handler = ThinkingCallbackHandler(on_event, lambda: session.current_phase)
    session.cb_handlers.append(handler)

    executor = build_agent_executor(session, session.llm)
    on_event({"type": "progress", "msg": "Agent 就绪，开始采集信息"})

    # ---------- 步骤 1：采集信息 ----------
    on_event({"type": "phase", "step": 1, "label": "采集需求信息", "kind": "text"})

    context_parts: list[str] = []

    if site_url.strip():
        from app.browser.site_explorer import explore_site

        shot_base_url = f"/proto/{session_id}"
        site_pages = explore_site(
            url=site_url,
            username=site_username,
            password=site_password,
            output_dir=session.output_dir,
            shot_base_url=shot_base_url,
            on_event=on_event,
        )
        # 步骤 2（前段）：站点功能分析
        site_summary = session.summarize_site(site_pages, extra_requirements)
        context_parts.append(
            "# 一、目标系统现状（浏览器自动采集并经产品分析得出）\n"
            + site_summary
        )
    else:
        on_event({
            "type": "browser", "action": "skip",
            "msg": "未填写系统网址，跳过站点浏览",
        })

    if doc_text.strip():
        on_event({
            "type": "progress",
            "msg": f"已读取需求文档（{len(doc_text)} 字符）",
        })
        context_parts.append("# 二、用户上传的需求文档\n" + doc_text[:8000])

    if extra_requirements.strip() and not site_url.strip():
        # 有站点时补充想法已在 summarize_site 中对齐，无需重复
        context_parts.append(
            "# 三、用户补充的想法与需求\n" + extra_requirements.strip()
        )

    combined_doc = "\n\n".join(context_parts).strip()
    if not combined_doc:
        # 理论上 main 层已校验，兜底
        raise ValueError("没有可用的需求来源（网站 / 文档 / 补充想法至少提供一项）")

    # ---------- 步骤 2-4：Agent 规划 / 逐页生成 / 组装 ----------
    on_event({"type": "progress", "msg": "开始页面规划与原型生成流水线"})

    final_output = ""
    try:
        for step in executor.stream(
            {"doc_text": combined_doc},
            {"callbacks": [handler]},
        ):
            # 工具/思考事件全部由 callback 推送，这里只取最终 output
            if isinstance(step, dict) and "output" in step:
                final_output = step.get("output", "") or ""
    except Exception as e:
        on_event({"type": "error", "msg": f"Agent 执行失败：{e}"})
        raise

    on_event({"type": "phase", "step": 4, "label": "原型组装完成", "kind": "text"})

    entry = _extract_entry_path(final_output, session.output_dir)
    preview_url = f"/proto/{session_id}/index.html"

    result = {
        "session_id": session_id,
        "output_dir": str(session.output_dir),
        "entry": entry,
        "log": final_output,
        "preview_url": preview_url,
    }
    on_event({"type": "done", **result})
    return result


def run_pipeline(doc_text: str, output_root: Path | None = None) -> dict[str, Any]:
    """非流式入口（兼容旧调用）：docx 文本 → 原型项目。"""
    return run_pipeline_streaming(
        on_event=lambda ev: None,
        doc_text=doc_text,
        output_root=output_root,
    )


def _extract_entry_path(text: str, output_dir: Path) -> str:
    """从 Agent 输出里抠出 PROTOTYPE_READY:<path>。"""
    m = re.search(r"PROTOTYPE_READY:?\s*(.+)", text)
    if m:
        return m.group(1).strip()
    # 兜底：直接返回 output_dir/index.html
    return str(output_dir / "index.html")
