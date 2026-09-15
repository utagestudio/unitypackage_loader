"""シェーダーノードの組み立てで共通に使う小さな関数。"""

from __future__ import annotations


def socket(sockets, identifier: str):
    """``identifier`` でソケットを引く。

    ``ShaderNodeMix`` は型ごとに同名のソケット（Factor / A / B / Result）を持つので、名前ではなく
    ``Factor_Float`` / ``A_Color`` のような identifier で引く。見つからなければ名前で引く。
    """
    for s in sockets:
        if s.identifier == identifier:
            return s
    return sockets[identifier]
