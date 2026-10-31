# -*- coding: utf-8 -*-
"""反馈 SDK 机闸（tests/test_feedback.py）。

判据面（反馈三档 + 隐私约定）：
  1) 默认 L0（关）：record 直接丢弃返回 dropped:True，且不产生落盘文件
  2) 首启问一次：should_ask_consent 状态机（consent_asked 标志置位后永不再问）
  3) L1 上行载荷最小面：只含事件类型/结果卡点选值/卡类型/时间戳/包版本
  4) L2 规则 delta：只含白名单字段（rule_id/field/before/after），越权字段拒绝
  5) L3 sanitize 槽位化：手机/金额/人名/公司/邮箱各 >=2 例，同序号去重
  6) 原文残留检测：载荷中含未槽位化手机号/邮箱 -> rejected:True + reason

纪律：pytest 夹具一律 tmp_path（测试数据隔离），不依赖工作区真实状态；
SDK 落盘目录显式注入 tmp 路径，另用 monkeypatch 验证 QUICKCUE_FEEDBACK_DIR
环境变量覆盖面。零第三方依赖（仅 pytest 测试框架 + 标准库断言）。
"""

import json
import os
import sys

import pytest

PUB_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(PUB_ROOT, "core", "py") not in sys.path:
    sys.path.insert(0, os.path.join(PUB_ROOT, "core", "py"))

from feedback import (  # noqa: E402
    FeedbackSdk,
    detect_residue,
    sanitize,
    should_ask_consent,
)


@pytest.fixture()
def fb_dir(tmp_path):
    d = tmp_path / "quickcue-fb"
    d.mkdir()
    return str(d)


@pytest.fixture()
def sdk_l1(fb_dir):
    return FeedbackSdk(level="L1", feedback_dir=fb_dir, package_version="0.1.0", now=1730000000)


# ------------------------------------------------------------------
# 1) 默认 L0 = 关：丢弃且不落盘
# ------------------------------------------------------------------
def test_default_l0_record_drops_and_writes_nothing(fb_dir):
    sdk = FeedbackSdk(feedback_dir=fb_dir)
    assert sdk.level == "L0"
    r = sdk.record("L1", {"event_type": "card_feedback", "result": "对"})
    assert r["dropped"] is True
    assert "reason" in r
    assert not os.path.exists(os.path.join(fb_dir, "feedback.jsonl"))


def test_default_l0_prepare_upload_rejects(fb_dir):
    sdk = FeedbackSdk(feedback_dir=fb_dir)
    r = sdk.prepare_upload("L1", {"event_type": "card_feedback", "result": "对"})
    assert r["ok"] is False
    assert r["rejected"] is True
    assert "L0" in r["reason"]


def test_l0_can_be_reopened_and_closed_again(fb_dir):
    sdk = FeedbackSdk(feedback_dir=fb_dir)
    sdk.set_level("L2")
    assert sdk.record("L2", {"rule_delta": {"rule_id": "r", "field": "f", "before": 1, "after": 2}})["dropped"] is False
    sdk.set_level("L0")  # 随时可关
    assert sdk.record("L2", {"rule_delta": {"rule_id": "r", "field": "f", "before": 1, "after": 2}})["dropped"] is True


def test_record_above_consent_level_drops(fb_dir):
    sdk = FeedbackSdk(level="L1", feedback_dir=fb_dir)
    r = sdk.record("L3", {"samples": [{"sample": "x"}]})
    assert r["dropped"] is True
    assert "reason" in r


# ------------------------------------------------------------------
# 2) 首启问一次状态机（纯函数，无 UI）
# ------------------------------------------------------------------
def test_should_ask_consent_state_machine():
    assert should_ask_consent(None) is True          # 无 state -> 该问
    assert should_ask_consent({}) is True            # 无标志 -> 该问
    assert should_ask_consent({"consent_asked": False}) is True
    assert should_ask_consent({"consent_asked": True}) is False
    assert should_ask_consent("junk") is True
    # 问过一次后即使补回 False 也不会复活询问义务（宿主置位后 state 持久为 True）
    state = {}
    assert should_ask_consent(state) is True
    state["consent_asked"] = True                    # 宿主问完置位（置位动作归宿主）
    for _ in range(3):
        assert should_ask_consent(state) is False    # 之后永不再问


# ------------------------------------------------------------------
# 3) L1 上行载荷最小面
# ------------------------------------------------------------------
def test_l1_payload_minimal_surface(sdk_l1):
    r = sdk_l1.prepare_upload("L1", {
        "event_type": "card_feedback", "result": "不对", "card_type": "客户问题速答",
        "utterance": "这句不该出现在载荷里",
    })
    assert r["ok"] is True
    up = r["upload"]
    assert set(up.keys()) == {"schema", "level", "package_version", "ts", "event"}
    assert up["level"] == "L1"
    assert up["package_version"] == "0.1.0"
    assert up["ts"]
    assert set(up["event"].keys()) == {"type", "result", "card_type"}
    assert up["event"]["result"] == "不对"
    assert "utterance" not in json.dumps(up, ensure_ascii=False)  # 原文不入载荷


def test_l1_result_whitelist_passes_through(sdk_l1):
    for v in ("对", "不对", "已处理"):
        r = sdk_l1.prepare_upload("L1", {"event_type": "card_feedback", "result": v})
        assert r["ok"] is True and r["upload"]["event"]["result"] == v


# ------------------------------------------------------------------
# 4) L2 规则 delta：白名单字段、不含话术原文
# ------------------------------------------------------------------
def test_l2_delta_structure_and_no_script_text(fb_dir):
    sdk = FeedbackSdk(level="L2", feedback_dir=fb_dir)
    r = sdk.prepare_upload("L2", {
        "event_type": "card_feedback", "result": "已处理", "card_type": "客户问题速答",
        "rule_delta": {"rule_id": "meeting.gate.max_per_meeting", "field": "max_per_meeting",
                       "before": 12, "after": 8},
        "script": "按坐席年费计，含全部功能模块",  # 话术原文混入 payload
    })
    assert r["ok"] is True
    up = r["upload"]
    assert up["rule_delta"] == {"rule_id": "meeting.gate.max_per_meeting",
                                "field": "max_per_meeting", "before": 12, "after": 8}
    dumped = json.dumps(up, ensure_ascii=False)
    assert "按坐席年费计" not in dumped                      # 话术原文不入载荷
    assert "script" not in dumped


def test_l2_delta_extra_field_rejected(fb_dir):
    sdk = FeedbackSdk(level="L2", feedback_dir=fb_dir)
    r = sdk.prepare_upload("L2", {
        "rule_delta": {"rule_id": "r", "field": "f", "before": 1, "after": 2, "script": "话术原文"},
    })
    assert r["ok"] is False and r["rejected"] is True
    assert "越权字段" in r["reason"]


def test_l2_delta_non_scalar_rejected(fb_dir):
    sdk = FeedbackSdk(level="L2", feedback_dir=fb_dir)
    r = sdk.prepare_upload("L2", {"rule_delta": {"rule_id": "r", "field": "f",
                                                 "before": {"嵌": "对象"}, "after": 2}})
    assert r["ok"] is False and r["rejected"] is True


def test_record_lands_jsonl_line(fb_dir):
    sdk = FeedbackSdk(level="L1", feedback_dir=fb_dir, now=1730000000)
    r = sdk.record("L1", {"event_type": "card_feedback", "result": "对", "card_type": "卡"})
    assert r["dropped"] is False
    lines = open(r["path"], encoding="utf-8").read().strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["schema"] == "quickcue.feedback/v1"
    assert row["level"] == "L1"
    assert row["payload"]["result"] == "对"


def test_env_dir_override(fb_dir, monkeypatch):
    monkeypatch.setenv("QUICKCUE_FEEDBACK_DIR", fb_dir)
    sdk = FeedbackSdk(level="L1")  # 不注入 feedback_dir，走环境变量
    r = sdk.record("L1", {"event_type": "card_feedback", "result": "对"})
    assert r["dropped"] is False and r["path"].startswith(fb_dir)
    assert os.path.isfile(r["path"])


# ------------------------------------------------------------------
# 5) L3 sanitize：六类 PII 槽位化（每类 >=2 例）+ 同序号去重
# ------------------------------------------------------------------
def test_sanitize_phone_two_examples():
    out = sanitize("回头打我电话 13812345678，或者备用号 15987654321 都行")
    assert out == "回头打我电话 <PHONE_1>，或者备用号 <PHONE_2> 都行"


def test_sanitize_phone_same_number_reuses_slot():
    out = sanitize("联系 13812345678；再联系 13812345678")
    assert out.count("<PHONE_1>") == 2
    assert "<PHONE_2>" not in out


def test_sanitize_amount_two_examples():
    out = sanitize("预算大概 3,500,000 元，另外加购 80万 的服务包")
    assert "<AMOUNT_1>" in out and "<AMOUNT_2>" in out
    assert "3,500,000" not in out and "80万" not in out


def test_sanitize_person_two_examples():
    out = sanitize("让张伟对接，抄送给李娜。")
    assert out == "让<PERSON_1>对接，抄送给<PERSON_2>。"


def test_sanitize_org_two_examples():
    out = sanitize("甲科技有限公司已付款，乙制造集团还没走流程")
    assert "<ORG_1>" in out and "<ORG_2>" in out
    assert "甲科技" not in out and "乙制造" not in out


def test_sanitize_email_two_examples():
    out = sanitize("发到 zhangwei@example.com 和 li.na@corp.org.cn")
    assert "<EMAIL_1>" in out and "<EMAIL_2>" in out
    assert "zhangwei@example.com" not in out


def test_sanitize_shared_slot_numbering_across_samples():
    slots = {}
    a = sanitize("电话 13812345678", slots)
    b = sanitize("再打 13812345678 找王建国", slots)
    assert a == "电话 <PHONE_1>"
    assert b == "再打 <PHONE_1> 找<PERSON_1>"  # 同号复用 + 共享序号表


def test_l3_samples_sanitized_in_upload(fb_dir):
    sdk = FeedbackSdk(level="L3", feedback_dir=fb_dir)
    r = sdk.prepare_upload("L3", {
        "samples": [
            {"reason": "命中判错", "sample": "王建国 13812345678 说预算 50万，发 zhang@ex.com"},
            {"reason": "未出卡", "sample": "让李娜联系甲科技有限公司，13899997777"},
        ],
    })
    assert r["ok"] is True
    dumped = json.dumps(r["upload"], ensure_ascii=False)
    assert "王建国" not in dumped and "13812345678" not in dumped and "13899997777" not in dumped
    assert "50万" not in dumped and "zhang@ex.com" not in dumped and "甲科技有限公司" not in dumped
    assert "<PERSON_1>" in dumped and "<PHONE_1>" in dumped and "<AMOUNT_1>" in dumped
    assert "<EMAIL_1>" in dumped and "<ORG_1>" in dumped


def test_l3_requires_samples(fb_dir):
    sdk = FeedbackSdk(level="L3", feedback_dir=fb_dir)
    r = sdk.prepare_upload("L3", {})
    assert r["ok"] is False and r["rejected"] is True and "samples" in r["reason"]


# ------------------------------------------------------------------
# 6) 原文残留检测：未槽位化手机号/邮箱 -> 拒绝
# ------------------------------------------------------------------
def test_residue_reject_phone_in_l2_delta(fb_dir):
    sdk = FeedbackSdk(level="L3", feedback_dir=fb_dir)
    r = sdk.prepare_upload("L3", {
        "rule_delta": {"rule_id": "r", "field": "note", "before": "", "after": "拨 13812345678 确认"},
        "samples": [{"sample": "样例文本无 PII"}],
    })
    assert r["ok"] is False and r["rejected"] is True
    assert "residue_detected(phone)" == r["reason"]


def test_residue_reject_email_in_l1_event(fb_dir):
    sdk = FeedbackSdk(level="L1", feedback_dir=fb_dir)
    r = sdk.prepare_upload("L1", {"event_type": "card_feedback", "result": "对",
                                  "card_type": "联系 admin@corp.example 的人"})
    assert r["ok"] is False and r["rejected"] is True
    assert "residue_detected(email)" == r["reason"]


def test_detect_residue_unit():
    assert detect_residue({"a": ["干净", {"b": "没有问题"}]}) == []
    assert detect_residue({"x": "手机 13812345678"}) == ["phone"]
    assert detect_residue({"x": "邮箱 a@b.com"}) == ["email"]
    assert detect_residue({"x": "13812345678 和 a@b.com"}) == ["phone", "email"]
    # 已槽位化的载荷不应误报
    assert detect_residue({"x": "<PHONE_1> 和 <EMAIL_1>"}) == []
