---
name: quickcue
display_name: 会中辅助
display_name_en: QuickCue
description: 客户问出口的那一刻，给你一句能接下去的话——本地规则判定半秒出卡，零联网零模型
description_zh: 会议中客户抛出问题或异议时对对话文本做疑问判定与强度打分，命中规则出一张话术提示卡；三层闸克制打扰，话术全在自己规则包里
description_en: One line you can say the moment the customer asks the hard question, local rule engine, zero LLM and zero network
category: 商业服务
version: 0.1.0
author: 弈枢AI Easur Work
---

# 会中辅助（QuickCue）

客户问出口的那一刻，屏幕角落递一句**你能直接说出口的话**。不是会后纪要——只服务「这一句怎么接」。

## 什么时候用 / 不用

- 用：会议（尤其客户/售前/谈判场景）进行中，把客户发言逐句喂进来，命中疑问即得提示卡。
- 不用：会后总结、全文转写、生成纪要（刻意不做）。

## 工具面（配合同名连接器 quickcue 使用）

| 工具 | 作用 |
| --- | --- |
| `cue.analyze` | 输入对话文本（含角色标注更佳），对命中规则的客户疑问句返回提示卡（话术参考＋证据行＋有效期，10 秒未处理自动过期） |
| `rules.validate` | 校验规则包 YAML，改完规则先跑它 |
| `feedback.record` | 记录对卡片的三态评价（对/不对/已处理），默认只落本地 |

## 环境要求（重要）

连接器内核为 **Python 3.10+ 标准库实现**（零第三方依赖，无需 pip 安装）。安装前自检——终端能跑通 `python --version` 且 ≥3.10 即可用；不满足时先装 Python（python.org/downloads）。

## 三层出卡闸（可解释、可调）

全局冷却 60 秒 / 同型冷却 180 秒 / 单场上限 12 张——保住它不被关掉的设计。参数与判据词表全在 `templates/rules.example.yaml`（全字段中文注释）。

## 零配置出第一张卡

`cue.analyze` 未注入知识库时自动回退**内置演示知识库**（常见客户疑问→话术），喂一句「你们这个价格能不能再优惠一点」即可看到演示卡；生产用宿主注入自有话术库（外部注入优先）。

## 边界与隐私

零 LLM、零联网、零账号、零第三方依赖；匿名反馈默认关闭，开启也只上行三态统计（本地脱敏后）。开源仓 github.com/wusongwwss-collab/quickcue（Apache-2.0）。
