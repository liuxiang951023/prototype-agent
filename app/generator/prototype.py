"""原型项目生成器。

把 Agent 收集到的每个页面 HTML（已是完整 HTML 文件）组装成
可访问的原型项目：
  output/<session_id>/
    ├── index.html        # 顶栏 + 左侧深色菜单 + iframe 内容区
    ├── pages/
    │   ├── customer_profile.html
    │   ├── contract_list.html
    │   └── ...
    └── pages_spec.json   # 页面清单（便于排查）
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

INDEX_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{project_name} · 高保真原型</title>
  <style>
    * {{ margin:0; padding:0; box-sizing:border-box; }}
    html, body {{ height:100%; font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif; }}
    body {{ display:flex; flex-direction:column; background:#f0f2f5; color:#333; }}
    .header {{
      height:56px; background:#fff; border-bottom:1px solid #e8e8e8;
      display:flex; align-items:center; justify-content:space-between;
      padding:0 20px; flex-shrink:0;
    }}
    .header .logo {{ display:flex; align-items:center; gap:10px; font-size:16px; font-weight:600; }}
    .header .logo .dot {{ width:10px; height:10px; border-radius:2px; background:#1890ff; }}
    .header .sub {{ font-size:12px; color:#999; font-weight:normal; margin-left:10px; }}
    .body {{ display:flex; flex:1; overflow:hidden; }}
    .sidebar {{ width:220px; background:#001529; color:rgba(255,255,255,.65);
      overflow-y:auto; padding:12px 0; flex-shrink:0; }}
    .sidebar .nav-item {{
      padding:12px 24px; cursor:pointer; font-size:14px;
      border-left:3px solid transparent; transition:all .2s;
    }}
    .sidebar .nav-item:hover {{ color:#fff; background:rgba(255,255,255,.08); }}
    .sidebar .nav-item.active {{ color:#fff; background:#1890ff; border-left-color:#fff; }}
    .content {{ flex:1; overflow:hidden; }}
    .content iframe {{ width:100%; height:100%; border:none; background:#f0f2f5; }}
  </style>
</head>
<body>
  <div class="header">
    <div class="logo"><span class="dot"></span>{project_name}<span class="sub">AI 生成 · 高保真可交互原型</span></div>
    <div class="sub">共 {page_count} 个页面 · 点击左侧菜单切换</div>
  </div>
  <div class="body">
    <div class="sidebar" id="sidebar"></div>
    <div class="content">
      <iframe id="frame" src=""></iframe>
    </div>
  </div>
  <script>
    var pages = {pages_json};
    var sidebar = document.getElementById('sidebar');
    var frame = document.getElementById('frame');
    pages.forEach(function(p, i) {{
      var item = document.createElement('div');
      item.className = 'nav-item' + (i === 0 ? ' active' : '');
      item.textContent = p.name;
      item.onclick = function() {{
        document.querySelectorAll('.nav-item').forEach(function(el) {{ el.classList.remove('active'); }});
        item.classList.add('active');
        frame.src = 'pages/' + p.key + '.html';
      }};
      if (i === 0) frame.src = 'pages/' + p.key + '.html';
      sidebar.appendChild(item);
    }});
  </script>
</body>
</html>
"""


def render_page_html(page_name: str, page_description: str) -> str:
    """兜底：当 LLM 没生成时，用与高保真风格一致的占位页。"""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <title>{page_name}</title>
  <style>
    * {{ margin:0; padding:0; box-sizing:border-box; }}
    body {{ font-family:-apple-system,"PingFang SC",sans-serif; background:#f0f2f5; color:#333; font-size:14px; }}
    .ui-page {{ padding:20px 24px; }}
    .ui-breadcrumb {{ font-size:13px; color:#999; margin-bottom:6px; }}
    .ui-title {{ font-size:20px; font-weight:600; margin-bottom:16px; }}
    .ui-card {{ background:#fff; border-radius:8px; box-shadow:0 1px 4px rgba(0,21,41,.08); padding:20px; }}
    .ui-tag-orange {{ display:inline-block; padding:1px 8px; border-radius:3px; font-size:12px;
      background:#fff7e6; color:#fa8c16; border:1px solid #ffd591; }}
    .ui-desc {{ color:#666; line-height:1.8; }}
  </style>
</head>
<body>
  <div class="ui-page">
    <div class="ui-breadcrumb">首页 / {page_name}</div>
    <div class="ui-title">{page_name}</div>
    <div class="ui-card">
      <p><span class="ui-tag-orange">待生成</span></p>
      <p class="ui-desc" style="margin-top:12px">{page_description}</p>
      <p class="ui-desc" style="margin-top:12px;color:#999">该页面信息不足，AI 未生成完整原型，请补充需求后重新生成。</p>
    </div>
  </div>
</body>
</html>
"""


def extract_json_from_llm_output(text: str) -> str:
    """从 LLM 输出里抠出 JSON 字符串。

    LLM 可能输出 ```json ... ``` 包裹，或前后带解释文字。
    这里返回最外层 JSON 字符串。
    """
    text = text.strip()
    # 去掉 markdown 代码块
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*\n", "", text)
        text = re.sub(r"\n```\s*$", "", text)
    # 找第一个 { 到最后一个 } 之间的内容
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    return text


def _strip_code_fence(text: str) -> str:
    """去掉 ```html ... ``` 包裹。"""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:html)?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def _ensure_html_skeleton(html: str, page_name: str) -> str:
    """若 LLM 漏了 doctype，补全（不引入任何 CDN 依赖）。"""
    html = _strip_code_fence(html)
    if not html.lstrip().lower().startswith("<!doctype"):
        html = (
            '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n'
            '  <meta charset="UTF-8">\n'
            f'  <title>{page_name}</title>\n'
            '</head>\n<body>\n' + html + "\n</body>\n</html>"
        )
    return html


def assemble_project(
    pages: list[dict[str, str]], output_dir: Path
) -> str:
    """组装完整原型项目，返回入口路径。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    pages_dir = output_dir / "pages"
    pages_dir.mkdir(exist_ok=True)

    # 1. 写入每个页面
    nav_pages = []
    for p in pages:
        key = p["key"]
        name = p.get("name", key)
        html = _ensure_html_skeleton(p["html"], name)
        (pages_dir / f"{key}.html").write_text(html, encoding="utf-8")
        nav_pages.append({"key": key, "name": name})

    # 2. 生成入口 index.html
    project_name = output_dir.name
    index_html = INDEX_TEMPLATE.format(
        project_name=project_name,
        pages_json=json.dumps(nav_pages, ensure_ascii=False),
        page_count=len(nav_pages),
    )
    index_path = output_dir / "index.html"
    index_path.write_text(index_html, encoding="utf-8")

    return str(index_path)
