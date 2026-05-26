# Segment Upscale Runner

通用视频分段加工队列节点。它借鉴 `Comfyui-Segment-Queue-Runner` 的逐段 prompt 队列、断点保护、成果物记录和合并思路，但不绑定 `WanVideoAnimateEmbeds`。你可以把一个大视频分成多段，每段进入任意自定义工作流，例如高清放大、插帧、去噪、调色、补帧，最后再把成果物拼接起来。

## 核心能力

- 分段预览与逐段执行。
- 每段作为独立 prompt 提交，上一段完成后才开始下一段。
- 重叠帧上下文：后一段可多读前一段尾部帧。
- `SegmentFrameTrimmer` 自动裁掉重叠输出。
- 自动识别 `RIFE VFI` 等插帧倍率。例如 `overlap_frames=8`、`multiplier=2` 时写入 `trim_out=15`。
- 段间清理：清 ComfyUI 执行缓存、CUDA 缓存、Python GC，并可调用 `malloc_trim_node.py` 深度清理。
- 自动移除子 prompt 里的清理/调试/预览分支，避免它们持有大 IMAGE tensor。
- checkpoint 自动记录与自动续跑。
- 已完成前段视频可加入合并列表。
- 参考图可按段替换。
- 音频可保持原样、按段从源视频切片，或禁用。
- 可选 ffmpeg 自动合并所有分段视频。
- 提供轻量后端接口：checkpoint 查询/清除、图片/视频列表、图片/视频上传。

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

## 裁剪与插帧

- `trim_multiplier_override=0`：自动识别 Trimmer 上游的 RIFE/VFI 倍率。
- 自动失败时可手动填 `2`、`4` 等。

公式：

```text
trim_out = (overlap_frames - 1) * multiplier + 1
```

例如 `overlap_frames=8`、`multiplier=2`，后续段会裁掉 `15` 个输出帧。

## 段间清理

推荐保持默认：

```text
cleanup_between_segments      = true
deep_cleanup_between_segments = true
prune_cleanup_debug_nodes     = true
cleanup_node_selectors        = DeepRAMCleanNode,VRAM_Debug,easy showAnything
```

如果你的工作流里还有纯预览或调试节点，可以把 class type 或节点 ID 加进 `cleanup_node_selectors`。

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

## 与 Segment Queue Runner 的关系

Queue Runner 原本偏向 WanVideo 动作迁移，会给 `WanVideoAnimateEmbeds` 接 transition video。本插件不做这类专用接线，而是保留更通用的能力：分段、清理、断点、参考图、音频、成果物记录、拼接。你的加工链路可以是 FlashVSR、RIFE、VHS、KJNodes 或其他任意 IMAGE→IMAGE/video 流程。
