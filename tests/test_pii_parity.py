# -*- coding: utf-8 -*-
"""YQ-1154 T1 机闸 G2：detect_residue × 服务端实现 同 fixtures 输出全等。

判据（任务书 T1 · G2；回流规范 v1 §7.2 闸①/§8 G2）：quickcue detect_residue 与
server 端实现同 fixtures 输出全等，fixtures 逐字取自主仓 D1 测试件
tests/test_oss_feedback_routes.py::_RESIDUE_FIXTURES（7 条文本）＋结构化补充面
（递归扫 dict/list，D1 经端到端 422 已覆盖，此处显式对拍）。

两段判据：
  1) 恒跑段：本仓 SDK detect_residue 输出 == D1 夹具期望值（canonical fixtures 钉死
     SDK 侧，公开仓 CI 无主仓也跑）；
  2) parity 段（开发期机闸）：服务端正本 app/oss_feedback_routes.py::detect_residue
     经 importlib 按路径加载，同 fixtures 输出与 SDK 全等。服务端缺席或 fastapi
     不可用时 parity 段整体 skip（公开仓 CI 不依赖主仓在场；
     NW_OSS_FEEDBACK_SERVER_ROOT 可覆盖主仓路径）。

纪律：零第三方依赖（pytest + 标准库）；固定夹具，禁依赖网络与运行数据目录。
"""

import importlib.util
import os
import sys

_PUB_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CORE_PY = os.path.join(_PUB_ROOT, "core", "py")
if _CORE_PY not in sys.path:
    sys.path.insert(0, _CORE_PY)

import pytest

from feedback import detect_residue as sdk_detect

# ---------------------------------------------------------------------------
# fixtures：逐字取自主仓 D1 测试件 _RESIDUE_FIXTURES（2026-11-05 版）
# ---------------------------------------------------------------------------
TEXT_FIXTURES = [
    ("回拨 13812345678 确认", ["phone"]),
    ("发到 someone@example.com", ["email"]),
    ("13812345678 与 a@b.co 都有", ["phone", "email"]),
    ("12345678901 不是手机号（1[0-2] 段不命中）", []),
    ("138123456789 连写 12 位不命中（边界断言）", []),
    ("客户 <PHONE_1> 的 <EMAIL_1> 已槽位化", []),
    ("正常中文样例，无联系方式", []),
]
# 结构化补充面（递归扫 dict/list；SDK 与服务端同为递归实现）
STRUCT_FIXTURES = [
    ({"a": ["回拨 13812345678 确认"], "b": {"c": "发到 someone@example.com"}},
     ["phone", "email"]),
    ([{"samples": [{"reason": "E-203", "sample": "正常中文样例，无联系方式"}]}], []),
]

SERVER_ROOT = os.environ.get("NW_OSS_FEEDBACK_SERVER_ROOT") or r"C:\yice\negotiation-workbench"
SERVER_MODULE = os.path.join(SERVER_ROOT, "app", "oss_feedback_routes.py")


def _server_detect():
    """服务端 detect_residue（按路径加载正本；不可用返回 None → parity 段 skip）。"""
    if not os.path.isfile(SERVER_MODULE):
        return None
    try:
        spec = importlib.util.spec_from_file_location(
            "_oss_feedback_server_g2", SERVER_MODULE)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.detect_residue
    except Exception:                                        # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# 恒跑段：SDK vs D1 canonical fixtures
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text, expected", TEXT_FIXTURES,
                         ids=[f"t{i}" for i in range(len(TEXT_FIXTURES))])
def test_sdk_matches_d1_text_fixtures(text, expected):
    assert sdk_detect(text) == expected, f"SDK 漂移（D1 夹具）：{text!r}"


@pytest.mark.parametrize("node, expected", STRUCT_FIXTURES,
                         ids=["struct-hit", "struct-clean"])
def test_sdk_matches_d1_struct_fixtures(node, expected):
    assert sdk_detect(node) == expected, f"SDK 漂移（结构化夹具）：{node!r}"


# ---------------------------------------------------------------------------
# parity 段：SDK vs 服务端正本（同 fixtures 全等）
# ---------------------------------------------------------------------------
def test_server_parity_same_fixtures():
    server = _server_detect()
    if server is None:
        pytest.skip(
            "服务端正本不可达（parity 段跳过；设 NW_OSS_FEEDBACK_SERVER_ROOT 指向主仓后启用）："
            + SERVER_MODULE)
    for text, expected in TEXT_FIXTURES:
        got_server = server(text)
        got_sdk = sdk_detect(text)
        assert got_server == got_sdk == expected, \
            f"G2 parity 破损：{text!r} server={got_server} sdk={got_sdk} 期望={expected}"
    for node, expected in STRUCT_FIXTURES:
        assert server(node) == sdk_detect(node) == expected, \
            f"G2 parity 破损（结构化）：{node!r}"
