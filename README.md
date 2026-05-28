# Segment Upscale Runner

通用视频分段加工队列节点。它把一个大视频拆成多个连续片段，逐段提交到任意自定义 ComfyUI 工作流，例如高清放大、插帧、去噪、调色、补帧，最后可选把所有成果物拼接成一个视频。

## 核心能力

- 分段预览与逐段执行。
- 每段作为独立 prompt 提交，上一段完成后才开始下一段。
- 模型上下文重叠帧：后一段可多读前一段尾部帧，给时序放大/去噪模型参考。
- VFI 前桥接裁剪：只把少量桥接帧送入 RIFE/VFI，避免整段重叠区重复插帧。
- `SegmentFrameTrimmer` 自动裁掉最终重复桥接输出。
- 自动识别 `RIFE VFI` 等插帧倍率。
- 可删除每段子 prompt 的 history，减少长任务历史记录积压。
- 可跳过子 prompt 里的预览、调试、show、旧清理旁路，避免不必要节点参与每段执行。
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
```

推荐低内存插帧接线：

```text
VHS_LoadVideo.images
  -> 高清放大/去噪/调色等需要上下文的链路
  -> SegmentVfiBridgeTrimmer
  -> RIFE/VFI
  -> SegmentFrameTrimmer
  -> VHS_VideoCombine.images
```

如果没有插帧节点，也可以：

```text
任意加工链路最终 IMAGE -> SegmentFrameTrimmer -> VHS_VideoCombine.images
```

`SegmentUpscaleRunner` 自身不要接 IMAGE，它只负责改写工作流并提交分段队列。

## 基本参数

- `segment_count`：分成几段。
- `start_segment`：从第几段开始。手动续跑时改这里。
- `overlap_frames`：重叠输入帧数。普通建议 `4~16`；不用重叠填 `0`。
- `load_video_node_id`：`VHS_LoadVideo` 节点 ID。
- `combine_video_node_id`：`VHS_VideoCombine` 节点 ID。
- `trimmer_node_id`：最终 `SegmentFrameTrimmer` 节点 ID，放在 RIFE/VFI 之后、VideoCombine 之前。
- `bridge_trimmer_node_id`：`SegmentVfiBridgeTrimmer` 节点 ID，放在放大/去噪之后、RIFE/VFI 之前。
- `vfi_bridge_frames`：进入 RIFE/VFI 前保留多少帧上一段尾部，普通建议 `1`。

如果使用 `VHS_LoadVideoFFmpeg`，Runner 会自动把 `load_skip` 换算成 `start_time = 原始 start_time + load_skip / frame_rate`，并写入 `frame_load_cap`。如果使用普通 `VHS_LoadVideo`，Runner 会保留原始 `skip_first_frames` 作为基础偏移，再叠加每段自己的 `load_skip`。如果节点 ID 填错或填到了错误类型的节点，但图里只有一个对应节点，Runner 会自动纠正并在日志里提示。

Runner 会把每段子任务提交给当前浏览器客户端，ComfyUI 前端应能继续显示子任务正在执行的节点；日志中也会显示 `前端执行状态转发=开/关`。本插件不再提供段间 RAM/VRAM 深度清理功能，因为 Windows 主进程的 Private Commit 通常不能靠工作流节点可靠归还。当前策略是减少无效大张量生成，让每段更容易回到稳定内存平台。

如果使用 `AIGODLIKE-COMFYUI-TRANSLATION` 中文包，它不会读取本插件的 `locales` 目录。可将 `aigodlike_translations/zh-CN/Nodes/Segment-Upscale-Runner.json` 复制到中文包的 `zh-CN/Nodes/` 目录，然后重启 ComfyUI。

## 重叠帧、桥接帧与插帧

这三个概念分开看会更直观：

- `overlap_frames`：给高清放大、去噪等时序模型看的输入上下文。例如填 `8`，第 2 段起会多读前一段尾部 8 帧。
- `vfi_bridge_frames`：真正送进 RIFE/VFI 的上一段尾部帧数。普通填 `1`，这样插帧节点仍会生成上一段尾帧到当前段首帧之间的过渡。
- `trim_multiplier_override`：插帧输出倍率。`0` 表示自动识别；自动失败时手动填 `2`、`4`。

推荐普通高清放大 + RIFE/VFI：

```text
overlap_frames     = 8
vfi_bridge_frames  = 1
trim_multiplier_override = 0
```

这种设置表示：放大模型仍然看到前 8 帧上下文，但 RIFE/VFI 只处理“上一段最后 1 帧 + 当前段”，避免对前 8 帧内部重复插帧。

当前 Comfy-VFI / RIFE VFI 的实际输出是 `输入帧数 * multiplier`。为了既保留段间插帧，又不增加总帧数，插件会做“后一段裁头 + 前一段裁尾”：

```text
head_trim = (vfi_bridge_frames - 1) * multiplier + 1
tail_trim = multiplier - 1  # 除最后一段外
```

例如 `vfi_bridge_frames=1`、`multiplier=2` 时，后一段只裁掉 1 个桥接源帧，保留“上一段尾帧 -> 当前段首帧”的插帧；前一段尾部裁掉 1 个由 VFI 补出的重复帧，合并后总帧数仍正确。

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

## 物理分片模式

默认的 `frame_window` 模式会复用同一个 `VHS_LoadVideo`，每段只改
`skip_first_frames / frame_load_cap`。这适合普通分段、生图、动作迁移等工作流。

如果工作流包含高清放大、RIFE/VFI 插帧等会制造巨大 IMAGE tensor 的节点，可以把
`segment_io_mode` 改成 `physical_slices`：

```text
segment_io_mode        = physical_slices
physical_slice_subdir  = SUR_physical_segments
physical_slice_crf     = 12
reuse_physical_slices  = true
```

开启后 Runner 会先把源视频按分段计划切成独立小视频：

```text
ComfyUI/input/SUR_physical_segments/{timestamp}/source/
```

随后每个子 prompt 只读取当前小视频，`LoadVideo` 的 skip 会被重置为 `0`。成果视频会写入：

```text
ComfyUI/output/SUR_physical_segments/{timestamp}/results/
```

最后自动合并也会从这个 results 目录内的分段成果生成最终视频。物理分片模式下建议把
`audio_mode` 设为 `segment_from_loadvideo`，这样音频会按最终保存帧范围从原始源视频切段，避免重叠上下文导致音频多出前置片段。

为了让主 prompt 不先执行 `VHS_LoadVideo` 并制造 IMAGE tensor，物理分片模式推荐新增
`SegmentVideoInfoProbe`：

```text
SegmentVideoInfoProbe.total_frames -> SegmentUpscaleRunner.total_frames
SegmentVideoInfoProbe.frame_rate   -> SegmentUpscaleRunner.frame_rate
```

`SegmentVideoInfoProbe` 只读取视频元信息，不解码整段视频。它的 `video`、`force_rate`、
`skip_first_frames`、`frame_load_cap` 应与源 `VHS_LoadVideo` 保持一致；源 `VHS_LoadVideo`
节点仍然保留在工作流里，Runner 会在每个子 prompt 中把它改成当前小片段。

## 使用步骤

1. 在 RIFE/VFI 前加 `SegmentVfiBridgeTrimmer`，在 VideoCombine 前加 `SegmentFrameTrimmer`。
2. 填写 `load_video_node_id`、`combine_video_node_id`、`trimmer_node_id`、`bridge_trimmer_node_id`。
3. 设置 `overlap_frames=8`、`vfi_bridge_frames=1` 作为起点。
4. `execute=false` 运行一次，检查预览计划。
5. 确认日志里后续段类似：

```text
ctx=8  pre_vfi_trim=7  vfi_bridge=1  head_trim=1  tail_trim=1
```

6. `execute=true` 运行。Runner 会中断当前 prompt，然后后台逐段提交真正的子 prompt。
7. 中断后再次运行，默认会自动从 checkpoint 续跑。
