"""入れ子の .unitypackage だけを持つ合成パッケージを作る（Issue #217）。bpy 不要。

    python3 tests/make_synthetic_nested_package.py <packages のフォルダ>

既存の合成パッケージ（synthetic_terrain / synthetic_mesh）を、パイプライン別の中身のように BuiltIn_Terrain.unitypackage と
URP_Mesh.unitypackage として入れる。外側にはモデル・prefab・シーンが無い。
"""

from __future__ import annotations

import hashlib
import io
import sys
import tarfile
from pathlib import Path


def _guid(name: str) -> str:
    return hashlib.md5(name.encode()).hexdigest()


def main() -> None:
    packages = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "_local" / "unitypackages"
    out = packages / "synthetic_nested.unitypackage"
    members = {
        "Assets/Nested/BuiltIn_Terrain.unitypackage": (packages / "synthetic_terrain.unitypackage").read_bytes(),
        "Assets/Nested/URP_Mesh.unitypackage": (packages / "synthetic_mesh.unitypackage").read_bytes(),
        "Assets/Nested/ReadMe.txt": b"Import the package for your render pipeline.\n",
    }
    with tarfile.open(out, "w:gz") as tar:
        for pathname, data in members.items():
            guid = _guid(pathname)
            for name, payload in (("pathname", (pathname + "\n").encode()), ("asset", data),
                                  ("asset.meta", f"fileFormatVersion: 2\nguid: {guid}\nDefaultImporter:\n".encode())):
                info = tarfile.TarInfo(f"{guid}/{name}")
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
    print(f"wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
