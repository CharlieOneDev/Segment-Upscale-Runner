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
- `models/FlashVSR-v1.1` 存在。本节点使用的就是 FlashVSR-v1.1，核心权重为 `diffusion_pytorch_model_streaming_dmd.safetensors`，并配合该目录下的 `TCDecoder.ckpt`、`LQ_proj_in.ckpt`。
- 使用 `GIMM-VFI` 时，`models/interpolation/gimm-vfi` 存在。
- `ffmpeg` 和 `ffprobe` 在 PATH 中可用。
- 如果要用 `RIFE` 或 `FILM`，还需要 `ComfyUI-Frame-Interpolation`。

常用参数：

```text
video_path              输入视频路径，或 ComfyUI/input 下的文件名；节点里的“选择/上传视频”按钮会自动填入
output_path             输出 mp4 路径；留空则输出到 output/VSRFI
scale                   放大倍率；0 表示只插帧不放大
interpolation_factor    插帧倍率；25fps 配 2 会输出 50fps
vfi_method              默认优先 RIFE；没有 ComfyUI-Frame-Interpolation 时回退到 GIMM-VFI
frames_per_chunk        每个 chunk 新处理的源帧数
bridge_frames           带入下一个 chunk 的上一块尾帧数
max_tile_kilopixels     0 表示让 VSRFI 根据显存自动选择瓦片大小
max_gimm_kilopixels     0 表示让 GIMM-VFI 自动选择光流限制
skip_first_frames       从源视频开头跳过多少帧
frame_load_cap          0 表示处理到视频结尾
shared_gpu_guard        共享 GPU 运行中占位保护；默认 auto
shared_gpu_guard_buffer_mb 运行中占位时保留的空闲显存；默认 4096
```

视频输入：

- 点节点上的“选择/上传视频”会把视频上传到 ComfyUI 的 `input` 目录，并把返回文件名写入 `video_path`。
- 节点面板会列出 `input`、`output`、`temp` 里找到的视频文件，选择后自动填入 `video_path`。
- 对 `input`、`output`、`temp` 内的视频，节点会显示浏览器原生视频预览；完整绝对路径仍可手动填写，但通常不能在前端预览。
- 已经在 `input` 里的文件，可以直接填文件名，例如 `demo.mp4`。
- 也可以填完整路径，但为了工作流可迁移，优先推荐上传或放到 `input`。

共享 GPU 或显存波动环境的起步建议：

```text
vfi_method              = RIFE
scale                   = 2
interpolation_factor    = 2
frames_per_chunk        = 19
bridge_frames           = 1
max_tile_kilopixels     = 6300  # 更保守；显存很稳定时可改回 0 自动
max_gimm_kilopixels     = 0
shared_gpu_guard        = auto
shared_gpu_guard_buffer_mb = 4096
frame_load_cap          = 0
```

如果只跑原版 VSRFI、不带本节点的桥接帧，`frames_per_chunk=20` 是很好的参考值。本节点为了补跨块插帧，会在第 2 块起把上一块尾帧带进来，所以非首块实际送入 FlashVSR 的源帧数是：

```text
effective_vsr_frames = frames_per_chunk + bridge_frames
```

因此推荐 `19/1`：

```text
frames_per_chunk = 19
bridge_frames    = 1
effective        = 19 + 1 = 20
```

这样每个非首块接近原版 VSRFI 的 20 帧窗口，同时保留 1 帧边界上下文给插帧。`20/1` 会变成 21 帧，通常也能跑，但在共享 GPU 环境里余量更小；`21/1` 会变成 22 帧，FlashVSR 会把它补到下一个时间窗口，日志里常见等效 29 帧，显存峰值会明显跳高，更容易 OOM。

如果 chunk 边界仍有闪烁或轻微跳变，可以试 `18/2`，它仍然是 `18 + 2 = 20`。桥接帧越多，边界上下文越多，但重复处理也越多，速度会下降。

RIFE 是默认推荐的插帧方式：它速度快、显存曲线更容易预测，适合长视频流式任务。GIMM-VFI 仍可选，适合你更看重运动质量、并且显存余量足够的场景。

### 运行中 chunk 间隙显存保护

`SUR VSRFI Stream Runner` 内置 `shared_gpu_guard`，默认 `auto`。它和外部 `vram_guard.py` 分工不同：

```text
外部 vram_guard.py      保护 ComfyUI 空闲期
shared_gpu_guard        保护当前流式任务的 chunk 间隙
```

运行方式是：

```text
读源帧 / 写出 / 音频 mux 等 CPU 间隙：占住空闲 VRAM
每个 VSRFI chunk 开始前：释放占位张量并 empty_cache()
FlashVSR / VFI 正在计算：不占位，把显存留给 ComfyUI
chunk 完成并清理后：重新占位，直到下一个 chunk
```

这层保护不能降低单个 chunk 内部的峰值，也不能阻止别人已经占住的显存；它防的是“你的 chunk 刚释放出显存，别的进程趁下一块开始前抢走”的时间窗。因此它要和稳定 chunk 设置一起使用，推荐 `19/1` 和固定 `max_tile_kilopixels`。

`shared_gpu_guard_buffer_mb` 是占位时留出来的安全余量。默认 `4096` 比较平衡；如果日志里运行时 `free=` 经常掉到 2GB 以下，可以改成 `6144` 或 `8192`。如果你在独占 GPU 或本地调试，不想要任何额外占位，把 `shared_gpu_guard=off` 即可。

运行日志里可以这样判断：

```text
[SUR Stream Guard] mode=auto buffer=4096MB; holding only between chunks
[SUR Stream Guard] holding 28.0GB during chunk gap; free=4.0GB, buffer=4.0GB
[SUR Stream Guard] released 28.0GB before VSRFI chunk
```

看到 `holding` 和下一块前的 `released` 成对出现，就说明运行中占位保护在工作。

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

## 共享 GPU 显存保护脚本

插件附带 `tools/vram_guard.py`，适用于多人或多任务共享同一张 GPU 的环境。它的作用是在 ComfyUI 空闲时提前占住大部分空闲显存；一旦 ComfyUI 队列开始执行 prompt，就立即停止占位进程，把显存让给 ComfyUI。

推荐启动方式：

```bash
python3 custom_nodes/Comfyui-Segment-Upscale-Runner/tools/vram_guard.py \
  --worker-python /path/to/your/comfyui/venv/bin/python \
  --comfy-url http://127.0.0.1:8188 \
  --buffer 2048 \
  --interval 0.05 \
  --reserve-interval 0.2 \
  --idle-seconds 0 \
  --release-on-queue-error
```

如果当前 `python3` 已经能 `import torch`，可以省略 `--worker-python`。如果系统 Python 没有 torch，而 ComfyUI venv 有 torch，就必须把 `--worker-python` 指到 ComfyUI 的 Python。

日志判断：

```text
[reserve] queue=idle ... guard(pid=xxxx)
```

表示 ComfyUI 空闲，占位保护正在工作。

```text
[vram_guard worker] grabbed=... free=2.0GB
[vram_guard worker] topup=...
```

表示脚本已经占住显存，并会在空闲显存重新变多时继续补占。

```text
[release] ComfyUI busy ... -> stop VRAM worker
[release] queue=busy ... guard(pid=-)
```

表示 ComfyUI 开始工作，占位进程已释放。外部脚本不会再保护运行中的 ComfyUI；`SUR VSRFI Stream Runner` 的 `shared_gpu_guard=auto` 会继续保护 chunk 间隙，但 chunk 内部仍然只能靠 `19/1`、固定 tile 和足够 buffer 来留余量。

运行中建议盯 `free=`：

```text
free >= 5GB      通常比较稳
free 2GB~5GB     可以跑，但有波动风险
free < 2GB       危险，后续某次卷积/解码可能突然 OOM
```

如果运行中经常掉到 2GB 以下，优先把 `max_tile_kilopixels` 固定到 `6300`，还不稳就降到 `5500` 或 `5000`。同时可以在启动 ComfyUI 前设置：

```bash
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
```

这可以缓解 PyTorch “reserved but unallocated” 较大时的碎片化问题，但不能替代降低 chunk 或 tile 峰值。

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
