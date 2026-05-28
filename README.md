# Segment Upscale Runner

这是一个 ComfyUI 长视频辅助插件，现在保留两条路线：

1. **通用分段处理模式**：把一个已有 ComfyUI 工作流按帧窗口拆成多个子 prompt，逐段执行，适合普通生图、动作迁移、调色、去噪、轻量视频处理等。
2. **VSRFI 流式处理模式**：专门给 FlashVSR 放大 + VFI 插帧这类重视频任务使用。它从磁盘按块读取视频、跨块携带桥接帧、直接写入 ffmpeg，避免在 ComfyUI 图里制造巨大的 `IMAGE` 视频张量。

已经验证不可行的旧路线，比如依赖物理切片再走普通 ComfyUI `IMAGE -> FlashVSR -> IMAGE -> VFI -> VideoCombine` 链路的模式，已从界面入口退掉。旧参数还保留为兼容字段，但新工作流不要再使用。

## 节点

- `Segment Upscale Runner`：通用分段队列。
- `SUR VSRFI Stream Runner`：流式 FlashVSR + VFI 长视频处理。
- `Segment Final Frame Trimmer`：通用分段模式下，保存前裁掉重复桥接输出帧。
- `Segment VFI Bridge Trimmer`：通用分段模式下，在 VFI 前只保留必要桥接帧。
- `Segment Run Log Viewer`：在画布里查看运行日志。
- `Segment Video Info Probe`：只读取视频元数据，不加载 `IMAGE` 张量。

## 该用哪个模式

长视频放大插帧：优先用 `SUR VSRFI Stream Runner`。

普通工作流分段执行：用 `Segment Upscale Runner`。

`SUR VSRFI Stream Runner` 解决的是之前最痛的地方：不再让每段结果进入 ComfyUI 的大 `IMAGE` 张量链，也不依赖 Windows 在每段后立刻回收 Python Private Commit。它的思路是少制造大对象，而不是事后强行清理。

## SUR VSRFI Stream Runner

依赖：

- 已安装 `custom_nodes/VSRFI-ComfyUI`。
- VSRFI 的 `requirements.txt` 已安装到同一个 ComfyUI venv。
- `models/FlashVSR-v1.1` 存在。
- 使用 `GIMM-VFI` 时，`models/interpolation/gimm-vfi` 存在。
- `ffmpeg` 和 `ffprobe` 在 PATH 中可用。
- 如果要用 `RIFE` 或 `FILM`，还需要 `ComfyUI-Frame-Interpolation`。

常用参数：

```text
video_path              输入视频路径，或 ComfyUI/input 下的文件名
output_path             输出 mp4 路径；留空则输出到 output/VSRFI
scale                   放大倍率；0 表示只插帧不放大
interpolation_factor    插帧倍率；25fps 配 2 会输出 50fps
vfi_method              默认 GIMM-VFI
frames_per_chunk        每个 chunk 新处理的源帧数
bridge_frames           带入下一个 chunk 的上一块尾帧数
max_tile_kilopixels     0 表示让 VSRFI 根据显存自动选择瓦片大小
max_gimm_kilopixels     0 表示让 GIMM-VFI 自动选择光流限制
skip_first_frames       从源视频开头跳过多少帧
frame_load_cap          0 表示处理到视频结尾
```

12GB 显存的起步建议：

```text
scale                   = 2
interpolation_factor    = 2
frames_per_chunk        = 21 或 32
bridge_frames           = 1
max_tile_kilopixels     = 0
max_gimm_kilopixels     = 0
frame_load_cap          = 0
```

如果 chunk 边界有闪烁或轻微跳变，先试：

```text
bridge_frames = 4
```

仍不稳再试：

```text
bridge_frames = 8
```

桥接帧越多，FlashVSR 得到的前文越多，但每个 chunk 会重复处理更多上一块尾帧，速度会下降。

## 流式桥接帧怎么工作

没有桥接帧时，chunk A 和 chunk B 各自插帧，A 的最后一帧和 B 的第一帧之间可能缺少跨边界的中间帧。

开启 `bridge_frames` 后，处理 chunk B 时会把上一块尾帧带进来：

```text
[chunk A 的尾帧] + [chunk B 的新源帧]
```

然后插件会裁掉已经由 chunk A 写出过的重复输出，只保留跨边界插帧和 chunk B 自己的输出。

以 `bridge_frames=1`、`interpolation_factor=2` 为例：

```text
送入 chunk B:     A_last, B_first, B_second...
VFI 输出:         A_last, mid(A_last,B_first), B_first, mid(B_first,B_second)...
裁掉:             A_last
保留:             mid(A_last,B_first), B_first, mid(B_first,B_second)...
```

因此完整视频的帧数会保持连续。对于 `N` 个源帧、插帧倍率 `m`，输出帧数约为：

```text
(N - 1) * m + 1
```

重复头部裁剪公式是：

```text
bridge_frames <= 0: 0
m <= 1:              bridge_frames
其他情况:            (bridge_frames - 1) * m + 1
```

## 通用分段处理模式

通用模式适合你已有的 ComfyUI 图，并且愿意让图里的视频以 `IMAGE` 张量方式流动。

推荐接线：

```text
VHS_LoadVideo.images
  -> 放大 / 去噪 / 调色 / 动作迁移 / 任意处理链
  -> SegmentVfiBridgeTrimmer
  -> RIFE / VFI
  -> SegmentFrameTrimmer
  -> VHS_VideoCombine.images
```

元数据连接：

```text
VHS_LoadVideo.frame_count -> SegmentUpscaleRunner.total_frames
VHS_LoadVideo.fps         -> SegmentUpscaleRunner.frame_rate
```

常用参数：

```text
segment_count             分段数
overlap_frames            模型上下文重叠帧，通常 4-16
vfi_bridge_frames         VFI 前桥接帧，通常 1
trim_multiplier_override  0 自动识别；失败时填 2 或 4
segment_io_mode           现在只保留 frame_window
```

旧的 `physical_slices` 已退役。它曾经把源视频先切成小视频，但每个小视频仍然进入普通 ComfyUI 大张量链路，不能根治 Windows 本地长任务内存积压。

## 多语言界面

插件内置 ComfyUI 原生语言文件：

- `locales/en/nodeDefs.json`
- `locales/zh/nodeDefs.json`
- `locales/zh-CN/nodeDefs.json`
- `locales/ja/nodeDefs.json`
- `locales/ja-JP/nodeDefs.json`

也附带 AIGODLIKE 中文翻译文件：

```text
aigodlike_translations/zh-CN/Nodes/Segment-Upscale-Runner.json
```

如果你的中文界面不读取插件自己的 `locales`，可以把上面这个 JSON 复制到 AIGODLIKE 中文包的 `zh-CN/Nodes/` 目录，然后重启 ComfyUI。

## 注意事项

- `frame_load_cap=0` 表示从 `skip_first_frames` 后一直处理到视频结尾。
- 输出 FPS = 源 FPS × `interpolation_factor`。
- 某些输入尺寸配 `scale=3` 可能触发 VSRFI 上游的 16 倍数约束问题；优先用 `scale=2` 或 `scale=4`，或者先裁剪/缩放输入尺寸。
- 流式节点不是靠 Windows API 清内存，而是绕开普通 ComfyUI 视频张量链，减少需要清理的大对象。
