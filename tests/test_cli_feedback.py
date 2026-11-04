# -*- coding: utf-8 -*-
"""终端反馈管理 CLI 机闸（tests/test_cli_feedback.py；YQ-1154 D3 席）。

判据面（hosts/cli/feedback_cli.py 五子命令 + 上行契约）：
  1) on/off：命令即同意动作；state.json 含 schema_version+ts；schema 变更须重问
  2) show：明示文件路径与未发送态（纯本地）
  3) send 全链（本地 mock HTTP 真连）：body 顶层 {"events":[...]} 包裹、信封八字段
     平铺 + payload 嵌套、L1 只含非空 event、L2/L3 剥离 event 键、nonce 32hex、
     schema_version 字符串 "1"、X-OSS-Signature 可验签（t.v1=HMAC-SHA256）
  4) 游标 sent.offset：逐条推进；200/deduped 都算送达；重跑不重发
  5) 422/400/413 单条永久跳过标 rejected（reason 码透传）；本地残留检测跳过不联网
  6) 404/401 配置类中止不烧记录（退出码 3、不重试）；5xx/429 退避重试 ≤3（退出码 5）
  7) TTY 降级：y/N 仅 TTY；非 TTY 未带 --yes 退出码 4 且零请求
  8) 端点 env NW_OSS_FEEDBACK_ENDPOINT 覆盖（缺省 easurwork.cn 正式端点）

纪律：pytest 夹具一律 tmp_path（QUICKCUE_FEEDBACK_DIR 指向 tmp）；不依赖工作区
真实状态；零第三方依赖（仅 pytest + 标准库 + 仓内 core/py/feedback.py）。
"""

import hashlib
import hmac
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

PUB_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (os.path.join(PUB_ROOT, "core", "py"), os.path.join(PUB_ROOT, "hosts", "cli")):
    if p not in sys.path:
        sys.path.insert(0, p)

import feedback_cli as cli  # noqa: E402

SECRET = "test-hmac-secret"


# ---------------------------------------------------------------------------
# 本地 mock HTTP 服务器（真连全链：urllib → 127.0.0.1 → 校验签名 → 按剧本回包）
# ---------------------------------------------------------------------------
def _sig_ok(secret, raw, header):
    m = re.match(r"^t=(\d+),v1=([0-9a-f]{64})$", header or "")
    if not m:
        return False
    expected = hmac.new(secret.encode("utf-8"), m.group(1).encode("utf-8") + b"." + raw,
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, m.group(2))


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):                                   # noqa: N802
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        srv = self.server
        with srv.lock:
            srv.requests.append({
                "path": self.path,
                "raw": raw,
                "body": json.loads(raw.decode("utf-8")) if raw else None,
                "sig": self.headers.get("X-OSS-Signature", ""),
                "sig_ok": _sig_ok(srv.secret, raw, self.headers.get("X-OSS-Signature", "")),
            })
            status, payload = (srv.responses.pop(0) if srv.responses
                               else (200, {"accepted": 1, "deduped": 0}))
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):                        # 静默
        pass


@pytest.fixture()
def mock_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.secret = SECRET
    srv.requests = []
    srv.responses = []
    srv.lock = threading.Lock()
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture()
def fbhome(tmp_path, monkeypatch):
    """隔离反馈目录 + 固定 HMAC + 退避归零（测试不真实 sleep）。"""
    d = tmp_path / ".quickcue"
    monkeypatch.setenv("QUICKCUE_FEEDBACK_DIR", str(d))
    monkeypatch.setenv("NW_OSS_FEEDBACK_HMAC", SECRET)
    monkeypatch.delenv("NW_OSS_FEEDBACK_ENDPOINT", raising=False)
    monkeypatch.delenv("QUICKCUE_PRODUCT_VERSION", raising=False)
    monkeypatch.setattr(cli, "BACKOFF_BASE", 0.0)
    return d


def seed(fbhome, *objs):
    p = fbhome / "feedback.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        for obj in objs:
            if isinstance(obj, str):
                f.write(obj + "\n")
            else:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def run_cli(argv, capsys=None):
    code = cli.main(argv)
    out = capsys.readouterr().out if capsys is not None else ""
    return code, out


# ---------------------------------------------------------------------------
# 1) on / off：命令即同意动作；state 结构与 schema 变更重问
# ---------------------------------------------------------------------------
def test_on_default_l1_writes_consent_state(fbhome, capsys):
    code, out = run_cli(["on"], capsys)
    assert code == 0
    state = json.load(open(fbhome / "state.json", encoding="utf-8"))
    assert state["schema_version"] == "1"
    assert state["consent"] is True and state["consent_asked"] is True
    assert state["level"] == "L1"
    assert state["updated_at"]
    assert "easurwork.cn/open-source/feedback" in out


def test_on_explicit_level_and_bad_level(fbhome, capsys):
    code, _ = run_cli(["on", "L3"], capsys)
    assert code == 0
    assert json.load(open(fbhome / "state.json", encoding="utf-8"))["level"] == "L3"
    code, _ = run_cli(["on", "L9"], capsys)              # 非法档位 → 用法错误
    assert code == 2


def test_on_bare_reopens_with_previous_level(fbhome, capsys):
    run_cli(["on", "L2"], capsys)
    run_cli(["off"], capsys)
    code, _ = run_cli(["on"], capsys)                    # 关过再开：沿用 L2
    assert code == 0
    assert json.load(open(fbhome / "state.json", encoding="utf-8"))["level"] == "L2"


def test_off_closes_but_keeps_level(fbhome, capsys):
    run_cli(["on", "L2"], capsys)
    code, out = run_cli(["off"], capsys)
    assert code == 0
    state = json.load(open(fbhome / "state.json", encoding="utf-8"))
    assert state["consent"] is False
    assert state["level"] == "L2"                        # 记住档位，on 可沿用


def test_state_schema_mismatch_requires_reask(fbhome, capsys):
    fbhome.mkdir(parents=True)
    with open(fbhome / "state.json", "w", encoding="utf-8") as f:
        json.dump({"schema_version": "0", "consent": True, "consent_asked": True,
                   "level": "L3"}, f)
    seed(fbhome, {"event_type": "card_feedback", "result": "对"})
    code, _ = run_cli(["send", "--yes"], capsys)         # schema 变更 ⇒ 同意位归零须重问
    assert code == 3


# ---------------------------------------------------------------------------
# 2) show：明示路径与未发送态（纯本地）
# ---------------------------------------------------------------------------
def test_show_lists_pending_with_path(fbhome, capsys):
    seed(fbhome,
         {"event_type": "card_feedback", "result": "不对", "recorded_at": 1730000000.0},
         {"level": "L2", "payload": {"rule_delta": {"rule_id": "gate.cool", "field": "sec",
                                                    "before": 60, "after": 90}}},
         {"samples": [{"reason": "判错", "sample": "样例"}]})
    code, out = run_cli(["show"], capsys)
    assert code == 0
    assert "待上行 3 条" in out
    assert str(fbhome / "feedback.jsonl") in out
    assert "尚未发送" in out
    assert "评价：不对" in out and "建议 gate.cool 60→90" in out and "失败样例 1 条" in out
    assert "[feedback send 上行]" in out


def test_show_empty(fbhome, capsys):
    code, out = run_cli(["show"], capsys)
    assert code == 0
    assert "本机没有待上行反馈" in out


# ---------------------------------------------------------------------------
# 3) send 前置闸：同意位 / HMAC / 非 TTY 确认
# ---------------------------------------------------------------------------
def test_send_requires_consent(fbhome, capsys):
    seed(fbhome, {"event_type": "card_feedback", "result": "对"})
    code, out = run_cli(["send", "--yes"], capsys)       # 未 on → 配置中止
    assert code == 3
    assert "feedback on" in out


def test_send_requires_hmac_env(fbhome, capsys, monkeypatch):
    run_cli(["on", "L1"], capsys)
    seed(fbhome, {"event_type": "card_feedback", "result": "对"})
    monkeypatch.delenv("NW_OSS_FEEDBACK_HMAC", raising=False)
    code, out = run_cli(["send", "--yes"], capsys)
    assert code == 3
    assert "NW_OSS_FEEDBACK_HMAC" in out


def test_send_non_tty_without_yes_exit_4_and_no_request(fbhome, capsys, mock_server, monkeypatch):
    monkeypatch.setenv("NW_OSS_FEEDBACK_ENDPOINT", "http://127.0.0.1:%d/api/oss-feedback/v1"
                       % mock_server.server_address[1])
    run_cli(["on", "L1"], capsys)
    seed(fbhome, {"event_type": "card_feedback", "result": "对"})
    code, out = run_cli(["send"], capsys)                # pytest stdin 非 TTY 且无 --yes
    assert code == 4
    assert "--yes" in out
    assert mock_server.requests == []                    # 一条都不发


def test_send_tty_declined_sends_nothing(fbhome, capsys, mock_server, monkeypatch):
    monkeypatch.setenv("NW_OSS_FEEDBACK_ENDPOINT", "http://127.0.0.1:%d/api/oss-feedback/v1"
                       % mock_server.server_address[1])
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    run_cli(["on", "L1"], capsys)
    seed(fbhome, {"event_type": "card_feedback", "result": "对"})
    code, out = run_cli(["send"], capsys)
    assert code == 0
    assert "已取消" in out
    assert mock_server.requests == []
    assert cli.read_cursor() == 0


def test_send_nothing_pending(fbhome, capsys):
    run_cli(["on", "L1"], capsys)
    code, out = run_cli(["send", "--yes"], capsys)
    assert code == 0


# ---------------------------------------------------------------------------
# 4) send 全链 200：信封/签名/游标/回执
# ---------------------------------------------------------------------------
def _endpoint_of(mock_server):
    return "http://127.0.0.1:%d/api/oss-feedback/v1" % mock_server.server_address[1]


def test_send_full_chain_envelope_signature_cursor_receipt(fbhome, capsys, mock_server, monkeypatch):
    monkeypatch.setenv("NW_OSS_FEEDBACK_ENDPOINT", _endpoint_of(mock_server))
    run_cli(["on", "L1"], capsys)
    seed(fbhome, {"event_type": "card_feedback", "result": "对", "card_type": "客户问题速答"})
    code, out = run_cli(["send", "--yes"], capsys)
    assert code == 0
    assert len(mock_server.requests) == 1
    req = mock_server.requests[0]
    assert req["path"] == "/api/oss-feedback/v1"
    assert req["sig_ok"] is True                          # HMAC 头真实可验（t.v1）
    body = req["body"]
    assert set(body.keys()) == {"events"} and len(body["events"]) == 1   # ② 顶层包裹
    ev = body["events"][0]
    assert set(ev.keys()) == {"schema_version", "product", "product_version", "channel",
                              "feedback_level", "nonce", "ts", "payload"}
    assert ev["schema_version"] == "1" and isinstance(ev["schema_version"], str)  # ①
    assert ev["product"] == "quickcue" and ev["channel"] == "github_npm"
    assert ev["feedback_level"] == "L1"
    assert re.fullmatch(r"[0-9a-f]{32}", ev["nonce"])     # secrets.token_hex(16)
    assert ev["payload"] == {"event": {"type": "card_feedback", "result": "对",
                                       "card_type": "客户问题速答"}}               # ③ L1
    # 游标推进 + 回执落盘（回执码 = nonce）
    assert cli.read_cursor() == 1
    receipt_path = fbhome / "sentreceipts" / ("%s.json" % ev["nonce"])
    assert receipt_path.is_file()
    receipt = json.load(open(receipt_path, encoding="utf-8"))
    assert receipt["response"] == {"accepted": 1, "deduped": 0}
    assert "已上行 1 条" in out and ev["nonce"][:8] in out


def test_send_cursor_rerun_does_not_resend(fbhome, capsys, mock_server, monkeypatch):
    monkeypatch.setenv("NW_OSS_FEEDBACK_ENDPOINT", _endpoint_of(mock_server))
    run_cli(["on", "L1"], capsys)
    seed(fbhome,
         {"event_type": "card_feedback", "result": "对"},
         {"event_type": "card_feedback", "result": "已处理"})
    code, _ = run_cli(["send", "--yes"], capsys)
    assert code == 0
    assert len(mock_server.requests) == 2 and cli.read_cursor() == 2
    code, out = run_cli(["send", "--yes"], capsys)        # 重跑：无可上行
    assert code == 0
    assert len(mock_server.requests) == 2                 # 不重发
    assert "没有可上行条目" in out


def test_send_deduped_counts_as_delivered(fbhome, capsys, mock_server, monkeypatch):
    monkeypatch.setenv("NW_OSS_FEEDBACK_ENDPOINT", _endpoint_of(mock_server))
    mock_server.responses.append((200, {"accepted": 0, "deduped": 1}))
    run_cli(["on", "L1"], capsys)
    seed(fbhome, {"event_type": "card_feedback", "result": "对"})
    code, out = run_cli(["send", "--yes"], capsys)
    assert code == 0
    assert cli.read_cursor() == 1                         # 去重命中同样推进游标
    assert "已上行 1 条" in out


# ---------------------------------------------------------------------------
# 5) 档位面：L1/L2/L3 信封剥离规则 + L3 本地脱敏
# ---------------------------------------------------------------------------
def test_send_levels_shape_l1_l2_l3(fbhome, capsys, mock_server, monkeypatch):
    monkeypatch.setenv("NW_OSS_FEEDBACK_ENDPOINT", _endpoint_of(mock_server))
    run_cli(["on", "L3"], capsys)
    seed(fbhome,
         {"event_type": "card_feedback", "result": "不对"},                     # MCP 自由形 → L1
         {"level": "L2", "payload": {"event_type": "card_feedback", "result": "已处理",
                                     "rule_delta": {"rule_id": "gate.cool", "field": "sec",
                                                    "before": 60, "after": 90}}},
         {"level": "L3", "payload": {"samples": [{"reason": "判错",
                                                  "sample": "客户 王建国 13812345678 说预算 50万"}]}})
    code, out = run_cli(["send", "--yes"], capsys)
    assert code == 0
    assert len(mock_server.requests) == 3
    l1, l2, l3 = [r["body"]["events"][0] for r in mock_server.requests]
    # L1：只含非空 event
    assert l1["feedback_level"] == "L1"
    assert set(l1["payload"].keys()) == {"event"} and l1["payload"]["event"]["result"] == "不对"
    # L2：剥离 event 键，只留 rule_delta
    assert l2["feedback_level"] == "L2"
    assert "event" not in l2["payload"]
    assert l2["payload"] == {"rule_delta": {"rule_id": "gate.cool", "field": "sec",
                                            "before": 60, "after": 90}}
    # L3：samples（本地已脱敏）
    assert l3["feedback_level"] == "L3"
    assert "event" not in l3["payload"]
    sample = json.dumps(l3["payload"], ensure_ascii=False)
    assert "王建国" not in sample and "13812345678" not in sample and "50万" not in sample
    assert "<PERSON_1>" in sample and "<PHONE_1>" in sample and "<AMOUNT_1>" in sample
    assert all(r["sig_ok"] for r in mock_server.requests)


def test_send_mcp_freeform_record_strips_bookkeeping(fbhome, capsys, mock_server, monkeypatch):
    monkeypatch.setenv("NW_OSS_FEEDBACK_ENDPOINT", _endpoint_of(mock_server))
    run_cli(["on", "L1"], capsys)
    seed(fbhome, {"event_type": "card_feedback", "result": "对", "recorded_at": 1730000000.0})
    code, _ = run_cli(["send", "--yes"], capsys)
    assert code == 0
    ev = mock_server.requests[0]["body"]["events"][0]
    assert "recorded_at" not in json.dumps(ev)            # 记账键不入上行载荷
    assert ev["payload"]["event"]["result"] == "对"       # ⑤ 中文 result 直上


def test_send_local_residue_reject_never_hits_network(fbhome, capsys, mock_server, monkeypatch):
    monkeypatch.setenv("NW_OSS_FEEDBACK_ENDPOINT", _endpoint_of(mock_server))
    run_cli(["on", "L1"], capsys)
    seed(fbhome, {"event_type": "card_feedback", "result": "电话13812345678"})  # 残留
    code, out = run_cli(["send", "--yes"], capsys)
    assert code == 0
    assert mock_server.requests == []                     # 本地拦截，零请求
    ledger = open(fbhome / "sentreceipts" / "rejected.jsonl", encoding="utf-8").read()
    assert "residue_detected(phone)" in ledger
    assert cli.read_cursor() == 1
    assert "本地校验永久跳过 1 条" in out


def test_send_bad_json_line_ledgered_not_silent(fbhome, capsys, mock_server, monkeypatch):
    monkeypatch.setenv("NW_OSS_FEEDBACK_ENDPOINT", _endpoint_of(mock_server))
    run_cli(["on", "L1"], capsys)
    seed(fbhome, "{不是json", {"event_type": "card_feedback", "result": "对"})
    code, _ = run_cli(["send", "--yes"], capsys)
    assert code == 0
    ledger = open(fbhome / "sentreceipts" / "rejected.jsonl", encoding="utf-8").read()
    assert "unreadable_line" in ledger
    assert cli.read_cursor() == 2 and len(mock_server.requests) == 1


def test_send_level_above_consent_marked_rejected(fbhome, capsys, mock_server, monkeypatch):
    monkeypatch.setenv("NW_OSS_FEEDBACK_ENDPOINT", _endpoint_of(mock_server))
    run_cli(["on", "L1"], capsys)
    seed(fbhome, {"level": "L2", "payload": {"rule_delta": {"rule_id": "r", "field": "f",
                                                            "before": 1, "after": 2}}})
    code, out = run_cli(["send", "--yes"], capsys)
    assert code == 0
    assert mock_server.requests == []                     # 不得超出同意范围
    ledger = open(fbhome / "sentreceipts" / "rejected.jsonl", encoding="utf-8").read()
    assert "level_below_consent" in ledger
    assert cli.read_cursor() == 1


# ---------------------------------------------------------------------------
# 6) 服务端 4xx/5xx 处置（D2 对齐语义）
# ---------------------------------------------------------------------------
def test_send_422_marks_single_record_rejected(fbhome, capsys, mock_server, monkeypatch):
    monkeypatch.setenv("NW_OSS_FEEDBACK_ENDPOINT", _endpoint_of(mock_server))
    mock_server.responses.append(
        (422, {"ok": False, "msg": "validation_failed",
               "errors": [{"index": 0, "reason": "residue:phone"}]}))
    run_cli(["on", "L1"], capsys)
    seed(fbhome, {"event_type": "card_feedback", "result": "对"})
    code, out = run_cli(["send", "--yes"], capsys)
    assert code == 0                                      # 4xx 单条跳过不算失败退出
    assert cli.read_cursor() == 1                         # 游标推进
    ledger = open(fbhome / "sentreceipts" / "rejected.jsonl", encoding="utf-8").read()
    assert "residue:phone" in ledger                      # 服务端 reason 码透传
    assert "已永久跳过" in out


def test_send_404_config_abort_no_burn(fbhome, capsys, monkeypatch):
    calls = []

    def fake_post(url, raw, signature, timeout=15.0):
        calls.append(url)
        return 404, b"not found"

    monkeypatch.setattr(cli, "_http_post", fake_post)
    run_cli(["on", "L1"], capsys)
    seed(fbhome, {"event_type": "card_feedback", "result": "对"})
    code, out = run_cli(["send", "--yes"], capsys)
    assert code == 3                                      # ⑦ 404 = 配置类中止
    assert len(calls) == 1                                # 不重试
    assert cli.read_cursor() == 0                         # 不烧记录
    assert not (fbhome / "sentreceipts" / "rejected.jsonl").exists()
    assert "未烧任何记录" in out


def test_send_401_config_abort_no_burn(fbhome, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_http_post", lambda url, raw, sig, timeout=15.0: (401, b"{}"))
    run_cli(["on", "L1"], capsys)
    seed(fbhome, {"event_type": "card_feedback", "result": "对"})
    code, _ = run_cli(["send", "--yes"], capsys)
    assert code == 3 and cli.read_cursor() == 0


def test_send_5xx_backoff_retry_max_then_abort(fbhome, capsys, monkeypatch):
    calls = []

    def fake_post(url, raw, signature, timeout=15.0):
        calls.append(1)
        return 500, b'{"ok":false,"msg":"boom"}'

    monkeypatch.setattr(cli, "_http_post", fake_post)
    run_cli(["on", "L1"], capsys)
    seed(fbhome, {"event_type": "card_feedback", "result": "对"})
    code, out = run_cli(["send", "--yes"], capsys)
    assert code == 5                                      # 重试穷尽 → 网络失败
    assert len(calls) == 1 + cli.RETRY_MAX                # 首发一次 + 退避重试 ≤3
    assert cli.read_cursor() == 0                         # 不烧记录
    assert "稍后重跑 send" in out


def test_send_429_retry_then_success(fbhome, capsys, monkeypatch):
    seq = [(429, b'{"ok":false,"msg":"rate_limited"}'),
           (200, b'{"accepted":1,"deduped":0}')]
    calls = []

    def fake_post(url, raw, signature, timeout=15.0):
        calls.append(1)
        return seq.pop(0)

    monkeypatch.setattr(cli, "_http_post", fake_post)
    run_cli(["on", "L1"], capsys)
    seed(fbhome, {"event_type": "card_feedback", "result": "对"})
    code, out = run_cli(["send", "--yes"], capsys)
    assert code == 0
    assert len(calls) == 2
    assert cli.read_cursor() == 1                         # 第二次重试成功 → 送达


def test_send_mixed_batch_cursor_never_regresses(fbhome, capsys, mock_server, monkeypatch):
    """本地拒（第2条）+ 待发（第1条）混批：游标单调推进，无回退重复记账。"""
    monkeypatch.setenv("NW_OSS_FEEDBACK_ENDPOINT", _endpoint_of(mock_server))
    run_cli(["on", "L1"], capsys)
    seed(fbhome,
         {"event_type": "card_feedback", "result": "对"},                          # 可发
         {"level": "L2", "payload": {"rule_delta": {"rule_id": "r", "field": "f",
                                                    "before": 1, "after": 2}}})   # 超档本地拒
    code, _ = run_cli(["send", "--yes"], capsys)
    assert code == 0
    assert len(mock_server.requests) == 1
    assert cli.read_cursor() == 2
    code, out = run_cli(["send", "--yes"], capsys)        # 重跑：无重发、无重复记账
    assert code == 0
    assert len(mock_server.requests) == 1
    ledger = open(fbhome / "sentreceipts" / "rejected.jsonl", encoding="utf-8").read()
    assert ledger.count("level_below_consent") == 1


# ---------------------------------------------------------------------------
# 7) 端点覆盖与缺省（⑥）
# ---------------------------------------------------------------------------
def test_default_endpoint_is_official(monkeypatch):
    monkeypatch.delenv("NW_OSS_FEEDBACK_ENDPOINT", raising=False)
    assert cli.DEFAULT_ENDPOINT == "https://www.easurwork.cn/api/oss-feedback/v1"
    assert cli.resolve_endpoint() == cli.DEFAULT_ENDPOINT


def test_endpoint_env_override_used_in_send(monkeypatch):
    monkeypatch.setenv("NW_OSS_FEEDBACK_ENDPOINT", "http://127.0.0.1:6112/api/oss-feedback/v1")
    assert cli.resolve_endpoint() == "http://127.0.0.1:6112/api/oss-feedback/v1"


# ---------------------------------------------------------------------------
# 8) clear：清空待上行 + 游标/台账重置，回执码台账保留
# ---------------------------------------------------------------------------
def test_clear_keeps_receipts(fbhome, capsys):
    run_cli(["on", "L1"], capsys)
    seed(fbhome, {"event_type": "card_feedback", "result": "对"})
    os.makedirs(fbhome / "sentreceipts", exist_ok=True)
    with open(fbhome / "sentreceipts" / ("a" * 32 + ".json"), "w", encoding="utf-8") as f:
        json.dump({"nonce": "a" * 32}, f)
    cli.append_rejected(0, "L1", "residue:phone")
    cli.write_cursor(1)
    code, out = run_cli(["clear"], capsys)
    assert code == 0
    assert not (fbhome / "feedback.jsonl").exists()
    assert not (fbhome / "sent.offset").exists()
    assert not (fbhome / "sentreceipts" / "rejected.jsonl").exists()
    assert (fbhome / "sentreceipts" / ("a" * 32 + ".json")).is_file()   # 回执保留
    assert cli.read_cursor() == 0
    assert "回执码台账" in out


# ---------------------------------------------------------------------------
# 9) 签名函数单测（与服务端正本同式）
# ---------------------------------------------------------------------------
def test_sign_header_shape():
    raw = b'{"events":[]}'
    header = cli._sign(SECRET, raw)
    m = re.match(r"^t=(\d+),v1=([0-9a-f]{64})$", header)
    assert m
    expected = hmac.new(SECRET.encode("utf-8"), m.group(1).encode("utf-8") + b"." + raw,
                        hashlib.sha256).hexdigest()
    assert m.group(2) == expected
