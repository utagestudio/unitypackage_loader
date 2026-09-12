"""Unity マテリアルの生データと、シェーダー非依存の中間表現。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any, Literal

from .unity_yaml import UnityRef, parse_documents

__all__ = ["TexRef", "UnityMaterial", "NormalizedMaterial", "parse_material", "MaterialParseError"]

Color = tuple[float, float, float, float]
AlphaMode = Literal["opaque", "cutout", "blend"]
Lighting = Literal["pbr", "toon", "unlit"]

WHITE: Color = (1.0, 1.0, 1.0, 1.0)
BLACK: Color = (0.0, 0.0, 0.0, 1.0)


class MaterialParseError(ValueError):
    pass


@dataclass(frozen=True)
class TexRef:
    guid: str
    file_id: int = 2800000
    scale: tuple[float, float] = (1.0, 1.0)
    offset: tuple[float, float] = (0.0, 0.0)

    @property
    def has_transform(self) -> bool:
        return self.scale != (1.0, 1.0) or self.offset != (0.0, 0.0)


@dataclass
class UnityMaterial:
    """.mat の生データ。値が入っているプロパティだけを持つ。"""

    guid: str = ""
    pathname: str = ""
    name: str = ""
    shader: UnityRef | None = None
    textures: dict[str, TexRef] = field(default_factory=dict)
    floats: dict[str, float] = field(default_factory=dict)
    ints: dict[str, int] = field(default_factory=dict)
    colors: dict[str, Color] = field(default_factory=dict)
    render_queue: int = -1
    keywords: list[str] = field(default_factory=list)
    # 現在のシェーダーには存在しないが .mat に残っているキーワード（m_InvalidKeywords）。
    # 別シェーダーから切り替えた名残なので、プロパティの残骸を無視する手掛かりになる
    invalid_keywords: list[str] = field(default_factory=list)
    # テクスチャ参照が null（fileID 0）でも Scale/Offset を持つプロパティの一覧
    texture_slots: set[str] = field(default_factory=set)

    # ---- 参照ヘルパ ----
    @property
    def shader_guid(self) -> str | None:
        return self.shader.guid if self.shader else None

    @property
    def shader_builtin_id(self) -> int | None:
        if self.shader and (self.shader.guid is None or self.shader.guid.endswith("f000000000000000")):
            return self.shader.file_id
        return None

    def tex(self, *names: str) -> TexRef | None:
        for name in names:
            ref = self.textures.get(name)
            if ref is not None:
                return ref
        return None

    def f(self, name: str, default: float = 0.0) -> float:
        value = self.floats.get(name)
        if value is None:
            value = self.ints.get(name)
        return float(value) if value is not None else default

    def flag(self, name: str, default: bool = False) -> bool:
        value = self.floats.get(name, self.ints.get(name))
        return bool(value) if value is not None else default

    def color(self, *names: str, default: Color = WHITE) -> Color:
        for name in names:
            value = self.colors.get(name)
            if value is not None:
                return value
        return default

    def has(self, *names: str) -> bool:
        return any(
            n in self.floats or n in self.ints or n in self.colors or n in self.texture_slots for n in names
        )


def _as_float(value: Any, default: float = 0.0) -> float:
    return float(value) if isinstance(value, (int, float)) else default


def _as_color(value: Any) -> Color | None:
    if isinstance(value, dict):
        return (
            _as_float(value.get("r")),
            _as_float(value.get("g")),
            _as_float(value.get("b")),
            _as_float(value.get("a"), 1.0),
        )
    return None


def _as_vec2(value: Any, default: tuple[float, float]) -> tuple[float, float]:
    if isinstance(value, dict):
        return (_as_float(value.get("x"), default[0]), _as_float(value.get("y"), default[1]))
    return default


def _items(seq: Any):
    """``[{name: value}, ...]`` を (name, value) で列挙する。"""
    if not isinstance(seq, list):
        return
    for item in seq:
        if isinstance(item, dict):
            for key, value in item.items():
                yield str(key), value


def parse_material(text: str, guid: str = "", pathname: str = "") -> UnityMaterial:
    docs = [d for d in parse_documents(text) if d.type_name == "Material"]
    if not docs:
        raise MaterialParseError("no Material document found")
    body = docs[0].body
    mat = UnityMaterial(guid=guid, pathname=pathname)
    mat.name = str(body.get("m_Name", "") or "")
    shader = body.get("m_Shader")
    mat.shader = shader if isinstance(shader, UnityRef) else None
    queue = body.get("m_CustomRenderQueue")
    mat.render_queue = int(queue) if isinstance(queue, int) else -1

    keywords = body.get("m_ValidKeywords")
    if isinstance(keywords, list):
        mat.keywords = [str(k) for k in keywords]
    legacy = body.get("m_ShaderKeywords")
    if isinstance(legacy, str) and legacy.strip():
        mat.keywords.extend(legacy.split())
    invalid = body.get("m_InvalidKeywords")
    if isinstance(invalid, list):
        mat.invalid_keywords = [str(k) for k in invalid]

    saved = body.get("m_SavedProperties") or {}
    if not isinstance(saved, dict):
        saved = {}
    for name, value in _items(saved.get("m_TexEnvs")):
        if not isinstance(value, dict):
            continue
        mat.texture_slots.add(name)
        ref = value.get("m_Texture")
        if isinstance(ref, UnityRef) and ref.guid:
            mat.textures[name] = TexRef(
                guid=ref.guid,
                file_id=ref.file_id,
                scale=_as_vec2(value.get("m_Scale"), (1.0, 1.0)),
                offset=_as_vec2(value.get("m_Offset"), (0.0, 0.0)),
            )
    for name, value in _items(saved.get("m_Floats")):
        if isinstance(value, (int, float)):
            mat.floats[name] = float(value)
    for name, value in _items(saved.get("m_Ints")):
        if isinstance(value, int):
            mat.ints[name] = value
    for name, value in _items(saved.get("m_Colors")):
        color = _as_color(value)
        if color is not None:
            mat.colors[name] = color
    return mat


@dataclass
class NormalizedMaterial:
    """Blender ノード生成に必要な情報だけを持つ、シェーダー非依存の表現。"""

    name: str
    family: str = "unknown"
    shader_name: str | None = None
    lighting: Lighting = "pbr"
    base_color_tex: TexRef | None = None
    base_color: Color = WHITE
    alpha_mode: AlphaMode = "opaque"
    alpha_cutoff: float = 0.5
    alpha_from_texture: bool = True  # False なら base_color.a だけを使う
    normal_tex: TexRef | None = None
    normal_strength: float = 1.0
    emission_tex: TexRef | None = None
    emission_color: Color = BLACK
    emission_strength: float = 1.0
    metallic: float = 0.0
    roughness: float = 0.5
    metallic_tex: TexRef | None = None  # Unity 形式: R=metallic, A=smoothness
    occlusion_tex: TexRef | None = None
    cull_backface: bool = True
    uv_scale: tuple[float, float] = (1.0, 1.0)
    uv_offset: tuple[float, float] = (0.0, 0.0)
    extras: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    source_guid: str = ""
    source_path: str = ""
    shader_guid: str | None = None

    def texture_refs(self) -> list[TexRef]:
        refs = [
            t
            for t in (self.base_color_tex, self.normal_tex, self.emission_tex, self.metallic_tex, self.occlusion_tex)
            if t is not None
        ]
        return refs

    @property
    def has_emission(self) -> bool:
        return self.emission_tex is not None or any(c > 0.0 for c in self.emission_color[:3])

    # ---- JSON 往復（カスタムプロパティへの保存と再構築用） ----
    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("base_color_tex", "normal_tex", "emission_tex", "metallic_tex", "occlusion_tex"):
            ref = getattr(self, key)
            data[key] = None if ref is None else {"guid": ref.guid, "scale": list(ref.scale), "offset": list(ref.offset)}
        data.pop("warnings", None)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NormalizedMaterial":
        known = {f.name for f in fields(cls)}
        kwargs: dict[str, Any] = {}
        for key, value in data.items():
            if key not in known:
                continue
            if key.endswith("_tex"):
                kwargs[key] = None if not value else TexRef(
                    guid=str(value["guid"]),
                    scale=tuple(value.get("scale", (1.0, 1.0))),
                    offset=tuple(value.get("offset", (0.0, 0.0))),
                )
            elif key in ("base_color", "emission_color", "uv_scale", "uv_offset"):
                kwargs[key] = tuple(value)
            else:
                kwargs[key] = value
        kwargs.setdefault("name", "")
        return cls(**kwargs)

    def extra_texture_guids(self) -> list[str]:
        """extras に入っている参考テクスチャ（MatCap 等）の GUID。"""
        found: list[str] = []

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                for key, v in value.items():
                    if key == "tex" and isinstance(v, str) and len(v) == 32:
                        found.append(v)
                    else:
                        walk(v)
            elif isinstance(value, (list, tuple)):
                for v in value:
                    walk(v)

        walk(self.extras)
        return found


MATCAP_NORMAL, MATCAP_ADD, MATCAP_SCREEN, MATCAP_MULTIPLY = 0, 1, 2, 3


def matcap_blend_mode(matcap: dict[str, Any]) -> int:
    """extras["matcap"] から Toon ノードグループの MatCap Mode（0..3）を決める。

    ブレンドモードを数値で持つのは lilToon だけで、MToon と VRChat Mobile は
    additive フラグで渡してくる。blend_mode が無い場合や範囲外の場合はそちらを見る。
    """
    mode = matcap.get("blend_mode")
    if mode is not None:
        try:
            mode = int(mode)
        except (TypeError, ValueError):
            mode = None
    if mode is None or not MATCAP_NORMAL <= mode <= MATCAP_MULTIPLY:
        mode = MATCAP_ADD if matcap.get("additive") else MATCAP_NORMAL
    return mode
