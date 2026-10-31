# QuickCue 会议包（packs/meeting）

会议侧「快提示」规则包：当客户提出疑问时，从历史会议验证过的高分话术（知识库直出）
即时生成一张应答参考卡，帮助使用者 60 秒内给出体面、贴合自身口径的回答。

本包包含：

| 文件 | 说明 |
| --- | --- |
| `rules.example.yaml` | 闸参数与判据面全量示例（冷却/上限/疑问词表/模板/卡面参数），**展示性示例**：当前发行版引擎未接入规则文件自动加载（规则包加载属后续版本能力），修改本文件暂不改变引擎行为 |
| `../../examples/meeting_demo_input.txt` | 示例对话输入（合成对话，可直接喂给 MCP 服务器的 `cue.analyze` 工具体验） |
| `../../hosts/mcp/meeting_server.py` | 会议侧 stdio MCP 服务器：`cue.analyze`（出卡）/ `rules.validate`（规则面校验）/ `feedback.record`（反馈落本地） |

## 引擎行为速览

- 触发判定：客户角色发言 ∧ 疑问判定 ∧ 完整性判定（L0）→ 疑问强度负例抑制（L1）
- 三层闸：全局冷却 60 秒 / 同型冷却 180 秒 / 场均上限 12 张（默认值，构造时可注入覆盖）
- 出卡：知识库命中即组卡（零大模型调用）；未命中不出卡
- 画像快车道：`role/style/phrases` 参与模板选择；`banned` 子串命中即弃候选（不改写文案）

## 60 → 70 → 80 分用户路径

「改 YAML → 本地生效 → delta 产生」的体验路径（当前发行版处于第 60 分档：引擎内置
默认值开箱即用，规则文件为展示样例）：

1. **60 分（当前）**：装好即用。引擎内置默认参数（60s/180s/12 张）与
   `rules.example.yaml` 逐字一致，无需任何配置。
2. **70 分（下一步）**：复制 `rules.example.yaml` 为 `rules.yaml`，改成自己的口径
   ——比如把场均上限调到 8、往 `banned` 黑名单里加自己忌口的词（如绝对化承诺）、
   把疑问词表按行业增删。规则包加载接入引擎后，本地 `rules.yaml` 即时生效，
   无需改代码。
3. **80 分（再下一步）**：在 `rules.yaml` 基础上沉淀**个人画像**（`role/style/
   phrases/banned`）：`phrases` 填自己的口头话术示例，引擎自动切换到「贴合我的
   口径」模板；通过 `feedback.record` 把"这张卡没用/答得好"的结构化反馈落本地
   `.quickcue/feedback.jsonl`（目录可用环境变量 `QUICKCUE_FEEDBACK_DIR` 覆盖），
   形成可复核的调参依据（delta 产生：默认参数 →
   个人参数的可解释差异全部落盘在 `rules.yaml` 与反馈流水里）。

## 反馈与隐私（默认关闭）

反馈 SDK（本仓含 Python 侧实现 `core/py/feedback.py`；Node 侧 `core/js/feedback.mjs`
接口同形）提供三档
匿名反馈，**默认关闭（L0）**：首启只问一次（包提供 `should_ask_consent(state)`
纯函数，询问 UI 归宿主），随时可关。

| 档位 | 上行内容 |
| --- | --- |
| L1 | 只回传结果卡点选：事件类型 + 结果卡点选值（对/不对/已处理）+ 卡类型 + 时间戳 + 包版本 |
| L2 | L1 + 规则 delta（规则 id、改前值→改后值；字段白名单，不含话术原文） |
| L3 | L2 + 本地脱敏后的失败样例 |

- **本地落盘**：反馈流水一律先落本地 `.quickcue/feedback.jsonl`（目录可用环境变量
  `QUICKCUE_FEEDBACK_DIR` 覆盖）；默认档 L0 时 `record` 直接丢弃（`dropped: true`）、
  不产生文件。
- **脱敏先行**：`sanitize` 在本机把手机号/金额/人名/公司名/邮箱槽位化为
  `<PHONE_N>`/`<AMOUNT_N>`/`<PERSON_N>`/`<ORG_N>`/`<EMAIL_N>`（同序号去重），
  原文永不出本地；`prepare_upload` 只做上行预检与脱敏、**不内置任何网络调用**
  （上行动作归宿主/云端侧），并对最终载荷做原文残留检测——含未槽位化手机号/邮箱
  即拒绝上行（`rejected: true` + reason）。
- **永不上行**：对话原文、录音、转写文本、人名、公司、金额等任何具体业务内容。

## 零依赖声明

本包与引擎仅依赖 Python 标准库（`re` / `json` / `os` / `time` / `datetime`），
无任何 pip 第三方依赖；可独立单测（见 `../../tests/test_parity_meeting.py` 的
行为等价机闸与 `../../tests/test_feedback.py` 的反馈 SDK 机闸）。
