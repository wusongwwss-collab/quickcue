# -*- coding: utf-8 -*-
"""YQ-1154 T1 机闸 G5：CONTRIBUTING 双用途声明存在性（规范 §2 双用途/§8 G5）。

判据（任务书 T1 · G5）：CONTRIBUTING.md 含「数据回流与贡献双用途」章节＋隐私页链接，
缺即红（内容由总控预写、随 D3 批入库，本闸只验证存在性与要素齐全）：
  1) 章节标题「## 数据回流与贡献双用途」；
  2) 隐私说明页链接 https://www.easurwork.cn/open-source/feedback；
  3) 「双用途声明」正文（开源默认包 + Easur Work 商业产品明示）；
  4) 铁律②「默认关闭」表述（匿名反馈基线口径在场）。
"""

import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_CONTRIBUTING = os.path.join(os.path.dirname(_HERE), "CONTRIBUTING.md")


def _contributing_text() -> str:
    assert os.path.isfile(_CONTRIBUTING), f"CONTRIBUTING.md 缺失：{_CONTRIBUTING}"
    with open(_CONTRIBUTING, encoding="utf-8") as f:
        return f.read()


def test_contributing_has_dual_use_section():
    text = _contributing_text()
    assert "## 数据回流与贡献双用途" in text, "缺「数据回流与贡献双用途」章节标题"


def test_contributing_has_privacy_link():
    text = _contributing_text()
    assert "https://www.easurwork.cn/open-source/feedback" in text, "缺隐私说明页链接"


def test_contributing_has_dual_use_statement():
    text = _contributing_text()
    assert "双用途声明" in text, "缺双用途声明正文"
    assert "Easur Work 商业产品" in text, "缺「可同时用于商业产品」明示"
    assert "默认关闭" in text, "缺「默认关闭」铁律②表述"
