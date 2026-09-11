"""GitHub Pages 用の最小限の index.html を生成する。

``blender --command extension server-generate`` が作った ``index.json`` を読み、
見出しと「Blender に登録する Repository URL」、zip へのリンクだけを載せる。

    python3 tools/make_site_index.py <site_dir> <base_url>
"""

from __future__ import annotations

import html
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

TEMPLATE = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 48rem; margin: 3rem auto; padding: 0 1rem; line-height: 1.6; color: #222; }}
  code {{ background: #f2f2f2; padding: .15rem .4rem; border-radius: .25rem; font-size: .95em; }}
  h1 {{ font-size: 1.6rem; }}
  ul {{ padding-left: 1.2rem; }}
  footer {{ margin-top: 3rem; color: #777; font-size: .85rem; }}
</style>
</head>
<body>
<h1>{title}</h1>
<p>Blender Extension Repository URL:</p>
<p><code>{repo_url}</code></p>
<ul>
{items}
</ul>
<footer>generated {date}</footer>
</body>
</html>
"""


def main() -> None:
    site = Path(sys.argv[1])
    base_url = sys.argv[2].rstrip("/")
    index = json.loads((site / "index.json").read_text("utf-8"))
    entries = index.get("data", [])
    title = entries[0]["name"] if entries else "Blender Extension Repository"
    items = []
    for e in entries:
        archive = e.get("archive_url", "")
        href = archive if archive.startswith(("http://", "https://")) else f"{base_url}/{archive.lstrip('./')}"
        items.append(
            f'<li>{html.escape(e.get("name", e.get("id", "")))} {html.escape(str(e.get("version", "")))}'
            f' — <a href="{html.escape(href)}">{html.escape(href)}</a></li>'
        )
    page = TEMPLATE.format(
        title=html.escape(title),
        repo_url=html.escape(f"{base_url}/index.json"),
        items="\n".join(items) or "<li>(no packages)</li>",
        date=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    )
    (site / "index.html").write_text(page, "utf-8")
    print(f"wrote {site / 'index.html'} ({len(entries)} package(s))")


if __name__ == "__main__":
    main()
