"""Shader Graph のつながりから、テクスチャの UV の Tiling & Offset を読む（Issue #193）。"""

import unittest

from tests import _paths  # noqa: F401
from tests.shader_graph_writer import Graph, base_uv_graph, new_format, old_format
from unitypackage_loader.core.material import TexRef, UnityMaterial
from unitypackage_loader.core.shader_graph import GraphError, UVTransform, apply_uv_transforms, texture_uv_transforms

BASE_UV = UVTransform((("_BaseUV", 0), ("_BaseUV", 1)), (("_BaseUV", 2), ("_BaseUV", 3)))
FORMATS = {"new": new_format, "old": old_format}


def no_subgraph(_guid):
    return None


class TransformTests(unittest.TestCase):
    def test_vector4_split_into_tiling_and_offset(self):
        for name, write in FORMATS.items():
            with self.subTest(format=name):
                self.assertEqual(texture_uv_transforms(write(base_uv_graph()), no_subgraph), {"_MainTex": BASE_UV})

    def test_separate_tiling_and_offset_properties(self):
        """Synty の PolygonShader と同じ作り（Tiling と Offset が別の Vector2）。"""
        graph = Graph()
        tiling, offset = graph.prop("_Base_Tiling", "Vector2"), graph.prop("_Base_Offset", "Vector2")
        to = graph.node("TilingAndOffsetNode")
        graph.link(tiling, 0, to, 1)
        graph.link(offset, 0, to, 2)
        graph.sample("_Base_Texture", to, 3)
        expected = UVTransform((("_Base_Tiling", 0), ("_Base_Tiling", 1)), (("_Base_Offset", 0), ("_Base_Offset", 1)))
        for name, write in FORMATS.items():
            with self.subTest(format=name):
                self.assertEqual(texture_uv_transforms(write(graph), no_subgraph), {"_Base_Texture": expected})

    def test_float_tiling_and_constant_offset(self):
        graph = Graph()
        tiling = graph.prop("_Tiling", "Vector1")
        to = graph.node("TilingAndOffsetNode")
        graph.link(tiling, 0, to, 1)  # Offset はつながず、スロットの値 (0, 0)
        graph.sample("_MainTex", to, 3)
        result = texture_uv_transforms(new_format(graph), no_subgraph)
        self.assertEqual(result, {"_MainTex": UVTransform((("_Tiling", 0), ("_Tiling", 0)), (0.0, 0.0))})

    def test_through_subgraph(self):
        """Tiling & Offset のサブグラフ（UnityJapanOffice の UVMapping）を、入力を結び付けてたどる。"""
        inner = Graph()
        inner_to = inner.prop("TilingAndOffset", "Vector4")
        inner_uv = inner.prop("UV", "Vector2")
        split = inner.node("SplitNode")
        inner.link(inner_to, 0, split, 0)
        tiling, offset = inner.node("CombineNode"), inner.node("CombineNode")
        for out_slot, combine, in_slot in ((1, tiling, 0), (2, tiling, 1), (3, offset, 0), (4, offset, 1)):
            inner.link(split, out_slot, combine, in_slot)
        to = inner.node("TilingAndOffsetNode")
        inner.link(inner_uv, 0, to, 0)
        inner.link(tiling, 6, to, 1)
        inner.link(offset, 6, to, 2)
        out = inner.node("SubGraphOutputNode")
        inner.link(to, 3, out, 1)
        to_guid, uv_guid = inner.properties[0][0], inner.properties[1][0]

        for name, write in FORMATS.items():
            with self.subTest(format=name):
                graph = Graph()
                base_uv = graph.prop("_BaseUV", "Vector4")
                uv = graph.node("UVNode", channel=0)
                sub = graph.subgraph_node("f" * 32, [(uv_guid, 11), (to_guid, 12)], [(1, "Out_Vector2")])
                graph.link(uv, 0, sub, 11)
                graph.link(base_uv, 0, sub, 12)
                parallax = graph.node("ParallaxOcclusionMappingNode")  # 近似として UV をそのまま渡す
                graph.link(sub, 1, parallax, 4)
                graph.sample("_MainTex", parallax, 9)
                text = write(inner)
                result = texture_uv_transforms(write(graph), lambda guid: text if guid == "f" * 32 else None)
                self.assertEqual(result, {"_MainTex": BASE_UV})
                # サブグラフが読めなければ分からない
                self.assertEqual(texture_uv_transforms(write(graph), no_subgraph), {})

    def test_not_returned(self):
        graph = Graph()
        graph.sample("_Plain", None)  # UV をつながない（恒等）
        graph.sample("_SecondUV", None, channel=1)
        uv1 = graph.node("UVNode", channel=1)
        to = graph.node("TilingAndOffsetNode")
        graph.link(uv1, 0, to, 0)
        graph.link(graph.prop("_Tiling2", "Vector2"), 0, to, 1)
        graph.sample("_DetailMap", to, 3)  # UV1 を Tiling しても返さない
        mul = graph.node("MultiplyNode")  # 解釈しないノード
        graph.sample("_Unknown", mul, 2)
        self.assertEqual(texture_uv_transforms(new_format(graph), no_subgraph), {})

    def test_same_texture_with_different_uvs(self):
        graph = base_uv_graph()
        graph.sample("_MainTex", None)  # 同じテクスチャを恒等の UV でも読む
        self.assertEqual(texture_uv_transforms(new_format(graph), no_subgraph), {})

    def test_cycle_with_many_branches(self):
        """つながりが循環していても、分岐の多いグラフでも、すぐに「分からない」で終わる。"""
        graph = Graph()
        combines = [graph.node("CombineNode") for _ in range(40)]
        for first, second in zip(combines, combines[1:]):
            for slot in range(4):
                graph.link(second, 6, first, slot)
        for slot in range(4):
            graph.link(combines[0], 6, combines[-1], slot)
        to = graph.node("TilingAndOffsetNode")
        graph.link(combines[0], 6, to, 1)
        graph.sample("_MainTex", to, 3)
        self.assertEqual(texture_uv_transforms(new_format(graph), no_subgraph), {})

    def test_broken(self):
        for text in ("{", '{"m_SerializableNodes": [{"JSONnodeData": "{"}]}', '{"m_Nodes": 3}'):
            with self.subTest(text=text):
                with self.assertRaises(GraphError):
                    texture_uv_transforms(text, no_subgraph)
        for text in ("", "[1, 2]"):  # グラフでないものは何も返さない
            self.assertEqual(texture_uv_transforms(text, no_subgraph), {})


class ApplyTests(unittest.TestCase):
    def test_replaces_texture_scale_and_offset(self):
        mat = UnityMaterial(
            textures={"_MainTex": TexRef("a" * 32, scale=(2.0, 2.0)), "_NormalMap": TexRef("b" * 32)},
            colors={"_BaseUV": (18.0, 9.0, 0.25, 0.5)},
        )
        result = apply_uv_transforms(mat, {"_MainTex": BASE_UV, "_Missing": BASE_UV})
        self.assertEqual((result.textures["_MainTex"].scale, result.textures["_MainTex"].offset), ((18.0, 9.0), (0.25, 0.5)))
        self.assertEqual(result.textures["_NormalMap"], mat.textures["_NormalMap"])
        self.assertEqual(mat.textures["_MainTex"].scale, (2.0, 2.0), "the original is left as it was")

    def test_float_and_missing_values(self):
        mat = UnityMaterial(textures={"_MainTex": TexRef("a" * 32)}, floats={"_Tiling": 4.0})
        transform = UVTransform((("_Tiling", 0), ("_Tiling", 0)), (0.0, 0.0))
        self.assertEqual(apply_uv_transforms(mat, {"_MainTex": transform}).textures["_MainTex"].scale, (4.0, 4.0))
        # マテリアルに値が無ければ置き換えない（グラフの既定値は読まない）
        self.assertIs(apply_uv_transforms(mat, {"_MainTex": BASE_UV}), mat)


if __name__ == "__main__":
    unittest.main()
