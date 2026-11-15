"""合成パッケージの生成と、統合テストの全期待値の実行をまとめて行う（#199）。

    python3 tests/run_integration.py                      # 無い合成パッケージを生成してから、全期待値を回す
    python3 tests/run_integration.py --generate           # 合成パッケージを作り直してから回す
    python3 tests/run_integration.py --generate-only      # 生成だけ（CI の fixtures ジョブ）
    python3 tests/run_integration.py --no-generate        # 生成せずに回す（CI の integration ジョブ）
    python3 tests/run_integration.py --only scene         # 期待値のファイル名に "scene" を含むものだけ
    python3 tests/run_integration.py --blender ~/blender/blender --packages fixtures

どのパッケージをどの期待値で回すかと、パッケージを作るスクリプトは ``tests/integration_runs.json`` に書く（CLAUDE.md と
CI に同じ一覧を手で書いていたのをまとめた）。1 本が失敗しても残りを回し、最後に結果をまとめて出して、失敗があれば 1 で終わる。
Blender の中ではなく、ふつうの Python（標準ライブラリだけ）で動かす。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNS_FILE = REPO_ROOT / "tests" / "integration_runs.json"
DEFAULT_PACKAGES = REPO_ROOT / "_local" / "unitypackages"
_SUMMARY = re.compile(r"=== integration_import: (.*) ===")
_IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"


def _group(title: str) -> None:
    print(f"::group::{title}" if _IN_ACTIONS else f"\n--- {title}", flush=True)


def _end_group() -> None:
    if _IN_ACTIONS:
        print("::endgroup::", flush=True)


def generate(config: dict, blender: str, packages: Path, force: bool) -> bool:
    """合成パッケージを作る。``force`` でなければ、出力がすべてあるスクリプトは飛ばす。成功すれば True。"""
    packages.mkdir(parents=True, exist_ok=True)
    ok = True
    for gen in config["generators"]:
        outputs = [packages / name for name in gen["outputs"]]
        if not force and all(p.is_file() for p in outputs):
            continue
        script = str(REPO_ROOT / gen["script"])
        if gen.get("blender"):
            command = [blender, "-b", "--factory-startup", "--python", script, "--", str(outputs[0])]
        else:
            args = [a.replace("{packages}", str(packages)) for a in gen.get("args", [])]
            command = [sys.executable, script, *args]
        _group(f"generate {gen['script']}")
        result = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True)
        lines = (result.stdout + result.stderr).splitlines()
        print("\n".join(line for line in lines if line.startswith("wrote") or "Error" in line or "Traceback" in line))
        _end_group()
        missing = [p.name for p in outputs if not p.is_file()]
        if result.returncode != 0 or missing:
            print(f"FAILED to generate {gen['script']} (exit {result.returncode}, missing {missing})")
            if result.returncode != 0:
                print("\n".join(lines[-30:]))
            ok = False
    return ok


def run(config: dict, blender: str, packages: Path, only: list[str]) -> list[tuple[str, str, bool, float]]:
    """全期待値を回し、(期待値, 結果の行, 成功か, 秒) を返す。"""
    results = []
    for entry in config["runs"]:
        expectations = entry["expectations"]
        if only and not any(word in expectations for word in only):
            continue
        command = [
            blender, "-b", "--factory-startup", "--python", str(REPO_ROOT / "tests" / "integration_import.py"),
            "--", str(packages / entry["package"]), str(REPO_ROOT / "tests" / expectations),
        ]
        _group(expectations)
        start = time.monotonic()
        result = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True)
        elapsed = time.monotonic() - start
        output = result.stdout + result.stderr
        found = _SUMMARY.findall(output)
        summary = found[-1] if found else "no summary (crashed?)"
        passed = result.returncode == 0 and bool(found)
        # 失敗した行と、例外の traceback だけを出す（全部出すと他の出力に埋もれる）
        lines = output.splitlines()
        shown = [line for line in lines if line.startswith("  FAIL")]
        if not passed:
            shown = shown or lines[-40:]
        print("\n".join(shown))
        print(f"{expectations}: {summary}")
        _end_group()
        if not passed and _IN_ACTIONS:
            print(f"::error title=integration test failed::{expectations}")
        results.append((expectations, summary, passed, elapsed))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--blender", default=os.environ.get("BLENDER", "blender"), help="Blender の実行ファイル（既定は PATH の blender）")
    parser.add_argument("--packages", type=Path, default=DEFAULT_PACKAGES, help="合成パッケージの置き場（既定は _local/unitypackages）")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--generate", action="store_true", help="合成パッケージを作り直してから回す")
    mode.add_argument("--generate-only", action="store_true", help="合成パッケージを作り直すだけ")
    mode.add_argument("--no-generate", action="store_true", help="合成パッケージを作らずに回す")
    parser.add_argument("--only", nargs="*", default=[], help="期待値のファイル名にこの文字列を含むものだけを回す")
    args = parser.parse_args()

    config = json.loads(RUNS_FILE.read_text("utf-8"))
    packages = args.packages.resolve()
    if not args.no_generate:
        if not generate(config, args.blender, packages, force=args.generate or args.generate_only):
            return 1
        if args.generate_only:
            return 0

    results = run(config, args.blender, packages, args.only)
    print("\n=== integration summary ===")
    for expectations, summary, passed, elapsed in results:
        print(f"{'ok  ' if passed else 'FAIL'} {expectations:<55} {summary}  ({elapsed:.0f}s)")
    failed = [r for r in results if not r[2]]
    print(f"{len(results) - len(failed)} of {len(results)} expectation files passed")
    return 1 if failed or not results else 0


if __name__ == "__main__":
    sys.exit(main())
