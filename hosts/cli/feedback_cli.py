# -*- coding: utf-8 -*-
"""feedback_cli — QuickCue 终端用户反馈管理 CLI（A 渠道同意载体；YQ-1154 D3 席）。

定位（规范 §4.1 A 渠道 / §4.2 轻量 CLI 定案）：
  QuickCue 是纯 MCP 产品（无 CLI）——本件为 v0.2 新增的**最小 CLI 面**，仅供
  终端用户在终端管理匿名反馈。不提供任何反馈写入口：反馈产生走 MCP 宿主
  feedback.record / 反馈 SDK record（落盘同一 .quickcue/feedback.jsonl），
  本 CLI 只做「同意 / 关闭 / 查看 / 发送 / 清空」五件事。

运行：python hosts/cli/feedback_cli.py <子命令>
  on [L1|L2|L3]   开启匿名反馈并定档（**命令即同意动作**；缺省沿用上次档位，再缺省 L1）
  off             随时关闭（停上行；记住档位，同意询问标记保留）
  show            查看待上行反馈（明示文件路径与未发送态；纯本地不联网）
  send [--yes]    发送待上行反馈——先逐条打印「将上行的最终脱敏形态」再确认；
                  y/N 确认仅 TTY 出现；非 TTY 未带 --yes 退出码 4；--yes 跳过确认
  clear           清空本地待上行反馈＋游标＋跳过台账（回执码台账保留，供删除申请）

信封（规范 §5.2 schema v1；服务端契约正本 = 主仓 app/oss_feedback_routes.py，
D2 席已对真端点实测对齐）：
  body 顶层 {"events":[事件,...]} 包裹；每个事件 = 信封八字段平铺 + payload 嵌套：
    schema_version 必须字符串 "1"；product="quickcue"；product_version ≤16 字符；
    channel="github_npm"；feedback_level=L1|L2|L3；nonce=secrets.token_hex(16)
    （32 hex，幂等键）；ts=ISO8601 UTC；payload 按档：
      L1 只含非空 event（result 白名单 = 对/不对/已处理，中文直上）；
      L2/L3 必须剥离 event 键（L2=rule_delta；L3=samples，可附带 rule_delta）。
  发送粒度＝**逐条单事件请求**：4xx 校验失败可精确定位单条（服务端 422 整批
  原子，逐条发使「单条标 rejected」语义无歧义；≤20 条/≤64KB 上限天然满足）。

传输：POST https://www.easurwork.cn/api/oss-feedback/v1（env
NW_OSS_FEEDBACK_ENDPOINT 可覆盖——测试/E2E 指向本地 mock 用）；
头 X-OSS-Signature: t=<unix秒>,v1=HMAC-SHA256(secret, t+"."+raw_body)；
secret = env NW_OSS_FEEDBACK_HMAC（缺失 = 配置中止退出码 3，不烧记录）。

错误处置（D2 对齐语义）：
  200（accepted/deduped 均算送达）→ 推进游标 + 回执文件 sentreceipts/<nonce>.json
  422/400/413   → 单条永久跳过标 rejected（sentreceipts/rejected.jsonl），推进游标
  404/401/403   → 配置类中止（端点未上线/签名或时钟配置错），不烧记录，退出码 3
  429/5xx/断连  → 指数退避重试 ≤3 次（同 nonce 幂等，重发不会重复计），穷尽退出码 5

本地文件（目录 = env QUICKCUE_FEEDBACK_DIR 或 <cwd>/.quickcue，与 MCP 侧一致）：
  feedback.jsonl              反馈记录（MCP/SDK 写，本 CLI 只读）
  state.json                  同意位（含 schema_version + updated_at；schema 变更须重问）
  sent.offset                 游标 = feedback.jsonl 已处理行数（已发送＋已跳过）
  sentreceipts/<nonce>.json   上行回执（**回执码 = nonce**；30 天内删除申请凭证，clear 不删）
  sentreceipts/rejected.jsonl 永久跳过台账（line_index + reason 码）

读取兼容两种记录形态：SDK 形（有 level+payload 键，按其档位）与 MCP
feedback.record 自由形（剥离 recorded_at/schema/sdk_version 记账键后，
按 samples→L3 / rule_delta→L2 / 其余→L1 推断档位）。当前同意档位低于记录
档位的记录按「不可上行」永久跳过（不得超出用户同意范围）。

退出码：0 成功（含无可上行、TTY 主动取消）｜2 用法错误｜3 配置类中止
（未开启反馈/缺 HMAC/404/401/403）｜4 非 TTY 未带 --yes｜5 网络失败。

零第三方依赖：仅 Python 标准库 + 仓内 core/py/feedback.py（同为标准库）。
"""

import argparse
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# 反馈 SDK 导入（同 meeting_server.py 手法：把 core/py 加入模块搜索路径）
_HERE = os.path.dirname(os.path.abspath(__file__))
_CORE_PY = os.path.normpath(os.path.join(_HERE, "..", "..", "core", "py"))
if _CORE_PY not in sys.path:
    sys.path.insert(0, _CORE_PY)

import feedback as fb  # noqa: E402

# ---------------------------------------------------------------------------
# 契约常量（与服务端正本逐字对齐；product/channel/白名单见模块 docstring）
# ---------------------------------------------------------------------------
PRODUCT = "quickcue"
CHANNEL = "github_npm"
SCHEMA_VERSION = "1"
LEVELS = ("L1", "L2", "L3")
PRODUCT_VERSION_DEFAULT = "0.2.0"        # 与 CHANGELOG 同步
PRODUCT_VERSION_ENV = "QUICKCUE_PRODUCT_VERSION"
PRODUCT_VERSION_MAX = 16

ENDPOINT_ENV = "NW_OSS_FEEDBACK_ENDPOINT"
DEFAULT_ENDPOINT = "https://www.easurwork.cn/api/oss-feedback/v1"
HMAC_ENV = "NW_OSS_FEEDBACK_HMAC"
PRIVACY_URL = "https://www.easurwork.cn/open-source/feedback"
HTTP_TIMEOUT_SEC = 15.0

RETRY_MAX = 3              # 429/5xx/断连 的重试上限（另有 1 次首发）
BACKOFF_BASE = 0.5         # 指数退避基数（秒）；测试置 0

# 本地文件名（目录 = fb.default_feedback_dir()）
STATE_FILE = "state.json"
OFFSET_FILE = "sent.offset"
RECEIPTS_DIR = "sentreceipts"
REJECTED_LEDGER = "rejected.jsonl"
# MCP feedback.record 自由形记录里的记账键（不入上行载荷）
_BOOKKEEPING_KEYS = ("recorded_at", "schema", "sdk_version")

# 退出码
EX_OK = 0
EX_USAGE = 2
EX_CONFIG = 3
EX_CONFIRM = 4
EX_NETWORK = 5


# ---------------------------------------------------------------------------
# 本地文件层
# ---------------------------------------------------------------------------
def _dir():
    return fb.default_feedback_dir()


def _state_path():
    return os.path.join(_dir(), STATE_FILE)


def _feedback_path():
    return os.path.join(_dir(), "feedback.jsonl")


def _offset_path():
    return os.path.join(_dir(), OFFSET_FILE)


def _receipts_dir():
    return os.path.join(_dir(), RECEIPTS_DIR)


def _rejected_path():
    return os.path.join(_receipts_dir(), REJECTED_LEDGER)


def _utc_now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _product_version():
    pv = os.environ.get(PRODUCT_VERSION_ENV) or PRODUCT_VERSION_DEFAULT
    return pv


def load_state():
    """读同意位。schema_version 与当前不符 ⇒ consent/consent_asked 归零（须重问）。"""
    path = _state_path()
    state = {}
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                state = json.load(f)
        except (ValueError, OSError):
            state = {}
    if not isinstance(state, dict):
        state = {}
    if state.get("schema_version") != SCHEMA_VERSION:
        state = {"schema_version": SCHEMA_VERSION, "consent_asked": False,
                 "consent": False, "level": state.get("level") if state.get("level") in LEVELS else None}
    return state


def save_state(consent, level=None, consent_asked=True):
    """写同意位（含 ts + schema_version，规范 §6.1「开启＝本地写 consent 标记文件」）。"""
    prev = load_state()
    if level is None:
        level = prev.get("level") if prev.get("level") in LEVELS else None
    state = {
        "schema_version": SCHEMA_VERSION,
        "consent_asked": bool(consent_asked),
        "consent": bool(consent),
        "level": level,
        "updated_at": _utc_now_iso(),
    }
    os.makedirs(_dir(), exist_ok=True)
    tmp = _state_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _state_path())
    return state


def read_cursor():
    try:
        with open(_offset_path(), encoding="utf-8") as f:
            return max(0, int(f.read().strip() or 0))
    except (ValueError, OSError):
        return 0


def write_cursor(n):
    os.makedirs(_dir(), exist_ok=True)
    with open(_offset_path(), "w", encoding="utf-8") as f:
        f.write(str(int(n)))


def read_records():
    """读全部反馈记录行：[(line_index, obj), ...]。
    空行 → obj=None（越过不记账）；JSON 坏行 → obj=原串（normalize 报 unreadable_line，
    走 rejected 台账留痕，不静默丢）。"""
    path = _feedback_path()
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    out = []
    for idx, line in enumerate(lines):
        line = line.strip()
        if not line:
            out.append((idx, None))
            continue
        try:
            out.append((idx, json.loads(line)))
        except ValueError:
            out.append((idx, line))
    return out


def append_rejected(line_index, level, reason):
    os.makedirs(_receipts_dir(), exist_ok=True)
    row = {"line_index": line_index, "rejected_at": _utc_now_iso(),
           "feedback_level": level, "reason": reason}
    with open(_rejected_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_receipt(nonce, level, line_index, response):
    os.makedirs(_receipts_dir(), exist_ok=True)
    row = {"nonce": nonce, "sent_at": _utc_now_iso(), "feedback_level": level,
           "line_index": line_index, "response": response}
    path = os.path.join(_receipts_dir(), "%s.json" % nonce)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(row, f, ensure_ascii=False, indent=2)
    return path


# ---------------------------------------------------------------------------
# 记录归一化与信封组装
# ---------------------------------------------------------------------------
def normalize_record(rec):
    """记录 → (level, payload, err)。兼容 SDK 形与 MCP feedback.record 自由形。"""
    if not isinstance(rec, dict):
        return None, None, "unreadable_line"
    if rec.get("level") in LEVELS and isinstance(rec.get("payload"), dict):
        return rec["level"], dict(rec["payload"]), None
    payload = {k: v for k, v in rec.items() if k not in _BOOKKEEPING_KEYS}
    if isinstance(payload.get("samples"), list) and payload["samples"]:
        level = "L3"
    elif isinstance(payload.get("rule_delta"), dict):
        level = "L2"
    else:
        level = "L1"
    return level, payload, None


def build_event(level, upload, product_version):
    """SDK upload → 服务端事件（信封八字段 + payload 嵌套；D2 契约③）。"""
    payload = {}
    if level == "L1":
        event = upload.get("event") or {}
        if not event:
            return None, "empty_event(L1 须带非空 event)"
        payload["event"] = event
    elif level == "L2":
        if not upload.get("rule_delta"):
            return None, "empty_payload(L2 须带 rule_delta)"
        payload["rule_delta"] = upload["rule_delta"]
    else:
        samples = upload.get("samples") or []
        if not samples:
            return None, "empty_payload(L3 须带非空 samples)"
        payload["samples"] = samples
        if upload.get("rule_delta"):
            payload["rule_delta"] = upload["rule_delta"]
    event = {
        "schema_version": SCHEMA_VERSION,
        "product": PRODUCT,
        "product_version": product_version,
        "channel": CHANNEL,
        "feedback_level": level,
        "nonce": secrets.token_hex(16),
        "ts": _utc_now_iso(),
        "payload": payload,
    }
    return event, None


def summarize_record(level, payload):
    """show 用的一行摘要（对应规范 §6.2 表格「内容摘要」列）。"""
    if level == "L1":
        result = payload.get("result")
        if not isinstance(result, str) or not result:
            ev = payload.get("event")
            result = ev.get("result") if isinstance(ev, dict) else None
        return "评价：" + (result if isinstance(result, str) and result else "点选")
    if level == "L2":
        d = payload.get("rule_delta")
        if isinstance(d, dict):
            rid = str(d.get("rule_id", ""))[:24]
            return "建议 %s %s→%s" % (rid or "?", d.get("before", "?"), d.get("after", "?"))
        return "规则调整"
    samples = payload.get("samples")
    n = len(samples) if isinstance(samples, list) else 0
    return "失败样例 %d 条（已脱敏）" % n


def _record_time(rec):
    ts = rec.get("ts") if isinstance(rec, dict) else None
    if isinstance(ts, str) and ts:
        return ts.replace("T", " ")[:16]
    ra = rec.get("recorded_at") if isinstance(rec, dict) else None
    if isinstance(ra, (int, float)):
        return datetime.fromtimestamp(ra).strftime("%m-%d %H:%M")
    return "--"


# ---------------------------------------------------------------------------
# HTTP 层（urllib；签名契约 = X-OSS-Signature: t=<unix>,v1=HMAC(secret, t+"."+raw)）
# ---------------------------------------------------------------------------
def _sign(secret, raw):
    t = str(int(time.time()))
    mac = hmac.new(secret.encode("utf-8"), t.encode("utf-8") + b"." + raw,
                   hashlib.sha256).hexdigest()
    return "t=%s,v1=%s" % (t, mac)


def _http_post(url, raw, signature, timeout=HTTP_TIMEOUT_SEC):
    """POST 一条事件。返回 (status, body_bytes)；status=None = 连接类失败。"""
    req = urllib.request.Request(url, data=raw, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("X-OSS-Signature", signature)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as e:                 # 4xx/5xx 都走这里
        try:
            return int(e.code), e.read()
        except Exception:                               # noqa: BLE001
            return int(e.code), b""
    except (urllib.error.URLError, OSError, TimeoutError):
        return None, None


def _post_with_retry(endpoint, raw, signature):
    """一次首发 + ≤RETRY_MAX 次指数退避（同 nonce 幂等）。
    返回 (status, body, outcome)：outcome ∈ ok|reject|config|network。
      ok=2xx；reject=单条永久失败(400/413/422)；config=配置类(401/403/404，不重试)；
      network=重试穷尽（429/5xx/断连）。"""
    last_status, last_body = None, None
    for attempt in range(RETRY_MAX + 1):
        if attempt:
            time.sleep(BACKOFF_BASE * (2 ** (attempt - 1)))
        status, body = _http_post(endpoint, raw, signature)
        if status is None:                              # 连接类失败 → 退避重试
            continue
        if 200 <= status < 300:
            return status, body, "ok"
        if status in (401, 403, 404):                   # 配置类：重试无意义
            return status, body, "config"
        if status == 429 or status >= 500:              # 瞬态：退避重试
            last_status, last_body = status, body
            continue
        return status, body, "reject"                   # 其余 4xx：单条 rejected
    return last_status, last_body, "network"


def resolve_endpoint():
    return os.environ.get(ENDPOINT_ENV) or DEFAULT_ENDPOINT


# ---------------------------------------------------------------------------
# 输出小件
# ---------------------------------------------------------------------------
def _print(text=""):
    print(text)


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------
def cmd_on(args):
    prev = load_state()
    if args.level:
        level = args.level
    elif prev.get("level") in LEVELS:
        level = prev["level"]                           # 关过再开：沿用上次档位
    else:
        level = "L1"
    save_state(consent=True, level=level)
    _print("已开启匿名反馈（档位 %s）。" % level)
    _print("上行什么（三档对照表）：%s" % PRIVACY_URL)
    _print("查看待上行：feedback show ｜ 发送：feedback send ｜ 关闭：feedback off")
    return EX_OK


def cmd_off(_args):
    save_state(consent=False)
    _print("已关闭匿名反馈：停止累积与上行。随时可用 feedback on 重新开启。")
    return EX_OK


def cmd_show(_args):
    cursor = read_cursor()
    records = read_records()
    pending = [(i, r) for (i, r) in records[cursor:] if r is not None]
    path = _feedback_path()
    if not pending:
        _print("本机没有待上行反馈（存于 %s）。" % path)
    else:
        _print("待上行 %d 条 · 存于本机 %s · 尚未发送" % (len(pending), path))
        _print(" #   级别   内容摘要                        时间")
        for n, (i, rec) in enumerate(pending, 1):
            level, payload, _err = normalize_record(rec)
            summary = summarize_record(level or "L1", payload or {})
            _print(" %-3d %-6s %-30s %s" % (n, level or "?", summary, _record_time(rec)))
        _print("[feedback send 上行]   [feedback clear 清空本地]")
    receipts = 0
    if os.path.isdir(_receipts_dir()):
        receipts = len([f for f in os.listdir(_receipts_dir()) if f.endswith(".json")])
    _print("（已上行 %d 条 · 永久跳过台账 %s）" % (
        receipts, "有" if os.path.isfile(_rejected_path()) else "无"))
    return EX_OK


def _confirm_interactive():
    """y/N 确认——仅 stdin 为 TTY 时出现（调用方保证非 TTY 不会走到这里）。"""
    try:
        ans = input("确认发送？[y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return ans in ("y", "yes")


def cmd_send(args):
    state = load_state()
    if not state.get("consent") or state.get("level") not in LEVELS:
        _print("匿名反馈未开启——先执行 feedback on [L1|L2|L3]（命令即同意动作）。")
        return EX_CONFIG
    secret = (os.environ.get(HMAC_ENV) or "").strip()
    if not secret:
        _print("未设置环境变量 %s（HMAC 签名密钥）——按配置中止，未发送任何记录。" % HMAC_ENV)
        return EX_CONFIG
    product_version = _product_version()
    if not isinstance(product_version, str) or not product_version or len(product_version) > PRODUCT_VERSION_MAX:
        _print("product_version 非法（须为 ≤%d 字符）：%r" % (PRODUCT_VERSION_MAX, product_version))
        return EX_USAGE
    endpoint = resolve_endpoint()

    cursor = read_cursor()
    records = read_records()
    sdk = fb.FeedbackSdk(level=state["level"], feedback_dir=_dir(),
                         package_version=product_version)

    prepared = []          # [(line_index, level, event_dict, raw_bytes, signature)]
    local_rejects = 0
    for idx, rec in records[cursor:]:
        if rec is None:
            cursor = max(cursor, idx + 1)           # 空行：越过（坏行走 rejected 台账，不静默丢）
            write_cursor(cursor)
            continue
        level, payload, err = normalize_record(rec)
        if err:
            append_rejected(idx, "?", err)
            cursor = max(cursor, idx + 1)
            write_cursor(cursor)
            local_rejects += 1
            continue
        prep = sdk.prepare_upload(level, payload)
        if not prep.get("ok"):
            append_rejected(idx, level, str(prep.get("reason", "prepare_rejected")))
            cursor = max(cursor, idx + 1)
            write_cursor(cursor)
            local_rejects += 1
            continue
        event, err = build_event(level, prep["upload"], product_version)
        if err:
            append_rejected(idx, level, err)
            cursor = max(cursor, idx + 1)
            write_cursor(cursor)
            local_rejects += 1
            continue
        raw = json.dumps({"events": [event]}, ensure_ascii=False).encode("utf-8")
        prepared.append((idx, level, event, raw, _sign(secret, raw)))

    if not prepared:
        _print("没有可上行条目（本地校验永久跳过 %d 条）；其余为空。" % local_rejects)
        return EX_OK

    # —— 发送前确认：先逐条打印「将上行的最终脱敏形态」（规范 §4.2/§6.3）——
    _print("即将上行 %d 条匿名反馈（签名传输，不含任何账号信息）" % len(prepared))
    if local_rejects:
        _print("（另有 %d 条本地校验未过，已永久跳过并记台账）" % local_rejects)
    for n, (_idx, level, event, _raw, _sig) in enumerate(prepared, 1):
        _print(" %d. %s %s" % (n, level, json.dumps(event, ensure_ascii=False, sort_keys=True)))
    _print("目标：%s（channel=%s）" % (endpoint, CHANNEL))
    if not args.yes:
        if not (hasattr(sys.stdin, "isatty") and sys.stdin.isatty()):
            _print("当前为非交互环境（stdin 非 TTY）：请加 --yes 显式确认后发送。")
            return EX_CONFIRM
        if not _confirm_interactive():
            _print("已取消，未发送任何记录。")
            return EX_OK

    # —— 逐条发送（单事件请求；游标逐条推进，崩溃安全）——
    sent = deduped = rejected = 0
    last_nonce = None
    abort_code = None
    for n, (idx, level, event, raw, signature) in enumerate(prepared, 1):
        status, body, outcome = _post_with_retry(endpoint, raw, signature)
        if outcome == "ok":
            resp = {}
            try:
                resp = json.loads((body or b"").decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                resp = {}
            write_receipt(event["nonce"], level, idx, resp if isinstance(resp, dict) else {})
            cursor = max(cursor, idx + 1)
            write_cursor(cursor)
            sent += 1
            last_nonce = event["nonce"]
            _print("第 %d 条 %s：已上行（回执码 %s）" % (n, level, event["nonce"]))
        elif outcome == "reject":
            reason = _reason_from_body(body)
            append_rejected(idx, level, reason or ("http_%s" % status))
            cursor = max(cursor, idx + 1)
            write_cursor(cursor)
            rejected += 1
            _print("第 %d 条 %s：服务端校验未过（%s）——已永久跳过" % (n, level, reason or status))
        elif outcome == "config":
            abort_code = EX_CONFIG
            _print("第 %d 条发送中止：HTTP %s（端点/签名配置类问题）——未烧任何记录，"
                   "游标保持不变。" % (n, status))
            break
        else:
            abort_code = EX_NETWORK
            _print("第 %d 条发送失败：网络/服务端暂不可用（已重试 %d 次）——"
                   "未烧任何记录，稍后重跑 send 即可。" % (n, RETRY_MAX))
            break

    _print("——")
    _print("已上行 %d 条 · 永久跳过 %d 条 · 本次本地跳过 %d 条" % (sent, rejected, local_rejects))
    if last_nonce:
        _print("如需删除，请 30 天内凭回执码（sentreceipts/ 目录，末条 %s）"
               "到 %s 申请。" % (last_nonce[:8] + "…", PRIVACY_URL))
    return abort_code if abort_code is not None else EX_OK


def _reason_from_body(body):
    """服务端 4xx 体 {"ok":false,"msg":...,"errors":[{"index","reason"}]} → reason 码。"""
    try:
        data = json.loads((body or b"").decode("utf-8"))
    except (ValueError, UnicodeDecodeError, AttributeError):
        return ""
    if not isinstance(data, dict):
        return ""
    errors = data.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        return str(errors[0].get("reason", ""))
    return str(data.get("msg", ""))


def cmd_clear(_args):
    removed = 0
    path = _feedback_path()
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            removed = len([x for x in f.read().splitlines() if x.strip()])
        os.remove(path)
    for extra in (_offset_path(), _rejected_path()):
        if os.path.isfile(extra):
            os.remove(extra)
    _print("已清空本地待上行反馈 %d 条（游标与跳过台账一并重置）。" % removed)
    _print("回执码台账（sentreceipts/）保留——30 天内删除申请仍可凭码提出。")
    return EX_OK


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        prog="feedback_cli",
        description="QuickCue 终端用户反馈管理（匿名反馈默认关闭；on 即同意动作）")
    sub = p.add_subparsers(dest="cmd", required=True)
    p_on = sub.add_parser("on", help="开启匿名反馈并定档（命令即同意动作）")
    p_on.add_argument("level", nargs="?", default=None, choices=list(LEVELS),
                      help="档位 L1/L2/L3（缺省沿用上次档位，再缺省 L1）")
    p_on.set_defaults(func=cmd_on)
    p_off = sub.add_parser("off", help="随时关闭")
    p_off.set_defaults(func=cmd_off)
    p_show = sub.add_parser("show", help="查看待上行反馈（纯本地）")
    p_show.set_defaults(func=cmd_show)
    p_send = sub.add_parser("send", help="发送待上行反馈（先看最终脱敏形态再确认）")
    p_send.add_argument("--yes", action="store_true", dest="yes",
                        help="跳过确认（非交互环境必带）")
    p_send.set_defaults(func=cmd_send)
    p_clear = sub.add_parser("clear", help="清空本地待上行反馈（回执码台账保留）")
    p_clear.set_defaults(func=cmd_clear)
    return p


def main(argv=None):
    # Windows 控制台中文输出（pytest 捕获对象无 reconfigure 时静默跳过）
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:                               # noqa: BLE001
            pass
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:                             # argparse 用法错误 → 2
        return EX_USAGE if e.code not in (0, None) else int(e.code or EX_OK)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
