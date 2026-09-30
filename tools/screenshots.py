"""Capture README screenshots from the demo server (tools/demo_server.py must be running).

    uv run --with playwright python tools/screenshots.py [--chrome /path/to/chrome] [--base http://localhost:9911]
"""

import argparse
import glob
import os
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parent.parent / "docs/images"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:9911")
    ap.add_argument("--chrome", default=next(iter(glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome"))), None))
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    base = args.base
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=args.chrome) if args.chrome else p.chromium.launch()

        def page(scheme="light", w=1440, h=900):
            ctx = b.new_context(viewport={"width": w, "height": h}, color_scheme=scheme, device_scale_factor=2)
            pg = ctx.new_page()
            return ctx, pg

        def login(pg):
            pg.goto(f"{base}/login")
            pg.fill("input[autocomplete=username]", "demo")
            pg.fill("input[type=password]", "demo-password-123")
            pg.click("button[type=submit]")
            pg.wait_for_url("**/rack")
            pg.wait_for_selector("article")
            pg.wait_for_timeout(1200)

        # landing (public)
        ctx, pg = page()
        pg.goto(base)
        pg.wait_for_timeout(1400)
        pg.screenshot(path=OUT / "landing.png")
        ctx.close()

        # rack, light, one strip hovered
        ctx, pg = page()
        login(pg)
        pg.locator("article", has_text="chat-ui").hover()
        pg.wait_for_timeout(300)
        pg.screenshot(path=OUT / "rack.png")
        # app panel: logs
        pg.locator("article", has_text="chat-ui").locator("button[aria-label=Logs]").click()
        pg.wait_for_timeout(1500)
        pg.screenshot(path=OUT / "app-logs.png")
        pg.keyboard.press("Escape")
        # system with a thread selected
        pg.goto(f"{base}/system")
        pg.wait_for_selector("button[aria-label^='CPU']")
        pg.wait_for_timeout(1200)
        pg.locator("button[aria-label^='CPU 3:']").click()
        pg.wait_for_selector("text=Running on cpu3")
        pg.wait_for_timeout(1500)
        pg.screenshot(path=OUT / "system.png", full_page=True)
        # new hosting with a live preview
        pg.goto(f"{base}/new")
        pg.fill("input[placeholder='/home/…/my-project']", "/home/deploy/projects/notes")
        pg.wait_for_timeout(600)
        cmd = pg.locator("input[placeholder='python app.py --port $PORT']")
        cmd.fill("uvicorn app:app --host 127.0.0.1 --port $PORT")
        pg.fill("input[placeholder='app.example.com']", "notes.example.com")
        pg.locator("input[placeholder='app.example.com']").blur()
        pg.wait_for_timeout(1800)
        pg.evaluate("window.scrollTo(0, 0)")
        pg.wait_for_timeout(300)
        pg.screenshot(path=OUT / "new-hosting.png")
        ctx.close()

        # rack, dark
        ctx, pg = page("dark")
        login(pg)
        pg.screenshot(path=OUT / "rack-dark.png")
        ctx.close()

        # phone
        ctx, pg = page("light", 390, 844)
        login(pg)
        pg.screenshot(path=OUT / "mobile.png")
        ctx.close()
        b.close()
    for f in sorted(OUT.glob("*.png")):
        print(f"{f.name:18} {f.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
