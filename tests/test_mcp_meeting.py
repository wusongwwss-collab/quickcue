# -*- coding: utf-8 -*-
"""MCP 真握手测试：以子进程 spawn hosts/mcp/meeting_server.py，
stdin 逐行喂 initialize → notifications/initialized → tools/list → tools/call，
按 JSON-RPC 2.0 逐条断言响应（比 dump-config 式静态检查硬）。

纪律：反馈落盘用 pytest tmp_path 作工作目录（QUICKCUE_FEEDBACK_DIR 指过去），
禁碰真实运行数据；子进程 UTF-8 显式（Windows 编码坑）。
"""

import json
import os
import queue
import subprocess
import sys
import threading

import pytest

PUB_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(PUB_ROOT, "hosts", "mcp", "meeting_server.py")

EXPECT_TOOLS = {"cue.analyze", "rules.validate", "feedback.record"}


class ServerHandle:
    """子进程封装：发送一条 JSON-RPC 消息，带超时收一行响应。"""

    def __init__(self, cwd, extra_env=None):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["QUICKCUE_FEEDBACK_DIR"] = os.path.join(cwd, ".quickcue")
        for k, v in (extra_env or {}).items():
            env[k] = v
        self.proc = subprocess.Popen(
            [sys.executable, "-X", "utf8", SERVER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, cwd=cwd, env=env,
            text=True, encoding="utf-8", bufsize=1)
        self._q = queue.Queue()
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self):
        for line in self.proc.stdout:
            self._q.put(line)

    def send(self, msg: dict):
        self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def recv(self, timeout=15.0) -> dict:
        line = self._q.get(timeout=timeout)
        return json.loads(line)

    def request(self, msg: dict, timeout=15.0) -> dict:
        self.send(msg)
        return self.recv(timeout)

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


@pytest.fixture()
def server(tmp_path):
    h = ServerHandle(cwd=str(tmp_path))
    yield h
    h.close()


def _req(req_id, method, params=None):
    return {"jsonrpc": "2.0", "id": req_id, "method": method,
            "params": params or {}}


def _text_payload(result: dict) -> dict:
    """tools/call 应答里的 content[0].text 还原成对象。"""
    assert result["isError"] is False
    blocks = result["content"]
    assert blocks and blocks[0]["type"] == "text"
    return json.loads(blocks[0]["text"])


def test_initialize_handshake(server):
    resp = server.request(_req(1, "initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "parity-test", "version": "0"},
    }))
    assert resp["jsonrpc"] == "2.0" and resp["id"] == 1
    assert "error" not in resp
    result = resp["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert result["serverInfo"]["name"] == "quickcue-meeting"
    assert "tools" in result["capabilities"]
    # 协议通知：initialized 不应答（下一条响应只可能来自后续请求）
    server.send({"jsonrpc": "2.0", "method": "notifications/initialized"})


def test_tools_list(server):
    server.request(_req(1, "initialize", {"protocolVersion": "2024-11-05"}))
    resp = server.request(_req(2, "tools/list"))
    names = {t["name"] for t in resp["result"]["tools"]}
    assert EXPECT_TOOLS <= names
    for t in resp["result"]["tools"]:
        assert t["inputSchema"]["type"] == "object"


def test_tools_call_cue_analyze_fires_card(server):
    server.request(_req(1, "initialize", {"protocolVersion": "2024-11-05"}))
    resp = server.request(_req(2, "tools/call", {
        "name": "cue.analyze",
        "arguments": {
            "utterance": "这个系统的部署周期要多久？",
            "role": "客户",
            "kb": ["标准两周内完成上线。", "可补充培训安排。"],
            "now": 1000.0,
            "utterance_id": "u-1",
        },
    }))
    payload = _text_payload(resp["result"])
    assert payload["fired"] is True and payload["skip_reason"] == ""
    assert len(payload["frames"]) == 2
    card = payload["frames"][0]["card"]
    assert card["subtype"] == "quick_cue"
    assert card["objection_type"] == "question"
    assert card["script"] == "标准两周内完成上线。"
    assert payload["frames"][1]["data"]["event_type"] == "quick_cue"


def test_tools_call_cue_analyze_skip_paths(server):
    server.request(_req(1, "initialize", {"protocolVersion": "2024-11-05"}))
    # role 非客户 → skip role
    resp = server.request(_req(2, "tools/call", {
        "name": "cue.analyze",
        "arguments": {"utterance": "随便问一句", "role": "销售", "now": 1000.0},
    }))
    payload = _text_payload(resp["result"])
    assert payload["fired"] is False and payload["skip_reason"] == "role"
    # 客户疑问但不命中演示知识库任何主题 → kb_miss
    resp = server.request(_req(3, "tools/call", {
        "name": "cue.analyze",
        "arguments": {"utterance": "这个功能是标配呢还是选配呀？", "role": "客户",
                      "now": 1000.0},
    }))
    payload = _text_payload(resp["result"])
    assert payload["fired"] is False and payload["skip_reason"] == "kb_miss"


def test_tools_call_cue_analyze_demo_kb_fallback(server):
    """零配置承诺闭合：未传 kb 且未配外部知识库 → 内置演示知识库兜底出卡。"""
    server.request(_req(1, "initialize", {"protocolVersion": "2024-11-05"}))
    resp = server.request(_req(2, "tools/call", {
        "name": "cue.analyze",
        "arguments": {"utterance": "部署到我们机房要多久？", "role": "客户",
                      "now": 1000.0, "utterance_id": "u-demo"},
    }))
    payload = _text_payload(resp["result"])
    assert payload["fired"] is True and payload["skip_reason"] == ""
    card = payload["frames"][0]["card"]
    assert card["kb_direct"] is True
    assert "两周" in card["script"]          # 演示库「交付周期」条目话术
    # 外部注入优先于演示库：传入 kb 时按注入候选出卡
    resp = server.request(_req(3, "tools/call", {
        "name": "cue.analyze",
        "arguments": {"utterance": "部署到我们机房要多久？", "role": "客户",
                      "kb": ["注入候选话术。"], "now": 2000.0},
    }))
    card = _text_payload(resp["result"])["frames"][0]["card"]
    assert card["script"] == "注入候选话术。"


def test_tools_call_cue_analyze_external_kb_file_priority(tmp_path):
    """外部知识库（QUICKCUE_KB_FILE）优先于内置演示库；坏文件视为未配置走兜底。"""
    kb_file = tmp_path / "kb.json"
    kb_file.write_text(json.dumps([
        {"keywords": ["部署", "多久"], "script": "外部库话术：30 天上线。"},
        "无条件候选话术。",
    ], ensure_ascii=False), encoding="utf-8")
    h = ServerHandle(cwd=str(tmp_path),
                     extra_env={"QUICKCUE_KB_FILE": str(kb_file)})
    try:
        h.request(_req(1, "initialize", {"protocolVersion": "2024-11-05"}))
        resp = h.request(_req(2, "tools/call", {
            "name": "cue.analyze",
            "arguments": {"utterance": "部署要多久？", "role": "客户", "now": 1000.0},
        }))
        card = _text_payload(resp["result"])["frames"][0]["card"]
        assert card["script"] == "外部库话术：30 天上线。"
    finally:
        h.close()
    # 配置了但文件不可读 ⇒ 视为未配置，回退演示库
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    h2 = ServerHandle(cwd=str(tmp_path), extra_env={"QUICKCUE_KB_FILE": str(bad)})
    try:
        h2.request(_req(1, "initialize", {"protocolVersion": "2024-11-05"}))
        resp = h2.request(_req(2, "tools/call", {
            "name": "cue.analyze",
            "arguments": {"utterance": "部署要多久？", "role": "客户", "now": 1000.0},
        }))
        card = _text_payload(resp["result"])["frames"][0]["card"]
        assert "两周" in card["script"]      # 演示库兜底生效
    finally:
        h2.close()


def test_tools_call_rules_validate(server):
    server.request(_req(1, "initialize", {"protocolVersion": "2024-11-05"}))
    good = {
        "global_cooldown_sec": 60, "same_type_cooldown_sec": 180,
        "max_per_meeting": 12, "question_words": ["？", "吗"],
        "customer_role": "客户",
        "templates": {"default": {"headline": "h"}, "sales": {"headline": "s"}},
    }
    resp = server.request(_req(2, "tools/call",
                               {"name": "rules.validate", "arguments": {"rules": good}}))
    payload = _text_payload(resp["result"])
    assert payload["valid"] is True and payload["errors"] == []
    bad = {"max_per_meeting": "十二", "question_words": [],
           "templates": {"unknown": {"headline": 1}}}
    resp = server.request(_req(3, "tools/call",
                               {"name": "rules.validate", "arguments": {"rules": bad}}))
    payload = _text_payload(resp["result"])
    assert payload["valid"] is False
    assert any("max_per_meeting" in e for e in payload["errors"])
    assert any("question_words" in e for e in payload["errors"])


def test_tools_call_feedback_record_writes_local_file(server, tmp_path):
    server.request(_req(1, "initialize", {"protocolVersion": "2024-11-05"}))
    resp = server.request(_req(2, "tools/call", {
        "name": "feedback.record",
        "arguments": {"feedback": {"cue_id": "u-1", "useful": False,
                                   "note": "卡片建议不符合现场口径"}},
    }))
    payload = _text_payload(resp["result"])
    assert payload["ok"] is True
    fb_file = os.path.join(str(tmp_path), ".quickcue", "feedback.jsonl")
    assert os.path.isfile(fb_file)
    with open(fb_file, encoding="utf-8") as f:
        lines = [json.loads(x) for x in f if x.strip()]
    assert len(lines) == 1
    assert lines[0]["cue_id"] == "u-1" and lines[0]["useful"] is False
    assert "recorded_at" in lines[0]


def test_unknown_method_and_tool_error(server):
    server.request(_req(1, "initialize", {"protocolVersion": "2024-11-05"}))
    resp = server.request(_req(2, "no/such/method"))
    assert resp["error"]["code"] == -32601
    resp = server.request(_req(3, "tools/call",
                               {"name": "no.such.tool", "arguments": {}}))
    assert resp["error"]["code"] == -32602
    # 参数校验错误也走 -32602 且进程存活（下一条仍可应答）
    resp = server.request(_req(4, "tools/call",
                               {"name": "cue.analyze", "arguments": {}}))
    assert resp["error"]["code"] == -32602
    resp = server.request(_req(5, "ping"))
    assert resp["result"] == {}


def test_bad_json_line_returns_parse_error(server):
    server.proc.stdin.write("this is not json\n")
    server.proc.stdin.flush()
    resp = server.recv()
    assert resp["error"]["code"] == -32700
    # 进程存活，可继续正常请求
    resp = server.request(_req(1, "initialize", {"protocolVersion": "2024-11-05"}))
    assert resp["result"]["protocolVersion"] == "2024-11-05"
