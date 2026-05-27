# Segment Upscale Runner

通用视频分段加工队列节点。它把一个大视频拆成多个连续片段，逐段提交到任意自定义 ComfyUI 工作流，例如高清放大、插帧、去噪、调色、补帧，最后可选把所有成果物拼接成一个视频。

## 核心能力

- 分段预览与逐段执行。
- 每段作为独立 prompt 提交，上一段完成后才开始下一段。
- 重叠帧上下文：后一段可多读前一段尾部帧。
- `SegmentFrameTrimmer` 自动裁掉重叠输出。
- 自动识别 `RIFE VFI` 等插帧倍率。例如 `overlap_frames=8`、`multiplier=2` 时写入 `trim_out=15`。
- 段间清理：内置深度 RAM 清理、ComfyUI 执行缓存清理、CUDA 缓存清理和 Python GC，不再依赖外部 `malloc_trim_node.py`。
- 可删除每段子 prompt 的 history，减少长任务历史记录积压。
- 自动移除子 prompt 里的清理、调试、预览、保存图片分支，避免它们持有大 IMAGE tensor。
- `Segment Run Log Viewer` 可在 ComfyUI 画布里显示 Runner 日志，适合云平台看不到终端的场景。
- checkpoint 自动记录与自动续跑。
- 已完成前段视频可加入合并列表。
- 参考图可按段替换。
- 音频可保持原样、按段从源视频切片，或禁用。
- 可选 ffmpeg 自动合并所有分段视频。
- 提供轻量后端接口：checkpoint 查询/清除、图片/视频列表、图片/视频上传。

## 界面语言

本插件提供 ComfyUI 原生 `locales` 翻译文件：

- `en`：English
- `zh` / `zh-CN`：中文
- `ja` / `ja-JP`：日本語

重启 ComfyUI 后，节点标题、参数名和 tooltip 会跟随 ComfyUI 当前语言显示。若你的前端或翻译插件只识别其中一种语言代码，两个常见代码都已内置。

## 推荐接线

```text
VHS_LoadVideo
  ├─ frame_count -> SegmentUpscaleRunner.total_frames
  ├─ fps         -> SegmentUpscaleRunner.frame_rate
  └─ images      -> 任意加工链路

任意加工链路最终 IMAGE -> SegmentFrameTrimmer -> VHS_VideoCombine.images
```

`SegmentUpscaleRunner` 自身不要接 IMAGE，它只负责改写工作流并提交分段队列。

## 基本参数

- `segment_count`：分成几段。
- `start_segment`：从第几段开始。手动续跑时改这里。
- `overlap_frames`：重叠输入帧数。普通建议 `4~16`；不用重叠填 `0`。
- `load_video_node_id`：`VHS_LoadVideo` 节点 ID。
- `combine_video_node_id`：`VHS_VideoCombine` 节点 ID。
- `trimmer_node_id`：`SegmentFrameTrimmer` 节点 ID。

如果使用 `VHS_LoadVideoFFmpeg`，Runner 会自动把 `load_skip` 换算成 `start_time = 原始 start_time + load_skip / frame_rate`，并写入 `frame_load_cap`。如果节点 ID 填错或填到了错误类型的节点，但图里只有一个对应节点，Runner 会自动纠正并在日志里提示。

新版 Runner 会把每段子任务提交给当前浏览器客户端，ComfyUI 前端应能继续显示子任务正在执行的节点；日志中也会显示 `前端执行状态转发=开/关`。段后清理会优先调用本插件内置的 `SegmentDeepRAMCleanNode`，并输出清理前后的 RAM/VRAM。`merge_segments` 新建节点默认开启，旧工作流如果关闭过该开关仍会按旧配置执行。

## 裁剪与插帧

- `trim_multiplier_override=0`：自动识别 Trimmer 上游的 RIFE/VFI 倍率。
- 自动失败时可手动填 `2`、`4` 等。

公式：

```text
trim_out = (overlap_frames - 1) * multiplier + 1
```

例如 `overlap_frames=8`、`multiplier=2`，后续段会裁掉 `15` 个输出帧。

`SegmentFrameTrimmer.clone_output` 默认关闭。只有当你确认裁剪后的 PyTorch 切片视图仍然导致原始大张量被长期引用时再打开；它会复制裁剪结果，释放引用更彻底，但保存前会临时增加 RAM 占用。

## 段间清理

推荐保持默认：

```text
cleanup_between_segments      = true
deep_cleanup_between_segments = true
prune_cleanup_debug_nodes     = true
clear_segment_history         = true
unload_models_between_segments = false
cleanup_node_selectors        = DeepRAMCleanNode,VRAM_Debug,easy showAnything,PreviewImage,SaveImage
```

说明：

- `deep_cleanup_between_segments` 会在每段 prompt 完成后调用插件内置的 `SegmentDeepRAMCleanNode` 清理内核。
- 清理顺序是先深度清理，再重置 ComfyUI executor cache。这样深度清理仍能看到 cache 中的大 tensor 并尝试释放底层 storage。
- `clear_segment_history` 会在拿到输出视频路径后删除该子 prompt 的 history。长任务建议开启。
- `unload_models_between_segments` 更激进，会卸载模型，下一段会重新加载，速度会慢一些。只有持续 OOM 时再打开。
- 如果工作流里还有纯预览、调试、临时保存节点，把它们的 class type 或节点 ID 加进 `cleanup_node_selectors`。

原来的单文件 `custom_nodes/malloc_trim_node.py` 可以不再安装。本插件同时注册了兼容类名 `DeepRAMCleanNode` 和新类名 `SegmentDeepRAMCleanNode`，方便旧工作流过渡；为避免重复类名提示，建议确认旧插件不再需要后再禁用或移走它。

## 画布内日志

云平台不方便看终端时，可以添加 `Segment Run Log Viewer` 节点：

```text
runner_node_id = SegmentUpscaleRunner 的节点 ID
max_lines      = 300
```

运行这个日志节点后，它会在节点 UI 里显示最近日志，并输出一份 `STRING`。`runner_node_id` 留空会显示当前内存中所有 Runner 日志。若只想清理某个 Runner 的日志，填入 ID 后开启 `clear_after_read` 再运行一次。

## Checkpoint 与续跑

推荐保持默认：

```text
enable_checkpoint          = true
auto_resume_checkpoint     = true
clear_checkpoint_on_finish = true
```

每段成功后会写入：

```text
sur_checkpoint_{节点ID}.json
```

中断或 OOM 后重新运行，若 checkpoint 存在，会自动从 `next_seg` 继续，并把已经完成的分段视频加入合并列表。

如果你想完全重新开始：

- 关闭 `auto_resume_checkpoint` 运行一次，或
- 调用 `/sur/checkpoint/clear?node_id=你的Runner节点ID`。

## 前段素材合并

`pre_segment_paths` 可以手动填已经完成的视频路径，逗号分隔。开启 `merge_segments=true` 后，这些视频会排在本次新生成视频之前一起合并。

## 参考图逐段替换

如果某个图片节点需要每段换不同参考图：

```text
reference_image_node_id   = LoadImage 节点ID
segment_reference_images  = image1.png,image2.png,image3.png
```

图片数量少于段数时，后续段沿用最后一张。

## 音频模式

`audio_mode` 有三种：

- `keep_original`：不改 `VHS_VideoCombine` 的音频接线。
- `segment_from_loadvideo`：从 `VHS_LoadVideo.video` 对应源视频按段切音频，写到当前段的 `VHS_VideoCombine.audio`。
- `disable_audio`：移除音频。

普通放大/插帧推荐先用 `keep_original`。如果每段输出视频需要独立音频，再改 `segment_from_loadvideo`。

## 自动合并

```text
merge_segments          = true
merged_filename_prefix  = sur_merged
```

要求系统可直接运行 `ffmpeg`，且各段编码参数一致。

## 使用步骤

1. 填写 `load_video_node_id`、`combine_video_node_id`、`trimmer_node_id`。
2. `execute=false` 运行一次，检查预览计划。
3. 确认日志里后续段类似：

```text
trim_in=8  trim_out=15
```

4. `execute=true` 运行。Runner 会中断当前 prompt，然后后台逐段提交真正的子 prompt。
5. 中断后再次运行，默认会自动从 checkpoint 续跑。
