# -*- coding: utf-8 -*-
"""meeting_server — QuickCue 会议侧 stdio MCP 服务器。

协议：JSON-RPC 2.0 over stdio（每行一条 JSON 消息，换行分帧）；
实现：Python 标准库，零第三方依赖。
Windows 注意：stdio 编码显式设置为 UTF-8（本模块启动即 reconfigure）。

工具面（tools/list 可见，tools/call 调用）：
  cue.analyze      输入 utterance + role（默认"客户"）+ 可选 profile / kb，
                   调 quickcue_engine 判定出卡；返回双帧或 skip 原因。
                   知识库取用优先级（外部注入优先，内置演示库兜底）：
                   ① 调用方传入 kb（字符串列表，按原样作话术候选）；
                   ② 未传 kb 时读环境变量 QUICKCUE_KB_FILE 指向的外部知识库
                     （JSON 文件：字符串数组=恒候选话术；对象数组每项
                     {keywords:[...], script, follow_up?} 按关键词匹配）；
                   ③ ①②均无（未注入、未配置外部知识库）时回退到本文件
                     BUILTIN_DEMO_KB 内置演示知识库（常见客户疑问→话术，
                     仅用于开箱演示零配置出卡；生产用宿主注入自有知识库）。
  rules.validate   校验规则面结构（已解析的 rules 对象；YAML 解析留宿主侧，
                   本服务器不引入 YAML 依赖），返回 valid / errors。
  feedback.record  收结构化反馈 JSON，追加落本地 .quickcue/feedback.jsonl
                   （目录可用环境变量 QUICKCUE_FEEDBACK_DIR 覆盖）；默认无上行，
                   数据只留在本机。

启动：python hosts/mcp/meeting_server.py
调试：向 stdin 逐行写 JSON-RPC 请求，响应逐行出现在 stdout。
"""

import json
import os
import sys

# 引擎导入：把 core/py 加入模块搜索路径（相对本文件 ../../core/py）
_HERE = os.path.dirname(os.path.abspath(__file__))
_CORE_PY = os.path.join(os.path.dirname(os.path.dirname(_HERE)), "core", "py")
if _CORE_PY not in sys.path:
    sys.path.insert(0, _CORE_PY)

import quickcue_engine as engine  # noqa: E402

SERVER_NAME = "quickcue-meeting"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2024-11-05"

# 协议头约定（initialize 应答固定携带）
SERVER_INFO = {"name": SERVER_NAME, "version": SERVER_VERSION}
CAPABILITIES = {"tools": {}}

# rules.validate 的结构面（与 packs/meeting/rules.example.yaml 字段一一对应）
_RULES_INT_KEYS = ("global_cooldown_sec", "same_type_cooldown_sec", "max_per_meeting",
                   "l1_min_strength", "card_confidence", "expires_in_sec",
                   "script_max_chars", "followup_max_chars")
_RULES_LIST_KEYS = ("question_words", "complete_ends", "incomplete_ends",
                    "strong_words", "openers")
_RULES_TEMPLATE_KEYS = ("default", "sales", "consultative", "technical", "phrased")

# ------------------------------------------------------------------
# 内置演示知识库（零配置兜底）
# ------------------------------------------------------------------
# 仅当调用方未传 kb、且未配置外部知识库（QUICKCUE_KB_FILE）时作 fallback，
# 保证「零配置喂一句客户疑问就出第一张提示卡」的开箱承诺。
# 外部注入存在时外部优先；本库只用于开箱演示，生产宿主注入自有知识库。
# 话术均为演示原创文案，人物与承诺口径均为虚构示例，不代表任何真实产品。
BUILTIN_DEMO_KB = [
    {
        "topic": "报价与费用",
        "keywords": ["报价", "费用", "价格", "多少钱", "收费", "优惠", "预算"],
        "script": "费用按年一口价打包，常用功能全含、不按模块另收钱；具体数字我"
                  "整理一份逐项明细表发您，您可以拿它直接对预算。",
        "follow_up": "您给我一个大致的年度预算区间，我按这个区间配一版方案，"
                     "能省的和不能省的当面讲清楚。",
    },
    {
        "topic": "交付周期",
        "keywords": ["部署", "交付", "上线", "实施", "周期", "多久", "时间"],
        "script": "标准实施两周走完：第 1 周环境准备与安装调试，第 2 周试运行加"
                  "人员培训；您这边只需提前备好一台服务器，其余由我们负责。",
        "follow_up": "如果赶时间，可以先切核心功能试运行，外围能力第二周补齐，"
                     "业务一天都不停。",
    },
    {
        "topic": "售后响应",
        "keywords": ["售后", "响应", "维保", "故障", "支持", "出问题", "报修"],
        "script": "问题反馈进专属服务群，工作时间半小时内有人应答；紧急故障远程"
                  "即时接入排查，处理进展当天同步，闭环后出书面说明。",
        "follow_up": "质保期内的版本升级与定期巡检都含在服务里，不额外计费。",
    },
    {
        "topic": "数据安全",
        "keywords": ["数据安全", "安全", "加密", "存储", "隐私", "泄露"],
        "script": "数据全部留在您自己的机房，不落第三方；传输走加密通道，操作留"
                  "独立审计日志，可随时导出核对，账号权限按角色分级。",
        "follow_up": "需要应付内控或合规检查时，我们提供配套的安全说明材料。",
    },
]

# 外部知识库环境变量（指向 JSON 文件；见模块 docstring「知识库取用优先级」）
EXTERNAL_KB_ENV = "QUICKCUE_KB_FILE"


def _entry_scripts(entry: dict) -> list:
    """把一条知识库条目展开成话术候选列表（script 必有，follow_up 可选）。"""
    scripts = [str(entry.get("script") or "")]
    if entry.get("follow_up"):
        scripts.append(str(entry["follow_up"]))
    return [s for s in scripts if s]


def _match_kb_entries(entries: list, question_text: str) -> list:
    """关键词匹配取话术：dict 条目按 keywords 匹配（无 keywords/空 = 恒命中），
    纯字符串条目视为恒候选话术；首个命中条目即返回其话术列表。"""
    t = (question_text or "").strip()
    for entry in entries:
        if isinstance(entry, str):
            scripts = [entry.strip()] if entry.strip() else []
        elif isinstance(entry, dict):
            kws = entry.get("keywords")
            if (not isinstance(kws, list)) or not kws or any(
                    str(k) in t for k in kws if isinstance(k, str)):
                scripts = _entry_scripts(entry)
            else:
                scripts = []
        else:
            scripts = []
        if scripts:
            return scripts
    return []


def _external_kb_loader():
    """读 QUICKCUE_KB_FILE 外部知识库，返回 kb_loader 或 None（未配置/不可读）。"""
    path = os.environ.get(EXTERNAL_KB_ENV)
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None                    # 配置了但读不了 ⇒ 视为未配置，走演示兜底
    entries = data if isinstance(data, list) else []

    def _loader(question_text: str) -> list:
        return _match_kb_entries(entries, question_text)

    return _loader


def _demo_kb_loader(question_text: str) -> list:
    """内置演示知识库 loader：疑问文本按关键词匹配演示条目，未命中返回空。"""
    return _match_kb_entries(BUILTIN_DEMO_KB, question_text)


# ------------------------------------------------------------------
# 工具实现
# ------------------------------------------------------------------
def tool_cue_analyze(args: dict) -> dict:
    """出卡判定：utterance 必填；role/profile/kb/now/utterance_id 可选。
    本工具无会话态——每次调用用全新三层闸（冷却语义由宿主按会话持有闸实例实现）。

    知识库 fallback（外部注入优先）：调用方传入 kb 按原样作候选；未传 kb 时
    依次尝试外部知识库（QUICKCUE_KB_FILE）与内置演示知识库（BUILTIN_DEMO_KB，
    仅开箱演示用，生产宿主注入自有知识库）——保证零配置也能出第一张演示卡；
    三者皆无话术命中时按引擎原语义 kb_miss 静默跳过。"""
    utterance = args.get("utterance")
    if not isinstance(utterance, str):
        raise ValueError("cue.analyze 需要字符串参数 utterance")
    role = args.get("role") or engine.CUSTOMER_ROLE
    profile = args.get("profile") if isinstance(args.get("profile"), dict) else None
    kb = args.get("kb") if isinstance(args.get("kb"), list) else None
    if kb is not None:
        kb_loader = lambda q: [str(x) for x in kb]        # noqa: E731 ① 调用方注入
    else:
        kb_loader = (_external_kb_loader()                # ② 外部知识库配置
                     or _demo_kb_loader)                  # ③ 内置演示库兜底
    now = args.get("now")
    if not isinstance(now, (int, float)):
        now = None
    gate = engine.QuickCueGate(
        global_cooldown=args.get("global_cooldown_sec"),
        same_type_cooldown=args.get("same_type_cooldown_sec"),
        max_per_meeting=args.get("max_per_meeting"))
    eng = engine.QuickCueEngine(
        gate=gate,
        kb_loader=kb_loader,
        profile_loader=(lambda: profile) if profile is not None else None)
    frames = eng.maybe_fire(role, utterance,
                            utterance_id=args.get("utterance_id"), now=now)
    return {
        "fired": frames is not None,
        "skip_reason": eng.last_skip_reason,
        "frames": frames or [],
    }


def tool_rules_validate(args: dict) -> dict:
    """校验规则面结构：入参为已解析的 rules 对象（YAML 请在宿主侧先解析成对象）。
    缺键不算错（引擎有内置默认）；类型不符才报错。"""
    rules = args.get("rules")
    if not isinstance(rules, dict):
        raise ValueError("rules.validate 需要对象参数 rules（已解析的规则面）")
    errors = []
    for key in _RULES_INT_KEYS:
        if key in rules and not isinstance(rules[key], int):
            errors.append(f"{key} 应为整数，实际为 {type(rules[key]).__name__}")
    for key in _RULES_LIST_KEYS:
        if key in rules:
            val = rules[key]
            if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
                errors.append(f"{key} 应为字符串列表")
            elif key == "question_words" and not val:
                errors.append("question_words 不可为空（L0 疑问判定会失灵）")
    if "customer_role" in rules and not isinstance(rules["customer_role"], str):
        errors.append("customer_role 应为字符串")
    templates = rules.get("templates")
    if templates is not None:
        if not isinstance(templates, dict):
            errors.append("templates 应为对象")
        else:
            for tkey, tval in templates.items():
                if tkey not in _RULES_TEMPLATE_KEYS:
                    errors.append(f"templates.{tkey} 不在已知模板键内"
                                  f"（已知：{'/'.join(_RULES_TEMPLATE_KEYS)}）")
                elif not isinstance(tval, dict) or not isinstance(tval.get("headline"), str):
                    errors.append(f"templates.{tkey} 应含字符串 headline")
    return {"valid": not errors, "errors": errors}


def _feedback_path() -> str:
    base = os.environ.get("QUICKCUE_FEEDBACK_DIR") or os.path.join(os.getcwd(), ".quickcue")
    return os.path.join(base, "feedback.jsonl")


def tool_feedback_record(args: dict) -> dict:
    """结构化反馈落本地 JSONL（默认无上行，数据只留在本机）。"""
    feedback = args.get("feedback")
    if not isinstance(feedback, dict):
        raise ValueError("feedback.record 需要对象参数 feedback")
    import time
    record = dict(feedback)
    record.setdefault("recorded_at", time.time())
    path = _feedback_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return {"ok": True, "path": path}


TOOLS = [
    {
        "name": "cue.analyze",
        "description": "QuickCue 出卡判定：客户 utterance 进、应答参考卡双帧出"
                       "（可选 profile 画像与 kb 话术候选注入；now 显式传可复现）。"
                       "未注入 kb 时外部知识库（QUICKCUE_KB_FILE）优先，"
                       "两者皆无则回退内置演示知识库（仅开箱演示用，"
                       "生产请注入自有知识库）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "utterance": {"type": "string", "description": "定稿句文本（必填）"},
                "role": {"type": "string", "description": "发言角色，默认 客户"},
                "profile": {"type": "object", "description": "用户画像（role/style/phrases/banned）"},
                "kb": {"type": "array", "items": {"type": "string"},
                       "description": "知识库话术候选列表（外部注入，优先于内置演示库）"},
                "now": {"type": "number", "description": "当前时刻（秒，可复现用）"},
                "utterance_id": {"type": "string", "description": "句子标识"},
                "global_cooldown_sec": {"type": "integer"},
                "same_type_cooldown_sec": {"type": "integer"},
                "max_per_meeting": {"type": "integer"},
            },
            "required": ["utterance"],
        },
    },
    {
        "name": "rules.validate",
        "description": "校验 QuickCue 规则面结构（入参为已解析的 rules 对象；"
                       "缺键走引擎内置默认，类型不符报错）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "rules": {"type": "object", "description": "已解析的规则面对象"},
            },
            "required": ["rules"],
        },
    },
    {
        "name": "feedback.record",
        "description": "记录结构化反馈到本地 .quickcue/feedback.jsonl（默认无上行）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "feedback": {"type": "object", "description": "反馈对象（自由结构）"},
            },
            "required": ["feedback"],
        },
    },
]

_TOOL_FUNCS = {
    "cue.analyze": tool_cue_analyze,
    "rules.validate": tool_rules_validate,
    "feedback.record": tool_feedback_record,
}


# ------------------------------------------------------------------
# JSON-RPC 2.0 分发
# ------------------------------------------------------------------
def _ok(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id,
            "error": {"code": code, "message": message}}


def dispatch(msg: dict) -> "dict | None":
    """单条请求分发；通知（无 id）返回 None（不应答）。"""
    method = msg.get("method")
    req_id = msg.get("id")
    if req_id is None:
        return None                     # notification：不回包
    params = msg.get("params") or {}
    try:
        if method == "initialize":
            # 协议版本回应客户端请求版本（客户端未带则用本服务器默认）
            want = params.get("protocolVersion") if isinstance(params, dict) else None
            return _ok(req_id, {
                "protocolVersion": want if isinstance(want, str) else PROTOCOL_VERSION,
                "capabilities": CAPABILITIES,
                "serverInfo": SERVER_INFO,
            })
        if method == "ping":
            return _ok(req_id, {})
        if method == "tools/list":
            return _ok(req_id, {"tools": TOOLS})
        if method == "tools/call":
            name = params.get("name")
            func = _TOOL_FUNCS.get(name)
            if func is None:
                return _err(req_id, -32602, f"未知工具：{name!r}")
            out = func(params.get("arguments") or {})
            return _ok(req_id, {"content": [
                {"type": "text", "text": json.dumps(out, ensure_ascii=False, sort_keys=True)}
            ], "isError": False})
        return _err(req_id, -32601, f"未知方法：{method!r}")
    except ValueError as exc:
        return _err(req_id, -32602, str(exc))
    except Exception as exc:            # 工具内部异常 → JSON-RPC error，进程不死
        return _err(req_id, -32603, f"内部错误：{exc}")


def main() -> int:
    # Windows stdio 编码显式 UTF-8；stdout 只走协议消息，禁混入日志
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            resp = _err(None, -32700, f"JSON 解析失败：{exc}")
        else:
            if not isinstance(msg, dict):
                resp = _err(None, -32600, "请求必须是 JSON 对象")
            else:
                resp = dispatch(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
