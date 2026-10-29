"""业务逻辑气泡运行时的确定性后处理。

当前页面生成已不再要求业务逻辑气泡，本模块退化为兜底清理：
  - 页面中没有任何气泡时，仅清除历史遗留的损坏脚本/弹窗，原样返回；
  - 若 LLM 仍自发输出了气泡（showLogic/WF_LOGIC），则按原机制
    容错抢救数据并注入一份保证语法合法的统一运行时，保证点击必有响应。
"""
from __future__ import annotations

import json
import re
from typing import Callable, Optional

_BUBBLE_RE = re.compile(r"showLogic\(\s*['\"]([A-Za-z0-9_]+)['\"]\s*\)")

_FIELDS = ("action", "precheck", "form", "submit",
           "success", "error", "state", "permission")

_MISSING_NOTE = "（该按钮的业务逻辑在 AI 输出时被截断或缺失，重新生成该页可补全）"


def normalize_logic_runtime(
    html: str,
    page_name: str = "",
    repair_fn: Optional[Callable[[list[str], dict], dict]] = None,
) -> tuple[str, dict]:
    """规范化页面中的业务逻辑气泡运行时。

    repair_fn: 可选，入参为 (缺失数据的按钮 key 列表, {key: 按钮文案})，
              返回 {key: logic_obj}。
    返回 (新 html, 统计信息)。
    """
    keys = _extract_bubble_keys(html)
    salvaged = _salvage_logic(html)

    # 无气泡页面：只清理历史遗留的旧运行时/坏脚本，不再注入任何东西
    if not keys:
        html = _strip_old_runtime(html)
        stats = {"bubble_keys": 0, "salvaged": 0, "repaired": 0, "stubbed": 0}
        return html, stats

    labels = extract_button_labels(html)

    missing = [k for k in keys if k not in salvaged]
    repaired: dict = {}
    if missing and repair_fn is not None:
        try:
            repaired = repair_fn(missing, labels) or {}
        except Exception:
            repaired = {}

    data: dict = {}
    for k in keys:
        obj = salvaged.get(k) or repaired.get(k)
        data[k] = _coerce_entry(obj, k)

    # 删掉损坏的旧脚本/弹窗，再注入统一运行时
    html = _strip_old_runtime(html)
    html = _inject_runtime(html, page_name, data)

    stats = {
        "bubble_keys": len(keys),
        "salvaged": len([k for k in keys if k in salvaged]),
        "repaired": len([k for k in missing if k in repaired]),
        "stubbed": len([k for k in keys
                        if k not in salvaged and k not in repaired]),
    }
    return html, stats


# ---------- 1. 气泡 key ----------

def _extract_bubble_keys(html: str) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for m in _BUBBLE_RE.finditer(html):
        k = m.group(1)
        if k not in seen:
            seen.add(k)
            ordered.append(k)
    return ordered


# 形如：<span class="wf-btn...">按钮文字<span ... onclick="showLogic('btn_xx')">
_LABEL_RE = re.compile(
    r"<[a-z]+\b[^>]*class\s*=\s*['\"][^'\"]*wf-btn[^'\"]*['\"][^>]*>"
    r"([^<]{1,20}?)\s*<[a-z]+\b[^>]*showLogic\(\s*['\"]([A-Za-z0-9_]+)['\"]",
    re.S | re.I,
)


def extract_button_labels(html: str) -> dict[str, str]:
    """从标注里提取 {logic_key: 按钮中文文案}，供缺失数据补齐时使用。"""
    out: dict[str, str] = {}
    for m in _LABEL_RE.finditer(html):
        label, key = m.group(1).strip(), m.group(2)
        if key not in out and label:
            out[key] = re.sub(r"\s+", "", label)
    return out


# ---------- 2. 容错扫描器 ----------

def _is_cjk(ch: str) -> bool:
    if not ch:
        return False
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF
        or 0x3000 <= code <= 0x303F
        or 0xFF00 <= code <= 0xFFEF
    )


def _find_logic_region(html: str) -> str:
    """返回 WF_LOGIC 对象所在的原始文本（从第一个 { 起，到文件末尾或对象结束）。"""
    idx = html.find("WF_LOGIC")
    if idx < 0:
        return ""
    brace = html.find("{", idx)
    if brace < 0:
        return ""
    return html[brace:]


class _Scanner:
    """字符扫描器：识别字符串/注释/括号深度。

    关键容错：双引号字符串内遇到半角引号时，若它紧邻中文（前一字符或
    后一字符是 CJK），判定为中文文案里误用的内容引号，而非字符串结束。
    """

    def __init__(self, text: str):
        self.s = text
        self.i = 0
        self.n = len(text)

    def peek(self, off: int = 0) -> str:
        j = self.i + off
        return self.s[j] if j < self.n else ""

    def scan_matched_object(self) -> Optional[str]:
        """从当前 '{' 起扫描一个完整的 {...} 文本；未闭合（截断）返回 None。"""
        if self.peek() != "{":
            return None
        start = self.i
        depth = 0
        state = "code"  # code | dq | sq | tpl | line | block
        prev_emit = ""
        while self.i < self.n:
            c = self.peek()
            nxt = self.peek(1)

            if state == "line":
                if c == "\n":
                    state = "code"
                self.i += 1
                continue
            if state == "block":
                if c == "*" and nxt == "/":
                    state = "code"
                    self.i += 2
                    continue
                self.i += 1
                continue
            if state == "tpl":
                if c == "\\":
                    self.i += 2
                    continue
                if c == "`":
                    state = "code"
                self.i += 1
                continue
            if state in ("dq", "sq"):
                if c == "\\":
                    prev_emit = nxt
                    self.i += 2
                    continue
                quote = '"' if state == "dq" else "'"
                if c == quote:
                    # 结构引号：后面（跳过空格/制表符）是语法符号、换行或 EOF；
                    # 否则视为中文文案中误用的内容引号
                    j = self.i + 1
                    while j < self.n and self.s[j] in " \t":
                        j += 1
                    nc = self.s[j] if j < self.n else ""
                    if nc == "" or nc in ",}]:;)\r\n":
                        state = "code"
                        self.i += 1
                        prev_emit = quote
                        continue
                    prev_emit = c
                    self.i += 1
                    continue
                prev_emit = c
                self.i += 1
                continue

            # code
            if c == "/" and nxt == "/":
                state = "line"
                self.i += 2
                continue
            if c == "/" and nxt == "*":
                state = "block"
                self.i += 2
                continue
            if c == '"':
                state = "dq"
                prev_emit = '"'
                self.i += 1
                continue
            if c == "'":
                state = "sq"
                prev_emit = "'"
                self.i += 1
                continue
            if c == "`":
                state = "tpl"
                self.i += 1
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    self.i += 1
                    return self.s[start:self.i]
            prev_emit = c if not c.isspace() else prev_emit
            self.i += 1
        return None  # 截断


def _salvage_logic(html: str) -> dict:
    """从 WF_LOGIC 区域逐个抢救顶层按钮条目，解析成 dict；截断的条目丢弃。"""
    region = _find_logic_region(html)
    if not region:
        return {}
    sc = _Scanner(region)
    # 定位到外层 {
    sc.i = 0
    if sc.peek() != "{":
        return {}
    sc.i += 1

    result: dict = {}
    while True:
        # 跳过空白/逗号
        while sc.i < sc.n and sc.peek() in " \t\r\n,":
            sc.i += 1
        if sc.i >= sc.n or sc.peek() == "}":
            break
        # 读 key（引号字符串，容错）
        key = _scan_key_token(sc)
        if key is None:
            break
        # 跳过到 ':'
        while sc.i < sc.n and sc.peek() != ":":
            sc.i += 1
        if sc.i >= sc.n:
            break  # 截断
        sc.i += 1
        # 跳过空白
        while sc.i < sc.n and sc.peek() in " \t\r\n":
            sc.i += 1
        if sc.peek() != "{":
            # 值不是对象（异常结构），跳过本条目到下一个逗号
            break
        obj_text = sc.scan_matched_object()
        if obj_text is None:
            break  # 截断，后面的条目全部无法抢救
        parsed = _parse_entry_object(obj_text)
        if isinstance(parsed, dict):
            result[key] = parsed
    return result


def _scan_key_token(sc: "_Scanner") -> Optional[str]:
    """读取顶层条目的 key：支持 "xx" / 'xx' / 裸标识符（带 CJK 容错）。"""
    while sc.i < sc.n and sc.peek() in " \t\r\n":
        sc.i += 1
    c = sc.peek()
    if c in ('"', "'"):
        quote = c
        sc.i += 1
        buf = []
        while sc.i < sc.n:
            ch = sc.peek()
            if ch == "\\":
                buf.append(sc.peek(1))
                sc.i += 2
                continue
            if ch == quote:
                j = sc.i + 1
                while j < sc.n and sc.s[j] in " \t":
                    j += 1
                nc = sc.s[j] if j < sc.n else ""
                if nc == "" or nc in ",}]:;)\r\n":
                    sc.i += 1
                    return "".join(buf)
                buf.append(ch)
                sc.i += 1
                continue
            buf.append(ch)
            sc.i += 1
        return None  # 截断
    # 裸 key
    m = re.match(r"[A-Za-z0-9_$]+", sc.s[sc.i:])
    if m:
        sc.i += m.end()
        return m.group(0)
    return None


def _parse_entry_object(text: str) -> Optional[dict]:
    """把单个按钮的 {...} JS 对象文本转成 dict。"""
    json_text = _js_object_to_json(text)
    if json_text is None:
        return None
    try:
        v = json.loads(json_text)
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        return None


def _js_object_to_json(text: str) -> Optional[str]:
    """扫描单个完整对象文本，输出严格 JSON。"""
    out: list[str] = []
    i, n = 0, len(text)
    state = "code"
    prev_emit = ""
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""

        if state == "line":
            if c == "\n":
                state = "code"
                out.append(c)
            i += 1
            continue
        if state == "block":
            if c == "*" and nxt == "/":
                state = "code"
                i += 2
                continue
            i += 1
            continue
        if state in ("dq", "sq", "tpl"):
            if c == "\\":
                out.append(c)
                if i + 1 < n:
                    out.append(text[i + 1])
                i += 2
                continue
            quote = {"dq": '"', "sq": "'", "tpl": "`"}[state]
            if c == quote:
                # 结构引号：向后看（跳过空格/制表符）是否为语法符号/换行/EOF
                j = i + 1
                while j < n and text[j] in " \t":
                    j += 1
                nc2 = text[j] if j < n else ""
                if state == "tpl" or nc2 == "" or nc2 in ",}]:;)\r\n":
                    out.append('"')
                    state = "code"
                    prev_emit = '"'
                    i += 1
                    continue
                # 中文文案中的内容引号 → 转义进 JSON 字符串
                out.append('\\"')
                prev_emit = c
                i += 1
                continue
            out.append(c)
            prev_emit = c
            i += 1
            continue

        # code
        if c == "/" and nxt == "/":
            state = "line"
            i += 2
            continue
        if c == "/" and nxt == "*":
            state = "block"
            i += 2
            continue
        if c in ('"', "'", "`"):
            state = {"\"": "dq", "'": "sq", "`": "tpl"}[c]
            out.append('"')
            prev_emit = '"'
            i += 1
            continue
        # 裸 key：{ 或 , 之后的 ASCII 标识符:
        m = re.match(r"[A-Za-z_$][A-Za-z0-9_$]*", text[i:]) if c.isascii() else None
        if m:
            word = m.group(0)
            j = i + m.end()
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] == ":" and word not in ("true", "false", "null"):
                out.append('"' + word + '":')
                i = j + 1
                prev_emit = ":"
                continue
            out.append(word)
            prev_emit = word[-1]
            i = j
            continue
        if c == ",":
            # 去掉 } / ] 前的尾逗号
            k = i + 1
            while k < n and text[k] in " \t\r\n":
                k += 1
            if k < n and text[k] in "}]":
                i += 1
                continue
        out.append(c)
        if not c.isspace():
            prev_emit = c
        i += 1
    return "".join(out) if state == "code" else None


# ---------- 3. 数据条目规整 ----------

def _coerce_entry(obj: Optional[dict], key: str) -> dict:
    """保证每条数据 8 个字段齐全、类型安全。"""
    if not isinstance(obj, dict):
        obj = {}

    def g(name: str, default: str = _MISSING_NOTE) -> str:
        v = obj.get(name)
        if v is None:
            return default
        return str(v).strip() or default

    form = obj.get("form")
    safe_form: list[dict] = []
    if isinstance(form, list):
        for f in form:
            if isinstance(f, dict):
                safe_form.append({
                    "name": str(f.get("name", "字段")).strip() or "字段",
                    "type": str(f.get("type", "文本")).strip() or "文本",
                    "required": bool(f.get("required", False)),
                    "rule": str(f.get("rule", "")).strip() or "待确认",
                })

    return {
        "action": g("action", f"触发『{_btn_label(key)}』对应的交互动作（详细逻辑缺失，待补充）"),
        "precheck": g("precheck"),
        "form": safe_form,
        "submit": g("submit"),
        "success": g("success"),
        "error": g("error"),
        "state": g("state", "无"),
        "permission": g("permission", "待确认"),
    }


def _btn_label(key: str) -> str:
    return key.replace("btn_", "").replace("_", " ")


# ---------- 4. 清理旧运行时 ----------

_SCRIPT_RE = re.compile(
    r"<script\b[^>]*>(.*?)</script\s*>", re.S | re.IGNORECASE
)


def _strip_old_runtime(html: str) -> str:
    # 4.1 删除含 WF_LOGIC / showLogic 的脚本块（损坏的未闭合脚本用宽松规则）
    def _drop(m: re.Match) -> str:
        body = m.group(1)
        return "" if ("WF_LOGIC" in body or "showLogic" in body) else m.group(0)

    html = _SCRIPT_RE.sub(_drop, html)

    # 4.2 处理截断后“没有 </script> 收尾”的损坏块
    html = re.sub(
        r"<script\b(?![^>]*src=)[^>]*>[^<]*(?:WF_LOGIC|showLogic).*$",
        "",
        html,
        flags=re.S | re.IGNORECASE,
    )

    # 4.3 删除旧弹窗 div（id 含 wf-logic-modal，用 div 配平扫描）
    html = _remove_modal_div(html)
    # 4.4 删除旧的 JSON 数据块（保证重复执行幂等）
    html = re.sub(
        r'<script\b[^>]*id\s*=\s*["\']wf-logic-data["\'][^>]*>.*?</script\s*>',
        "",
        html,
        flags=re.S | re.IGNORECASE,
    )
    return html


def _remove_modal_div(html: str) -> str:
    pattern = re.compile(
        r"<div\b[^>]*id\s*=\s*['\"]wf-logic-modal['\"][^>]*>", re.I
    )
    m = pattern.search(html)
    if not m:
        return html
    start = m.start()
    i = m.end()
    depth = 1
    tag_re = re.compile(r"<(/?)div\b[^>]*?(/?)>", re.I | re.S)
    while i < len(html):
        tm = tag_re.search(html, i)
        if not tm:
            break
        if tm.group(2) == "/":
            pass  # 自闭合，不变
        elif tm.group(1) == "/":
            depth -= 1
        else:
            depth += 1
        i = tm.end()
        if depth == 0:
            return html[:start] + html[i:]
    # 未闭合（截断）：直接丢弃从弹窗开始的所有内容，后面由注入器重建收尾
    return html[:start]


# ---------- 5. 注入统一运行时 ----------

_MODAL_HTML = """
  <!-- 业务逻辑弹窗（统一运行时自动注入） -->
  <div id="wf-logic-modal" class="wf-logic-modal" onclick="if(event.target===this)hideLogic()">
    <div class="wf-logic-card">
      <div class="wf-logic-head">
        <span class="wf-logic-title" id="wf-logic-title">业务逻辑</span>
        <span class="wf-close" onclick="hideLogic()">&times;</span>
      </div>
      <div id="wf-logic-body"></div>
    </div>
  </div>
"""

_RUNTIME_JS = r"""
(function(){
  var DATA = JSON.parse(document.getElementById('wf-logic-data').textContent);
  function esc(s){ return String(s==null?'':s).replace(/[&<>"]/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }
  function section(label, val){
    return '<div class="wf-logic-section"><div class="wf-logic-label">'+esc(label)+
           '</div><div class="wf-logic-value">'+esc(val)+'</div></div>';
  }
  window.showLogic = function(key){
    var d = DATA[key];
    if(!d){ alert('该按钮的业务逻辑说明缺失：' + key + '\\n（请重新生成本页面补全）'); return; }
    document.getElementById('wf-logic-title').textContent = '业务逻辑：' + key.replace(/^btn_/, '');
    var h = section('触发动作', d.action) + section('前置校验', d.precheck);
    if(d.form && d.form.length){
      h += '<div class="wf-logic-section"><div class="wf-logic-label">表单字段</div>';
      h += '<table class="wf-logic-table"><tr><th>字段</th><th>类型</th><th>必填</th><th>校验规则</th></tr>';
      d.form.forEach(function(f){
        h += '<tr><td>'+esc(f.name)+'</td><td>'+esc(f.type)+'</td><td>'+(f.required?'是':'否')+
             '</td><td>'+esc(f.rule)+'</td></tr>';
      });
      h += '</table></div>';
    }
    h += section('提交动作', d.submit) + section('成功分支', d.success) +
         section('失败分支', d.error) + section('状态流转', d.state) +
         section('权限要求', d.permission);
    document.getElementById('wf-logic-body').innerHTML = h;
    document.getElementById('wf-logic-modal').style.display = 'flex';
  };
  window.hideLogic = function(){
    document.getElementById('wf-logic-modal').style.display = 'none';
  };
  document.addEventListener('keydown', function(e){ if(e.key === 'Escape') hideLogic(); });
})();
"""

_RUNTIME_CSS = """
.wf-logic-modal{display:none;position:fixed;top:0;left:0;width:100%;height:100%;
 background:rgba(0,0,0,.4);z-index:99999;align-items:center;justify-content:center;}
.wf-logic-card{background:#fff;border-radius:8px;box-shadow:0 8px 24px rgba(0,0,0,.15);
 max-width:600px;width:90%;max-height:80vh;overflow:auto;}
.wf-logic-head{display:flex;justify-content:space-between;align-items:center;padding:16px 20px;
 border-bottom:1px solid #f0f0f0;position:sticky;top:0;background:#fff;z-index:1;}
.wf-logic-title{font-weight:bold;color:#1890ff;font-size:15px;}
.wf-close{cursor:pointer;font-size:22px;color:#999;line-height:1;}
.wf-close:hover{color:#333;}
.wf-logic-section{padding:10px 20px;border-bottom:1px solid #f5f5f5;}
.wf-logic-label{font-size:12px;color:#999;margin-bottom:4px;}
.wf-logic-value{font-size:13px;color:#333;line-height:1.7;word-break:break-word;white-space:pre-wrap;}
.wf-logic-table{width:100%;border-collapse:collapse;margin-top:6px;}
.wf-logic-table th{background:#fafafa;border:1px solid #e8e8e8;padding:6px 10px;text-align:left;
 font-size:12px;color:#666;}
.wf-logic-table td{border:1px solid #e8e8e8;padding:6px 10px;font-size:12px;color:#333;}
"""


def _inject_runtime(html: str, page_name: str, data: dict) -> str:
    # 数据块：JSON 天然合法；防止数据里出现 </script> 闭合序列
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    block = (
        _MODAL_HTML
        + f'<script type="application/json" id="wf-logic-data">{payload}</script>\n'
        + f"<script>{_RUNTIME_JS}</script>\n"
    )

    has_style = re.search(r"<style\b", html, re.I)
    css_tag = f"<style>{_RUNTIME_CSS}</style>\n"

    # 截断收尾：若没有 </body>，直接补齐
    if "</body" not in html.lower():
        prefix = html.rstrip()
        if not has_style:
            prefix += "\n" + css_tag
        else:
            # 样式存在但可能未闭合；CSS 追加到 head 更稳妥，这里直接前置注入样式
            prefix += css_tag
        if "</html" not in html.lower():
            return prefix + block + "</body></html>\n"
        return prefix + block + "</body>\n"

    # 正常页面：CSS 放 head 末尾，运行时放 body 末尾
    if not has_style:
        html = re.sub(r"(</head>)", css_tag + r"\1", html, count=1,
                      flags=re.I)
    return re.sub(r"(</body>)", block + r"\1", html, count=1, flags=re.I)
