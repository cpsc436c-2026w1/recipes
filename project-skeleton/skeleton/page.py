"""The number as users see it: one static HTML page (secondary technology, scaffolded; swap in your dashboard)."""
import html
from datetime import datetime, timezone
from pathlib import Path

from skeleton.lake_local import LOCAL_ROOT


def write_page(number: int, shape: str, source: str, root: Path = LOCAL_ROOT) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{shape}.html"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    path.write_text(f"""<!doctype html><meta charset="utf-8"><title>Updates in the last hour</title>
<style>body{{font-family:system-ui,sans-serif;margin:3rem;color:#222}}b{{font-size:5rem;display:block}}</style>
<p>Updates in the last hour</p><b>{number}</b>
<p>From {html.escape(source)}, {stamp}</p>
""")
    return path
