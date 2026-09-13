"""GitHub Pages 用のサイトを ``web/`` から組み立てる。

``blender --command extension server-generate`` が作った ``index.json`` を読み、
``web/`` の各ファイルを ``site/`` にコピーしながら、テンプレート変数を埋める。
``index.json`` と Extension の zip は触らない（Blender の Repository URL を壊さないため）。

    python3 tools/build_site.py <site_dir> <base_url> [--web-dir web] [--env-file .env]

置換する変数:
    {{VERSION}}   index.json の先頭エントリの version
    {{ZIP_URL}}   zip の絶対 URL（index.json の archive_url を base_url で解決）
    {{BASE_URL}}  末尾スラッシュ無しのサイト URL（例: https://example.github.io/repo）
    {{DATE}}      ビルド日（UTC, YYYY-MM-DD）
    {{GTM_HEAD}}  Google Tag Manager の <head> 用タグ（GTM ID が無ければ空）
    {{GTM_BODY}}  Google Tag Manager の <body> 直後用 noscript タグ（GTM ID が無ければ空）

``<!-- gtm-only -->`` と ``<!-- /gtm-only -->`` で囲んだ部分（アクセス解析と Cookie の案内など）は、
GTM ID があるときだけ残し（囲みのコメントは外す）、無ければ丸ごと取り除く。

GTM ID は環境変数 ``GTM_ID``、無ければ ``--env-file``（既定はリポジトリ直下の ``.env``）の
``GTM_ID=...`` から読む。``GTM-`` で始まる英大文字と数字の形式でなければ埋め込まない。
GitHub Actions ではリポジトリの Variables の ``GTM_ID`` を環境変数として渡す（``.github/workflows/pages.yml``）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

TEXT_EXTS = {".html", ".xml", ".txt", ".css", ".js", ".svg", ".json", ".webmanifest"}
REPO_ROOT = Path(__file__).resolve().parent.parent
GTM_ID_RE = re.compile(r"^GTM-[A-Z0-9]{4,12}$")
GTM_ONLY_RE = re.compile(r"[ \t]*<!-- gtm-only -->(.*?)<!-- /gtm-only -->[ \t]*\n?", re.DOTALL)

GTM_HEAD = """<!-- Google Tag Manager -->
<script>(function(w,d,s,l,i){w[l]=w[l]||[];w[l].push({'gtm.start':
new Date().getTime(),event:'gtm.js'});var f=d.getElementsByTagName(s)[0],
j=d.createElement(s),dl=l!='dataLayer'?'&l='+l:'';j.async=true;j.src=
'https://www.googletagmanager.com/gtm.js?id='+i+dl;f.parentNode.insertBefore(j,f);
})(window,document,'script','dataLayer','%(id)s');</script>
<!-- End Google Tag Manager -->"""

GTM_BODY = """<!-- Google Tag Manager (noscript) -->
<noscript><iframe src="https://www.googletagmanager.com/ns.html?id=%(id)s"
height="0" width="0" style="display:none;visibility:hidden"></iframe></noscript>
<!-- End Google Tag Manager (noscript) -->"""


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


def read_env_file(path: Path) -> dict[str, str]:
    """``KEY=VALUE`` 形式の .env を読む。無ければ空。``#`` 始まりと空行は無視し、値の両端の引用符を外す。"""
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw in path.read_text("utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def resolve_gtm_id(environ: Mapping[str, str], env_file: Path) -> str:
    """環境変数 → .env の順で GTM_ID を探す。形式が不正なら警告して空を返す。"""
    raw = (environ.get("GTM_ID") or "").strip() or read_env_file(env_file).get("GTM_ID", "").strip()
    if not raw:
        return ""
    if not GTM_ID_RE.match(raw):
        print(f"warning: ignoring GTM_ID with unexpected format: {raw!r}", file=sys.stderr)
        return ""
    return raw


def apply_gtm_blocks(text: str, enabled: bool) -> str:
    """``gtm-only`` の囲みを、GTM が有効なら中身だけ残し、無効なら行ごと取り除く。"""
    if enabled:
        return GTM_ONLY_RE.sub(lambda m: m.group(0).replace("<!-- gtm-only -->", "").replace("<!-- /gtm-only -->", ""), text)
    return GTM_ONLY_RE.sub("", text)


def render(text: str, variables: dict[str, str]) -> str:
    for key, value in variables.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def build(site: Path, base_url: str, web: Path, gtm_id: str = "") -> list[Path]:
    base_url = base_url.rstrip("/")
    version, zip_url = load_release(site, base_url)
    variables = {
        "VERSION": version or "dev",
        "ZIP_URL": zip_url or f"{base_url}/",
        "BASE_URL": base_url,
        "DATE": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "GTM_HEAD": GTM_HEAD % {"id": gtm_id} if gtm_id else "",
        "GTM_BODY": GTM_BODY % {"id": gtm_id} if gtm_id else "",
    }
    written: list[Path] = []
    for src in sorted(web.rglob("*")):
        if src.is_dir():
            continue
        rel = src.relative_to(web)
        dst = site / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.suffix.lower() in TEXT_EXTS:
            dst.write_text(apply_gtm_blocks(render(src.read_text("utf-8"), variables), bool(gtm_id)), "utf-8")
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
    parser.add_argument("--web-dir", default=str(REPO_ROOT / "web"))
    parser.add_argument("--env-file", default=str(REPO_ROOT / ".env"))
    args = parser.parse_args()
    site = Path(args.site_dir)
    site.mkdir(parents=True, exist_ok=True)
    gtm_id = resolve_gtm_id(os.environ, Path(args.env_file))
    written = build(site, args.base_url, Path(args.web_dir), gtm_id)
    print(f"wrote {len(written)} files to {site}" + (f" (GTM {gtm_id})" if gtm_id else " (no GTM)"))


if __name__ == "__main__":
    main()
