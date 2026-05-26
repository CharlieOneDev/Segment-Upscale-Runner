# Segment Upscale Runner

ComfyUI 分段放大队列节点，专为高清放大场景设计。

## 功能

- 将长视频自动分成 N 段依次提交执行，每段完成后才执行下一段
- 每段独立保存为单独的 mp4 文件，命名规则：`sur_seg01_20250525_120000_.mp4`
- 支持断点续跑：修改 `start_segment` 从任意段继续
- 分段计算帧数自动对齐到 4 的倍数（兼容多数放大模型）

## 安装

将整个 `Comfyui-Segment-Upscale-Runner` 文件夹放入：

```
ComfyUI/custom_nodes/Comfyui-Segment-Upscale-Runner/
```

## 接线方式

```
VHS_LoadVideo
  ├─ frame_count ──→ [total_frames]
  ├─ fps         ──→ [frame_rate]
  └─ (视频数据流接放大节点...)
                              ↓
                    (放大节点 → VHS_VideoCombine)

SegmentUpscaleRunner
  ├─ total_frames     ← VHS_LoadVideo.frame_count
  ├─ frame_rate       ← VHS_LoadVideo.fps
  ├─ segment_count    = 4  (分几段)
  ├─ start_segment    = 1  (从第几段开始，断点续跑时修改)
  ├─ execute          = false → 预览 / true → 执行
  ├─ load_video_node_id   = "节点ID"   ← 右键节点标题栏查看
  └─ combine_video_node_id = "节点ID"
```

## 使用步骤

1. 将节点加入工作流，连接 `VHS_LoadVideo` 的 `frame_count` 和 `fps`
2. 填写 `load_video_node_id` 和 `combine_video_node_id`（右键节点标题 → Copy Node ID 或查看节点 ID）
3. 设置 `execute = false`，运行一次 → 查看预览计划，确认分段是否合理
4. 设置 `execute = true`，运行 → 自动逐段执行，每段保存独立视频

## 断点续跑

中断后，将 `start_segment` 改为上次失败的段号，重新运行即可。

## 输出文件

保存在 `VHS_VideoCombine` 节点原设置的目录下，文件名格式：
```
sur_seg01_20250525_143022_.mp4
sur_seg02_20250525_143022_.mp4
...
```

## CNB 容器注意事项

节点会自动探测 ComfyUI 端口（依次尝试 8188/8000/9000/8080），
也可通过环境变量指定：
```
COMFYUI_HOST=127.0.0.1
COMFYUI_PORT=8188
```
