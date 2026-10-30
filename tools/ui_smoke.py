"""微信待办 · 前端冒烟自检（Playwright，只读）。

用途：改动 web/ 之后快速确认主流程没坏。默认**不写任何数据**，可以放心跑在真实实例上。

用法（在项目根目录）：
    python tools/ui_smoke.py                 # 默认 http://127.0.0.1:<.env 的 PORT>
    python tools/ui_smoke.py --base http://127.0.0.1:8799
    python tools/ui_smoke.py --browser firefox
    python tools/ui_smoke.py --screenshots out/   # 额外保存截图

依赖：playwright（`pip install playwright && python -m playwright install chromium`）。
退出码：0 = 全部通过；1 = 有失败项。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# 开发态直接 `python tools\ui_smoke.py` 时，把项目根放进 sys.path
_DEV_ROOT = Path(__file__).resolve().parent.parent
if str(_DEV_ROOT) not in sys.path:
    sys.path.insert(0, str(_DEV_ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402

from app.paths import APP_DIR, ENV_FILE  # noqa: E402

ROOT = APP_DIR

COLOR_JS = r"""
() => {
  const isGray = (s) => {
    const m = s && s.match(/rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)/);
    if (!m) return true;
    const r = +m[1], g = +m[2], b = +m[3];
    if (/,\s*0\)$/.test(s)) return true;
    return Math.max(r, g, b) - Math.min(r, g, b) <= 10;
  };
  const hits = [];
  const skip = new Set(['IMG', 'svg', 'path', 'circle', 'rect', 'line', 'g']);
  for (const el of document.querySelectorAll('body *')) {
    if (skip.has(el.tagName) || el.tagName.startsWith('SVG')) continue;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || +cs.opacity === 0) continue;
    for (const v of [cs.color, cs.backgroundColor, cs.borderTopColor, cs.outlineColor]) {
      if (!v || v === 'transparent') continue;
      if (!isGray(v)) hits.push(el.tagName + ' ' + v);
    }
  }
  return hits.slice(0, 5);
}
"""


def read_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*([A-Z0-9_]+)\s*=\s*(.*)$", line)
            if m:
                env[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return env


class Checker:
    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.results.append((name, bool(ok), detail))
        print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""))
        return bool(ok)

    @property
    def failed(self) -> list[str]:
        return [f"{n}: {d}" for n, ok, d in self.results if not ok]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=None, help="服务地址，默认读 .env 的 PORT")
    ap.add_argument("--token", default=None, help="访问令牌，默认读 .env 的 APP_TOKEN")
    ap.add_argument("--browser", default="chromium", choices=["chromium", "firefox", "webkit"])
    ap.add_argument("--screenshots", default=None, help="截图输出目录")
    args = ap.parse_args()

    env = read_env()
    base = args.base or f"http://127.0.0.1:{env.get('PORT', '8765')}"
    token = args.token or env.get("APP_TOKEN", "")
    base = base.rstrip("/")
    app_url = f"{base}/?token={token}"
    shot_dir = Path(args.screenshots) if args.screenshots else None
    if shot_dir:
        shot_dir.mkdir(parents=True, exist_ok=True)

    c = Checker()

    with sync_playwright() as p:
        browser_type = getattr(p, args.browser)
        try:
            browser = browser_type.launch(args=["--disable-lcd-text"] if args.browser == "chromium" else [])
        except Exception as exc:  # noqa: BLE001
            print(f"无法启动 {args.browser}：{exc}")
            print("提示：python -m playwright install " + args.browser)
            return 1

        # ---------- 桌面 ----------
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, service_workers="block")
        page = ctx.new_page()
        console_errors: list[str] = []
        page_errors: list[str] = []
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: page_errors.append(str(e)))
        page.goto(app_url, wait_until="networkidle")
        page.wait_for_timeout(1200)

        c.check("桌面：无 JS 异常", not page_errors, "; ".join(page_errors[:2]))
        c.check("桌面：无 console error", not console_errors, "; ".join(console_errors[:2]))

        layout = page.evaluate(
            """() => {
              const box = (sel) => { const el = document.querySelector(sel);
                if (!el) return {visible: false, w: 0};
                const r = el.getBoundingClientRect(); return {visible: r.width > 0, w: Math.round(r.width)}; };
              return {rail: box('#rail'), detail: box('#detailPanel'), tabs: box('#tabs'),
                      fab: box('#addBtn'), scrollW: document.documentElement.scrollWidth, innerW: innerWidth};
            }"""
        )
        c.check("桌面：三栏导航/详情可见", layout["rail"]["visible"] and layout["detail"]["visible"])
        c.check("桌面：移动 tabs/FAB 已隐藏", not layout["tabs"]["visible"] and not layout["fab"]["visible"])
        c.check("桌面：无横向滚动", layout["scrollW"] <= layout["innerW"])
        c.check("桌面：零彩色", not page.evaluate(COLOR_JS))

        has_tasks = page.locator("#taskList .task").count() > 0
        c.check("桌面：列表有任务或空态", has_tasks or page.locator("#stateView:not([hidden])").count() > 0,
                f"tasks={page.locator('#taskList .task').count()}")
        c.check("桌面：分组表头存在", page.locator("#taskList .group h2").count() > 0 or not has_tasks)

        if has_tasks:
            page.keyboard.press("j")
            page.wait_for_timeout(250)
            c.check("键盘：j 选中任务", page.locator("#taskList .task.is-selected").count() == 1)
            page.keyboard.press("Enter")
            page.wait_for_timeout(400)
            c.check("键盘：Enter 打开详情", page.locator("#detailBody .detail-read").count() == 1)
            page.keyboard.press("e")
            page.wait_for_timeout(400)
            c.check("键盘：e 进入编辑", page.locator("#detailBody .edit-form").count() == 1)
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
            c.check("键盘：Esc 取消编辑", page.locator("#detailState").inner_text().strip() in ("只读", "readonly"))
            page.locator("#taskList .task .task-body").first.click()
            page.wait_for_timeout(400)

        # 搜索空态
        page.fill("#searchInput", "zzz-不存在的任务-zzz")
        page.wait_for_timeout(600)
        c.check("搜索：空结果提示", page.locator(".empty-result").count() == 1)
        page.press("#searchInput", "Escape")
        page.wait_for_timeout(400)

        if shot_dir:
            page.screenshot(path=str(shot_dir / f"smoke-1440-{args.browser}.png"))
        ctx.close()

        # ---------- 窄屏 ----------
        # Firefox 不支持 is_mobile / has_touch，用视口模拟即可
        mobile_opts = {} if args.browser == "firefox" else {"is_mobile": True, "has_touch": True}
        ctx = browser.new_context(viewport={"width": 390, "height": 844},
                                  service_workers="block", **mobile_opts)
        page = ctx.new_page()
        mobile_errors: list[str] = []
        page.on("pageerror", lambda e: mobile_errors.append(str(e)))
        page.goto(app_url, wait_until="networkidle")
        page.wait_for_timeout(1000)
        m = page.evaluate(
            """() => {
              const box = (sel) => { const el = document.querySelector(sel);
                if (!el) return {visible: false, display: null};
                return {visible: el.getBoundingClientRect().width > 0, display: getComputedStyle(el).display}; };
              return {tabs: box('#tabs'), rail: box('#rail'), detail: box('#detailPanel'),
                      fab: box('#addBtn'), scrollW: document.documentElement.scrollWidth, innerW: innerWidth};
            }"""
        )
        c.check("移动：tabs/FAB 可见", m["tabs"]["visible"] and m["fab"]["visible"])
        c.check("移动：桌面导航/详情隐藏", not m["rail"]["visible"] and not m["detail"]["visible"])
        c.check("移动：无横向滚动", m["scrollW"] <= m["innerW"])
        c.check("移动：无 JS 异常", not mobile_errors, "; ".join(mobile_errors[:2]))
        if page.locator("#taskList .task-card").count():
            page.locator("#taskList .task-card .task-body").first.click()
            page.wait_for_timeout(600)
            c.check("移动：点卡片打开抽屉", page.evaluate(
                "() => {const s=document.querySelector('#detailSheet'); return s ? !s.hidden : false;}"))
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
        if shot_dir:
            page.screenshot(path=str(shot_dir / f"smoke-390-{args.browser}.png"))
        ctx.close()

        # ---------- 设置页 ----------
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, service_workers="block")
        page = ctx.new_page()
        s_errors: list[str] = []
        page.on("pageerror", lambda e: s_errors.append(str(e)))
        page.goto(f"{base}/settings.html?token={token}", wait_until="networkidle")
        page.wait_for_timeout(900)
        c.check("设置页：无 JS 异常", not s_errors, "; ".join(s_errors[:2]))
        c.check("设置页：无横向滚动", page.evaluate(
            "() => document.documentElement.scrollWidth <= innerWidth"))
        c.check("设置页：分区锚点", page.locator("a[href^='#']").count() >= 4)
        c.check("设置页：零彩色", not page.evaluate(COLOR_JS))
        ctx.close()

        browser.close()

    print()
    if c.failed:
        print(f"失败 {len(c.failed)} 项：")
        for item in c.failed:
            print(" -", item)
        return 1
    print(f"全部通过（{len(c.results)} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
