"""GitHub Pages 用のサイトを ``web/`` から組み立てる。

``blender --command extension server-generate`` が作った ``index.json`` を読み、
``web/`` の各ファイルを ``site/`` にコピーしながら、テンプレート変数を埋める。
``index.json`` と Extension の zip は触らない（Blender の Repository URL を壊さないため）。

    python3 tools/build_site.py <site_dir> <base_url> [--web-dir web]

置換する変数:
    {{VERSION}}   index.json の先頭エントリの version
    {{ZIP_URL}}   zip の絶対 URL（index.json の archive_url を base_url で解決）
    {{BASE_URL}}  末尾スラッシュ無しのサイト URL（例: https://example.github.io/repo）
    {{DATE}}      ビルド日（UTC, YYYY-MM-DD）
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

TEXT_EXTS = {".html", ".xml", ".txt", ".css", ".js", ".svg", ".json", ".webmanifest"}


def load_release(site: Path, base_url: str) -> tuple[str, str]:
    """index.json から (version, zip_url) を返す。無ければ空文字（ローカル確認用）。"""
    index = site / "index.json"
    if not index.exists():
        return "", ""
    data = json.loads(index.read_text("utf-8")).get("data", [])
    if not data:
        return "", ""
    entry = data[0]
    archive = str(entry.get("archive_url", ""))
    if archive.startswith(("http://", "https://")):
        zip_url = archive
    else:
        zip_url = f"{base_url}/{archive.lstrip('./')}"
    return str(entry.get("version", "")), zip_url


def render(text: str, variables: dict[str, str]) -> str:
    for key, value in variables.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def build(site: Path, base_url: str, web: Path) -> list[Path]:
    base_url = base_url.rstrip("/")
    version, zip_url = load_release(site, base_url)
    variables = {
        "VERSION": version or "dev",
        "ZIP_URL": zip_url or f"{base_url}/",
        "BASE_URL": base_url,
        "DATE": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }
    written: list[Path] = []
    for src in sorted(web.rglob("*")):
        if src.is_dir():
            continue
        rel = src.relative_to(web)
        dst = site / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.suffix.lower() in TEXT_EXTS:
            dst.write_text(render(src.read_text("utf-8"), variables), "utf-8")
        else:
            shutil.copy2(src, dst)
        written.append(dst)
    leftovers = [str(f) for f in written if f.suffix.lower() in TEXT_EXTS and "{{" in f.read_text("utf-8")]
    if leftovers:
        print("warning: unreplaced template variables in", ", ".join(leftovers), file=sys.stderr)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("site_dir")
    parser.add_argument("base_url")
    parser.add_argument("--web-dir", default=str(Path(__file__).resolve().parent.parent / "web"))
    args = parser.parse_args()
    site = Path(args.site_dir)
    site.mkdir(parents=True, exist_ok=True)
    written = build(site, args.base_url, Path(args.web_dir))
    print(f"wrote {len(written)} files to {site}")


if __name__ == "__main__":
    main()
