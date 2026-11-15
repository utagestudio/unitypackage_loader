"""テストと合成パッケージの生成で使う、Unity のテキスト形式（YAML）のドキュメントを組み立てる関数。

シーン・prefab のドキュメントは ``HEADER`` の後にこれらの戻り値をつなげれば 1 つのアセットになる。
以前は ``tests/test_hierarchy.py`` と ``tests/make_synthetic_scene_package.py`` に同じ関数がコピーされていて、
引数が少しずつずれ始めていたので、ここにまとめた（#199）。bpy には依存しない。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

HEADER = "%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n"

# モデル（FBX 等）の PrefabInstance で、ルートの GameObject / Transform を指す fileID（.meta の fileIdsGeneration が 2 の値）
ROOT_GAME_OBJECT = 919132149155446097
ROOT_TRANSFORM = -8679921383154817045


def game_object(file_id, name, active=1):
    return f"--- !u!1 &{file_id}\nGameObject:\n  m_Name: {name}\n  m_IsActive: {active}\n"


def transform(file_id, go, father=0, pos=(0, 0, 0), rot=(0, 0, 0, 1), scale=(1, 1, 1), class_id=4, kind="Transform"):
    """Transform（``class_id=224, kind="RectTransform"`` で UI の RectTransform）。"""
    return (
        f"--- !u!{class_id} &{file_id}\n{kind}:\n  m_GameObject: {{fileID: {go}}}\n"
        f"  m_LocalRotation: {{x: {rot[0]}, y: {rot[1]}, z: {rot[2]}, w: {rot[3]}}}\n"
        f"  m_LocalPosition: {{x: {pos[0]}, y: {pos[1]}, z: {pos[2]}}}\n"
        f"  m_LocalScale: {{x: {scale[0]}, y: {scale[1]}, z: {scale[2]}}}\n"
        f"  m_Father: {{fileID: {father}}}\n"
    )


def mesh_renderer(filter_id, renderer_id, go, mesh_guid, material, mesh_file_id=4300000, enabled=1):
    """MeshFilter と、マテリアルを 1 つ持つ MeshRenderer。"""
    return (
        f"--- !u!33 &{filter_id}\nMeshFilter:\n  m_GameObject: {{fileID: {go}}}\n"
        f"  m_Mesh: {{fileID: {mesh_file_id}, guid: {mesh_guid}, type: 3}}\n"
        f"--- !u!23 &{renderer_id}\nMeshRenderer:\n  m_GameObject: {{fileID: {go}}}\n  m_Enabled: {enabled}\n"
        f"  m_Materials:\n  - {{fileID: 2100000, guid: {material}, type: 2}}\n"
    )


def skinned_renderer(renderer_id, go, mesh_guid, material, mesh_file_id=4300000):
    return (
        f"--- !u!137 &{renderer_id}\nSkinnedMeshRenderer:\n  m_GameObject: {{fileID: {go}}}\n  m_Enabled: 1\n"
        f"  m_Materials:\n  - {{fileID: 2100000, guid: {material}, type: 2}}\n"
        f"  m_Mesh: {{fileID: {mesh_file_id}, guid: {mesh_guid}, type: 3}}\n"
    )


def lod_group(file_id, go, levels: Sequence[Sequence[int]], enabled=1):
    """LODGroup（#104）。``levels`` は段の順に並べた Renderer の fileID。"""
    body = ""
    for index, renderers in enumerate(levels):
        body += f"  - screenRelativeHeight: {round(0.5 / (index + 1), 4)}\n    fadeTransitionWidth: 0\n"
        if renderers:
            body += "    renderers:\n" + "".join(f"    - renderer: {{fileID: {r}}}\n" for r in renderers)
        else:
            body += "    renderers: []\n"
    return (
        f"--- !u!205 &{file_id}\nLODGroup:\n  m_GameObject: {{fileID: {go}}}\n  serializedVersion: 2\n"
        f"  m_LocalReferencePoint: {{x: 0, y: 0, z: 0}}\n  m_Size: 2\n  m_FadeMode: 0\n  m_AnimateCrossFading: 0\n"
        f"  m_LastLODIsBillboard: 0\n  m_LODs:\n{body}  m_Enabled: {enabled}\n"
    )


def mod(target, guid, path, value="", reference="{fileID: 0}"):
    """PrefabInstance の ``m_Modifications`` の 1 行。"""
    return (
        f"    - target: {{fileID: {target}, guid: {guid}, type: 3}}\n"
        f"      propertyPath: {path}\n      value: {value}\n      objectReference: {reference}\n"
    )


def instance(
    file_id,
    source,
    parent,
    mods: Iterable[str],
    removed_components: Iterable[int | tuple[int, str]] = (),
    removed_game_objects: Iterable[int | tuple[int, str]] = (),
):
    """PrefabInstance。消したものは元のアセットでの fileID で挙げる（別のアセットを指すときは ``(fileID, GUID)``）。"""

    def refs(key, items):
        items = list(items)
        if not items:
            return f"    {key}: []\n"
        lines = []
        for item in items:
            target, guid = item if isinstance(item, tuple) else (item, source)
            lines.append(f"    - {{fileID: {target}, guid: {guid}, type: 3}}\n")
        return f"    {key}:\n" + "".join(lines)

    return (
        f"--- !u!1001 &{file_id}\nPrefabInstance:\n  m_Modification:\n"
        f"    m_TransformParent: {{fileID: {parent}}}\n    m_Modifications:\n{''.join(mods)}"
        f"{refs('m_RemovedComponents', removed_components)}{refs('m_RemovedGameObjects', removed_game_objects)}"
        f"  m_SourcePrefab: {{fileID: 100100000, guid: {source}, type: 3}}\n"
    )


def stripped_transform(file_id, source_file_id, source_guid, instance_id, class_id=4, kind="Transform"):
    """PrefabInstance の中の Transform を外から指すための stripped ドキュメント。"""
    return (
        f"--- !u!{class_id} &{file_id} stripped\n{kind}:\n"
        f"  m_CorrespondingSourceObject: {{fileID: {source_file_id}, guid: {source_guid}, type: 3}}\n"
        f"  m_PrefabInstance: {{fileID: {instance_id}}}\n"
    )


def light_doc(file_id, go, light_type, intensity=1, light_range=10, spot=30, inner=21.80208, color=(1, 1, 1), enabled=1,
              shadow=2, lightmapping=4, area=(1, 1), use_temperature=0, temperature=6570):
    return (
        f"--- !u!108 &{file_id}\nLight:\n  m_GameObject: {{fileID: {go}}}\n  m_Enabled: {enabled}\n  serializedVersion: 10\n"
        f"  m_Type: {light_type}\n  m_Color: {{r: {color[0]}, g: {color[1]}, b: {color[2]}, a: 1}}\n"
        f"  m_Intensity: {intensity}\n  m_Range: {light_range}\n  m_SpotAngle: {spot}\n  m_InnerSpotAngle: {inner}\n"
        f"  m_Shadows:\n    m_Type: {shadow}\n  m_Lightmapping: {lightmapping}\n  m_AreaSize: {{x: {area[0]}, y: {area[1]}}}\n"
        f"  m_ColorTemperature: {temperature}\n  m_UseColorTemperature: {use_temperature}\n  m_ShadowRadius: 0\n  m_ShadowAngle: 0\n"
    )


def mono_behaviour(file_id, go, script_guid):
    """スクリプトのコンポーネント（URP / HDRP のライトの追加データなど。#120）。"""
    return (
        f"--- !u!114 &{file_id}\nMonoBehaviour:\n  m_GameObject: {{fileID: {go}}}\n  m_Enabled: 1\n"
        f"  m_Script: {{fileID: 11500000, guid: {script_guid}, type: 3}}\n"
    )


def camera_doc(file_id, go, fov=60, orthographic=0, size=5, near=0.3, far=1000, mode=1, focal=50, sensor=(36, 24), gate=2):
    return (
        f"--- !u!20 &{file_id}\nCamera:\n  m_GameObject: {{fileID: {go}}}\n  m_Enabled: 1\n  serializedVersion: 2\n"
        f"  m_projectionMatrixMode: {mode}\n  m_GateFitMode: {gate}\n  m_FocalLength: {focal}\n"
        f"  m_SensorSize: {{x: {sensor[0]}, y: {sensor[1]}}}\n  m_LensShift: {{x: 0, y: 0}}\n"
        f"  near clip plane: {near}\n  far clip plane: {far}\n  field of view: {fov}\n"
        f"  orthographic: {orthographic}\n  orthographic size: {size}\n"
    )


def decal_doc(file_id: int, go: int, material: str, size, offset) -> str:
    """URP の DecalProjector（MonoBehaviour。Shibuya と同じフィールド。#165）。"""
    return (
        f"--- !u!114 &{file_id}\nMonoBehaviour:\n  m_GameObject: {{fileID: {go}}}\n  m_Enabled: 1\n"
        "  m_Script: {fileID: 11500000, guid: 0777d029ed3dffa4692f417d4aba19ca, type: 3}\n"
        f"  m_Material: {{fileID: 2100000, guid: {material}, type: 2}}\n  m_DrawDistance: 1000\n"
        "  m_UVScale: {x: 1, y: 1}\n  m_UVBias: {x: 0, y: 0}\n  m_ScaleMode: 1\n"
        f"  m_Offset: {{x: {offset[0]}, y: {offset[1]}, z: {offset[2]}}}\n"
        f"  m_Size: {{x: {size[0]}, y: {size[1]}, z: {size[2]}}}\n  m_FadeFactor: 1\n"
    )
