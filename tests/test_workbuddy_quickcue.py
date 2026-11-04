# -*- coding: utf-8 -*-
"""WorkBuddy 送审包机闸（YQ-1153）——源件格式 + 构建产物完整性 + 幂等。

判据：
  1. wb-skill/SKILL.md YAML 头：必填字段齐全；冒号后恰一空格；值不以引号包裹
  2. connector-meta.json：必填字段与 author 口径（弈枢AI Easur Work）；name 与 mcp.json 服务键一致
  3. mcp.json：command=python（内核为 Python 3.10+ 标准库）且 args 用 ${CONNECTOR_DIR} 占位
  4. build.mjs（需 node）：产物清单完整且连跑两次逐字节一致；缺 node 时本文件跳过第 4 组
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WB = os.path.join(ROOT, "hosts", "workbuddy")
DIST = os.path.join(ROOT, "dist")

FIELDS = ["name", "display_name", "display_name_en", "description",
          "description_zh", "description_en", "category", "version", "author"]

NODE = shutil.which("node")


def _head():
    with open(os.path.join(WB, "wb-skill", "SKILL.md"), encoding="utf-8") as f:
        return f.read().split("---")[1]


@pytest.mark.parametrize("field", FIELDS)
def test_skill_head_field(field):
    assert re.search(rf"^{field}: .+", _head(), re.M), field


def test_skill_head_spacing_and_no_quotes():
    lines = [l for l in _head().split("\n") if re.match(r"^\S", l)]
    assert all(re.match(r"^[\w_]+: ", l) for l in lines)
    assert not any(re.match(r'^[\w_]+: ["\']', l) for l in lines)


def test_skill_naming_and_author():
    h = _head()
    assert re.search(r"display_name: 会中辅助", h)
    assert re.search(r"author: 弈枢AI Easur Work", h)


def _meta():
    with open(os.path.join(WB, "wb-connector", "connector-meta.json"), encoding="utf-8") as f:
        return json.load(f)


@pytest.mark.parametrize("field", ["name", "source", "display_name", "display_name_en", "name_en",
                                   "description_zh", "category", "version", "author", "type",
                                   "examples_zh", "examples_en"])
def test_meta_fields(field):
    assert _meta().get(field)


def test_meta_author_and_type():
    assert _meta()["author"] == "弈枢AI Easur Work"
    assert _meta()["type"] == "mcp"
    assert _meta()["name"] == "quickcue"


def test_mcp_json():
    with open(os.path.join(WB, "wb-connector", "mcp.json"), encoding="utf-8") as f:
        mcp = json.load(f)
    assert list(mcp["mcpServers"]) == [_meta()["name"]]
    entry = mcp["mcpServers"]["quickcue"]
    assert entry["command"] == "python"
    assert "${CONNECTOR_DIR}" in entry["args"][0]


MUST_EXIST = [
    ("wb-skill", "SKILL.md"), ("wb-skill", "references", "README.md"),
    ("wb-skill", "templates", "rules.example.yaml"), ("wb-skill", "LICENSE"),
    ("wb-connector", "connector-meta.json"), ("wb-connector", "mcp.json"),
    ("wb-connector", "icon.svg"), ("wb-connector", "hosts", "mcp", "meeting_server.py"),
    ("wb-connector", "core", "py", "quickcue_engine.py"), ("wb-connector", "core", "py", "feedback.py"),
    ("wb-connector", "skills", "SKILL.md"), ("wb-connector", "LICENSE"),
]


def _hash_tree():
    out = []
    for base, _dirs, files in os.walk(DIST):
        for fn in files:
            p = os.path.join(base, fn)
            rel = os.path.relpath(p, DIST).replace("\\", "/")
            out.append(rel + ":" + hashlib.sha256(open(p, "rb").read()).hexdigest()[:16])
    return "|".join(sorted(out))


@pytest.mark.skipif(NODE is None, reason="node 不可用（CI 已配 setup-node）")
def test_build_complete_and_idempotent():
    subprocess.run([NODE, os.path.join(WB, "build.mjs")], cwd=ROOT, check=True,
                   capture_output=True)
    for seg in MUST_EXIST:
        assert os.path.isfile(os.path.join(DIST, *seg)), seg
    h1 = _hash_tree()
    subprocess.run([NODE, os.path.join(WB, "build.mjs")], cwd=ROOT, check=True,
                   capture_output=True)
    assert h1 == _hash_tree()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
