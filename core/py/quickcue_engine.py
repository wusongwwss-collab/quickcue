# -*- coding: utf-8 -*-
"""quickcue_engine — QuickCue 会议侧快提示引擎（纯标准库 · 零第三方依赖 · 可独立单测）。

本模块是「实时辅助·快提示」判定内核的独立发行版，与产品仓内实现保持逐字等价的
行为口径（parity 机闸把关）。全部判定均为确定性规则，全零大模型调用路径。

能力一览：
  - L0 触发判定：客户角色 ∧ 疑问判定 ∧ 完整性判定（判据词表见本文件顶部常量）
  - L1 疑问强度：无标点疑问词陈述句等负例抑制（结构证据优先、词证需佐证）
  - 三层闸：全局冷却 / 同型冷却 / 场均上限，纯状态机，毫秒级判定
  - 知识库直出组卡：命中即组卡，未命中不出卡（无回退生成路径）
  - 双帧构造：帧①完整卡 + 帧②事件通知条，交调用方按既有通道广播
  - 用户画像快车道：role/style/phrases 参与模板选择；banned 子串命中即弃候选
    （不现场改写文案）；画像缺省时全路径安全降级（默认模板 + 不过滤）

与产品仓底本的唯一差异：闸参数不再从运行环境配置软读取，改为构造参数显式注入
（默认值 60 / 180 / 12 与底本一致；环境变量覆盖逻辑请在本包之外自行接线）。

依赖：仅 Python 标准库（re / time）。测试不依赖真实时间——所有入口均可显式传 now。
"""

import re
import time

# ============================================================
# L0 判据常量（疑问词/结束标点/未完词结尾；子串口径，禁漂移）
# ============================================================
# 疑问词表（11 词，子串命中即判疑问）：
QUESTION_WORDS = ["？", "?", "吗", "呢", "怎样", "如何", "是否", "什么", "为什么", "能不能", "可不可以"]
# 结束标点（句尾命中 → 完整）：
COMPLETE_ENDS = ("。", "？", "！", "…", "……", "”", "」", "?", "!")
# 连接词/未完词结尾（句尾命中 → 不完整）：
INCOMPLETE_ENDS = ("但是", "然后", "因为", "所以", "如果", "就是", "还有", "以及",
                   "那么", "再说", "比如说", "比如", "的话", "接着", "后来",
                   "后面", "下一步", "一方面")

# L0 客户角色口径（role=="客户" = 对方发言；判定只对客户侧触发）：
CUSTOMER_ROLE = "客户"
# 快提示卡固定卡内细分（语义借用 "question"，下游只读 headline 安全）：
CUE_TYPE = "question"
SUBTYPE = "quick_cue"

# ============================================================
# L1 疑问强度（负例抑制闸）
# ============================================================
# 结构证据（单项即过阈值）：
_QUESTION_MARKS = ("？", "?")            # 问号出现在句中任意位置
_FINAL_PARTICLES = ("吗", "呢")          # 句末语气词（剥离尾部标点后判定）
# 词证（需与其他证据叠加才过阈值）：
_STRONG_WORDS = ("怎样", "如何", "为什么", "能不能", "可不可以", "是否")  # 动词性疑问结构
_OPENERS = ("请问", "麻烦问一下", "问一下", "咨询一下")                    # 开口即问
L1_MIN_STRENGTH = 2   # 问号/句末语气词=+2（单项过）；强疑问词/开口语=+1（需两两叠加）


def is_question(text: str) -> bool:
    """疑问判定：疑问词表逐词子串匹配，命中任一即为疑问。"""
    return any(w in (text or "") for w in QUESTION_WORDS)


def is_complete(text: str) -> bool:
    """完整性判定：结束标点结尾 → 完整；连接词/未完词结尾 → 不完整；
    其余（无标点是语音转写常态）→ 完整。"""
    t = (text or "").strip()
    if not t:
        return True
    if t.endswith(COMPLETE_ENDS):
        return True
    for w in INCOMPLETE_ENDS:
        if t.endswith(w):
            return False
    return True


def l0_pass(role: str, text: str) -> bool:
    """L0 触发判据：role=="客户" ∧ 疑问判定 ∧ 完整性判定。"""
    return (role == CUSTOMER_ROLE) and is_question(text) and is_complete(text)


def question_strength(text: str) -> int:
    """L1 疑问强度打分（0-4）：
    +2 问号（任意位置）／+2 句末语气词 吗/呢（剥尾部标点后）；
    +1 强疑问词（怎样/如何/为什么/能不能/可不可以/是否）／+1 开口语（请问等）。
    负例抑制原理：无标点疑问词陈述句（如「我觉得这方案没什么问题」只含 什么=0 分、
    「他说怎样都行」只含 怎样=1 分）低于阈值 L1_MIN_STRENGTH=2，被抑制。"""
    t = (text or "").strip()
    if not t:
        return 0
    score = 0
    if any(m in t for m in _QUESTION_MARKS):
        score += 2
    core = t.rstrip("。！!…，,、 \t")     # 剥尾部标点后看句末语气词
    if core.endswith(_FINAL_PARTICLES):
        score += 2
    if any(w in t for w in _STRONG_WORDS):
        score += 1
    if any(o in t for o in _OPENERS):
        score += 1
    return score


def l1_pass(text: str) -> bool:
    return question_strength(text) >= L1_MIN_STRENGTH


# ============================================================
# 三层闸（全局冷却 / 同型冷却 / 场均上限；纯状态机）
# ============================================================
QUICK_CUE_GLOBAL_COOLDOWN = 60     # ① 任意快提示全局冷却（秒）
QUICK_CUE_SAME_TYPE_COOLDOWN = 180  # ② 同型（question）冷却（秒）
QUICK_CUE_MAX_PER_MEETING = 12     # ③ 场均快提示卡上限


class QuickCueGate:
    """快提示三层闸状态机。每个会话（一场会议）持有并复用一个实例；
    参数默认取本模块常量（60/180/12），环境变量覆盖逻辑在包外做显式注入。"""

    def __init__(self, global_cooldown: int | None = None,
                 same_type_cooldown: int | None = None,
                 max_per_meeting: int | None = None):
        self.global_cooldown = (int(global_cooldown) if global_cooldown is not None
                                else QUICK_CUE_GLOBAL_COOLDOWN)
        self.same_type_cooldown = (int(same_type_cooldown) if same_type_cooldown is not None
                                   else QUICK_CUE_SAME_TYPE_COOLDOWN)
        self.max_per_meeting = (int(max_per_meeting) if max_per_meeting is not None
                                else QUICK_CUE_MAX_PER_MEETING)
        # 会话态：上次任意出卡时刻 / 各型上次出卡时刻 / 已出卡计数
        self._qc_last_any_at = 0.0
        self._qc_last_type_at: dict[str, float] = {}
        self._qc_card_count = 0

    def blocked(self, cue_type: str, now: float) -> str | None:
        """三层闸判定（纯确定性，毫秒级）：放行返回 None，阻断返回原因串（供日志/单测）。"""
        if now - self._qc_last_any_at < self.global_cooldown:
            return "global_cooldown"
        if now - self._qc_last_type_at.get(cue_type, 0.0) < self.same_type_cooldown:
            return "same_type_cooldown"
        if self._qc_card_count >= self.max_per_meeting:
            return "per_meeting_cap"
        return None

    def commit(self, cue_type: str, now: float):
        """出卡后计数落位（出卡路径统一调用，保证三层闸额度一致）。"""
        self._qc_last_any_at = now
        self._qc_last_type_at = {**self._qc_last_type_at, cue_type: now}
        self._qc_card_count += 1


# ============================================================
# 用户画像快车道（模板选择 + banned 过滤）
# ============================================================
# 模板池：固定字面量（下行帧不携带用户画像原文——硬约束）。
_TEMPLATES: dict[str, dict] = {
    "default":      {"headline": "客户问题速答（历史话术参考）"},
    "sales":        {"headline": "客户提问 · 商务口径速答"},
    "consultative": {"headline": "客户提问 · 顾问式应答"},
    "technical":    {"headline": "客户提问 · 技术口径速答"},
    "phrased":      {"headline": "客户提问速答（贴合我的口径）"},
}


def choose_template(my_profile: dict | None) -> str:
    """模板选择（role/style/phrases 参与；None/全空 ⇒ 默认模板——None 全路径安全）。"""
    if not my_profile:
        return "default"
    blob = "|".join(str(my_profile.get(k) or "") for k in ("role", "style", "phrases")).lower()
    if not blob.strip("|"):
        return "default"
    if any(k in blob for k in ("顾问", "咨询", "问诊")):
        return "consultative"
    if any(k in blob for k in ("销售", "商务", "客户成功")):
        return "sales"
    if any(k in blob for k in ("技术", "研发", "架构")):
        return "technical"
    if any(my_profile.get(k) for k in ("phrases",)):
        return "phrased"       # 有口径示例但无行业命中 → 贴合口径模板
    return "default"


def filter_banned(candidates: list, my_profile: dict | None) -> list:
    """banned 命中即弃候选、取下一条（不现场改写文案）。
    分号/换行分隔、子串、大小写不敏感；画像为 None/无 banned ⇒ 原样返回（不过滤降级）。"""
    out = list(candidates)
    if not my_profile:
        return out
    raw = str(my_profile.get("banned") or "")
    banned = [b.strip().lower() for b in re.split(r"[;；\n\r]+", raw) if b.strip()]
    if not banned:
        return out
    return [c for c in out
            if not any(b in str(c or "").lower() for b in banned)]


# ============================================================
# 知识库直出组卡 + 双帧构造（全零大模型）
# ============================================================
def build_card_data(scripts: list, template_key: str = "default",
                    utterance_id: str | None = None, now: float | None = None) -> dict | None:
    """知识库直出模板卡。scripts 过滤后为空 ⇒ 返回 None（未命中不出卡，无大模型回退路径）。"""
    if not scripts:
        return None
    tpl = _TEMPLATES.get(template_key) or _TEMPLATES["default"]
    data = {
        "agent": "objection_handler",          # 下游渲染端不依赖该字段语义
        "card_type": "objection",              # 复用既有卡型分支，不新增卡型
        "objection_type": CUE_TYPE,            # "question"：语义借用，下游只读 headline 安全
        "subtype": SUBTYPE,                    # 纯增量键，老端不读零影响、新端区分渲染样式
        "headline": tpl["headline"],
        "script": str(scripts[0])[:300],
        "evidence": "来自历史会议验证过的高分应对话术（KB 直出，未调用大模型）",
        "follow_up": str(scripts[1])[:200] if len(scripts) > 1 else "",
        "data_card": None,
        "confidence": 0.75,
        "expires_in_sec": 10,
        "kb_direct": True,
    }
    data["utterance_id"] = utterance_id
    data["expires_at"] = (time.time() if now is None else now) + 10.0
    return data


def _first_point(script: str, limit: int = 80) -> str:
    """取话术首句作事件帧提示要点（message = headline（+首条话术要点））。"""
    s = (script or "").strip()
    if not s:
        return ""
    for sep in ("。", "！", "!", "？", "?", "；", ";"):
        idx = s.find(sep)
        if idx > 0:
            s = s[:idx]
            break
    return s[:limit]


def build_frames(card_data: dict) -> list:
    """双帧同批产出：帧① objection_card 完整卡；帧② event 通知条。
    双帧由调用方逐帧走既有广播通道（card_ts＝帧①发出时刻，由调用方打点）。"""
    headline = str(card_data.get("headline") or "")
    point = _first_point(str(card_data.get("script") or ""))
    message = f"{headline}（{point}）" if point else headline
    frame1 = {"type": "objection_card", "card": card_data}
    frame2 = {"type": "event", "data": {
        "event_type": SUBTYPE,          # "quick_cue"
        "severity": "info",
        "message": message[:200],
        "route": "",
    }}
    return [frame1, frame2]


# ============================================================
# 引擎（宿主接线入口；全零大模型）
# ============================================================
class QuickCueEngine:
    """quick_cue 快提示引擎（最小版）。零大模型调用：本类没有任何模型调用路径；
    知识库候选由 kb_loader 注入（入参为疑问文本，返回话术字符串列表），
    用户画像由 profile_loader 注入（会话级懒加载，一场一次）。
    返回双帧 list 交调用方广播；不触发返回 None（last_skip_reason 留因）。"""

    def __init__(self, gate: QuickCueGate | None = None,
                 kb_loader=None,               # Callable[[question_text], list[str]] | None
                 profile_loader=None):         # Callable[[], dict | None] | None
        self.gate = gate or QuickCueGate()
        self.kb_loader = kb_loader
        self.profile_loader = profile_loader
        self.last_skip_reason = ""

    def _load_profile(self) -> "dict | None":
        if not callable(self.profile_loader):
            return None
        try:
            return self.profile_loader()
        except Exception:
            return None    # 画像读取失败 ⇒ 全路径安全降级（默认模板+不过滤）

    def _load_scripts(self, question_text: str) -> list:
        if not callable(self.kb_loader):
            return []
        try:
            out = self.kb_loader(question_text)
            return [str(x) for x in out] if out else []
        except Exception:
            return []       # 知识库失败 ⇒ 视为未命中，不出卡（零大模型，无回退生成）

    def maybe_fire(self, role: str, text: str,
                   utterance_id: str | None = None, now: float | None = None) -> "list | None":
        """定稿句进、双帧出（或 None）。判定顺序（先闸后组的时序）：
        L0 → L1 → 三层闸 → 画像模板选择/banned 过滤 → 知识库直出组卡 → commit → 双帧。"""
        t = (text or "").strip()
        if not l0_pass(role, t):
            self.last_skip_reason = ("role" if role != CUSTOMER_ROLE
                                     else ("l0_question" if not is_question(t) else "l0_incomplete"))
            return None
        if not l1_pass(t):
            self.last_skip_reason = "l1_strength"
            return None
        now = time.time() if now is None else now
        why = self.gate.blocked(CUE_TYPE, now)
        if why:
            self.last_skip_reason = f"gate:{why}"
            return None
        profile = self._load_profile()
        scripts = filter_banned(self._load_scripts(t), profile)
        if not scripts:
            self.last_skip_reason = "kb_miss"
            return None
        data = build_card_data(scripts, choose_template(profile),
                               utterance_id=utterance_id, now=now)
        if data is None:               # 防御分支（scripts 非空时不会触达）
            self.last_skip_reason = "kb_miss"
            return None
        frames = build_frames(data)
        self.gate.commit(CUE_TYPE, now)   # 出卡路径统一计数
        self.last_skip_reason = ""
        return frames
