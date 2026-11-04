# -*- coding: utf-8 -*-
"""parity 机闸：公开仓引擎 vs 产品仓底本 行为逐字等价测试。

做法：以 importlib 按文件路径分别加载两份实现（互不受 sys.path 包名影响）——
  - 产品仓侧：app/quick_cue.py（路径可用环境变量 NW_QUICKCUE_PARITY_SOURCE 覆盖）
  - 公开仓侧：core/py/quickcue_engine.py
同一组场景（固定 now、固定知识库/画像注入）分别喂给两份实现，逐场景断言：
  1) build_frames 输出 JSON 逐字相等（json.dumps sort_keys 对比）
  2) last_skip_reason（含 gate: 原因串）逐字相等
  3) QuickCueGate.blocked 原因串逐字相等

纪律：所有用例显式传 now，禁依赖真实时间与任何运行数据目录；
产品仓底本缺失时本文件整体跳过（parity 属开发期机闸，非公开仓功能测试）。
"""

import importlib.util
import json
import os
import sys

import pytest

# 公开仓仓根 = 本文件上两级
PUB_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUB_ENGINE = os.path.join(PUB_ROOT, "core", "py", "quickcue_engine.py")
# 产品仓底本路径：经环境变量 NW_QUICKCUE_PARITY_SOURCE 注入（parity 机闸在产品仓同机执行）；
# 未注入或路径不存在时整体跳过，不影响公开仓功能测试。
MAIN_ENGINE = os.environ.get("NW_QUICKCUE_PARITY_SOURCE") or ""

# 双侧路径都注入 sys.path（兜底各自的模块内相对导入诉求；两份实现模块级仅依赖标准库）
for _p in (os.path.dirname(MAIN_ENGINE), os.path.dirname(PUB_ENGINE)):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)


def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MAIN_MISSING = not os.path.isfile(MAIN_ENGINE)

pytestmark = pytest.mark.skipif(
    MAIN_MISSING, reason=f"产品仓底本不存在（parity 机闸需同机执行）：{MAIN_ENGINE}")


@pytest.fixture(scope="module")
def pub_mod():
    return _load("pub_quickcue_engine", PUB_ENGINE)


@pytest.fixture(scope="module")
def main_mod():
    return _load("main_quick_cue", MAIN_ENGINE)


# ------------------------------------------------------------------
# 场景定义：每个场景是「喂给一份实现的一个脚本」，返回可比对的三个量。
# 场景在两份实现上各自独立重放（同参数、同注入），结果必须逐字相等。
# ------------------------------------------------------------------

KB_HIT = ["先回应部署周期：标准两周内完成上线。", "可补充现场勘察与培训安排。"]
NOW0 = 1000.0


def _mk_engine(mod, *, gate_kwargs=None, kb=None, profile="__unset__"):
    gate = mod.QuickCueGate(**(gate_kwargs or {}))
    kb_loader = (lambda q: list(kb)) if kb is not None else None
    profile_loader = (lambda: dict(profile)) if profile != "__unset__" else None
    return mod.QuickCueEngine(gate=gate, kb_loader=kb_loader, profile_loader=profile_loader)


def run_one_fire(mod, *, role="客户", text="这个系统的部署周期要多久？", now=NOW0,
                 gate_kwargs=None, kb=None, profile="__unset__", utterance_id="u-1"):
    eng = _mk_engine(mod, gate_kwargs=gate_kwargs, kb=kb, profile=profile)
    frames = eng.maybe_fire(role, text, utterance_id=utterance_id, now=now)
    return json.dumps(frames, sort_keys=True, ensure_ascii=False), eng.last_skip_reason


def run_two_fires(mod, *, t1, t2, gate_kwargs=None, kb=None):
    """同闸两连发（冷却/上限类场景）：返回第二次的结果与两次的 skip 串。"""
    eng = _mk_engine(mod, gate_kwargs=gate_kwargs, kb=kb)
    f1 = eng.maybe_fire("客户", "部署周期大概要多久？", utterance_id="u-1", now=t1)
    f2 = eng.maybe_fire("客户", "费用这块怎么算的呢？", utterance_id="u-2", now=t2)
    return (json.dumps([f1, f2], sort_keys=True, ensure_ascii=False),
            eng.last_skip_reason)


SCENARIOS = {
    # 1 命中双帧：客户疑问 + 知识库命中 → 完整双帧
    "hit_double_frame": dict(
        kind="one", kwargs=dict(kb=KB_HIT, now=NOW0)),
    # 2 role 非客户 skip
    "role_not_customer_skip": dict(
        kind="one", kwargs=dict(role="销售", kb=KB_HIT, now=NOW0)),
    # 3 L1 强度不足 skip（含"什么"但为陈述句，0 分）
    "l1_strength_skip": dict(
        kind="one",
        kwargs=dict(text="我觉得这个方案没什么问题", kb=KB_HIT, now=NOW0)),
    # 4 L0 不完整 skip（连接词结尾）
    "l0_incomplete_skip": dict(
        kind="one",
        kwargs=dict(text="费用怎么算？但是", kb=KB_HIT, now=NOW0)),
    # 5 知识库未命中 kb_miss
    "kb_miss": dict(
        kind="one", kwargs=dict(kb=[], now=NOW0)),
    # 6 无知识库注入（loader 缺省）同样 kb_miss
    "kb_loader_absent": dict(
        kind="one", kwargs=dict(now=NOW0)),
    # 7 60s 内二连问 → gate:global_cooldown
    "gate_global_cooldown": dict(
        kind="two", kwargs=dict(t1=NOW0, t2=NOW0 + 30.0, kb=KB_HIT)),
    # 8 同型 180s 冷却：过全局未过同型 → gate:same_type_cooldown
    "gate_same_type_cooldown": dict(
        kind="two", kwargs=dict(t1=NOW0, t2=NOW0 + 100.0, kb=KB_HIT)),
    # 9 场均触顶：max_per_meeting=1，第二次过两道冷却仍被上限拦
    "gate_per_meeting_cap": dict(
        kind="two",
        kwargs=dict(t1=NOW0, t2=NOW0 + 180.0, kb=KB_HIT,
                    gate_kwargs=dict(max_per_meeting=1))),
    # 10 冷却窗口边界：恰好 180s（now-last == 180 不含于 <）→ 放行出第二张卡
    "cooldown_boundary_pass": dict(
        kind="two", kwargs=dict(t1=NOW0, t2=NOW0 + 180.0, kb=KB_HIT)),
    # 11 banned 过滤：首条候选命中黑名单被弃、取第二条
    "banned_filter": dict(
        kind="one",
        kwargs=dict(kb=["我们打绝对低价。", "按稳扎稳打的节奏交付。"],
                    profile={"banned": "绝对低价;承诺"}, now=NOW0)),
    # 12 画像注入 sales 模板分支
    "profile_sales": dict(
        kind="one",
        kwargs=dict(kb=KB_HIT, profile={"role": "销售", "style": "务实"},
                    now=NOW0)),
    # 13 画像注入 consultative 模板分支
    "profile_consultative": dict(
        kind="one",
        kwargs=dict(kb=KB_HIT, profile={"style": "顾问式陪访"}, now=NOW0)),
    # 14 画像注入 technical 模板分支
    "profile_technical": dict(
        kind="one",
        kwargs=dict(kb=KB_HIT, profile={"role": "技术支持"}, now=NOW0)),
    # 15 画像注入 phrased 分支（仅 phrases 命中）
    "profile_phrased": dict(
        kind="one",
        kwargs=dict(kb=KB_HIT, profile={"phrases": ["这事儿包在我身上"]}, now=NOW0)),
    # 16 画像全空字段 → default 模板
    "profile_empty_fields_default": dict(
        kind="one",
        kwargs=dict(kb=KB_HIT, profile={"role": "", "style": None}, now=NOW0)),
    # 17 banned 全杀 → 过滤后为空 → kb_miss
    "banned_all_kill_kb_miss": dict(
        kind="one",
        kwargs=dict(kb=["低价方案"], profile={"banned": "低价"}, now=NOW0)),
}


# 每个场景在两份实现上重放并比对（frames JSON / skip 串）
@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_parity_engine_scenario(pub_mod, main_mod, name):
    spec = SCENARIOS[name]
    runner = run_one_fire if spec["kind"] == "one" else run_two_fires
    pub_out = runner(pub_mod, **spec["kwargs"])
    main_out = runner(main_mod, **spec["kwargs"])
    assert pub_out == main_out, (
        f"parity 失败（场景 {name}）:\n  公开仓={pub_out!r}\n  产品仓={main_out!r}")


def test_parity_gate_blocked_reasons(pub_mod, main_mod):
    """QuickCueGate.blocked 三层原因串逐字相等（含边界值口径）。"""
    for gate_kwargs, seq in [
        (dict(global_cooldown=60, same_type_cooldown=180, max_per_meeting=12),
         [0.0, 10.0, 59.999, 60.0, 100.0, 179.999, 180.0]),
        (dict(max_per_meeting=1), [100.0, 280.0, 400.0]),
        (dict(), [5.0, 70.0, 200.0]),
    ]:
        for now in seq:
            g1 = pub_mod.QuickCueGate(**gate_kwargs)
            g2 = main_mod.QuickCueGate(**gate_kwargs)
            # 两闸同步重放同一段出卡历史，再比对每一时刻的判定
            for evt in [100.0, 300.0]:
                if evt < now:
                    g1.commit("question", evt)
                    g2.commit("question", evt)
            assert g1.blocked("question", now) == g2.blocked("question", now), (
                f"blocked 不等 kwargs={gate_kwargs} now={now}")


def test_parity_gate_default_params(pub_mod, main_mod):
    """默认闸参数相等（60/180/12），且默认闸在远时刻判定一致。"""
    g1 = pub_mod.QuickCueGate()
    g2 = main_mod.QuickCueGate()
    assert (g1.global_cooldown, g1.same_type_cooldown, g1.max_per_meeting) == \
           (g2.global_cooldown, g2.same_type_cooldown, g2.max_per_meeting) == \
           (60, 180, 12)
    for now in (1.0, 59.0, 61.0, 1000.0):
        assert g1.blocked("question", now) == g2.blocked("question", now)


def test_parity_judge_functions(pub_mod, main_mod):
    """判据函数面（is_question/is_complete/question_strength/choose_template/
    filter_banned/build_card_data/_first_point）逐对等值。"""
    texts = ["", "？", "费用怎么算", "费用怎么算？", "费用怎么算呢。",
             "我觉得没什么问题", "他说怎样都行", "请问怎么收费", "但是",
             "方案可以吗！", "那之后呢", "能不能便宜点；如果"]
    for t in texts:
        assert pub_mod.is_question(t) == main_mod.is_question(t), t
        assert pub_mod.is_complete(t) == main_mod.is_complete(t), t
        assert pub_mod.question_strength(t) == main_mod.question_strength(t), t
        assert pub_mod.l0_pass("客户", t) == main_mod.l0_pass("客户", t), t
        assert pub_mod.l0_pass("销售", t) == main_mod.l0_pass("销售", t), t
        assert pub_mod.l1_pass(t) == main_mod.l1_pass(t), t
    profiles = [None, {}, {"role": "销售"}, {"style": "咨询顾问"},
                {"phrases": "x"}, {"banned": "a;b"}, {"banned": ""}]
    for p in profiles:
        assert pub_mod.choose_template(p) == main_mod.choose_template(p), p
        cands = ["绝对低价", "正常话术", "", None, "AB c"]
        assert json.dumps(pub_mod.filter_banned(cands, p), sort_keys=True) == \
               json.dumps(main_mod.filter_banned(cands, p), sort_keys=True), p
        assert json.dumps(pub_mod.build_card_data(["一。二", "三"],
                                                  pub_mod.choose_template(p),
                                                  utterance_id="u9", now=42.0),
                          sort_keys=True, ensure_ascii=False) == \
               json.dumps(main_mod.build_card_data(["一。二", "三"],
                                                   main_mod.choose_template(p),
                                                   utterance_id="u9", now=42.0),
                          sort_keys=True, ensure_ascii=False), p
    for s in ["", "第一句。第二句", "没有分隔符的一句很长的话"*5, "a；b；c"]:
        assert pub_mod._first_point(s) == main_mod._first_point(s), s
        assert json.dumps(pub_mod.build_frames({"headline": "h", "script": s}),
                          sort_keys=True, ensure_ascii=False) == \
               json.dumps(main_mod.build_frames({"headline": "h", "script": s}),
                          sort_keys=True, ensure_ascii=False), s
