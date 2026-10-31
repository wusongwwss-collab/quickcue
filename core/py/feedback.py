# -*- coding: utf-8 -*-
"""feedback — QuickCue 反馈 SDK（Python 侧；Easur Open 反馈组件）。

定位（组件设计长期约定）：
  遥测默认关、首启问一次、随时可关、说明白上行什么。
  反馈三档：L1=只回传结果卡点选（对/不对/已处理）；L2=加规则 delta；
  L3=加脱敏失败样例。

职责边界：
  - 本模块只做「档位状态机 + 本地落盘 + 上行预检与脱敏」，**不内置任何 UI、
    不内置任何网络调用**（零依赖之外不出网）；上行动作归宿主/云端侧。
  - 本地落盘路径：`.quickcue/feedback.jsonl`（相对当前工作目录；目录可用环境
    变量 ``QUICKCUE_FEEDBACK_DIR`` 覆盖）。
  - ``sanitize`` 把中文语境 PII 槽位化（手机号/金额/人名/公司名/邮箱），
    原文永不出本地；``prepare_upload`` 对最终载荷做原文残留检测（未槽位化的
    手机号/邮箱模式命中即拒绝）。

接口面（与 core/js/feedback.mjs 同形）：
  - should_ask_consent(state) -> bool     纯函数：state 里 consent_asked 标志；
                                          未问过 -> True（宿主据此问一次并置位）；
                                          问过 -> False 永不再问。不内置任何 UI。
  - sanitize(text, slots=None) -> str     本地 PII 槽位化（slots 跨多次调用共享序号）。
  - detect_residue(payload) -> list[str]  原文残留检测（返回命中的 PII 类型列表）。
  - FeedbackSdk(level="L0", feedback_dir=None, package_version=...).record(level, payload)
  - FeedbackSdk(...).prepare_upload(level, payload)
  - JS 侧同名 snake_case 别名（record/prepare_upload/sanitize/should_ask_consent）均可直调。

零第三方依赖：仅 Python 标准库（re / json / os / time / datetime）。
"""

import json
import os
import re
import time
from datetime import datetime

SDK_SCHEMA = "quickcue.feedback/v1"
SDK_VERSION = "0.1.0"

# 档位：L0=关（默认）；L1/L2/L3 逐档叠加（高档位内容包含低档位内容）
LEVEL_ORDER = {"L0": 0, "L1": 1, "L2": 2, "L3": 3}

FEEDBACK_ENV_DIR = "QUICKCUE_FEEDBACK_DIR"

# L2 规则 delta 只允许这些字段（结构层面保证「不含话术原文」）
_RULE_DELTA_KEYS = ("rule_id", "field", "before", "after")

# L1 结果卡点选值白名单（对 / 不对 / 已处理）
_RESULT_VALUES = ("对", "不对", "已处理")


# ------------------------------------------------------------------
# 首启问一次（纯函数，无 UI、无副作用）
# ------------------------------------------------------------------
def should_ask_consent(state):
    """首启问一次状态机：state 由宿主持久化，含 consent_asked 标志。

    - state 缺失/非 dict/无 consent_asked 键/为假 -> True（该问了）
    - consent_asked 为真 -> False（问过一次，永不再问）
    置位动作归宿主（问完后由宿主把 state["consent_asked"] 置 True 落盘）。
    """
    if not isinstance(state, dict):
        return True
    return not bool(state.get("consent_asked"))


# ------------------------------------------------------------------
# 本地 PII 槽位化（中文语境正则；同槽去重：<PHONE_1>/<PHONE_2> …）
# ------------------------------------------------------------------
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# 手机号：11 位连写，或 3-4-4 带空格/连字符分隔
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)|(?<!\d)1[3-9]\d{2}[ \-]\d{4}[ \-]\d{4}(?!\d)")
# 金额：¥/￥ 前缀，或数字 + 元/块/万/亿 后缀（中文语境，允许数字与单位间空白）
_AMOUNT_RE = re.compile(
    r"(?:[￥¥]\s*\d[\d,，]*(?:\.\d+)?(?:万|千万|百万|亿)?)"
    r"|(?:\d[\d,，]*(?:\.\d+)?\s*(?:万|千万|百万|亿)?\s*(?:元|块钱?))"
    r"|(?:\d[\d,，]*(?:\.\d+)?\s*(?:万|亿)(?![\u4e00-\u9fa5A-Za-z0-9]))"
)
# 公司/机构：实体名 + 常见机构后缀
_ORG_RE = re.compile(r"[\u4e00-\u9fa5A-Za-z0-9]{2,16}?(?:股份有限公司|有限公司|集团公司|集团|科技公司|银行|大学|医院|研究院)")
_ORG_SUFFIXES = ("股份有限公司", "集团公司", "有限公司", "科技公司", "研究院", "集团", "银行", "大学", "医院")
# 实体名部分出现这些功能词/动词 -> 收窄到其后（「让李娜联系甲科技有限公司」-> 甲科技有限公司）
_ORG_NAME_STOP = set("让请和与跟向对给找叫去来在由把被从至到是说讲问已曾还没不未正将想"
                     "联系告知提供接待负责表示认为建议要求协助沟通对接汇报")
# 人名：常见姓氏 + 1~2 个汉字（启发式）
_SURNAMES = ("王李张刘陈杨黄赵吴周徐孙马朱胡郭何林罗高郑梁谢宋唐许韩冯邓曹彭"
             "曾肖田董潘袁蔡蒋余于杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖"
             "贾夏韦付方白邹孟熊秦邱江尹薛闫段雷侯龙史陶黎贺顾毛郝龚邵万钱"
             "严覃武戴莫孔向汤")
_NAME_RE = re.compile(r"[" + _SURNAMES + r"][\u4e00-\u9fa5]{1,2}")
# 姓氏后紧跟这些字视为非人名（「马上」「付款」等常用词误报抑制）
_NAME_FOLLOW_STOP = set("上下来去的是在和与说会能要不很路天年月日点次个中时为款货钱费单")
# 人名尾部这些虚词/动词回退掉（「张伟说」-> 槽位化「张伟」留「说」）
_NAME_TRAILING_TRIM = set("说的了是在和与等吗呢吧啊对不对也很但就都又还把被向往联系")

_PII_ORDER = ("email", "phone", "amount", "org", "name")
_PII_SLOT = {"email": "EMAIL", "phone": "PHONE", "amount": "AMOUNT", "org": "ORG", "name": "PERSON"}

# 残留检测：只押高精度模式（手机号 / 邮箱）——槽位化对这两类是确定性的，
# 因此最终载荷里再出现即视为「原文残留」，直接拒绝上行。
_RESIDUE_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_RESIDUE_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


class _SanitizeState:
    """槽位序号表：同一原文同槽复用，不同原文按出现顺序递增（跨文本可共享）。"""

    def __init__(self):
        self._slots = {}  # (pii_type, original) -> "<PHONE_1>"

    def slot_for(self, pii_type, original):
        key = (pii_type, original)
        if key not in self._slots:
            self._slots[key] = "<%s_%d>" % (_PII_SLOT[pii_type], len(
                [k for k in self._slots if k[0] == pii_type]) + 1)
        return self._slots[key]


def sanitize(text, slots=None):
    """本地 PII 槽位化：手机号-><PHONE_N> 金额-><AMOUNT_N> 人名-><PERSON_N>
    公司名-><ORG_N> 邮箱-><EMAIL_N>。同序号去重（两个不同人名 -> PERSON_1/PERSON_2）。
    非字符串输入原样返回；slots 传 dict 可跨多次调用共享序号（prepare_upload 内部用它）。
    """
    if not isinstance(text, str) or not text:
        return text
    state = slots if isinstance(slots, _SanitizeState) else _SanitizeState()

    def _sub_by_type(pii_type, pattern):
        def _repl(m):
            return state.slot_for(pii_type, m.group(0))
        return pattern.sub(_repl, text)

    # 应用顺序：email（防其中数字被手机号吃掉）-> phone -> amount -> org -> name
    text = _sub_by_type("email", _EMAIL_RE)
    text = _sub_by_type("phone", _PHONE_RE)
    text = _sub_by_type("amount", _AMOUNT_RE)

    def _org_repl(m):
        token = m.group(0)
        suffix = next((s for s in _ORG_SUFFIXES if token.endswith(s)), "")
        if not suffix:
            return token
        name_part = token[: len(token) - len(suffix)]
        cut = max((i for i, ch in enumerate(name_part) if ch in _ORG_NAME_STOP), default=-1)
        if cut >= 0:
            prefix = name_part[: cut + 1]  # 功能词前缀原样保留（不吞正文）
            name_part = name_part[cut + 1:]
        else:
            prefix = ""
        if len(name_part) < 1:
            return token
        return prefix + state.slot_for("org", name_part + suffix)

    text = _ORG_RE.sub(_org_repl, text)

    def _name_repl(m):
        token = m.group(0)
        # 姓氏后紧跟虚词 -> 非人名，跳过
        if len(token) >= 2 and token[1] in _NAME_FOLLOW_STOP:
            return token
        # 尾部虚词/动词回退但**保留原字**（张伟说 -> <PERSON_N>说，不吞正文）
        tail = ""
        while len(token) > 2 and token[-1] in _NAME_TRAILING_TRIM:
            tail = token[-1] + tail
            token = token[:-1]
        if len(token) < 2:
            return m.group(0)
        return state.slot_for("name", token) + tail

    text = _NAME_RE.sub(_name_repl, text)
    return text


def detect_residue(payload):
    """原文残留检测：递归扫描载荷（dict/list/str），返回命中的未槽位化 PII 类型
    列表（"phone" / "email"，去重保序）。空列表 = 干净。"""
    hits = []

    def _walk(node):
        if isinstance(node, str):
            if _RESIDUE_PHONE_RE.search(node) and "phone" not in hits:
                hits.append("phone")
            if _RESIDUE_EMAIL_RE.search(node) and "email" not in hits:
                hits.append("email")
        elif isinstance(node, dict):
            for v in node.values():
                _walk(v)
        elif isinstance(node, (list, tuple)):
            for v in node:
                _walk(v)

    _walk(payload)
    return hits


# ------------------------------------------------------------------
# 反馈 SDK 主体
# ------------------------------------------------------------------
def _now_iso(now=None):
    ts = time.time() if now is None else now
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def default_feedback_dir():
    """落盘目录：环境变量 QUICKCUE_FEEDBACK_DIR 优先，否则 <cwd>/.quickcue。"""
    return os.environ.get(FEEDBACK_ENV_DIR) or os.path.join(os.getcwd(), ".quickcue")


class FeedbackSdk:
    """反馈 SDK：档位状态机（默认 L0=关）+ 本地落盘 + 上行预检与脱敏。

    不内置 UI（首启询问用 should_ask_consent 纯函数，UI 归宿主）；
    不内置网络调用（prepare_upload 只产「上行载荷」，上行动作归宿主/云端侧）。
    """

    def __init__(self, level="L0", feedback_dir=None, package_version=SDK_VERSION, now=None):
        if level not in LEVEL_ORDER:
            raise ValueError("未知反馈档位：%r（可选 L0/L1/L2/L3）" % (level,))
        self._level = level
        self._dir = feedback_dir  # None = 走 default_feedback_dir()（每次读环境变量）
        self._package_version = package_version
        self._now = now

    # -- 档位状态机 ------------------------------------------------
    @property
    def level(self):
        return self._level

    def set_level(self, level):
        if level not in LEVEL_ORDER:
            raise ValueError("未知反馈档位：%r（可选 L0/L1/L2/L3）" % (level,))
        self._level = level

    def _feedback_path(self):
        base = self._dir if self._dir else default_feedback_dir()
        return os.path.join(base, "feedback.jsonl")

    # -- record：本地落盘（默认 L0 直接丢弃） ------------------------
    def record(self, level, payload):
        """反馈落本地 .quickcue/feedback.jsonl（JSONL 追加一行）。

        - 当前档位为 L0（关）-> 丢弃，返回 {"dropped": True, "reason": ...}
        - 请求档位高于已开启档位 -> 丢弃（不得超出用户同意的范围）
        - 否则追加落盘并返回 {"dropped": False, "path": ...}
        """
        if level not in LEVEL_ORDER or level == "L0":
            raise ValueError("record 档位须为 L1/L2/L3，收到 %r" % (level,))
        if self._level == "L0":
            return {"dropped": True, "reason": "feedback_off(L0)"}
        if LEVEL_ORDER[level] > LEVEL_ORDER[self._level]:
            return {"dropped": True, "reason": "level_below_consent(%s<%s)" % (self._level, level)}
        path = self._feedback_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        line = json.dumps({
            "schema": SDK_SCHEMA,
            "ts": _now_iso(self._now),
            "level": level,
            "payload": payload,
            "sdk_version": SDK_VERSION,
        }, ensure_ascii=False)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return {"dropped": False, "path": path}

    # -- prepare_upload：上行预检与脱敏（不联网） --------------------
    def prepare_upload(self, level, payload):
        """上行预检与脱敏：只产「上行载荷」，**不做任何网络调用**。

        - L1 只含：事件类型/结果卡点选值/卡类型/时间戳/包版本
        - L2 加规则 delta（改前值->改后值、规则 id；字段白名单，不含话术原文）
        - L3 加脱敏失败样例（先 sanitize 再入载荷）
        - 载荷组装后做原文残留检测：含未槽位化手机号/邮箱 -> rejected:True+reason
        返回 {"ok": True, "upload": {...}} 或 {"ok": False, "rejected": True, "reason": "..."}。
        """
        if level not in LEVEL_ORDER or level == "L0":
            raise ValueError("prepare_upload 档位须为 L1/L2/L3，收到 %r" % (level,))
        if self._level == "L0":
            return {"ok": False, "rejected": True, "reason": "feedback_off(L0)"}
        if LEVEL_ORDER[level] > LEVEL_ORDER[self._level]:
            return {"ok": False, "rejected": True,
                    "reason": "level_below_consent(%s<%s)" % (self._level, level)}
        if not isinstance(payload, dict):
            return {"ok": False, "rejected": True, "reason": "payload 须为对象"}

        upload = {
            "schema": SDK_SCHEMA,
            "level": level,
            "package_version": self._package_version,
            "ts": _now_iso(self._now),
            "event": self._extract_event(payload),
        }
        if LEVEL_ORDER[level] >= LEVEL_ORDER["L2"]:
            delta = self._extract_rule_delta(payload)
            if isinstance(delta, dict) and delta.get("__rejected__"):
                return {"ok": False, "rejected": True, "reason": delta["__rejected__"]}
            if delta is not None:
                upload["rule_delta"] = delta
        if LEVEL_ORDER[level] >= LEVEL_ORDER["L3"]:
            samples = payload.get("samples")
            if not isinstance(samples, list) or not samples:
                return {"ok": False, "rejected": True, "reason": "L3 须带非空 samples 列表"}
            slots = _SanitizeState()
            upload["samples"] = [
                {"reason": s.get("reason", ""), "sample": sanitize(s.get("sample", ""), slots)}
                for s in samples if isinstance(s, dict)
            ]

        residues = detect_residue(upload)
        if residues:
            return {"ok": False, "rejected": True,
                    "reason": "residue_detected(%s)" % "+".join(residues)}
        return {"ok": True, "upload": upload}

    # -- 载荷装配内部件 ---------------------------------------------
    @staticmethod
    def _extract_event(payload):
        """L1 最小面：事件类型/结果卡点选值/卡类型（值一律标量字符串化）。"""
        event = {}
        if "event_type" in payload:
            event["type"] = str(payload["event_type"])
        result = payload.get("result")
        if isinstance(result, str) and result in _RESULT_VALUES:
            event["result"] = result
        elif result is not None:
            event["result"] = str(result)
        if "card_type" in payload:
            event["card_type"] = str(payload["card_type"])
        return event

    @staticmethod
    def _extract_rule_delta(payload):
        """L2 规则 delta：字段白名单（rule_id/field/before/after），值限标量——
        结构层面保证不含话术原文；越权字段直接拒绝。"""
        delta = payload.get("rule_delta")
        if delta is None:
            return None
        if not isinstance(delta, dict):
            return {"__rejected__": "rule_delta 须为对象"}
        for k in delta:
            if k not in _RULE_DELTA_KEYS:
                return {"__rejected__": "rule_delta 含越权字段:%s" % k}
        out = {}
        for k in _RULE_DELTA_KEYS:
            if k not in delta:
                continue
            v = delta[k]
            if not isinstance(v, (str, int, float, bool)) and v is not None:
                return {"__rejected__": "rule_delta.%s 须为标量" % k}
            out[k] = v
        return out


# JS 侧同形入口（createFeedbackSdk）的 Python 对照物
def create_feedback_sdk(**kwargs):
    return FeedbackSdk(**kwargs)
