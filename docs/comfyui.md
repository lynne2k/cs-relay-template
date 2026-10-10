# ComfyUI visual workflows / 可视化工作流

Two standalone JSON files can be loaded into the ComfyUI canvas. They generate video from prepared endpoint portraits; they do not generate portraits, download photographs, or edit a whole relay automatically.

这两份 JSON 可导入 ComfyUI 画布，用已准备好的首尾端点图生成视频；不包含端点生图、自动抓图或整条视频的自动剪辑。

| Workflow / 工作流 | Input pair / 首尾图 | Download / 下载 |
| --- | --- | --- |
| Shirt-removal age transition / 脱衣今昔变化 | A Present → A Past | [JSON](../templates/comfyui/age_transition_shirt_removal_v2.json) · [Raw download](https://raw.githubusercontent.com/lynne2k/cs-relay-template/main/templates/comfyui/age_transition_shirt_removal_v2.json) |
| Player handoff / 人物交接 | A Past → B Present | [JSON](../templates/comfyui/player_handoff_v2.json) · [Raw download](https://raw.githubusercontent.com/lynne2k/cs-relay-template/main/templates/comfyui/player_handoff_v2.json) |

A and B denote consecutive subjects; Present and Past denote their present-day and historical appearances. A、B 表示相邻的两个人物，Present、Past 分别表示现在和过去。

## Dependencies / 依赖

The serialization target is **ComfyUI v0.39.2**, revision `3c1b7a17fdf239d35ce98cc777756f572d089795`. All 17 processing nodes are ComfyUI core nodes at this revision, including `MiniMaxH3ImageToVideo`, the audio/video decoders, `CreateVideo` and `SaveVideo`. The `res_multistep` sampler is also included in core. No third-party custom-node pack is required. Follow the [official installation instructions](https://docs.comfy.org/installation/overview) and [H3 guide](https://docs.comfy.org/tutorials/video/minimax/minimax-h3) for runtime and hardware requirements.

工作流按 **ComfyUI v0.39.2** 的核心节点接口整理，不需要另外安装同名第三方节点包。基础 H3 支持出现在更早版本，但保存视频等节点参数已变化，不能把本文件视为任意旧版通用。硬件、显存、Python/CUDA 环境请按官方指南准备，本项目不代为安装或启动服务器。

Download these five files from the official [Comfy-Org/MiniMax-H3 repository](https://huggingface.co/Comfy-Org/MiniMax-H3). Paths are relative to your own ComfyUI root. Links pin model revision `e5eb578a89295337b8ff433a035929ce0279e0b6`; no weights are bundled here.

五个权重需自行下载，目录相对你自己的 ComfyUI 根目录；链接固定了模型版本，不含个人机器路径。

| ComfyUI folder / 放置目录 | File / 文件 |
| --- | --- |
| `models/diffusion_models/` | [minimax_h3_fl2va_pruned_int8_convrot.safetensors](https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/e5eb578a89295337b8ff433a035929ce0279e0b6/diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors) |
| `models/loras/` | [minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors](https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/e5eb578a89295337b8ff433a035929ce0279e0b6/loras/minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors) |
| `models/text_encoders/` | [qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors](https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/e5eb578a89295337b8ff433a035929ce0279e0b6/text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors) |
| `models/vae/` | [minimax_h3_video_vae_fp16.safetensors](https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/e5eb578a89295337b8ff433a035929ce0279e0b6/vae/minimax_h3_video_vae_fp16.safetensors) |
| `models/vae/` | [minimax_h3_audio_vae_fp32.safetensors](https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/e5eb578a89295337b8ff433a035929ce0279e0b6/vae/minimax_h3_audio_vae_fp32.safetensors) |

Our MIT license covers our code, prompts and workflow files, not ComfyUI, model weights or portrait rights. See the [ComfyUI license](https://github.com/Comfy-Org/ComfyUI/blob/v0.39.2/LICENSE) and [MiniMax H3 model license](https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/LICENSE). 请分别遵守软件、模型及照片的条款，模板许可不代表对外部素材的授权。

## Import and run / 导入使用

1. Download a JSON above and drag it into the ComfyUI canvas, or open it with the workflow-open command. / 下载 JSON 后拖入画布，或通过打开工作流菜单导入。
2. Select your prepared portraits in the two `LoadImage` nodes. Generic placeholder images are not bundled assets. / 在两个图片加载节点中选择自己的首尾图；示例文件名不代表仓库附带这些照片。
3. Prepare **both images at 544 × 736 pixels**, matching head size, eye level, pose, framing and background. Review identity and period-specific clothing. / 两张图均提前统一到 544 × 736，保持头部比例、视线高度、姿态、构图和背景一致，确认人物与时期服装。
4. Use the **same processed A Past file** as the age graph's last image and the handoff graph's first image. The handoff ends at B Present. / 前段尾图和后段首图选择同一个处理后的 A Past 文件，后段尾图选择 B Present。
5. Confirm the five model selectors, prompt, seed and output prefix, then queue the graph. Repeat for subsequent subjects. / 检查权重、提示词、种子及输出前缀后运行，按相同规则继续后续人物。
6. Review clips and assemble them in ChatCut or another editor; change captions at actual transitions. / 检查实际片段后交给剪辑器，字幕按转变时刻切换，见[剪辑交接](editing.md)。

A cycle is `A Present → A Past → B Present`. The last subject may receive only the age graph. 一个完整周期包含今昔变化和人物交接两段；最后一个人可仅追加今昔变化半周期。

## Preset / 默认参数

| Setting / 参数 | Value / 值 |
| --- | --- |
| Width × height / 尺寸 | 544 × 736 |
| Frames / 帧数 | 107 |
| FPS / 帧率 | 24 |
| Sampler / 采样器 | `res_multistep` |
| Scheduler / 调度器 | `simple` |
| Steps / 步数 | 4, with the specified turbo LoRA |
| Denoise / 去噪比例 | 1.0 |
| Encoding / 编码 | MP4, H.264, 8-bit sRGB |

107 frames at 24 fps is approximately 4.458 seconds per clip or 8.917 seconds per cycle, not an exact five-second contract. If changing dimensions, prepare both endpoint files at the new dimensions. If changing models, frame count or FPS, verify supported settings and inspect actual output timing before captioning.

每段原生约 4.458 秒，双段周期约 8.917 秒，不额外拉伸时长。修改尺寸时同步处理两张端点图；更换模型或时长参数后重新确认支持范围、成片帧数和字幕时间。

## Reproducibility / 复现与批处理

The UI files are exported from the same 17-node graph builder and prompt files used by [`comfyui_worker.py`](../scripts/comfyui_worker.py). They share graph structure but differ in endpoint roles, prompt and output naming. [`export_comfyui_workflows.py`](../scripts/export_comfyui_workflows.py) reproduces the checked-in JSON; tests check connectivity, widget serialization and API equivalence.

```sh
python scripts/export_comfyui_workflows.py --check
python -m unittest discover -s tests
```

See [worker configuration](configuration.md) for manifest-based batch execution. UI files use workflow format 0.4 (`nodes`/`links`); `.api.json` reports use API prompts (`class_type`/`inputs`), a different representation. `SaveVideo` uses v0.39.2 DynamicCombo keys `format`, `format.codec` and `format.codec.encoding`.

These new files were checked offline for structure, wiring, parameters and API equivalence; **this release did not rerun GPU inference on the pinned version**. Check actual video on your own installation. The existing production example is shown in the repository README.

本次新增文件经过结构、连线、参数及 API 一致性离线检查，**没有为了发布模板而在固定版本上重新执行 GPU 推理**。静态检查不是成片质量验收；基础流程既有演示在仓库首页可查看。

## Interface references / 接口来源

- [Official workflow JSON 0.4 specification](https://docs.comfy.org/specs/workflow_json/0.4)
- [H3 core node](https://github.com/Comfy-Org/ComfyUI/blob/v0.39.2/comfy_extras/nodes_minimax_h3.py)
- [Video nodes](https://github.com/Comfy-Org/ComfyUI/blob/v0.39.2/comfy_extras/nodes_video.py)
- [DynamicCombo API expansion](https://github.com/Comfy-Org/ComfyUI/blob/v0.39.2/comfy_api/latest/_io.py)
