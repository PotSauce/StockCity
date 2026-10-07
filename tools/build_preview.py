"""Bundle docs/ into one self-contained HTML file (used for shareable previews).

    python tools/build_preview.py preview/stock-city.html
"""
import json
import re
import sys
from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent / "docs"


def main(out):
    html = (DOCS / "index.html").read_text()
    css = (DOCS / "style.css").read_text()
    js = (DOCS / "app.js").read_text()
    state = json.loads((DOCS / "data" / "state.json").read_text())
    title = re.search(r"<title>.*?</title>", html).group(0)
    fonts = "\n".join(re.findall(r'<link rel="(?:preconnect|stylesheet)" href="https://fonts[^>]*>', html))
    importmap = re.search(r'<script type="importmap">.*?</script>', html, re.S).group(0)
    body = re.search(r"<!--BODY-->(.*)<!--/BODY-->", html, re.S).group(1)
    data = json.dumps(state, separators=(",", ":")).replace("</", "<\\/")
    page = f"""{title}
{fonts}
<style>
{css}
html, body {{ height: 100%; }}
:root {{ box-sizing: border-box; height: 100%; }}
</style>
{importmap}
{body}
<script>window.__STATE__ = {data};</script>
<script type="module">
{js}
</script>
"""
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(page)
    print(f"wrote {out} ({len(page) / 1024:.0f} KB)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "preview/stock-city.html")
