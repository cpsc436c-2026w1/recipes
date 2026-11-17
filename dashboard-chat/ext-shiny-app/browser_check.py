"""Open the app in a headless browser, ask one question of the stub, and say what the page shows.

  uv run python ext-shiny-app/browser_check.py                      prints the two boxes before and after
  uv run python ext-shiny-app/browser_check.py --screenshot app.png  and saves a picture of the page

It starts the app itself, with APP_MODEL=stub, on a free port of 127.0.0.1, and stops it at the end. No key, no AWS
account, no connection to another machine. It needs Chromium for Playwright; if you do not have it:
`uv run playwright install chromium` (about 150 MB, once).
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

RECIPE = Path(__file__).resolve().parent.parent
QUESTION = "Show the cake orders"


def browser_available() -> bool:
    """True when Playwright can start its Chromium on this machine."""
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:
        return False


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_app_on_free_port(env: dict | None = None, tries: int = 3) -> tuple[subprocess.Popen, int]:
    """start_app on a free port. Another process can take the port between free_port() and the app's bind (seen in
    CI as "address already in use"), so a start that fails that way is tried again on a new port."""
    for attempt in range(tries):
        port = free_port()
        try:
            return start_app(port, env), port
        except RuntimeError as err:
            if "address already in use" not in str(err).lower() or attempt == tries - 1:
                raise
    raise AssertionError("unreachable")


def start_app(port: int, env: dict | None = None) -> subprocess.Popen:
    """The app as `shiny run` starts it, with the stub in place of the gateway, on 127.0.0.1 only."""
    full = {**os.environ, "APP_MODEL": "stub", **(env or {})}
    for name in ("GATEWAY_URL", "COURSE_MCP_TOKEN", "COURSE_GATEWAY_URL", "KB_MCP_TOKEN"):
        full.pop(name, None)
    app = subprocess.Popen([sys.executable, "-m", "shiny", "run", "ext-shiny-app/app.py", "--port", str(port)],
                           cwd=RECIPE, env=full, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for _ in range(120):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1)
            return app
        except OSError:
            if app.poll() is not None:
                raise RuntimeError(f"the app stopped at start:\n{app.stdout.read().decode()[-2000:]}") from None
            time.sleep(0.5)
    app.terminate()
    raise RuntimeError("the app did not answer within 60 seconds")


def boxes(page) -> dict:
    """The two value boxes of the page: {"Orders shown": "1,200", "Their amount": "27,594.01 CAD"}."""
    out = {}
    for text in page.locator(".bslib-value-box").all_inner_texts():
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        out[lines[0]] = lines[-1]
    return out


def check(question: str = QUESTION, screenshot: Path | None = None, wait_for_reply: str | None = None) -> dict:
    """Ask one question in the page and return what the page showed before and after.

    It waits until the first box changes (a filter), or, with wait_for_reply, until the chat holds that text
    (a question that leaves the table as it is, such as one the SQL check refuses)."""
    from playwright.sync_api import sync_playwright

    app, port = start_app_on_free_port()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            page.goto(f"http://127.0.0.1:{port}/")
            page.wait_for_function("() => document.querySelectorAll('.bslib-value-box').length === 2 && "
                                   "/\\d/.test(document.querySelector('.bslib-value-box').innerText)", timeout=60000)
            before = boxes(page)
            page.locator("shiny-chat-container [role=textbox]").first.click()
            page.keyboard.type(question)
            page.keyboard.press("Enter")
            if wait_for_reply:
                reply = json.dumps(wait_for_reply)
                page.wait_for_function(
                    f"() => document.querySelector('shiny-chat-container').innerText.includes({reply})", timeout=60000)
            else:
                shown = json.dumps(before["Orders shown"])
                page.wait_for_function(
                    f"() => !document.querySelector('.bslib-value-box').innerText.includes({shown})", timeout=60000)
            page.wait_for_timeout(1500)  # let the table and the reply finish drawing
            for failed in page.locator("shiny-chat-container").get_by_text("failed", exact=True).all():
                failed.click()  # unfold a failed tool call, so that the reason is on the page and is read below
            page.wait_for_timeout(500)
            result = {"before": before, "after": boxes(page),
                      "title": page.locator(".card-header").first.inner_text().strip(),
                      "sql": page.locator(".card-footer").first.inner_text().strip(),
                      "rows_in_table": page.locator(".shiny-data-grid tbody tr").count(),
                      "chat": page.locator("shiny-chat-container").inner_text()}
            if screenshot:
                page.screenshot(path=str(screenshot))
            browser.close()
            return result
    finally:
        app.terminate()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--question", default=QUESTION)
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument("--wait-for-reply", help="text of the reply to wait for, when the question does not filter")
    args = parser.parse_args()
    if not browser_available():
        raise SystemExit("no Chromium for Playwright on this machine: uv run playwright install chromium")
    print(json.dumps(check(args.question, args.screenshot, args.wait_for_reply), indent=2))
