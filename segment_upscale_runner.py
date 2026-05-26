"""
ComfyUI 分段放大队列节点 (Segment Upscale Runner)
- 专为高清放大场景设计，不需要过渡帧
- 每段独立保存为单独视频
- 支持断点续跑
- 适合 CNB 容器环境
"""

import math, copy, json, time, os, threading, urllib.request, urllib.error
import server, folder_paths
from aiohttp import web

# ── 日志缓冲（前端弹窗读取）──────────────────────────────────────

_sur_log_buf: dict = {}

def _sur_log(uid, msg):
    text = "" if msg is None else str(msg)
    print(text)
    if not uid:
        return
    k = str(uid)
    buf = _sur_log_buf.setdefault(k, [])
    lines = text.splitlines() or [""]
    buf.extend(lines)
    if len(buf) > 3000:
        _sur_log_buf[k] = buf[-3000:]

def _sur_log_clear(uid):
    _sur_log_buf.pop(str(uid), None)


# ── 分段计算 ──────────────────────────────────────────────────────

def calc_segments(total_frames: int, segments: int) -> list[tuple[int, int]]:
    """
    将 total_frames 均匀分成 segments 段，每段返回 (skip, limit)。
    limit 向上对齐到 4 的倍数（兼容多数放大模型要求）。
    """
    base = total_frames // segments
    remainder = total_frames % segments
    result = []
    skip = 0
    for i in range(segments):
        # 前 remainder 段多分 1 帧，保证总帧数精确
        raw = base + (1 if i < remainder else 0)
        limit = ((raw + 3) // 4) * 4  # 对齐到 4 的倍数
        result.append((skip, limit))
        skip += raw  # 注意：skip 按实际帧数推进，不是对齐后的
    return result


# ── ComfyUI 主机探测 ──────────────────────────────────────────────

_SUR_HOST_CACHE = None

def _sur_collect_hosts() -> list[str]:
    candidates = []
    seen = set()

    def add(host, port):
        if not port:
            return
        try:
            port = int(port)
        except Exception:
            return
        host = str(host or "").strip()
        if host in ("", "0.0.0.0", "::", "[::]"):
            host = "127.0.0.1"
        for prefix in ("http://", "https://"):
            if host.startswith(prefix):
                host = host[len(prefix):]
        host = host.strip("/ ")
        key = f"{host}:{port}"
        if key not in seen:
            seen.add(key)
            candidates.append(key)

    inst = getattr(getattr(server, "PromptServer", None), "instance", None)
    if inst:
        add(getattr(inst, "address", None), getattr(inst, "port", None))
        add(getattr(inst, "host", None), getattr(inst, "port", None))

    add(os.environ.get("COMFYUI_HOST"), os.environ.get("COMFYUI_PORT"))

    for port in (8188, 8000, 9000, 8080):
        add("127.0.0.1", port)

    return candidates

def _sur_probe_host(host: str) -> bool:
    for ep in ("/system_stats", "/queue", "/object_info"):
        try:
            with urllib.request.urlopen(f"http://{host}{ep}", timeout=1.5) as resp:
                if getattr(resp, "status", 200) < 500:
                    return True
        except urllib.error.HTTPError as e:
            if e.code < 500:
                return True
        except Exception:
            continue
    return False

def _sur_get_host(force_refresh=False) -> str:
    global _SUR_HOST_CACHE
    if _SUR_HOST_CACHE and not force_refresh:
        return _SUR_HOST_CACHE
    for h in _sur_collect_hosts():
        if _sur_probe_host(h):
            _SUR_HOST_CACHE = h
            return h
    _SUR_HOST_CACHE = "127.0.0.1:8188"
    return _SUR_HOST_CACHE


# ── ComfyUI API 操作 ──────────────────────────────────────────────

def _queue_prompt(workflow: dict, client_id: str = "") -> str:
    payload = json.dumps({"prompt": workflow, "client_id": client_id}).encode("utf-8")
    last_err = None
    for host in [_sur_get_host(), _sur_get_host(force_refresh=True)]:
        try:
            req = urllib.request.Request(
                f"http://{host}/prompt", data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())["prompt_id"]
        except Exception as e:
            last_err = e
    raise last_err

def _wait_for_prompt(prompt_id: str, poll: float = 3.0) -> bool:
    """轮询等待直到 prompt 完成，返回 True=成功 False=出错"""
    while True:
        time.sleep(poll)
        for host in [_sur_get_host(), _sur_get_host(force_refresh=True)]:
            try:
                with urllib.request.urlopen(
                    f"http://{host}/history/{prompt_id}", timeout=10
                ) as resp:
                    history = json.loads(resp.read())
                if prompt_id in history:
                    st = history[prompt_id].get("status", {})
                    if st.get("completed"):
                        return True
                    if st.get("status_str") == "error":
                        return False
                break
            except Exception:
                continue

def _interrupt_current():
    """中断当前正在执行的 prompt"""
    try:
        from comfy import model_management as _mm
        _mm.interrupt_current_processing()
        return
    except Exception:
        pass
    for host in [_sur_get_host(), _sur_get_host(force_refresh=True)]:
        try:
            urllib.request.urlopen(
                urllib.request.Request(
                    f"http://{host}/interrupt", data=b"", method="POST"
                ), timeout=5
            )
            return
        except Exception:
            continue


# ── 工具函数 ──────────────────────────────────────────────────────

def _now_stamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")

def _build_plan_text(total_frames: int, segments: int, start_from: int,
                     load_video_node_id: str) -> str:
    if total_frames <= 0:
        return "✗ total_frames 必须大于 0"
    seg_list = calc_segments(total_frames, segments)
    lines = [
        f"LoadVideo 节点 ID: {load_video_node_id}",
        f"总帧数: {total_frames}  共 {segments} 段  从第 {start_from} 段开始",
        "",
    ]
    for i, (skip, limit) in enumerate(seg_list):
        seg_num = i + 1
        status = "→ 执行" if seg_num >= start_from else "  跳过"
        lines.append(f"  第{seg_num}段  skip={skip}  limit={limit}  {status}")
    lines.append("")
    lines.append("输出文件命名: sur_seg{段号}_{时间戳}.mp4")
    return "\n".join(lines)


# ── 主节点 ────────────────────────────────────────────────────────

class SegmentUpscaleRunner:
    CATEGORY = "video/utils"
    FUNCTION = "run"
    OUTPUT_NODE = True
    RETURN_TYPES = ()
    RETURN_NAMES = ()

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "total_frames": (
                    "INT",
                    {
                        "default": 0, "min": 0, "max": 99999,
                        "forceInput": True,
                        "tooltip": "视频总帧数，连接 VHS_LoadVideo 的 frame_count 输出",
                    },
                ),
                "frame_rate": (
                    "FLOAT",
                    {
                        "default": 24.0, "min": 1.0, "max": 120.0,
                        "forceInput": True,
                        "tooltip": "视频帧率，连接 VHS_LoadVideo 的 fps 输出",
                    },
                ),
                "segment_count": (
                    "INT",
                    {
                        "default": 4, "min": 1, "max": 50, "step": 1,
                        "display": "slider",
                        "tooltip": "将视频分成几段执行，每段单独保存",
                    },
                ),
                "start_segment": (
                    "INT",
                    {
                        "default": 1, "min": 1, "max": 50, "step": 1,
                        "display": "slider",
                        "tooltip": "从第几段开始执行（断点续跑时修改此值）",
                    },
                ),
                "execute": (
                    "BOOLEAN",
                    {
                        "default": False,
                        "tooltip": "False=仅预览分段计划，True=开始执行",
                    },
                ),
                "load_video_node_id": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": "工作流中 VHS_LoadVideo 节点的 ID（在节点标题栏右键可查看）",
                    },
                ),
                "combine_video_node_id": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": "工作流中 VHS_VideoCombine 节点的 ID",
                    },
                ),
            },
            "hidden": {
                "prompt": "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
                "unique_id": "UNIQUE_ID",
            },
        }

    def run(
        self,
        total_frames=0, frame_rate=24.0,
        segment_count=4, start_segment=1,
        execute=False,
        load_video_node_id="", combine_video_node_id="",
        prompt=None, extra_pnginfo=None, unique_id=None,
        **_legacy,
    ):
        total_frames = int(total_frames or 0)
        frame_rate = float(frame_rate or 24.0)
        segments = int(segment_count or 4)
        start_from = max(1, int(start_segment or 1))
        load_nid = (load_video_node_id or "").strip()
        combine_nid = (combine_video_node_id or "").strip()
        uid = unique_id

        def log(msg):
            _sur_log(uid, f"[SUR] {msg}")

        # ── 预览模式 ──────────────────────────────────────────────
        if not execute:
            plan = _build_plan_text(total_frames, segments, start_from, load_nid)
            _sur_log(uid, "[预览模式]\n" + plan)
            # 中断自身（防止后续节点执行）
            threading.Thread(
                target=lambda: (time.sleep(0.01), _interrupt_current()),
                daemon=True,
            ).start()
            return {}

        # ── 执行前校验 ────────────────────────────────────────────
        if total_frames <= 0:
            _sur_log(uid, "[SUR] ✗ total_frames 必须 > 0，请连接 VHS_LoadVideo 的 frame_count 输出")
            return {}
        if not load_nid:
            _sur_log(uid, "[SUR] ✗ load_video_node_id 不能为空")
            return {}
        if not combine_nid:
            _sur_log(uid, "[SUR] ✗ combine_video_node_id 不能为空")
            return {}

        # 优先使用 extra_pnginfo 中的完整工作流（包含所有节点）
        full_prompt = (extra_pnginfo or {}).get("sqr_full_prompt") or prompt
        client_id = str((extra_pnginfo or {}).get("sqr_client_id") or "")

        if load_nid not in (full_prompt or {}):
            _sur_log(uid, f"[SUR] ✗ 找不到 VHS_LoadVideo 节点 ID「{load_nid}」")
            return {}
        if combine_nid not in (full_prompt or {}):
            _sur_log(uid, f"[SUR] ✗ 找不到 VHS_VideoCombine 节点 ID「{combine_nid}」")
            return {}

        seg_list = calc_segments(total_frames, segments)
        segs_to_run = [(i + 1, skip, limit)
                       for i, (skip, limit) in enumerate(seg_list)
                       if i + 1 >= start_from]

        run_stamp = _now_stamp()
        base_prompt = copy.deepcopy(full_prompt)

        # 启动后台线程执行，立即中断当前这次 prompt
        def submit_all():
            log(f"{'═'*20} 开始执行 stamp={run_stamp} {'═'*20}")
            log(f"LoadVideo 节点: [{load_nid}]  VideoCombine 节点: [{combine_nid}]")
            log(f"总帧数={total_frames}  共{segments}段  执行第{start_from}~{len(seg_list)}段")

            for seg_num, skip, limit in segs_to_run:
                log(f"── 第{seg_num}/{len(seg_list)}段  skip={skip}  limit={limit} ──")

                wf = copy.deepcopy(base_prompt)

                # 1. 修改 VHS_LoadVideo：设置 skip_first_frames 和 frame_load_cap
                wf[load_nid]["inputs"]["skip_first_frames"] = skip
                wf[load_nid]["inputs"]["frame_load_cap"] = limit

                # 2. 修改 VHS_VideoCombine：设置唯一文件名前缀，避免覆盖
                seg_prefix = f"sur_seg{seg_num:02d}_{run_stamp}_"
                orig_prefix = wf[combine_nid]["inputs"].get("filename_prefix", "")
                # 保留原有子文件夹路径（如 "output/video/"），只替换文件名部分
                slash = max(orig_prefix.rfind("/"), orig_prefix.rfind("\\"))
                subfolder = orig_prefix[:slash + 1] if slash >= 0 else ""
                wf[combine_nid]["inputs"]["filename_prefix"] = subfolder + seg_prefix
                wf[combine_nid]["inputs"]["save_output"] = True

                # 3. 删除本节点自身，避免递归触发
                if uid and str(uid) in wf:
                    del wf[str(uid)]

                # 4. 提交并等待
                try:
                    pid = _queue_prompt(wf, client_id=client_id)
                    log(f"  已提交 prompt_id={pid[:8]}...  等待完成...")
                    ok = _wait_for_prompt(pid)
                    if ok:
                        log(f"✓ 第{seg_num}段完成  输出前缀: {subfolder + seg_prefix}")
                    else:
                        log(f"✗ 第{seg_num}段执行出错，已跳过")
                except Exception as e:
                    log(f"✗ 第{seg_num}段提交失败: {type(e).__name__}: {e}")

            log(f"{'═'*20} 全部完成 {'═'*20}")

        # 先中断自身，再在后台跑队列
        _interrupt_current()
        t = threading.Thread(target=submit_all, daemon=True)
        t.start()

        return {}


# ── HTTP 接口：前端拉取日志 ───────────────────────────────────────

@server.PromptServer.instance.routes.get("/sur/log")
async def sur_log_api(request):
    uid = request.query.get("node_id", "")
    lines = _sur_log_buf.get(str(uid), [])
    return web.json_response({"lines": lines})

@server.PromptServer.instance.routes.post("/sur/log/clear")
async def sur_log_clear_api(request):
    uid = request.query.get("node_id", "")
    _sur_log_clear(uid)
    return web.json_response({"ok": True})


# ── 节点注册 ──────────────────────────────────────────────────────

NODE_CLASS_MAPPINGS = {
    "SegmentUpscaleRunner": SegmentUpscaleRunner,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "SegmentUpscaleRunner": "Segment Upscale Runner 🎬",
}
