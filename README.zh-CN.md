# CS Relay Template

[English](README.md)

一个可以复用、检查来源、恢复任务的人像今昔接力视频工作流模板：收集原图 → 审核身份和时期 → 生成对齐的端点图 → 生成年龄转变与人物交接片段 → 检查画面 → 剪辑与字幕。

本地控制器支持 macOS 和 Linux。ComfyUI 可以在本机运行，也可以部署在自行配置的 SSH 工作机上；ChatCut 是可选的剪辑工具。任务格式不依赖特定云平台、显卡型号、个人目录或账户。

这是工作流模板，不保证模型生成质量，也不是端到端无人值守产品。选图、生成端点和视觉审核仍需要人工判断；模型、权重、节点和第三方编辑器须另行准备。

## 包含什么

- 原图与生成端点的登记、哈希、来源和审核记录。
- 职业选手“战队＋年份”及主播“Past/Present”两套素材规则。
- 年龄转变＋人物交接的双段周期，以及最后一个人的半周期。
- 提示词模板、任务打包、可配置的 SSH 传输与结果登记。
- 字幕事件、SRT、本地预览拼接、画面检查联系表。
- 可移植的 ChatCut／其他编辑器交接规范。

仓库不包含真实选手照片、生产历史、生成视频、云实例配置或认证材料，也没有内置登录态抓图工具及 ChatCut 自动化客户端。

## 开始使用

需要 Python 3.10 或以上版本。通过系统包管理器安装 FFmpeg 和 ffprobe；使用 SSH 传输时还需要 `ssh`、`rsync`。可选的 `qa` 依赖用于画面检查脚本。

在仓库根目录执行：

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[qa,dev]'
python scripts/cs_relay.py --help
python scripts/create_demo.py jobs/demo-relay
```

演示会生成合成几何图片，登记两个人物、四种状态，再打包三段接力计划。不需要真人照片、网络请求或 GPU；其中审核记录只描述合成测试素材，并未生成或审核视频。

准备使用自己的照片时，执行 `python scripts/cs_relay.py init jobs/example-relay --asset-policy user-provided` 创建空任务。如果需要 SSH 工作机，复制 [配置示例](config.example.json)，填入自己的主机别名与 ComfyUI 工作目录，然后初始化任务：

```sh
cp config.example.json config.local.json
# 编辑 config.local.json，填入自己的部署参数。
python scripts/cs_relay.py init jobs/remote-example \
  --asset-policy user-provided --config config.local.json
```

示例中的主机与根目录故意留空；空配置可以做本地素材工作，不能误连某台默认服务器。[配置文档](docs/configuration.md)说明了每个字段及工作机约定。

按[制作流程](docs/workflow.md)登记双方的原图和端点后，可以使用不含真人素材的[两人接力示例](examples/chain-plan.json)：

```sh
python scripts/build_relay_chain.py jobs/example-relay examples/chain-plan.json
python scripts/cs_relay.py make-srt jobs/example-relay
python scripts/cs_relay.py validate jobs/example-relay
python scripts/cs_relay.py package jobs/example-relay
```

`package` 在素材门禁通过后生成请求与素材包。上传成功只代表文件已传输；工作机仍需通过[请求／结果约定](docs/configuration.md)执行推理。生成结果登记回本地任务后，检查实际视频，再交给编辑器。

## 必须保持的规则

- 历史职业照片：先看 HLTV 选手 Profile 确定当年战队，再在对应日期的 Ranking 页五人战队卡片中直接取得定妆照。不要进入 Team 动态页寻找历史照。
- 主播照片：Past 和 Present 原图都齐全并审核后再生图；不确定具体年份时用 Past/Present。
- 身份参考与布局参考分开；原图保留不改，生成结果作为新版本保存。
- 一个周期是 `A Present → A Past → B Present`；前段尾帧和后段首帧引用同一个预处理后的 A Past 端点。
- 最后一个人可追加 `Present → Past` 半周期。
- 字幕使用 `Name | Team | Year` 或 `Name | Present/Past`，无黑色底边，并随转变和交接切换。

当前示例使用每段 107 帧、24 fps，即约 4.458 秒；双段周期约 8.917 秒。这是特定模型与节点的 preset，不是所有模型都严格输出五秒。换后端必须检查实际帧数，再对齐字幕。

同一输入端点不能保证模型输出首尾逐像素一致。脱衣模板也可能产生露腹、短内衫、队标变形、身份漂移等问题；必须检查实际动作、接缝、字幕和音频，不能只看工具返回“成功”。

## 进一步阅读

- [制作流程](docs/workflow.md)
- [配置与可替换工作机](docs/configuration.md)
- [可复用人物资产](docs/assets.md)
- [剪辑交接与字幕](docs/editing.md)
- [公开发布边界](docs/publication.md)

本地检查命令：`python -m unittest discover -s tests`。JSON Schema 描述结构，本地 `validate` 还检查文件和工作流约束；技术检查通过与人工视觉通过是两件事。

代码、文档、仓库内的提示词模板采用 [MIT](LICENSE) 许可。照片、商标、模型权重和第三方服务仍适用各自的权利与条款；仓库许可不包含这些素材的使用授权。
