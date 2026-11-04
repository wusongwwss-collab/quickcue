# QuickCue · 会中辅助

> 客户问出口的那一刻，给你一句能接下去的话。

**v0.2.0 已发布**（2026-11-05）：新增终端用户反馈管理 CLI；零依赖判定引擎、MCP 服务器、规则包示例与反馈 SDK 全部在本仓库。

## 它做什么

- 会议进行中，客户抛出问题或异议时，屏幕角落递一句你能直接说出口的话
- 不调大模型：命中话术库直接组卡，半秒内出卡，判定链零 LLM
- 角色判定、疑问强度、三层闸（全局冷却 60 秒 / 单场 12 张 / 去重）都是可解释的规则
- 话术与触发条件全在你自己的规则包里，改动即时生效
- 不是会后纪要：只服务「这一句怎么接」

## 文档

完整介绍、快速开始、规则包与 FAQ：

**https://www.easurwork.cn/open-source/quickcue/**

## 许可

Apache-2.0

---

## 快速开始（v0.1.0 已发布）

QuickCue 会议侧采用「一个零依赖判定引擎 + 一个 MCP 服务器」的形态：判定引擎（`core/py/quickcue_engine.py`，纯 Python 标准库实现）本地完成角色判定、疑问强度打分、三层出卡闸（全局冷却 60 秒 / 同型冷却 180 秒 / 单场上限 12 张）与话术组卡——零 LLM、零联网、零第三方依赖。

### 1. 挂进 MCP 宿主

服务器为 stdio 形态（Python 3.10+，无需 pip 安装任何依赖）：

```bash
python <仓库路径>/hosts/mcp/meeting_server.py
```

宿主配置段示例（JSON，路径替换为你本机的仓库路径）：

```json
{
  "mcpServers": {
    "quickcue-meeting": {
      "command": "python",
      "args": ["/你的路径/quickcue/hosts/mcp/meeting_server.py"]
    }
  }
}
```

配置保存后**重启宿主**，在工具列表确认 `cue.analyze` / `rules.validate` / `feedback.record` 真实可见（以宿主工具列表为准）。

### 2. cue.analyze：出卡工具

输入对话文本（含发言角色标注更佳，`role` 默认「客户」），对命中规则的客户疑问句返回一张「一句话提示卡」（话术参考 + 证据行 + 有效期），10 秒未处理自动过期，出卡频率由冷却闸克制。配套工具：`rules.validate` 校验规则面结构（建议每次改规则后先跑它）；`feedback.record` 记录对卡片的三态评价（对 / 不对 / 已处理），默认只落本地。

### 3. 内置演示知识库：零配置出第一张卡

`cue.analyze` 未注入知识库、也未配置外部知识库（环境变量 `QUICKCUE_KB_FILE`）时，自动回退到仓内**内置演示知识库**（常见客户疑问→话术），保证零配置喂一句客户疑问就出第一张演示卡。内置演示库仅用于开箱演示；生产用宿主注入自有知识库，外部注入优先于演示库。

可以直接试示例对话：把 `examples/meeting_demo_input.txt` 里的客户疑问句逐句喂给 `cue.analyze`，观察哪些出卡、哪些被抑制。

### 4. 规则包位置

会议侧规则包在 `packs/meeting/rules.example.yaml`（闸参数 / 判据词表 / 话术模板全量示例，全部字段带中文注释；当前发行版引擎内置默认值与它逐字一致，规则文件自动加载属后续版本能力）。改规则流程与反馈隐私口径见 `packs/meeting/README.md` 与 `skills/SKILL.md`。

## 终端用户反馈管理（v0.2.0 新增）

匿名反馈**默认关闭**。反馈记录由 MCP 宿主侧产生（`feedback.record`，落本机
`.quickcue/feedback.jsonl`，不出网）；是否上行、何时上行完全由你在终端决定——
`on` 命令即同意动作，`send` 前逐条展示「将上行的最终脱敏形态」并确认。

```bash
python hosts/cli/feedback_cli.py on [L1|L2|L3]   # 开启并定档（命令即同意；缺省 L1）
python hosts/cli/feedback_cli.py off             # 随时关闭（停累积与上行）
python hosts/cli/feedback_cli.py show            # 查看待上行反馈（纯本地）
python hosts/cli/feedback_cli.py send [--yes]    # 发送（先看最终形态再 y/N 确认）
python hosts/cli/feedback_cli.py clear           # 清空本地待上行（回执码台账保留）
```

三档上行内容（详见 [隐私说明页](https://www.easurwork.cn/open-source/feedback)）：
L1=你的点选评价（对/不对/已处理）；L2=加规则改动方向（改前→改后）；L3=加本地脱敏后的故障样例。
永不上行：会议内容原文、目录路径、账号/邮箱/设备号、IP。

本地文件（目录 `.quickcue/`，可用环境变量 `QUICKCUE_FEEDBACK_DIR` 指定）：
`state.json` 同意位｜`sent.offset` 已处理游标｜`sentreceipts/` 上行回执（**回执码**，
30 天内可凭码申请删除；`clear` 不删除）。

发送细节：目标 `https://www.easurwork.cn/api/oss-feedback/v1`，HMAC 签名传输
（密钥经环境变量 `NW_OSS_FEEDBACK_HMAC` 注入）；单条失败（422 校验不过）永久跳过
并留台账；网络/服务端故障自动退避重试（≤3 次），不丢记录；`y/N` 确认仅在交互
终端出现，脚本/管道环境请加 `--yes`。零第三方依赖，Python 3.10+ 标准库即可。
