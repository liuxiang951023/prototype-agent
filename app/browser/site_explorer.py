"""站点自动浏览器。

工作流程（全部在 pipeline 工作线程内，用 Playwright 同步 API）：
1. 打开用户给定的系统地址
2. 若提供了账号密码且页面存在密码框，启发式定位用户名/密码输入框并登录
3. 采集首页的导航菜单（nav/aside/.sidebar/.menu 内同域链接）
4. 逐个进入菜单页面：提取页面结构（标题/标题栏/按钮/表头/表单标签/链接/正文），
   并截图存档，通过 on_event 实时推送 browser 事件
5. 返回所有页面的结构化摘要（同时落盘 site_scan.json）

注意：账号密码仅在内存中使用，不写入任何日志或落盘文件。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

# 单页 DOM 摘要最长字符（控制送给 LLM 的体量）
_BODY_TEXT_LIMIT = 1200
# 最多遍历的菜单页面数（含首页），避免失控
_MAX_PAGES = 12
_NAV_TIMEOUT_MS = 25_000

# 登录按钮候选文案
_LOGIN_TEXTS = ["登录", "登 录", "登陆", "Login", "Log in", "Sign in", "Sign In"]
# 导航收集中排除的链接文案/路径
_EXCLUDE_WORDS = [
    "退出", "注销", "登出", "logout", "sign out",
    "头像", "消息通知", "帮助", "文档", "官网",
]


def explore_site(
    url: str,
    username: str,
    password: str,
    output_dir: Path,
    shot_base_url: str,
    on_event: Callable[[dict], None],
    max_pages: int = _MAX_PAGES,
) -> list[dict[str, Any]]:
    """浏览目标站点并采集功能信息。

    返回页面摘要列表，每项形如：
      {"index", "title", "url", "shot_url", "headings", "buttons",
       "table_headers", "labels", "nav_links", "body_text"}
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "未安装 playwright，请先执行：\n"
            "  .venv/bin/pip install playwright\n"
            "  .venv/bin/python -m playwright install chromium"
        ) from e

    url = (url or "").strip()
    if not url:
        raise ValueError("站点地址为空")
    if not url.startswith(("http://", "https://")):
        url = "http://" + url

    shot_dir = output_dir / "screenshots"
    shot_dir.mkdir(parents=True, exist_ok=True)

    pages_info: list[dict[str, Any]] = []

    on_event({"type": "browser", "action": "start", "url": url,
              "msg": f"正在打开站点：{url}"})

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
            ignore_https_errors=True,
        )
        page = context.new_page()
        page.set_default_timeout(_NAV_TIMEOUT_MS)

        # ---------- 1. 打开首页 ----------
        page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
        _settle(page)

        # ---------- 2. 尝试登录 ----------
        _try_login(page, username, password, on_event)

        # ---------- 3. 首页本身先采一次 ----------
        home_summary = _collect_summary(page)
        home_summary["index"] = 0
        home_summary["url"] = page.url
        shot_path = shot_dir / "page_0_home.png"
        page.screenshot(path=str(shot_path))
        home_summary["shot"] = f"{shot_base_url}/screenshots/page_0_home.png"
        pages_info.append(home_summary)
        on_event({
            "type": "browser", "action": "page", "index": 0,
            "title": home_summary.get("title") or page.url,
            "url": page.url,
            "shot": home_summary["shot"],
            "msg": f"已采集首页：{home_summary.get('title') or page.url}",
        })

        # ---------- 4. 收集导航菜单并逐页采集 ----------
        origin = urlparse(page.url).netloc
        candidates = _collect_nav_candidates(page, origin)
        on_event({
            "type": "browser", "action": "nav",
            "msg": f"发现 {len(candidates)} 个候选菜单，最多深入 {max_pages - 1} 个页面",
        })

        # URL 规范化：去掉 hash 与末尾斜杠，避免 home.html 与 home.html# 重复采集
        visited: set[str] = {_norm_url(page.url)}
        idx = 0
        for cand in candidates:
            if idx >= max_pages - 1:
                break
            href = cand.get("href") or ""
            text = (cand.get("text") or "").strip()
            if not text or not href:
                continue
            norm = _norm_url(href)
            if not norm or norm in visited:
                continue
            visited.add(norm)

            ok = _open_menu_page(page, cand, origin)
            if not ok:
                on_event({
                    "type": "browser", "action": "skip",
                    "msg": f"菜单「{text}」无法打开，已跳过",
                })
                continue
            idx += 1

            summary = _collect_summary(page)
            summary["index"] = idx
            summary["url"] = page.url
            shot_name = f"page_{idx}.png"
            page.screenshot(path=str(shot_dir / shot_name))
            summary["shot"] = f"{shot_base_url}/screenshots/{shot_name}"
            pages_info.append(summary)

            on_event({
                "type": "browser", "action": "page", "index": idx,
                "title": summary.get("title") or text,
                "url": page.url,
                "shot": summary["shot"],
                "msg": f"已采集页面（{idx + 1}/{max_pages}）：{summary.get('title') or text}",
            })

        context.close()
        browser.close()

    # 落盘原始采集结果，便于排查
    (output_dir / "site_scan.json").write_text(
        json.dumps(pages_info, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    on_event({
        "type": "browser", "action": "done",
        "count": len(pages_info),
        "msg": f"站点浏览完成，共采集 {len(pages_info)} 个页面",
    })
    return pages_info


def _norm_url(href: str) -> str:
    """规范化 URL 用于去重：去掉 fragment 与末尾斜杠。纯 hash 链接返回空串。"""
    base = href.split("#", 1)[0].rstrip("/")
    return base


# ---------- 登录 ----------

def _try_login(page, username: str, password: str,
               on_event: Callable[[dict], None]) -> None:
    """启发式登录。失败不抛异常（可能系统本身不需要登录）。"""
    try:
        pwd = page.locator("input[type='password']").first
        if pwd.count() == 0:
            on_event({"type": "browser", "action": "login",
                      "msg": "未发现登录表单，直接以访客身份浏览"})
            return

        if not (username and password):
            on_event({
                "type": "browser", "action": "login",
                "msg": "页面需要登录但未提供账号密码，仅能采集登录页（"
                       "可在上方补充账号密码后重新生成）",
            })
            return

        # 用户名框：password 之前最近的可见文本类输入框
        user_selectors = [
            "input[name*='user' i]", "input[name*='account' i]",
            "input[name*='name' i]", "input[name*='mail' i]",
            "input[name*='phone' i]", "input[type='text']:visible",
            "input[type='email']:visible", "input[type='tel']:visible",
            "input:not([type]):visible",
        ]
        user_box = None
        for sel in user_selectors:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 5)):
                item = loc.nth(i)
                try:
                    if item.is_visible():
                        user_box = item
                        break
                except Exception:
                    continue
            if user_box is not None:
                break

        if user_box is None:
            on_event({"type": "browser", "action": "login_failed",
                      "msg": "找到密码框但无法定位账号输入框，跳过自动登录"})
            return

        on_event({"type": "browser", "action": "login",
                  "msg": "正在使用提供的账号登录系统…"})
        user_box.fill(username)
        pwd.fill(password)

        clicked = False
        for txt in _LOGIN_TEXTS:
            btn = page.locator(
                f"button:has-text('{txt}'), a:has-text('{txt}'), "
                f"[role='button']:has-text('{txt}'), "
                f"input[type='submit'][value*='{txt}']"
            ).first
            try:
                if btn.count() > 0 and btn.is_visible():
                    btn.click()
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            # 兜底：在密码框上回车提交
            pwd.press("Enter")

        _settle(page, timeout_ms=12_000)

        # 登录后若密码框仍可见，视为登录失败
        if page.locator("input[type='password']").count() > 0:
            try:
                still = page.locator("input[type='password']").first.is_visible()
            except Exception:
                still = False
            if still:
                on_event({
                    "type": "browser", "action": "login_failed",
                    "msg": "自动登录后仍停留在登录页（可能需要验证码或账号密码有误），"
                           "将继续采集当前可见页面",
                })
                return

        on_event({"type": "browser", "action": "login",
                  "msg": "登录成功，开始遍历系统菜单"})
    except Exception as e:
        on_event({"type": "browser", "action": "login_failed",
                  "msg": f"自动登录过程出现异常：{e}，将继续采集当前可见页面"})


# ---------- 导航收集与页面打开 ----------

def _collect_nav_candidates(page, origin: str) -> list[dict[str, str]]:
    """从首页 DOM 收集同域导航菜单链接，去重、过滤、保序。"""
    try:
        raw = page.evaluate(
            """() => {
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 0 && r.height > 0
                  && s.display !== 'none' && s.visibility !== 'hidden';
              };
              const roots = document.querySelectorAll(
                'nav a, aside a, .sidebar a, .side-bar a, .menu a, .ant-menu a, '
                + '.el-menu a, .nav a, .navbar a, header a, [class*=sidebar i] a, '
                + '[class*=menu i] a'
              );
              const out = [];
              roots.forEach(a => {
                if (!visible(a)) return;
                const t = (a.innerText || a.textContent || '').replace(/\\s+/g, '').trim();
                const h = a.href || '';
                if (t && h && h.startsWith('http')) out.push({text: t, href: h});
              });
              return out;
            }"""
        )
    except Exception:
        return []

    seen: set[str] = set()
    result: list[dict[str, str]] = []
    for item in raw:
        text = (item.get("text") or "").strip()
        href = (item.get("href") or "").strip()
        if not text or not href:
            continue
        if len(text) > 12 or len(text) < 2:
            continue
        if any(w.lower() in text.lower() or w.lower() in href.lower()
               for w in _EXCLUDE_WORDS):
            continue
        # 同域（含子域），或相对路径被浏览器补全后同域
        try:
            if urlparse(href).netloc != origin and origin not in href:
                continue
        except Exception:
            continue
        if href in seen:
            continue
        seen.add(href)
        result.append({"text": text, "href": href})
    return result


def _open_menu_page(page, cand: dict[str, str], origin: str) -> bool:
    """优先点击菜单元素（SPA 友好），失败则直接跳转 href。"""
    text = cand["text"]
    href = cand["href"]

    # 1. 尝试在菜单区域内点击匹配文本的链接
    try:
        link = page.locator(
            "nav a, aside a, .sidebar a, .side-bar a, .menu a, "
            ".ant-menu a, .el-menu a, .nav a, [class*=sidebar i] a, "
            f"[class*=menu i] a"
        ).filter(has_text=text).first
        if link.count() > 0:
            link.click(timeout=6_000)
            _settle(page, timeout_ms=10_000)
            return True
    except Exception:
        pass

    # 2. 直接跳转
    try:
        page.goto(href, wait_until="domcontentloaded", timeout=12_000)
        _settle(page, timeout_ms=8_000)
        return True
    except Exception:
        return False


# ---------- 页面内容采集 ----------

def _collect_summary(page) -> dict[str, Any]:
    """在页面上下文里提取结构化摘要。"""
    try:
        data = page.evaluate(
            """(bodyLimit) => {
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 0 && r.height > 0
                  && s.display !== 'none' && s.visibility !== 'hidden';
              };
              const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
              const uniq = (arr) => [...new Set(arr.filter(Boolean))];

              const title = document.title || '';
              const headings = uniq([...document.querySelectorAll('h1,h2,h3')]
                .filter(visible).map(e => clean(e.innerText)).filter(t => t.length <= 30))
                .slice(0, 12);
              const buttons = uniq([...document.querySelectorAll(
                  'button, [role=button], .btn, a.btn, [class*=btn i]'
                )].filter(visible).map(e => clean(e.innerText))
                .filter(t => t && t.length <= 12)).slice(0, 30);
              const tableHeaders = uniq([...document.querySelectorAll('th')]
                .filter(visible).map(e => clean(e.innerText))).slice(0, 30);
              const labels = uniq([...document.querySelectorAll('label, .form-label, [class*=label i]')]
                .filter(visible).map(e => clean(e.innerText))
                .filter(t => t && t.length <= 16)).slice(0, 30);
              const navLinks = uniq([...document.querySelectorAll(
                  'nav a, aside a, [class*=sidebar i] a, [class*=menu i] a'
                )].filter(visible).map(e => clean(e.innerText))
                .filter(t => t && t.length <= 12)).slice(0, 30);
              const bodyText = (document.body.innerText || '')
                .replace(/[ \\t]+/g, ' ').replace(/\\n{3,}/g, '\\n\\n')
                .trim().slice(0, bodyLimit);
              return {title, headings, buttons, tableHeaders, labels, navLinks, bodyText};
            }""",
            _BODY_TEXT_LIMIT,
        )
    except Exception as e:
        data = {
            "title": "", "headings": [], "buttons": [], "tableHeaders": [],
            "labels": [], "navLinks": [], "bodyText": f"（页面采集失败：{e}）",
        }
    return data


def _settle(page, timeout_ms: int = 8_000) -> None:
    """等待网络空闲；超时则降级为固定短等。"""
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        time.sleep(0.8)
