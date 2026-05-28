"""
ComfyUI 通用视频分段加工队列节点 (Segment Upscale Runner)
- 将大视频拆成多个连续片段，逐段提交到任意自定义 IMAGE/video 工作流
- 每段独立保存为单独视频，可选自动合并
- 支持断点续跑
- 支持模型上下文重叠帧：用前一段尾部 N 帧作为下一段时序上下文
- 支持 VFI 前桥接裁剪：只把少量桥接帧送入插帧节点，减少重复插帧内存
- 保存时由 SegmentFrameTrimmer 节点自动去重，合并后无重叠、无跳帧
"""

import copy, ctypes, gc, hashlib, json, time, os, platform, shutil, subprocess, sys, tempfile, threading, traceback, urllib.request, urllib.error
import importlib.util
import server, folder_paths
from aiohttp import web

# ── 日志缓冲（前端弹窗读取）──────────────────────────────────────

_sur_log_buf: dict = {}
_SUR_JOB_LOCK = threading.Lock()
_SUR_ACTIVE_JOBS: dict[str, threading.Thread] = {}

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


def _sur_log_text(uid: str = "", max_lines: int = 300) -> str:
    max_lines = max(1, int(max_lines or 300))
    uid = str(uid or "").strip()
    if uid:
        lines = _sur_log_buf.get(uid, [])
        if not lines:
            return f"[SUR] 没有找到节点 ID={uid} 的日志。"
        return "\n".join(lines[-max_lines:])

    chunks = []
    for key in sorted(_sur_log_buf.keys()):
        lines = _sur_log_buf.get(key, [])
        if not lines:
            continue
        chunks.append(f"===== Runner Node {key} =====")
        chunks.extend(lines[-max_lines:])
    return "\n".join(chunks[-max_lines:]) if chunks else "[SUR] 暂无日志。"


_SUR_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_SUR_SPEED_FILE = os.path.join(_SUR_PLUGIN_DIR, "sur_speed.json")


def _sur_checkpoint_path(unique_id) -> str:
    safe = str(unique_id or "global").replace("/", "_").replace("\\", "_")
    return os.path.join(_SUR_PLUGIN_DIR, f"sur_checkpoint_{safe}.json")


def _sur_write_checkpoint(unique_id, data: dict):
    if not unique_id:
        return
    try:
        with open(_sur_checkpoint_path(unique_id), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[SUR] checkpoint 写入失败: {e}")


def _sur_read_checkpoint(unique_id):
    if not unique_id:
        return None
    try:
        path = _sur_checkpoint_path(unique_id)
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _sur_clear_checkpoint(unique_id):
    if not unique_id:
        return
    try:
        path = _sur_checkpoint_path(unique_id)
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def _sur_load_speed_record():
    try:
        if os.path.exists(_SUR_SPEED_FILE):
            with open(_SUR_SPEED_FILE, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def _sur_save_speed_record(total_secs: float, total_frames_run: int):
    if total_secs <= 0 or total_frames_run <= 0:
        return
    try:
        with open(_SUR_SPEED_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "spf": round(total_secs / total_frames_run, 4),
                    "date": time.strftime("%Y-%m-%d %H:%M"),
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
    except Exception:
        pass


def _sur_media_roots() -> list[str]:
    roots, seen = [], set()
    for getter_name in ("get_input_directory", "get_output_directory", "get_temp_directory"):
        getter = getattr(folder_paths, getter_name, None)
        if not callable(getter):
            continue
        try:
            path = getter()
        except Exception:
            continue
        if not path:
            continue
        real = os.path.realpath(str(path))
        if real not in seen:
            seen.add(real)
            roots.append(real)
    return roots


def _sur_resolve_media_path(path: str | None) -> str | None:
    raw = str(path or "").strip().strip('"').strip("'")
    if not raw:
        return None
    if os.path.isfile(raw):
        return os.path.realpath(raw)
    try:
        ann = folder_paths.get_annotated_filepath(raw)
        if ann and os.path.isfile(ann):
            return os.path.realpath(ann)
    except Exception:
        pass

    candidates, seen = [], set()

    def add(p):
        if not p:
            return
        real = os.path.realpath(p)
        if real not in seen:
            seen.add(real)
            candidates.append(real)

    if os.path.isabs(raw):
        add(raw)
    else:
        add(raw)
        base = os.path.basename(raw)
        for root in _sur_media_roots():
            add(os.path.join(root, raw))
            if base != raw:
                add(os.path.join(root, base))

    for cand in candidates:
        if os.path.isfile(cand):
            return cand

    base = os.path.basename(raw)
    if base == raw:
        for root in _sur_media_roots():
            try:
                for dirpath, _, files in os.walk(root):
                    if base in files:
                        return os.path.realpath(os.path.join(dirpath, base))
            except Exception:
                continue
    return None


def _sur_safe_copy_name(src_path: str, unique_id=None, prefix: str = "sur_copy") -> str:
    try:
        real = os.path.realpath(src_path)
        st = os.stat(real)
        sig_src = f"{real}|{st.st_mtime_ns}|{st.st_size}"
    except Exception:
        real = os.path.realpath(src_path)
        sig_src = real
    sig = hashlib.sha1(sig_src.encode("utf-8", errors="ignore")).hexdigest()[:12]
    base = os.path.basename(src_path)
    return f"{prefix}_{unique_id}_{sig}_{base}" if unique_id else f"{prefix}_{sig}_{base}"


def _sur_copy_into_input(src_path: str, desired_name: str | None = None, unique_id=None, prefix: str = "sur_copy") -> str:
    src_real = _sur_resolve_media_path(src_path) or os.path.realpath(str(src_path))
    if not os.path.isfile(src_real):
        raise FileNotFoundError(src_path)
    input_dir = folder_paths.get_input_directory()
    os.makedirs(input_dir, exist_ok=True)
    if os.path.realpath(os.path.dirname(src_real)) == os.path.realpath(input_dir):
        return src_real
    name = (desired_name or "").strip() or os.path.basename(src_real)
    dst = os.path.join(input_dir, name)
    if os.path.exists(dst):
        dst = _sur_unique_filepath(dst) if desired_name else os.path.join(
            input_dir, _sur_safe_copy_name(src_real, unique_id=unique_id, prefix=prefix)
        )
    shutil.copy2(src_real, dst)
    return dst


def _sur_prepare_reference_images(ref_images: list[str], unique_id=None) -> list[str]:
    if not ref_images:
        return []
    prepared = []
    for raw in ref_images:
        src = _sur_resolve_media_path(raw) or str(raw or "").strip()
        if not src:
            continue
        try:
            copied = _sur_copy_into_input(src, unique_id=unique_id, prefix="sur_ref")
            prepared.append(copied)
        except Exception:
            prepared.append(src)
    return prepared


# ── 分段计算 ──────────────────────────────────────────────────────

def calc_segments(total_frames: int, segments: int,
                  overlap: int = 0) -> list[tuple[int, int, int]]:
    """
    将 total_frames 均匀分为 segments 段，支持可选重叠帧。

    每段返回 (load_skip, load_limit, output_trim)：
      load_skip    → VHS_LoadVideo.skip_first_frames
      load_limit   → VHS_LoadVideo.frame_load_cap
      output_trim  → SegmentFrameTrimmer.trim_frames（第1段=0，后续=重叠帧数）

    设计保证：
      - 每段保存内容 = [load_skip + output_trim, load_skip + load_limit - 1]
      - 相邻段保存内容严格连续，无重叠、无跳帧
      - 第2段起会多读 overlap 帧作为模型时序上下文（帧内容来自上一段尾部）
      - cursor 严格按真实帧数推进，不受对齐影响

    注：原版代码对 load_limit 做了 4 的倍数对齐，但会导致段边界重叠帧。
        此版本去掉强制对齐以保证边界干净。若所用模型要求批大小为 4 的倍数，
        请将 segment_count 设置为能使每段帧数正好整除 4 的值。
    """
    base      = total_frames // segments
    remainder = total_frames % segments
    result    = []
    cursor    = 0   # 下一段真实内容的起始帧（不含重叠）

    for i in range(segments):
        raw            = base + (1 if i < remainder else 0)
        actual_overlap = min(overlap, cursor) if i > 0 else 0   # 第1段不重叠

        load_skip  = cursor - actual_overlap   # 真实读取起始帧
        load_limit = actual_overlap + raw      # 真实读取帧数（重叠 + 内容）
        trim       = actual_overlap            # 保存时从头裁掉的帧数

        result.append((load_skip, load_limit, trim))
        cursor += raw   # 只按真实内容帧数推进

    return result


# ── ComfyUI 主机探测 ──────────────────────────────────────────────

_SUR_HOST_CACHE = None

def _sur_collect_hosts() -> list[str]:
    candidates, seen = [], set()

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
        key  = f"{host}:{port}"
        if key not in seen:
            seen.add(key)
            candidates.append(key)

    inst = getattr(getattr(server, "PromptServer", None), "instance", None)
    if inst:
        add(getattr(inst, "address", None), getattr(inst, "port", None))
        add(getattr(inst, "host",    None), getattr(inst, "port", None))
    add(os.environ.get("COMFYUI_HOST"), os.environ.get("COMFYUI_PORT"))
    for port in (8188, 8000, 9000, 8080):
        add("127.0.0.1", port)
    return candidates

def _sur_probe_host(host: str) -> bool:
    for ep in ("/system_stats", "/queue", "/object_info"):
        try:
            with urllib.request.urlopen(f"http://{host}{ep}", timeout=1.5) as r:
                if getattr(r, "status", 200) < 500:
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


def _sur_current_client_id() -> str:
    try:
        srv = getattr(getattr(server, "PromptServer", None), "instance", None)
        cid = getattr(srv, "client_id", None) if srv is not None else None
        return str(cid or "")
    except Exception:
        return ""


# ── ComfyUI API 操作 ──────────────────────────────────────────────

def _queue_prompt(workflow: dict, client_id: str = "") -> str:
    payload  = json.dumps({"prompt": workflow, "client_id": client_id}).encode()
    last_err = None
    for host in [_sur_get_host(), _sur_get_host(force_refresh=True)]:
        try:
            req = urllib.request.Request(
                f"http://{host}/prompt", data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())["prompt_id"]
        except Exception as e:
            last_err = e
    raise last_err

def _wait_for_prompt(prompt_id: str, poll: float = 3.0) -> bool:
    while True:
        time.sleep(poll)
        for host in [_sur_get_host(), _sur_get_host(force_refresh=True)]:
            try:
                with urllib.request.urlopen(
                    f"http://{host}/history/{prompt_id}", timeout=10
                ) as r:
                    history = json.loads(r.read())
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


# ── Prompt 图工具 ─────────────────────────────────────────────────

def _node_class(node: dict) -> str:
    return str((node or {}).get("class_type") or (node or {}).get("type") or "")


def _node_inputs(node: dict) -> dict:
    inputs = (node or {}).get("inputs") or {}
    return inputs if isinstance(inputs, dict) else {}


def _link_node_id(value):
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        first = value[0]
        if isinstance(first, (str, int)):
            return str(first)
    return None


def _resolve_literal_input(prompt: dict, node_id: str, input_name: str, default=None, depth: int = 0):
    if not prompt or depth > 6:
        return default
    node = prompt.get(str(node_id))
    if not node:
        return default
    value = _node_inputs(node).get(input_name, default)
    src_id = _link_node_id(value)
    if src_id is None:
        return value

    src = prompt.get(src_id)
    if not src:
        return default
    src_inputs = _node_inputs(src)
    for key in ("value", "int", "float", "number"):
        if key in src_inputs:
            linked = _link_node_id(src_inputs[key])
            if linked is None:
                return src_inputs[key]
            return _resolve_literal_input(prompt, linked, key, default, depth + 1)
    return default


def _as_positive_int(value, default: int = 1) -> int:
    try:
        return max(1, int(round(float(value))))
    except Exception:
        return default


def _is_interpolation_node(class_type: str) -> bool:
    name = (class_type or "").lower()
    return (
        "vfi" in name
        or "rife" in name
        or "film" in name
        or "frame interpolation" in name
        or "interpolation" in name
    )


def _infer_trimmer_trim_multiplier(prompt: dict, trimmer_nid: str) -> tuple[int, str]:
    """
    SegmentFrameTrimmer 可能被放在 VFI/RIFE 之后。
    此时 trim_frames 需要写入“输出帧数”，而 overlap_frames 是“输入帧数”。
    对 RIFE 这类输出长度 (N - 1) * multiplier + 1 的节点，8 帧 overlap、
    multiplier=2 时应裁 15 帧，保留跨段边界插帧。
    """
    if not prompt or not trimmer_nid or str(trimmer_nid) not in prompt:
        return 1, "未检测"

    start = _link_node_id(_node_inputs(prompt[str(trimmer_nid)]).get("images"))
    if not start:
        return 1, "Trimmer 未连接 images"

    visited = set()
    found: list[str] = []
    product = 1

    def walk(nid: str, depth: int = 0):
        nonlocal product
        if depth > 12 or nid in visited:
            return
        visited.add(nid)
        node = prompt.get(str(nid))
        if not node:
            return
        class_type = _node_class(node)
        if _is_interpolation_node(class_type):
            mult = _as_positive_int(
                _resolve_literal_input(prompt, str(nid), "multiplier", 2),
                default=2,
            )
            if mult > 1:
                product *= mult
                found.append(f"{nid}:{class_type} x{mult}")
        for value in _node_inputs(node).values():
            src_id = _link_node_id(value)
            if src_id:
                walk(src_id, depth + 1)

    walk(start)
    if not found:
        return 1, "Trimmer 上游未检测到 VFI/RIFE"
    return product, ", ".join(found)


def _output_trim_for_overlap(input_overlap: int, trim_multiplier: int) -> int:
    input_overlap = max(0, int(input_overlap or 0))
    trim_multiplier = max(1, int(trim_multiplier or 1))
    if input_overlap <= 0 or trim_multiplier <= 1:
        return input_overlap
    return (input_overlap - 1) * trim_multiplier + 1


def _output_tail_trim_for_segment(has_overlap: bool, is_last_segment: bool, trim_multiplier: int) -> int:
    trim_multiplier = max(1, int(trim_multiplier or 1))
    if not has_overlap or is_last_segment or trim_multiplier <= 1:
        return 0
    return trim_multiplier - 1


def _bridge_plan(input_overlap: int, bridge_frames: int, bridge_enabled: bool) -> tuple[int, int, int]:
    """Return (pre_vfi_trim, vfi_input_overlap, final_output_trim_input)."""
    input_overlap = max(0, int(input_overlap or 0))
    if not bridge_enabled or input_overlap <= 0:
        return 0, input_overlap, input_overlap
    bridge_frames = max(1, int(bridge_frames or 1))
    kept_overlap = min(input_overlap, bridge_frames)
    pre_vfi_trim = input_overlap - kept_overlap
    return pre_vfi_trim, kept_overlap, kept_overlap


def _node_references_any(node: dict, removed_ids: set[str]) -> bool:
    for value in _node_inputs(node).values():
        src_id = _link_node_id(value)
        if src_id in removed_ids:
            return True
    return False


def _is_cleanup_debug_node(class_type: str) -> bool:
    name = (class_type or "").lower()
    return (
        class_type == "DeepRAMCleanNode"
        or "debug" in name
        or "show" in name
        or "preview" in name
    )


def _parse_selectors(text: str) -> set[str]:
    return {
        part.strip()
        for part in str(text or "").replace(";", ",").split(",")
        if part.strip()
    }


def _matches_selector(nid: str, node: dict, selectors: set[str]) -> bool:
    if not selectors:
        return False
    class_type = _node_class(node)
    lowered = {s.lower() for s in selectors}
    return (
        str(nid) in selectors
        or class_type in selectors
        or class_type.lower() in lowered
    )


def _prune_in_graph_cleanup_branch(prompt: dict, selectors: set[str] | None = None) -> tuple[list[str], list[str]]:
    """
    DeepRAMCleanNode 若接 IMAGE，会在 prompt 执行中持有大 tensor。
    Runner 不再把深度内存清理作为默认段后动作；这里只移除已选择的
    清理/调试节点以及它们下游的 show/debug 分支，避免子 prompt 多做旁路计算。
    """
    if not isinstance(prompt, dict):
        return [], []

    selectors = selectors or {"DeepRAMCleanNode"}
    to_remove = {
        str(nid)
        for nid, node in prompt.items()
        if _matches_selector(str(nid), node, selectors)
    }
    if not to_remove:
        return [], []

    blockers: list[str] = []
    changed = True
    while changed:
        changed = False
        for nid, node in list(prompt.items()):
            nid = str(nid)
            if nid in to_remove:
                continue
            if _node_references_any(node, to_remove):
                class_type = _node_class(node)
                if _is_cleanup_debug_node(class_type):
                    to_remove.add(nid)
                    changed = True
                else:
                    blockers.append(f"{nid}:{class_type}")

    if blockers:
        return [], sorted(set(blockers))

    removed = []
    for nid in sorted(to_remove, key=lambda x: int(x) if x.isdigit() else x):
        node = prompt.pop(nid, None)
        if node is not None:
            removed.append(f"{nid}:{_node_class(node)}")
    return removed, []


def _sur_unique_filepath(path: str) -> str:
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    idx = 2
    while True:
        cand = f"{root}_{idx}{ext}"
        if not os.path.exists(cand):
            return cand
        idx += 1


def _sur_get_output_video_info(prompt_id: str, combine_node_id: str, log=None):
    last_err = None
    for host in [_sur_get_host(), _sur_get_host(force_refresh=True)]:
        try:
            with urllib.request.urlopen(
                f"http://{host}/history/{prompt_id}", timeout=10
            ) as r:
                history = json.loads(r.read())
            node_out = history.get(prompt_id, {}).get("outputs", {}).get(str(combine_node_id), {})
            gifs = node_out.get("gifs", [])
            if not gifs:
                return None, None
            gi = gifs[0]
            base_dir = (
                folder_paths.get_output_directory()
                if gi.get("type") == "output"
                else folder_paths.get_input_directory()
            )
            subfolder = gi.get("subfolder", "")
            video_path = os.path.join(base_dir, subfolder, gi["filename"]) if subfolder else os.path.join(base_dir, gi["filename"])
            return video_path, None
        except Exception as e:
            last_err = e
    if log and last_err:
        log(f"  获取输出视频路径失败: {type(last_err).__name__}: {last_err}")
    return None, None


def _sur_parse_rate(text) -> float:
    raw = str(text or "").strip()
    if not raw or raw in ("0/0", "N/A"):
        return 0.0
    if "/" in raw:
        a, b = raw.split("/", 1)
        try:
            den = float(b)
            return float(a) / den if den else 0.0
        except Exception:
            return 0.0
    try:
        return float(raw)
    except Exception:
        return 0.0


def _sur_probe_video_info(path: str) -> tuple[int, float]:
    real = _sur_resolve_media_path(path) or os.path.realpath(str(path or ""))
    if not os.path.isfile(real):
        raise FileNotFoundError(path)

    ffprobe = shutil.which("ffprobe") or "ffprobe"
    cmd = [
        ffprobe, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=avg_frame_rate,r_frame_rate,nb_frames,duration:format=duration",
        "-of", "json", real,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if result.returncode == 0 and result.stdout:
            data = json.loads(result.stdout)
            streams = data.get("streams") or []
            stream = streams[0] if streams else {}
            fps = _sur_parse_rate(stream.get("avg_frame_rate")) or _sur_parse_rate(stream.get("r_frame_rate"))
            frames = 0
            nb_frames = stream.get("nb_frames")
            if nb_frames not in (None, "", "N/A"):
                try:
                    frames = int(float(nb_frames))
                except Exception:
                    frames = 0
            duration = 0.0
            for src in (stream, data.get("format") or {}):
                try:
                    duration = max(duration, float(src.get("duration") or 0))
                except Exception:
                    pass
            if frames <= 0 and fps > 0 and duration > 0:
                frames = int(round(duration * fps))
            if frames > 0 and fps > 0:
                return frames, fps
    except Exception:
        pass

    try:
        import cv2
        cap = cv2.VideoCapture(real)
        if cap.isOpened():
            fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
            frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            cap.release()
            if frames > 0 and fps > 0:
                return frames, fps
    except Exception:
        pass

    raise RuntimeError(f"无法读取视频信息: {real}")


def _sur_delete_prompt_history(prompt_id: str, log=None) -> bool:
    if not prompt_id:
        return False
    deleted = False
    try:
        import server as _server
        srv = getattr(getattr(_server, "PromptServer", None), "instance", None)
        queue = getattr(srv, "prompt_queue", None) if srv is not None else None
        if queue is not None and hasattr(queue, "delete_history_item"):
            queue.delete_history_item(prompt_id)
            deleted = True
    except Exception:
        deleted = False

    if not deleted:
        payload = json.dumps({"delete": [prompt_id]}).encode("utf-8")
        for host in [_sur_get_host(), _sur_get_host(force_refresh=True)]:
            try:
                req = urllib.request.Request(
                    f"http://{host}/history",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=5):
                    deleted = True
                    break
            except Exception:
                continue

    if deleted and log:
        log(f"  已清理子 prompt history: {prompt_id[:8]}...")
    return deleted


def _sur_find_audio_filename(prompt: dict, load_nid: str) -> str | None:
    node = (prompt or {}).get(str(load_nid), {})
    video = _node_inputs(node).get("video", "")
    if video and isinstance(video, str):
        return video
    return None


def _sur_auto_node_id(prompt: dict, current_id: str, wanted_class, label: str, log=None) -> str:
    current_id = str(current_id or "").strip()
    wanted_classes = {wanted_class} if isinstance(wanted_class, str) else set(wanted_class or [])
    if current_id and current_id in (prompt or {}) and _node_class((prompt or {}).get(current_id, {})) in wanted_classes:
        return current_id
    matches = [
        str(nid)
        for nid, node in (prompt or {}).items()
        if _node_class(node) in wanted_classes
    ]
    if len(matches) == 1:
        if log:
            wanted_label = " / ".join(sorted(wanted_classes))
            log(f"⚠ {label} 节点 ID「{current_id or '空'}」无效，已自动改用唯一的 {wanted_label} 节点 ID「{matches[0]}」")
        return matches[0]
    return current_id


def _sur_base_video_start_time(prompt: dict, load_nid: str) -> float:
    try:
        return max(0.0, float(_node_inputs((prompt or {}).get(str(load_nid), {})).get("start_time", 0) or 0))
    except Exception:
        return 0.0


def _sur_base_video_skip_frames(prompt: dict, load_nid: str) -> int:
    try:
        return max(0, int(_node_inputs((prompt or {}).get(str(load_nid), {})).get("skip_first_frames", 0) or 0))
    except Exception:
        return 0


def _sur_set_load_video_segment(
    wf: dict,
    load_nid: str,
    load_skip: int,
    load_limit: int,
    frame_rate: float,
    base_start_time: float = 0.0,
    base_skip_frames: int = 0,
    log=None,
):
    node = wf.get(str(load_nid))
    if not node:
        return
    inputs = node.setdefault("inputs", {})
    class_type = _node_class(node)
    inputs["frame_load_cap"] = int(load_limit)

    if "skip_first_frames" in inputs:
        absolute_skip = int(base_skip_frames) + int(load_skip)
        inputs["skip_first_frames"] = absolute_skip
        if log:
            log(f"  LoadVideo: skip_first_frames={absolute_skip} frame_load_cap={load_limit}")
        return

    if "start_time" in inputs or "FFmpeg" in class_type:
        fps = float(frame_rate or 0)
        if fps <= 0:
            inputs["start_time"] = float(base_start_time)
            if log:
                log("  ⚠ LoadVideoFFmpeg: frame_rate 无效，无法按帧换算 start_time，本段沿用原始 start_time")
            return
        start_time = float(base_start_time) + (int(load_skip) / fps)
        inputs["start_time"] = start_time
        if log:
            log(f"  LoadVideoFFmpeg: start_time={start_time:.3f}s frame_load_cap={load_limit}")
        return

    inputs["skip_first_frames"] = int(base_skip_frames) + int(load_skip)
    if log:
        log("  ⚠ LoadVideo 未声明 skip_first_frames/start_time，已尝试写入 skip_first_frames")


def _sur_clean_rel_dir(text: str, default: str = "SUR_physical_segments") -> str:
    raw = str(text or "").strip().replace("\\", "/").strip("/")
    if not raw:
        raw = default
    parts = []
    for part in raw.split("/"):
        part = part.strip().strip(".")
        if not part or part in (".", ".."):
            continue
        safe = "".join(ch if ch not in '<>:"|?*' else "_" for ch in part)
        if safe:
            parts.append(safe[:80])
    return "/".join(parts) or default


def _sur_input_rel_path(path: str) -> str:
    input_root = os.path.realpath(folder_paths.get_input_directory())
    real = os.path.realpath(path)
    try:
        rel = os.path.relpath(real, input_root)
    except Exception:
        return real
    return rel.replace("\\", "/")


def _sur_ffmpeg_float(value: float) -> str:
    text = f"{float(value):.6f}".rstrip("0").rstrip(".")
    return text or "0"


def _sur_slice_video_by_frames(
    src_path: str,
    out_path: str,
    start_frame: int,
    frame_count: int,
    frame_rate: float,
    crf: int = 12,
    reuse_existing: bool = True,
    log=None,
) -> bool:
    start_frame = max(0, int(start_frame or 0))
    frame_count = max(1, int(frame_count or 1))
    end_frame = start_frame + frame_count
    if reuse_existing and os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
        if log:
            log(f"  分片已存在，复用: {_sur_input_rel_path(out_path)}")
        return True

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    vf = f"trim=start_frame={start_frame}:end_frame={end_frame},setpts=PTS-STARTPTS"
    cmd = [
        ffmpeg, "-y", "-v", "error",
        "-i", src_path,
        "-vf", vf,
        "-an",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", str(max(0, min(51, int(crf or 0)))),
        "-pix_fmt", "yuv420p",
    ]
    if frame_rate and frame_rate > 0:
        cmd += ["-r", _sur_ffmpeg_float(frame_rate)]
    cmd += ["-movflags", "+faststart", out_path]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        if log:
            err = (result.stderr or result.stdout or "").strip()
            log(f"  ✗ ffmpeg 分片失败: {err[-500:]}")
        return False
    return os.path.isfile(out_path) and os.path.getsize(out_path) > 0


def _sur_prepare_physical_slices(
    src_path: str,
    seg_list: list[tuple[int, int, int]],
    run_stamp: str,
    subdir: str,
    base_frame_offset: int,
    frame_rate: float,
    crf: int,
    reuse_existing: bool,
    log=None,
) -> dict[int, dict[str, str]]:
    src_real = _sur_resolve_media_path(src_path) or os.path.realpath(str(src_path))
    if not os.path.isfile(src_real):
        raise FileNotFoundError(src_path)

    input_root = folder_paths.get_input_directory()
    safe_subdir = _sur_clean_rel_dir(subdir)
    slice_dir = os.path.join(input_root, safe_subdir, run_stamp, "source")
    os.makedirs(slice_dir, exist_ok=True)

    result: dict[int, dict[str, str]] = {}
    if log:
        log(f"物理分片模式=开  源: {os.path.basename(src_real)}")
        log(f"分片目录: input/{safe_subdir}/{run_stamp}/source")

    for idx, (load_skip, load_limit, _trim) in enumerate(seg_list, start=1):
        start_frame = max(0, int(base_frame_offset) + int(load_skip))
        name = f"seg{idx:03d}_f{start_frame:06d}_n{int(load_limit):04d}.mp4"
        out_path = os.path.join(slice_dir, name)
        if log:
            log(f"  切片 第{idx}/{len(seg_list)}段: start={start_frame} frames={load_limit}")
        ok = _sur_slice_video_by_frames(
            src_real, out_path, start_frame, load_limit, frame_rate,
            crf=crf, reuse_existing=reuse_existing, log=log
        )
        if not ok:
            raise RuntimeError(f"切片失败: {name}")
        result[idx] = {
            "abs": os.path.realpath(out_path),
            "rel": _sur_input_rel_path(out_path),
        }
    return result


def _sur_set_load_video_file_segment(
    wf: dict,
    load_nid: str,
    video_abs: str,
    video_rel: str,
    load_limit: int,
    log=None,
):
    node = wf.get(str(load_nid))
    if not node:
        return
    inputs = node.setdefault("inputs", {})
    class_type = _node_class(node)
    inputs["video"] = os.path.realpath(video_abs) if "Path" in class_type else video_rel
    inputs["frame_load_cap"] = int(load_limit)
    if "skip_first_frames" in inputs:
        inputs["skip_first_frames"] = 0
    if "start_time" in inputs or "FFmpeg" in class_type:
        inputs["start_time"] = 0.0
    if log:
        log(f"  LoadVideo: video={video_rel} skip=0 frame_load_cap={load_limit}")


def _sur_set_segment_audio(
    wf: dict,
    combine_nid: str,
    load_nid: str,
    seg_num: int,
    saved_start: int,
    saved_frames: int,
    frame_rate: float,
    audio_mode: str,
    audio_filename: str | None,
    base_start_time: float = 0.0,
    base_skip_frames: int = 0,
    log=None,
):
    if not combine_nid or combine_nid not in wf:
        return
    mode = str(audio_mode or "keep_original")
    if mode == "disable_audio":
        wf[combine_nid]["inputs"].pop("audio", None)
        if log:
            log("  音频: 已禁用")
        return
    if mode != "segment_from_loadvideo":
        return
    if not audio_filename:
        wf[combine_nid]["inputs"]["audio"] = [load_nid, 2]
        if log:
            log("  音频: 未找到源文件名，回退到 LoadVideo 音频输出")
        return
    audio_id = f"sur_audio_{seg_num}"
    start_time = (
        max(0.0, float(base_start_time) + (int(base_skip_frames) + saved_start) / frame_rate)
        if frame_rate > 0 else float(base_start_time)
    )
    duration = max(0.0, saved_frames / frame_rate) if frame_rate > 0 and saved_frames > 0 else 0.0
    wf[audio_id] = {
        "class_type": "VHS_LoadAudioUpload",
        "inputs": {
            "audio": audio_filename,
            "start_time": start_time,
            "duration": duration,
        },
    }
    wf[combine_nid]["inputs"]["audio"] = [audio_id, 0]
    if log:
        log(f"  音频: start={start_time:.3f}s duration={duration:.3f}s")


def _sur_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on", "开", "是"):
        return True
    if text in ("0", "false", "no", "off", "关", "否", ""):
        return False
    return default


def _sur_set_segment_reference_image(
    wf: dict,
    ref_node_id: str,
    ref_images: list[str],
    seg_index: int,
    log=None,
):
    ref_node_id = str(ref_node_id or "").strip()
    if not ref_node_id or not ref_images or ref_node_id not in wf:
        return
    idx = min(max(0, seg_index), len(ref_images) - 1)
    img_path = ref_images[idx]
    img_name = os.path.basename(img_path) if os.path.isabs(img_path) else img_path
    wf[ref_node_id]["inputs"]["image"] = img_name
    wv = wf[ref_node_id].get("widgets_values", [])
    if isinstance(wv, list) and wv:
        wv[0] = img_name
    if log:
        log(f"  参考图[{idx + 1}]: {img_name}")


def _sur_merge_videos(video_paths: list[str], output_path: str, log=None) -> bool:
    if not video_paths:
        return False
    list_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
            list_path = f.name
            for p in video_paths:
                safe_path = os.path.realpath(str(p)).replace("\\", "/").replace("'", "'\\''")
                f.write(f"file '{safe_path}'\n")
        ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", output_path]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            return True
        if log:
            log(f"  ffmpeg copy 合并失败，尝试重新编码合并: {result.stderr[-300:]}")
        fallback = [
            ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", list_path,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-c:a", "aac", "-b:a", "192k",
            output_path,
        ]
        result = subprocess.run(fallback, capture_output=True, text=True)
        if result.returncode == 0:
            return True
        if log:
            log(f"  ffmpeg 合并失败: {result.stderr[-300:]}")
        return False
    except FileNotFoundError:
        if log:
            log("  找不到 ffmpeg，无法自动合并；请确认 ffmpeg 在 PATH 中")
        return False
    except Exception as e:
        if log:
            log(f"  合并异常: {type(e).__name__}: {e}")
        return False
    finally:
        if list_path:
            try:
                os.unlink(list_path)
            except Exception:
                pass


def _sur_import_torch():
    try:
        import torch
        return torch
    except Exception:
        return None


def _sur_import_psutil():
    try:
        import psutil
        return psutil
    except Exception:
        return None


def _sur_zero_tensor_storage(tensor) -> bool:
    try:
        tensor.untyped_storage().resize_(0)
        return True
    except Exception:
        pass
    try:
        tensor.storage().resize_(0)
        return True
    except Exception:
        return False


def _sur_tensor_gb(tensor) -> float:
    try:
        return tensor.element_size() * tensor.nelement() / 1024**3
    except Exception:
        return 0.0


def _sur_trim_ram_os() -> bool:
    try:
        if platform.system() == "Windows":
            kernel32 = ctypes.windll.kernel32
            psapi = ctypes.windll.psapi
            try:
                msvcrt = ctypes.CDLL("msvcrt")
                if hasattr(msvcrt, "_heapmin"):
                    msvcrt._heapmin()
            except Exception:
                pass
            try:
                count = kernel32.GetProcessHeaps(0, None)
                if count:
                    heap_array = (ctypes.c_void_p * count)()
                    got = kernel32.GetProcessHeaps(count, heap_array)
                    for i in range(min(count, got)):
                        try:
                            kernel32.HeapCompact(heap_array[i], 0)
                        except Exception:
                            pass
            except Exception:
                pass
            handle = kernel32.GetCurrentProcess()
            kernel32.SetProcessWorkingSetSize(handle, ctypes.c_size_t(-1), ctypes.c_size_t(-1))
            psapi.EmptyWorkingSet(handle)
            return True
        if platform.system() == "Linux":
            libc = ctypes.CDLL("libc.so.6")
            libc.malloc_trim(0)
            return True
    except Exception:
        return False
    return False


def _sur_process():
    psutil = _sur_import_psutil()
    if psutil is None:
        return None
    try:
        return psutil.Process(os.getpid())
    except Exception:
        return None


def _sur_ram_gb(process=None) -> float:
    try:
        process = process or _sur_process()
        return process.memory_info().rss / 1024**3 if process else 0.0
    except Exception:
        return 0.0


def _sur_vram_gb(torch_module=None) -> float:
    torch = torch_module or _sur_import_torch()
    try:
        return torch.cuda.memory_allocated() / 1024**3 if torch and torch.cuda.is_available() else 0.0
    except Exception:
        return 0.0


def _sur_get_prompt_executors() -> list[tuple[object, str]]:
    executors: list[tuple[object, str]] = []

    def add(obj, source):
        if obj is not None and id(obj) not in {id(e) for e, _ in executors}:
            executors.append((obj, source))

    try:
        import server as _server
        srv = getattr(getattr(_server, "PromptServer", None), "instance", None)
        if srv is not None:
            for attr in ("prompt_executor", "executor"):
                add(getattr(srv, attr, None), f"PromptServer.{attr}")
    except Exception:
        pass

    try:
        import execution
        for attr in ("executor", "prompt_executor", "current_executor"):
            add(getattr(execution, attr, None), f"execution.{attr}")
        cls = getattr(execution, "PromptExecutor", None)
        if cls is not None:
            for obj in gc.get_objects():
                try:
                    if isinstance(obj, cls):
                        add(obj, "gc(isinstance)")
                except Exception:
                    continue
    except Exception:
        pass

    try:
        for obj in gc.get_objects():
            try:
                if type(obj).__name__ == "PromptExecutor":
                    add(obj, "gc(typename)")
            except Exception:
                continue
    except Exception:
        pass

    return executors


def _sur_deep_nullify(obj, torch, stats: dict, depth: int = 0):
    if obj is None or torch is None or depth > 8:
        return
    try:
        if isinstance(obj, torch.Tensor):
            stats["tensor_count"] += 1
            shape = list(obj.shape)
            if len(stats["shapes"]) < 8:
                stats["shapes"].append(shape)
            _sur_zero_tensor_storage(obj)
            return
    except Exception:
        return

    if isinstance(obj, dict):
        for key in list(obj.keys()):
            try:
                _sur_deep_nullify(obj[key], torch, stats, depth + 1)
                obj[key] = None
            except Exception:
                pass
        return
    if isinstance(obj, list):
        for i in range(len(obj)):
            try:
                _sur_deep_nullify(obj[i], torch, stats, depth + 1)
                obj[i] = None
            except Exception:
                pass
        return
    if isinstance(obj, tuple):
        for item in obj:
            _sur_deep_nullify(item, torch, stats, depth + 1)


def _sur_deep_clear_cache_obj(cache, torch, lines: list[str], label: str, stats: dict, depth: int = 0) -> int:
    if cache is None or depth > 8:
        return 0
    removed = 0

    pending = getattr(cache, "_pending_store_tasks", None)
    if isinstance(pending, set):
        for task in list(pending):
            try:
                task.cancel()
            except Exception:
                pass
        pending.clear()

    cache_dict = getattr(cache, "cache", None)
    if isinstance(cache_dict, dict):
        for key in list(cache_dict.keys()):
            try:
                _sur_deep_nullify(cache_dict[key], torch, stats, depth + 1)
                del cache_dict[key]
                removed += 1
            except Exception:
                pass

    subcaches = getattr(cache, "subcaches", None)
    if isinstance(subcaches, dict):
        for subkey, subcache in list(subcaches.items()):
            removed += _sur_deep_clear_cache_obj(subcache, torch, lines, f"{label}.{subkey}", stats, depth + 1)
        try:
            subcaches.clear()
        except Exception:
            pass

    for attr in ("children", "used_generation", "timestamps"):
        val = getattr(cache, attr, None)
        if hasattr(val, "clear"):
            try:
                val.clear()
            except Exception:
                pass

    if removed:
        lines.append(f"  清理 {label}: {removed} 个 cache entry")
    return removed


def _sur_deep_clear_executor(executor, source: str, torch, lines: list[str]) -> int:
    stats = {"tensor_count": 0, "shapes": []}
    outputs = getattr(executor, "outputs", {})
    n_nodes = len(outputs) if hasattr(outputs, "__len__") else 0

    if isinstance(outputs, dict):
        for node_id in list(outputs.keys()):
            try:
                _sur_deep_nullify(outputs[node_id], torch, stats)
            except Exception:
                pass
        try:
            outputs.clear()
        except Exception:
            pass

    for attr in ("outputs_ui", "old_prompt", "node_store", "_cache"):
        obj = getattr(executor, attr, None)
        if hasattr(obj, "clear"):
            try:
                obj.clear()
            except Exception:
                pass

    caches = getattr(executor, "caches", None)
    if caches is not None:
        seen = set()
        for name in ("outputs", "objects"):
            cache = getattr(caches, name, None)
            if cache is not None and id(cache) not in seen:
                seen.add(id(cache))
                _sur_deep_clear_cache_obj(cache, torch, lines, f"{source}.caches.{name}", stats)
        for cache in getattr(caches, "all", []) or []:
            if cache is not None and id(cache) not in seen:
                seen.add(id(cache))
                _sur_deep_clear_cache_obj(cache, torch, lines, f"{source}.caches.{type(cache).__name__}", stats)

    lines.append(
        f"  {source}: 清空 {n_nodes} 节点, "
        f"{stats['tensor_count']} 个 tensor 已 resize_(0), shapes={stats['shapes']}"
    )
    return stats["tensor_count"]


def _sur_wait_for_ffmpeg_subprocesses(lines: list[str], timeout: int = 120) -> bool:
    psutil = _sur_import_psutil()
    if psutil is None:
        lines.append("  ffmpeg子进程: psutil 不可用，跳过等待")
        return False
    try:
        proc = psutil.Process(os.getpid())
        ffmpeg_procs = []
        for child in proc.children(recursive=True):
            try:
                name = child.name().lower()
                cmdline = " ".join(child.cmdline()).lower()
                if "ffmpeg" in name or "ffmpeg" in cmdline:
                    ffmpeg_procs.append(child)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        if not ffmpeg_procs:
            lines.append("  ffmpeg子进程: 无（已完成或未启动）✓")
            return True
        lines.append(f"  ffmpeg子进程: 发现 {len(ffmpeg_procs)} 个，等待完成（超时{timeout}s）...")
        deadline = time.time() + timeout
        for proc in ffmpeg_procs:
            try:
                remaining = max(0, deadline - time.time())
                lines.append(f"    等待 PID {proc.pid}  {' '.join(proc.cmdline()[:6])[:80]}")
                proc.wait(timeout=remaining)
                lines.append(f"    ✓ PID {proc.pid} 已完成")
            except psutil.TimeoutExpired:
                lines.append(f"    ⚠ PID {proc.pid} 超时，尝试终止")
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    pass
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                lines.append(f"    ✓ PID {proc.pid} 已消失")
        return True
    except Exception as e:
        lines.append(f"  ffmpeg等待异常: {e}")
        return False


def _sur_wait_for_vhs_threads(lines: list[str], timeout: int = 60) -> bool:
    vhs_threads = []
    for thread in threading.enumerate():
        name = thread.name.lower()
        if any(key in name for key in ("vhs", "video", "ffmpeg", "combine", "encode")):
            vhs_threads.append(thread)
    try:
        for mod_name, mod in list(sys.modules.items()):
            if "videohelpersuite" in mod_name.lower() or "vhs" in mod_name.lower():
                for attr in ("active_threads", "thread_pool", "background_tasks", "_threads"):
                    val = getattr(mod, attr, None)
                    if isinstance(val, list):
                        vhs_threads.extend([t for t in val if isinstance(t, threading.Thread)])
                    elif isinstance(val, threading.Thread):
                        vhs_threads.append(val)
    except Exception:
        pass

    uniq = []
    seen = set()
    for thread in vhs_threads:
        if id(thread) not in seen and thread is not threading.current_thread():
            seen.add(id(thread))
            uniq.append(thread)
    if not uniq:
        lines.append("  VHS线程: 无活跃线程（Windows子进程模式或已完成）")
        return True
    lines.append(f"  VHS线程: 发现 {len(uniq)} 个，等待（超时{timeout}s）...")
    for thread in uniq:
        thread.join(timeout=timeout)
        lines.append(f"    {'✓' if not thread.is_alive() else '⚠超时'}: {thread.name}")
    return all(not thread.is_alive() for thread in uniq)


def _sur_clear_cell_tensor_refs(lines: list[str], size_threshold_gb: float = 0.05) -> int:
    torch = _sur_import_torch()
    if torch is None:
        lines.append("  cell引用清理: torch 不可用，跳过")
        return 0
    cleared = 0
    total_gb = 0.0
    try:
        for obj in gc.get_objects():
            try:
                if type(obj).__name__ != "cell":
                    continue
                try:
                    contents = obj.cell_contents
                except ValueError:
                    continue
                if isinstance(contents, torch.Tensor) and not contents.is_cuda:
                    gb = _sur_tensor_gb(contents)
                    if gb >= size_threshold_gb:
                        _sur_zero_tensor_storage(contents)
                        cleared += 1
                        total_gb += gb
            except Exception:
                continue
    except Exception:
        pass
    if cleared:
        lines.append(f"  cell引用清理: {cleared} 个 cell, 合计 {total_gb:.2f}GB tensor 已释放")
    else:
        lines.append("  cell引用清理: 无大 tensor cell ✓")
    return cleared


def _sur_is_video_like_tensor(tensor) -> bool:
    try:
        shape = list(tensor.shape)
        if len(shape) != 4:
            return False
        if int(shape[-1]) not in (1, 3, 4):
            return False
        if int(shape[0]) < 1 or int(shape[1]) < 64 or int(shape[2]) < 64:
            return False
        return True
    except Exception:
        return False


def _sur_aggressive_clear_video_tensors(lines: list[str], size_threshold_gb: float = 0.05, max_shapes: int = 12) -> int:
    torch = _sur_import_torch()
    if torch is None:
        lines.append("  残留视频 tensor 清理: torch 不可用，跳过")
        return 0
    candidates = []
    seen = set()
    try:
        for obj in gc.get_objects():
            try:
                if id(obj) in seen:
                    continue
                if isinstance(obj, torch.Tensor) and not obj.is_cuda and _sur_is_video_like_tensor(obj):
                    gb = _sur_tensor_gb(obj)
                    if gb >= size_threshold_gb:
                        seen.add(id(obj))
                        candidates.append((gb, obj, list(obj.shape), str(obj.dtype)))
            except Exception:
                continue
    except Exception:
        pass

    if not candidates:
        lines.append("  残留视频 tensor 清理: 未发现大 IMAGE/VIDEO tensor ✓")
        return 0

    candidates.sort(key=lambda x: x[0], reverse=True)
    total_gb = 0.0
    cleared = 0
    shapes = []
    for gb, tensor, shape, dtype in candidates:
        try:
            if _sur_zero_tensor_storage(tensor):
                cleared += 1
                total_gb += gb
                if len(shapes) < max_shapes:
                    shapes.append(f"{shape}/{dtype}/{gb:.2f}GB")
        except Exception:
            continue

    candidates.clear()
    gc.collect()
    gc.collect()
    if cleared:
        lines.append(
            f"  残留视频 tensor 清理: {cleared} 个 IMAGE/VIDEO tensor 已 resize_(0)，"
            f"合计约 {total_gb:.2f}GB"
        )
        lines.append("  shapes: " + "; ".join(shapes))
    else:
        lines.append("  残留视频 tensor 清理: 发现候选但无法 resize")
    return cleared


def _sur_trace_tensor_holders(lines: list[str], size_threshold_gb: float = 0.05, max_tensors: int = 8):
    torch = _sur_import_torch()
    if torch is None:
        lines.append("  === tensor 引用链追踪 ===")
        lines.append("  torch 不可用，跳过")
        return
    lines.append("  === tensor 引用链追踪 ===")
    big_tensors = []
    try:
        for obj in gc.get_objects():
            try:
                if isinstance(obj, torch.Tensor) and not obj.is_cuda:
                    gb = _sur_tensor_gb(obj)
                    if gb >= size_threshold_gb:
                        big_tensors.append((gb, obj))
            except Exception:
                continue
    except Exception:
        pass
    big_tensors.sort(key=lambda x: x[0], reverse=True)
    lines.append(f"  共 {len(big_tensors)} 个大CPU tensor（≥{size_threshold_gb}GB）")
    for i, (gb, tensor) in enumerate(big_tensors[:max_tensors]):
        lines.append(f"\n  [tensor #{i + 1}] {gb:.3f}GB  shape={list(tensor.shape)}  dtype={tensor.dtype}")
        try:
            refs = [r for r in gc.get_referrers(tensor) if r is not big_tensors and r is not locals()]
            lines.append(f"    直接引用者 ({len(refs)} 个):")
            for ref in refs[:5]:
                ref_type = type(ref).__name__
                if ref_type == "cell":
                    lines.append("      ★ cell（闭包变量）← 可由 cell 清理处理")
                elif isinstance(ref, dict):
                    keys = [k for k, v in ref.items() if v is tensor]
                    holders = [type(h).__name__ for h in gc.get_referrers(ref)[:3] if not isinstance(h, (list, dict))]
                    lines.append(f"      dict[key={keys}] ← 持有者: {holders}")
                elif isinstance(ref, list):
                    holders = [type(h).__name__ for h in gc.get_referrers(ref)[:3] if not isinstance(h, (list, dict))]
                    lines.append(f"      list(len={len(ref)}) ← 持有者: {holders}")
                elif ref_type == "frame":
                    lines.append(f"      ★ frame: {ref.f_code.co_filename}:{ref.f_lineno} in {ref.f_code.co_name}()")
                else:
                    lines.append(f"      {ref_type}({getattr(type(ref), '__module__', '')})")
        except Exception as e:
            lines.append(f"    追踪失败: {e}")


def _sur_analyze_threads(lines: list[str]):
    lines.append("  === 线程 ===")
    for thread in threading.enumerate():
        tag = "[d]" if thread.daemon else "[n]"
        lines.append(f"  {tag} {thread.name}  alive={thread.is_alive()}")
        try:
            frame = sys._current_frames().get(thread.ident)
            if frame:
                stack = traceback.extract_stack(frame)
                if stack:
                    last = stack[-1]
                    lines.append(f"    @ {last.filename}:{last.lineno} {last.name}()")
        except Exception:
            pass


def _sur_analyze_subprocesses(lines: list[str]):
    lines.append("  === 子进程 ===")
    psutil = _sur_import_psutil()
    if psutil is None:
        lines.append("  psutil 不可用，跳过")
        return
    try:
        proc = psutil.Process(os.getpid())
        children = proc.children(recursive=True)
        if not children:
            lines.append("  无子进程 ✓")
            return
        total = 0.0
        for child in children:
            try:
                mem = child.memory_info().rss / 1024**3
                total += mem
                cmd = " ".join(child.cmdline()[:5])[:80] if child.cmdline() else child.name()
                tag = " ★ffmpeg" if "ffmpeg" in cmd.lower() else ""
                lines.append(f"  PID {child.pid}  {child.status():<8}  {mem:.2f}GB  {cmd}{tag}")
            except Exception:
                pass
        lines.append(f"  合计: {len(children)} 个  {total:.2f}GB")
    except Exception as e:
        lines.append(f"  分析失败: {e}")


def _sur_analyze_executor_cache(lines: list[str]):
    lines.append("  === executor/cache 分析 ===")
    executors = _sur_get_prompt_executors()
    if not executors:
        lines.append("  ✗ executor 未找到")
        return
    torch = _sur_import_torch()
    for executor, source in executors:
        outputs = getattr(executor, "outputs", {})
        lines.append(f"  来源: {source}  outputs节点数: {len(outputs) if hasattr(outputs, '__len__') else 0}")
        node_sizes = []

        def scan(obj, depth=0):
            if torch is None or depth > 5:
                return []
            if isinstance(obj, torch.Tensor):
                return [_sur_tensor_gb(obj)]
            if isinstance(obj, dict):
                vals = []
                for v in obj.values():
                    vals.extend(scan(v, depth + 1))
                return vals
            if isinstance(obj, (list, tuple)):
                vals = []
                for v in obj:
                    vals.extend(scan(v, depth + 1))
                return vals
            return []

        if isinstance(outputs, dict):
            for node_id, val in outputs.items():
                sizes = scan(val)
                if sizes:
                    node_sizes.append((sum(sizes), node_id, len(sizes)))
        if node_sizes:
            node_sizes.sort(reverse=True)
            lines.append(f"  outputs 含tensor节点: {len(node_sizes)} 个  合计 {sum(x[0] for x in node_sizes):.2f}GB")
            for gb, node_id, count in node_sizes[:8]:
                lines.append(f"    node={node_id}  {gb:.3f}GB  ({count}个tensor)")
        else:
            lines.append("  outputs 无大tensor或新版 cache 持有位置不在 outputs")


def _sur_analyze_models(lines: list[str]):
    lines.append("  === comfy model_management ===")
    try:
        import comfy.model_management as mm
        loaded = getattr(mm, "current_loaded_models", [])
        lines.append(f"  已加载模型: {len(loaded)} 个")
        total = 0.0
        for model in loaded:
            try:
                raw_model = getattr(model, "model", model)
                name = type(raw_model).__name__
                device = str(getattr(model, "device", "?"))
                size = 0.0
                if hasattr(raw_model, "parameters"):
                    size = sum(p.element_size() * p.nelement() for p in raw_model.parameters()) / 1024**3
                total += size
                cpu_tag = " ⚠CPU-offload" if device.startswith("cpu") else ""
                lines.append(f"  {name}  {device}  {size:.2f}GB{cpu_tag}")
            except Exception:
                pass
        lines.append(f"  合计: {total:.2f}GB")
    except Exception as e:
        lines.append(f"  读取失败: {e}")


def _sur_check_gc_garbage(lines: list[str]):
    lines.append("  === gc.garbage ===")
    gc.collect()
    if not gc.garbage:
        lines.append("  gc.garbage: 空 ✓")
        return
    counts = {}
    for obj in gc.garbage:
        counts[type(obj).__name__] = counts.get(type(obj).__name__, 0) + 1
    lines.append(f"  ⚠ {len(gc.garbage)} 个对象")
    for name, count in sorted(counts.items(), key=lambda x: -x[1])[:5]:
        lines.append(f"    {name}: {count}")


def _clear_cache_obj(cache, lines: list[str], label: str, depth: int = 0) -> int:
    if cache is None or depth > 8:
        return 0
    removed = 0
    pending = getattr(cache, "_pending_store_tasks", None)
    if isinstance(pending, set):
        for task in list(pending):
            try:
                task.cancel()
            except Exception:
                pass
        pending.clear()
    cache_dict = getattr(cache, "cache", None)
    if isinstance(cache_dict, dict):
        removed += len(cache_dict)
        cache_dict.clear()
    subcaches = getattr(cache, "subcaches", None)
    if isinstance(subcaches, dict):
        for subkey, subcache in list(subcaches.items()):
            removed += _clear_cache_obj(subcache, lines, f"{label}.{subkey}", depth + 1)
        subcaches.clear()
    for attr in ("children", "used_generation"):
        val = getattr(cache, attr, None)
        if hasattr(val, "clear"):
            try:
                val.clear()
            except Exception:
                pass
    return removed


def _clear_comfy_execution_cache(log=None, reset_executors: bool = True):
    lines: list[str] = []
    cleared_entries = 0
    reset_count = 0
    try:
        executors = []
        try:
            import server as _server
            srv = getattr(getattr(_server, "PromptServer", None), "instance", None)
            for attr in ("prompt_executor", "executor"):
                obj = getattr(srv, attr, None) if srv is not None else None
                if obj is not None:
                    executors.append(obj)
        except Exception:
            pass
        try:
            import execution
            cls = getattr(execution, "PromptExecutor", None)
            if cls is not None:
                for obj in gc.get_objects():
                    try:
                        if isinstance(obj, cls):
                            executors.append(obj)
                    except Exception:
                        continue
        except Exception:
            pass

        seen = set()
        for executor in executors:
            if id(executor) in seen:
                continue
            seen.add(id(executor))
            caches = getattr(executor, "caches", None)
            if caches is not None:
                for name in ("outputs", "objects"):
                    cleared_entries += _clear_cache_obj(getattr(caches, name, None), lines, f"caches.{name}")
            for attr in ("outputs", "outputs_ui", "old_prompt", "node_store", "_cache"):
                obj = getattr(executor, attr, None)
                if hasattr(obj, "clear"):
                    try:
                        cleared_entries += len(obj) if hasattr(obj, "__len__") else 0
                        obj.clear()
                    except Exception:
                        pass
            if reset_executors and hasattr(executor, "reset"):
                try:
                    executor.reset()
                    reset_count += 1
                except Exception:
                    pass
    except Exception as e:
        if log:
            log(f"  Comfy cache 清理异常: {type(e).__name__}: {e}")

    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
    except Exception:
        pass
    gc.collect()
    if log:
        log(f"  Comfy cache 轻清理完成: {cleared_entries} 个 entry, reset={reset_count}")
    return cleared_entries


def _sur_post_segment_cache_purge(log=None, trim_working_set: bool = True):
    """Release per-prompt execution state after a child segment finishes."""
    torch = _sur_import_torch()
    lines: list[str] = []
    before_ram = _sur_ram_gb()
    before_vram = _sur_vram_gb(torch)
    cleared = _clear_comfy_execution_cache(log=None, reset_executors=True)
    video_cleared = _sur_aggressive_clear_video_tensors(
        lines,
        size_threshold_gb=0.25,
        max_shapes=10,
    )

    try:
        if torch and torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
            torch.cuda.synchronize()
    except Exception:
        pass

    collected = 0
    try:
        collected += gc.collect()
        collected += gc.collect()
    except Exception:
        pass

    os_trimmed = False
    if trim_working_set:
        os_trimmed = _sur_trim_ram_os()

    after_ram = _sur_ram_gb()
    after_vram = _sur_vram_gb(torch)
    if log:
        log(
            "  段后内部缓存清理: "
            f"cache={cleared}  video_tensor={video_cleared}  gc={collected}  "
            f"RAM={before_ram:.2f}->{after_ram:.2f}GB  "
            f"VRAM={before_vram:.2f}->{after_vram:.2f}GB  "
            f"WindowsTrim={'开' if os_trimmed else '跳过/不可用'}"
        )
        for line in lines:
            log(line)
    return cleared


# ── 工具函数 ──────────────────────────────────────────────────────

def _now_stamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")

def _build_plan_text(total_frames, segments, start_from,
                     overlap, load_nid, trimmer_nid, bridge_trimmer_nid,
                     vfi_bridge_frames: int = 1,
                     trim_multiplier: int = 1, trim_note: str = "",
                     segment_io_mode: str = "frame_window",
                     physical_slice_subdir: str = "SUR_physical_segments") -> str:
    if total_frames <= 0:
        return "✗ total_frames 必须大于 0"

    seg_list = calc_segments(total_frames, segments, overlap)
    bridge_enabled = bool(bridge_trimmer_nid and overlap > 0)
    lines = [
        f"LoadVideo 节点 ID : {load_nid}",
        f"最终裁帧节点 ID : {trimmer_nid or '（未设置，重叠输出不会自动去重）'}",
        f"VFI 前桥接节点 ID: {bridge_trimmer_nid or '（未设置，兼容旧模式：重叠帧会进入 VFI）'}",
        f"总帧数: {total_frames}  共 {segments} 段  模型上下文重叠: {overlap} 帧  从第 {start_from} 段开始",
        f"VFI 桥接帧: {vfi_bridge_frames if bridge_enabled else '未启用'}",
        f"插帧输出倍率: x{trim_multiplier}"
        + (f"（{trim_note}）" if trim_note else ""),
        f"分段输入模式: {segment_io_mode}"
        + (f"  input/output 子目录: {physical_slice_subdir}" if segment_io_mode == "physical_slices" else ""),
        "",
        f"  {'段':>3}  {'load_skip':>9}  {'load_limit':>10}  {'ctx':>5}  {'pre_vfi':>7}  {'head_trim':>9}  {'tail_trim':>9}"
        f"  {'保存范围':>14}  {'保存帧数':>8}  状态",
        f"  {'-'*3}  {'-'*9}  {'-'*10}  {'-'*5}  {'-'*7}  {'-'*9}  {'-'*9}  {'-'*14}  {'-'*8}  ----",
    ]
    for i, (skip, limit, trim) in enumerate(seg_list):
        seg_num     = i + 1
        saved_start = skip + trim
        saved_end   = skip + limit - 1
        saved_n     = limit - trim
        pre_vfi_trim, _, final_trim_input = _bridge_plan(trim, vfi_bridge_frames, bridge_enabled)
        out_trim    = _output_trim_for_overlap(final_trim_input, trim_multiplier)
        tail_trim   = _output_tail_trim_for_segment(overlap > 0, seg_num == len(seg_list), trim_multiplier)
        status      = "→ 执行" if seg_num >= start_from else "  跳过"
        lines.append(
            f"  第{seg_num:>2}段  skip={skip:>7}  limit={limit:>8}"
            f"  ctx={trim:>3}  pre={pre_vfi_trim:>3}  head={out_trim:>3}  tail={tail_trim:>3}"
            f"  [{saved_start:>5} ~ {saved_end:>5}]  {saved_n:>6}帧  {status}"
        )
    speed = _sur_load_speed_record()
    frames_to_run = sum(max(0, limit - trim) for i, (skip, limit, trim) in enumerate(seg_list) if i + 1 >= start_from)
    if speed and frames_to_run > 0:
        est = float(speed.get("spf", 0) or 0) * frames_to_run
        if est > 0:
            est_text = f"{est / 3600:.1f}小时" if est >= 3600 else f"{est / 60:.0f}分钟"
            lines += [
                "",
                f"预计耗时: 约 {est_text}（基于 {speed.get('date', '历史')} 的 {speed.get('spf')}s/帧记录）",
            ]
    lines += ["", "输出文件命名: sur_seg{段号}_{时间戳}.mp4"]
    return "\n".join(lines)


# ── SegmentFrameTrimmer 节点 ──────────────────────────────────────

class SegmentFrameTrimmer:
    """
    插在最终 IMAGE 输出 和 VHS_VideoCombine 之间。
    SegmentUpscaleRunner 每段执行时会自动设置 trim_frames：
      - 第1段：trim_frames = 0（直通）
      - 后续段：裁掉头部桥接帧对应的输出帧
        （若本节点放在 RIFE/VFI 后，Runner 会自动换算插帧倍率）
    这样每段保存的内容严格连续，合并视频时不会出现重复帧或跳帧。
    """
    CATEGORY    = "video/utils"
    FUNCTION    = "trim"
    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("images",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE",),
                "trim_frames": (
                    "INT",
                    {
                        "default": 0, "min": 0, "max": 256, "step": 1,
                        "tooltip": (
                            "自动参数。Runner 会写入最终保存前需要裁掉的输出帧数；"
                            "普通使用不要手动修改。"
                        ),
                    },
                ),
                "tail_trim_frames": (
                    "INT",
                    {
                        "default": 0, "min": 0, "max": 256, "step": 1,
                        "tooltip": (
                            "自动参数。Runner 会写入最终保存前需要从尾部裁掉的输出帧数；"
                            "普通使用不要手动修改。"
                        ),
                    },
                ),
            }
        }

    def trim(self, images, trim_frames: int, tail_trim_frames: int = 0):
        if images is None:
            raise ValueError(
                "[SegmentFrameTrimmer] images 为 None，"
                "请将本节点的 images 输入直接连接到上游放大/插帧节点，"
                "不要经过类型为 * 的透传节点中转。"
            )
        n = images.shape[0]
        head = max(0, min(int(trim_frames), n - 1))
        tail = max(0, int(tail_trim_frames or 0))
        tail = min(tail, max(0, n - head - 1))
        end = n - tail if tail > 0 else n
        return (images[head:end],)


class SegmentVfiBridgeTrimmer:
    """
    放在高清放大/去噪等时序上下文节点之后、RIFE/VFI 之前。
    Runner 会在第2段起裁掉多余的上下文帧，只保留少量桥接帧给 VFI 生成段间过渡。
    """
    CATEGORY    = "video/utils"
    FUNCTION    = "trim"
    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("images",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE",),
                "trim_frames": (
                    "INT",
                    {
                        "default": 0, "min": 0, "max": 256, "step": 1,
                        "tooltip": (
                            "自动参数。Runner 会写入 VFI/RIFE 前要裁掉的上下文帧数；"
                            "普通使用不要手动修改。"
                        ),
                    },
                ),
            }
        }

    def trim(self, images, trim_frames: int):
        if images is None:
            raise ValueError(
                "[SegmentVfiBridgeTrimmer] images 为 None，"
                "请把本节点放在放大/去噪输出之后、RIFE/VFI 之前。"
            )
        n = images.shape[0]
        trim_frames = max(0, min(int(trim_frames), n - 1))
        if trim_frames > 0:
            return (images[trim_frames:],)
        return (images,)


# ── 日志显示节点 ─────────────────────────────────────────────────

class SegmentRunLogViewer:
    CATEGORY = "video/utils"
    FUNCTION = "show_log"
    OUTPUT_NODE = True
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("log_text",)

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "runner_node_id": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": "填写 SegmentUpscaleRunner 的节点 ID。留空时显示当前内存中所有 Runner 日志。",
                    },
                ),
                "max_lines": (
                    "INT",
                    {
                        "default": 300,
                        "min": 20,
                        "max": 3000,
                        "step": 20,
                        "tooltip": "最多显示多少行日志。",
                    },
                ),
                "clear_after_read": (
                    "BOOLEAN",
                    {
                        "default": False,
                        "tooltip": "读取后清空该 Runner 日志。runner_node_id 留空时不会清空所有日志。",
                    },
                ),
            }
        }

    def show_log(self, runner_node_id="", max_lines=300, clear_after_read=False):
        text = _sur_log_text(runner_node_id, max_lines)
        if clear_after_read and str(runner_node_id or "").strip():
            _sur_log_clear(str(runner_node_id).strip())
        return {"ui": {"text": [text]}, "result": (text,)}


class SegmentVideoInfoProbe:
    CATEGORY = "video/utils"
    FUNCTION = "probe"
    RETURN_TYPES = ("INT", "FLOAT", "INT", "FLOAT")
    RETURN_NAMES = ("total_frames", "frame_rate", "source_total_frames", "source_fps")

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "video": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": "源视频文件名或路径。可填写 ComfyUI/input 下的视频文件名。",
                    },
                ),
                "force_rate": (
                    "FLOAT",
                    {
                        "default": 0.0, "min": 0.0, "max": 120.0, "step": 1.0,
                        "tooltip": "与 VHS_LoadVideo.force_rate 保持一致。0 表示使用源视频帧率。",
                    },
                ),
                "skip_first_frames": (
                    "INT",
                    {
                        "default": 0, "min": 0, "max": 999999, "step": 1,
                        "tooltip": "与 VHS_LoadVideo.skip_first_frames 保持一致，用于只处理源视频后半段。",
                    },
                ),
                "frame_load_cap": (
                    "INT",
                    {
                        "default": 0, "min": 0, "max": 999999, "step": 1,
                        "tooltip": "要处理的总帧数。0 表示从 skip 后一直处理到视频末尾。",
                    },
                ),
            }
        }

    def probe(self, video="", force_rate=0.0, skip_first_frames=0, frame_load_cap=0):
        source_total, source_fps = _sur_probe_video_info(video)
        skip = max(0, int(skip_first_frames or 0))
        cap = max(0, int(frame_load_cap or 0))
        out_fps = float(force_rate or 0) or source_fps
        available = max(0, source_total - skip)

        if force_rate and source_fps > 0 and abs(float(force_rate) - source_fps) > 0.001:
            available = int(available / source_fps * float(force_rate))

        total = min(cap, available) if cap > 0 else available
        return (int(total), float(out_fps), int(source_total), float(source_fps))


# ── 主节点 ────────────────────────────────────────────────────────

class SegmentUpscaleRunner:
    CATEGORY    = "video/utils"
    FUNCTION    = "run"
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
                        "tooltip": "视频总帧数，连接 VHS_LoadVideo 的 frame_count 输出。",
                    },
                ),
                "frame_rate": (
                    "FLOAT",
                    {
                        "default": 24.0, "min": 1.0, "max": 120.0,
                        "forceInput": True,
                        "tooltip": "视频帧率，连接 VHS_LoadVideo 的 fps 输出。",
                    },
                ),
                "segment_count": (
                    "INT",
                    {
                        "default": 4, "min": 1, "max": 50, "step": 1,
                        "display": "slider",
                        "tooltip": "将视频分成几段执行，每段单独保存。",
                    },
                ),
                "start_segment": (
                    "INT",
                    {
                        "default": 1, "min": 1, "max": 50, "step": 1,
                        "display": "slider",
                        "tooltip": "从第几段开始（断点续跑时修改此值）。",
                    },
                ),
                "overlap_frames": (
                    "INT",
                    {
                        "default": 0, "min": 0, "max": 64, "step": 1,
                        "display": "slider",
                        "tooltip": (
                            "模型上下文重叠帧数（建议 4~16，0=不重叠）。\n"
                            "第2段起会向前多读这些帧给放大/去噪等时序模型参考；\n"
                            "如接了 VFI 前桥接裁剪，重叠区不会整段进入 RIFE/VFI。"
                        ),
                    },
                ),
                "execute": (
                    "BOOLEAN",
                    {
                        "default": False,
                        "tooltip": "False=仅预览分段计划，True=开始执行。",
                    },
                ),
                "load_video_node_id": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": "VHS_LoadVideo 节点 ID（右键节点标题栏查看）。",
                    },
                ),
                "combine_video_node_id": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": "VHS_VideoCombine 节点 ID。",
                    },
                ),
                "trimmer_node_id": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": (
                            "最终 SegmentFrameTrimmer 节点 ID。\n"
                            "把它放在 RIFE/VFI 之后、VHS_VideoCombine 之前，Runner 会自动写入最终去重帧数。"
                        ),
                    },
                ),
                "bridge_trimmer_node_id": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": (
                            "可选但强烈推荐。SegmentVfiBridgeTrimmer 节点 ID。\n"
                            "把它放在放大/去噪之后、RIFE/VFI 之前，避免把整个重叠区重复插帧。"
                        ),
                    },
                ),
                "vfi_bridge_frames": (
                    "INT",
                    {
                        "default": 1, "min": 1, "max": 16, "step": 1,
                        "tooltip": "进入 RIFE/VFI 前保留多少帧上一段尾部作为桥接。普通插帧建议 1；边界仍抖动可试 2。",
                    },
                ),
                "trim_multiplier_override": (
                    "INT",
                    {
                        "default": 0, "min": 0, "max": 16, "step": 1,
                        "tooltip": (
                            "插帧输出倍率。0=自动识别最终裁帧节点上游的 RIFE/VFI 倍率。\n"
                            "自动失败时手动填 2/4 等。"
                        ),
                    },
                ),
                "prune_cleanup_debug_nodes": (
                    "BOOLEAN",
                    {
                        "default": True,
                        "tooltip": (
                            "从每段子 prompt 中跳过预览、show、debug、旧清理分支。"
                            "这不是内存清理，只是避免不必要的旁路节点参与每段执行。"
                        ),
                    },
                ),
                "clear_segment_history": (
                    "BOOLEAN",
                    {
                        "default": True,
                        "tooltip": "每段成功记录输出路径后删除该子 prompt 的 history，减少长任务中的历史记录内存积压。",
                    },
                ),
                "merge_segments": (
                    "BOOLEAN",
                    {
                        "default": True,
                        "tooltip": "全部完成后用 ffmpeg concat 自动合并每段视频。要求各段编码参数一致。",
                    },
                ),
                "merged_filename_prefix": (
                    "STRING",
                    {
                        "default": "sur_merged",
                        "tooltip": "自动合并输出的文件名前缀，会保留 VideoCombine 原本的输出子目录。",
                    },
                ),
                "enable_checkpoint": (
                    "BOOLEAN",
                    {
                        "default": True,
                        "tooltip": "每段成功后写入 checkpoint。中断或 OOM 后可自动续跑。",
                    },
                ),
                "auto_resume_checkpoint": (
                    "BOOLEAN",
                    {
                        "default": True,
                        "tooltip": "执行时若存在 checkpoint，自动从 next_seg 继续，并把已完成分段加入合并列表。",
                    },
                ),
                "clear_checkpoint_on_finish": (
                    "BOOLEAN",
                    {
                        "default": True,
                        "tooltip": "全部完成后自动删除 checkpoint。关闭后可保留记录便于排查。",
                    },
                ),
                "pre_segment_paths": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": "手动指定已完成的前段视频路径，逗号分隔；合并时会放在本次输出前面。",
                    },
                ),
                "reference_image_node_id": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": "可选。需要按段替换参考图时填写 LoadImage 等图片节点 ID。",
                    },
                ),
                "segment_reference_images": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": "可选。每段参考图路径/文件名，逗号分隔；不足时沿用最后一张。",
                    },
                ),
                "audio_mode": (
                    ["keep_original", "segment_from_loadvideo", "disable_audio"],
                    {
                        "default": "keep_original",
                        "tooltip": (
                            "keep_original=不改 VideoCombine 音频；\n"
                            "segment_from_loadvideo=按保存帧范围从源视频音频切段；\n"
                            "disable_audio=移除音频。"
                        ),
                    },
                ),
            },
            "hidden": {
                "prompt":       "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
                "unique_id":    "UNIQUE_ID",
            },
        }

    def run(
        self,
        total_frames=0, frame_rate=24.0,
        segment_count=4, start_segment=1,
        overlap_frames=0, execute=False,
        load_video_node_id="", combine_video_node_id="",
        trimmer_node_id="",
        bridge_trimmer_node_id="",
        vfi_bridge_frames=1,
        trim_multiplier_override=0,
        prune_cleanup_debug_nodes=True,
        clear_segment_history=True,
        merge_segments=True,
        merged_filename_prefix="sur_merged",
        enable_checkpoint=True,
        auto_resume_checkpoint=True,
        clear_checkpoint_on_finish=True,
        pre_segment_paths="",
        reference_image_node_id="",
        segment_reference_images="",
        audio_mode="keep_original",
        segment_io_mode="frame_window",
        physical_slice_subdir="SUR_physical_segments",
        physical_slice_crf=12,
        reuse_physical_slices=True,
        prompt=None, extra_pnginfo=None, unique_id=None,
        **_legacy,
    ):
        total_frames = int(total_frames or 0)
        frame_rate   = float(frame_rate or 24.0)
        segments     = int(segment_count or 4)
        start_from   = max(1, int(start_segment or 1))
        overlap      = max(0, int(overlap_frames or 0))
        load_nid     = str(load_video_node_id or "").strip()
        combine_nid  = str(combine_video_node_id or "").strip()
        trimmer_nid  = str(trimmer_node_id or "").strip()
        bridge_trimmer_nid = str(bridge_trimmer_node_id or "").strip()
        try:
            vfi_bridge_frames = max(1, int(vfi_bridge_frames or 1))
        except Exception:
            vfi_bridge_frames = 1
        try:
            trim_override = max(0, int(trim_multiplier_override or _legacy.get("trim_multiplier_override") or 0))
        except Exception:
            trim_override = 0
        prune_cleanup_debug_nodes = _sur_bool(prune_cleanup_debug_nodes, True)
        clear_segment_history = _sur_bool(clear_segment_history, True)
        merge_segments = _sur_bool(merge_segments, True)
        cleanup_selectors = _parse_selectors(
            _legacy.get(
                "cleanup_node_selectors",
                "DeepRAMCleanNode,SegmentDeepRAMCleanNode,VRAM_Debug,easy showAnything,PreviewImage,SaveImage",
            )
        )
        merged_filename_prefix = str(merged_filename_prefix or "sur_merged").strip() or "sur_merged"
        enable_checkpoint = _sur_bool(enable_checkpoint, True)
        auto_resume_checkpoint = _sur_bool(auto_resume_checkpoint, True)
        clear_checkpoint_on_finish = _sur_bool(clear_checkpoint_on_finish, True)
        pre_segment_paths = str(pre_segment_paths or "").strip()
        reference_image_node_id = str(reference_image_node_id or "").strip()
        segment_reference_images = str(segment_reference_images or "").strip()
        audio_mode = str(audio_mode or "keep_original")
        requested_segment_io_mode = str(segment_io_mode or _legacy.get("segment_io_mode") or "frame_window")
        segment_io_mode = "frame_window"
        physical_mode = False
        physical_slice_subdir = _sur_clean_rel_dir(
            physical_slice_subdir or _legacy.get("physical_slice_subdir") or "SUR_physical_segments"
        )
        try:
            raw_crf = physical_slice_crf if physical_slice_crf is not None else _legacy.get("physical_slice_crf", 12)
            physical_slice_crf = max(0, min(30, int(raw_crf)))
        except Exception:
            physical_slice_crf = 12
        reuse_physical_slices = _sur_bool(reuse_physical_slices, True)
        legacy_widget_shift = False
        uid          = unique_id

        def log(msg):
            _sur_log(uid, f"[SUR] {msg}")

        if requested_segment_io_mode != "frame_window":
            log(f"分段输入模式 {requested_segment_io_mode} 已退役，本次自动改用 frame_window。")

        extra_info = extra_pnginfo if isinstance(extra_pnginfo, dict) else {}
        full_prompt = extra_info.get("sur_full_prompt") or prompt
        client_id   = str(extra_info.get("sur_client_id") or _sur_current_client_id() or "")
        load_nid = _sur_auto_node_id(
            full_prompt, load_nid,
            ("VHS_LoadVideo", "VHS_LoadVideoFFmpeg", "VHS_LoadVideoPath", "VHS_LoadVideoFFmpegPath"),
            "load_video_node_id", log=log
        )
        combine_nid = _sur_auto_node_id(full_prompt, combine_nid, "VHS_VideoCombine", "combine_video_node_id", log=log)
        trimmer_nid = _sur_auto_node_id(full_prompt, trimmer_nid, "SegmentFrameTrimmer", "trimmer_node_id", log=log)
        bridge_trimmer_nid = _sur_auto_node_id(
            full_prompt,
            bridge_trimmer_nid,
            "SegmentVfiBridgeTrimmer",
            "bridge_trimmer_node_id",
            log=log,
        )
        trim_multiplier, trim_note = _infer_trimmer_trim_multiplier(full_prompt, trimmer_nid)
        if trim_override > 0:
            trim_multiplier, trim_note = trim_override, "手动覆盖"
        bridge_enabled = bool(bridge_trimmer_nid and overlap > 0)

        # ── 预览模式 ──────────────────────────────────────────────
        if not execute:
            plan = _build_plan_text(
                total_frames, segments, start_from,
                overlap, load_nid, trimmer_nid, bridge_trimmer_nid,
                vfi_bridge_frames,
                trim_multiplier, trim_note,
                segment_io_mode=segment_io_mode,
                physical_slice_subdir=physical_slice_subdir,
            )
            _sur_log(uid, "[预览模式]\n" + plan)
            _interrupt_current()
            return {}

        # ── 执行前校验 ────────────────────────────────────────────
        checks = [
            (total_frames <= 0,
             "total_frames 必须 > 0，请连接 VHS_LoadVideo 的 frame_count 输出"),
            (not load_nid,    "load_video_node_id 不能为空"),
            (not combine_nid, "combine_video_node_id 不能为空"),
            (overlap > 0 and not trimmer_nid,
             "overlap_frames > 0 时必须填写 trimmer_node_id（SegmentFrameTrimmer 节点）"),
        ]
        for cond, msg in checks:
            if cond:
                log(f"✗ {msg}")
                _interrupt_current()
                return {}

        nid_checks = [(load_nid, "VHS_LoadVideo"), (combine_nid, "VHS_VideoCombine")]
        if trimmer_nid:
            nid_checks.append((trimmer_nid, "SegmentFrameTrimmer"))
        if bridge_trimmer_nid:
            nid_checks.append((bridge_trimmer_nid, "SegmentVfiBridgeTrimmer"))
        for nid, label in nid_checks:
            if nid not in (full_prompt or {}):
                log(f"✗ 找不到 {label} 节点 ID「{nid}」")
                _interrupt_current()
                return {}

        seg_list = calc_segments(total_frames, segments, overlap)

        pre_paths = []
        for raw in [p.strip() for p in pre_segment_paths.split(",") if p.strip()]:
            resolved = _sur_resolve_media_path(raw)
            if resolved and os.path.isfile(resolved):
                pre_paths.append(resolved)

        ckpt = _sur_read_checkpoint(uid) if enable_checkpoint and auto_resume_checkpoint else None
        resume_note = ""
        run_stamp = _now_stamp()
        if isinstance(ckpt, dict):
            try:
                next_seg = int(ckpt.get("next_seg") or 1)
                if 1 < next_seg <= len(seg_list) + 1:
                    start_from = max(start_from, next_seg)
                    run_stamp = str(ckpt.get("run_stamp") or run_stamp)
                    ckpt_paths = [
                        p for p in ckpt.get("segment_output_paths", [])
                        if isinstance(p, str) and os.path.isfile(p)
                    ]
                    if ckpt_paths:
                        pre_paths = ckpt_paths + [p for p in pre_paths if p not in ckpt_paths]
                    resume_note = f"自动续跑: checkpoint next_seg={next_seg}, 已完成视频={len(ckpt_paths)}"
            except Exception:
                resume_note = "checkpoint 存在但解析失败，本次按当前参数执行"
        elif enable_checkpoint and not auto_resume_checkpoint and uid:
            _sur_clear_checkpoint(uid)

        segs_to_run = [
            (i + 1, skip, limit, trim)
            for i, (skip, limit, trim) in enumerate(seg_list)
            if i + 1 >= start_from
        ]
        base_prompt = copy.deepcopy(full_prompt)
        job_key     = str(uid or "global")
        audio_filename = _sur_find_audio_filename(base_prompt, load_nid)
        base_start_time = _sur_base_video_start_time(base_prompt, load_nid)
        base_skip_frames = _sur_base_video_skip_frames(base_prompt, load_nid)
        source_video_path = _sur_resolve_media_path(audio_filename) if audio_filename else None
        base_frame_offset = int(base_skip_frames)
        if base_start_time > 0 and frame_rate > 0:
            base_frame_offset += int(round(base_start_time * frame_rate))
        ref_images_list = [
            x.strip()
            for x in segment_reference_images.split(",")
            if x.strip()
        ]
        if ref_images_list:
            ref_images_list = _sur_prepare_reference_images(ref_images_list, unique_id=uid)

        if physical_mode and not source_video_path:
            log("✗ 物理分片模式需要 VHS_LoadVideo.video 指向可解析的源视频文件")
            _interrupt_current()
            return {}

        def submit_all():
            try:
                log(f"{'═'*20} 开始执行 stamp={run_stamp} {'═'*20}")
                log(f"LoadVideo [{load_nid}]  VideoCombine [{combine_nid}]"
                    + (f"  FinalTrimmer [{trimmer_nid}]" if trimmer_nid else "")
                    + (f"  BridgeTrimmer [{bridge_trimmer_nid}]" if bridge_trimmer_nid else ""))
                log(f"总帧数={total_frames}  共{segments}段  模型上下文重叠={overlap}帧"
                    f"  执行第{start_from}~{len(seg_list)}段")
                if bridge_enabled:
                    log(f"VFI 前桥接裁剪=开  桥接帧={vfi_bridge_frames}（重叠区不会整段进入 RIFE/VFI）")
                elif overlap > 0:
                    log("VFI 前桥接裁剪=关（兼容旧模式：重叠区会完整进入 RIFE/VFI）")
                if resume_note:
                    log(resume_note)
                if pre_paths:
                    log(f"前段素材/已完成分段: {len(pre_paths)} 个")
                if ref_images_list:
                    log(f"分段参考图: {len(ref_images_list)} 张")
                if audio_mode != "keep_original":
                    log(f"音频模式: {audio_mode}" + (f"  源: {audio_filename}" if audio_filename else ""))
                if trimmer_nid:
                    log(f"插帧输出倍率=x{trim_multiplier}  来源: {trim_note}")
                if legacy_widget_shift:
                    log("检测到旧版工作流 widgets_values 顺序，已按旧参数自动兼容")
                log("前端执行状态转发=" + ("开" if client_id else "关（未取得 client_id）"))
                log(
                    f"history清理={'开' if clear_segment_history else '关'}"
                    + f"  跳过预览/调试旁路={'开' if prune_cleanup_debug_nodes else '关'}"
                    + f"  自动合并={'开' if merge_segments else '关'}"
                    + f"  物理分片段后缓存清理={'开' if physical_mode else '关'}"
                )

                physical_slices: dict[int, dict[str, str]] = {}
                physical_output_subfolder = ""
                if physical_mode:
                    physical_output_subfolder = f"{physical_slice_subdir}/{run_stamp}/results/"
                    if audio_mode == "keep_original":
                        log("  ⚠ 物理分片模式建议将 audio_mode 设为 segment_from_loadvideo，避免重叠段音频未同步裁剪。")
                    try:
                        physical_slices = _sur_prepare_physical_slices(
                            source_video_path,
                            seg_list,
                            run_stamp,
                            physical_slice_subdir,
                            base_frame_offset,
                            frame_rate,
                            physical_slice_crf,
                            reuse_physical_slices,
                            log=log,
                        )
                    except Exception as e:
                        log(f"✗ 物理分片准备失败: {type(e).__name__}: {e}")
                        return
                    log(f"成果目录: output/{physical_output_subfolder.rstrip('/')}")

                segment_output_paths: list[str] = list(pre_paths)
                _t0 = time.time()
                _all_done = False

                if not segs_to_run:
                    log("没有需要执行的分段；如果开启合并，将尝试使用 checkpoint/pre_segment_paths 中的视频。")
                    if len(segment_output_paths) >= len(seg_list):
                        _all_done = True

                for run_index, (seg_num, load_skip, load_limit, trim) in enumerate(segs_to_run):
                    pre_vfi_trim, vfi_overlap, final_trim_input = _bridge_plan(
                        trim, vfi_bridge_frames, bridge_enabled
                    )
                    output_trim = _output_trim_for_overlap(final_trim_input, trim_multiplier)
                    tail_trim = _output_tail_trim_for_segment(
                        overlap > 0,
                        seg_num == len(seg_list),
                        trim_multiplier,
                    )
                    saved_start = load_skip + trim
                    saved_end   = load_skip + load_limit - 1
                    saved_n     = load_limit - trim
                    log(f"── 第{seg_num}/{len(seg_list)}段  "
                        f"skip={load_skip}  limit={load_limit}  "
                        f"ctx={trim}  pre_vfi_trim={pre_vfi_trim}  "
                        f"vfi_bridge={vfi_overlap}  head_trim={output_trim}  tail_trim={tail_trim}"
                        f"  保存[{saved_start}~{saved_end}] ──")

                    wf = copy.deepcopy(base_prompt)
                    if prune_cleanup_debug_nodes:
                        pruned, blockers = _prune_in_graph_cleanup_branch(wf, cleanup_selectors)
                        if pruned:
                            log("  已移除图内清理/调试分支: " + ", ".join(pruned))
                        elif blockers:
                            log("  图内清理/调试节点连接到非调试节点，保留: " + ", ".join(blockers))

                    # 1. 修改 VHS_LoadVideo / VHS_LoadVideoFFmpeg
                    if physical_mode:
                        slice_info = physical_slices.get(seg_num)
                        if not slice_info:
                            log(f"✗ 第{seg_num}段缺少物理分片，已终止")
                            segment_failed = True
                            break
                        _sur_set_load_video_file_segment(
                            wf, load_nid,
                            slice_info["abs"], slice_info["rel"],
                            load_limit,
                            log=log,
                        )
                    else:
                        _sur_set_load_video_segment(
                            wf, load_nid, load_skip, load_limit, frame_rate,
                            base_start_time=base_start_time,
                            base_skip_frames=base_skip_frames,
                            log=log
                        )

                    # 2. 修改 SegmentFrameTrimmer（写入 VFI 后的输出裁剪帧数）
                    if bridge_trimmer_nid and bridge_trimmer_nid in wf:
                        wf[bridge_trimmer_nid]["inputs"]["trim_frames"] = pre_vfi_trim
                    if trimmer_nid and trimmer_nid in wf:
                        wf[trimmer_nid]["inputs"]["trim_frames"] = output_trim
                        wf[trimmer_nid]["inputs"]["tail_trim_frames"] = tail_trim

                    # 3. 修改 VHS_VideoCombine：唯一文件名前缀，避免覆盖
                    seg_prefix  = f"sur_seg{seg_num:02d}_{run_stamp}_"
                    orig_prefix = wf[combine_nid]["inputs"].get("filename_prefix", "")
                    slash       = max(orig_prefix.rfind("/"), orig_prefix.rfind("\\"))
                    subfolder   = physical_output_subfolder if physical_mode else (orig_prefix[:slash + 1] if slash >= 0 else "")
                    wf[combine_nid]["inputs"]["filename_prefix"] = subfolder + seg_prefix
                    wf[combine_nid]["inputs"]["save_output"]     = True
                    wf[combine_nid]["inputs"]["save_metadata"]   = False

                    # 4. 可选：按段替换参考图、按段切音频
                    _sur_set_segment_reference_image(
                        wf, reference_image_node_id, ref_images_list, seg_num - 1, log=log
                    )
                    _sur_set_segment_audio(
                        wf, combine_nid, load_nid, seg_num, saved_start, saved_n,
                        frame_rate, audio_mode, audio_filename,
                        base_start_time=base_start_time,
                        base_skip_frames=base_skip_frames,
                        log=log
                    )

                    # 5. 删除本节点自身，避免递归触发
                    if uid and str(uid) in wf:
                        del wf[str(uid)]

                    # 6. 提交、等待、段后清理
                    segment_failed = False
                    pid = ""
                    try:
                        pid = _queue_prompt(wf, client_id=client_id)
                        log(f"  已提交 prompt_id={pid[:8]}...  等待完成...")
                        ok  = _wait_for_prompt(pid)
                        if ok:
                            log(f"✓ 第{seg_num}段完成  输出前缀: {subfolder + seg_prefix}")
                            vpath, _ = _sur_get_output_video_info(pid, combine_nid, log=log)
                            if vpath:
                                segment_output_paths.append(vpath)
                                log(f"  ✓ 输出视频: {os.path.basename(vpath)}")
                            if seg_num == len(seg_list):
                                _all_done = True
                            if enable_checkpoint and uid:
                                _sur_write_checkpoint(uid, {
                                    "unique_id": uid,
                                    "run_stamp": run_stamp,
                                    "completed_seg": seg_num,
                                    "total_segs": len(seg_list),
                                    "next_seg": min(seg_num + 1, len(seg_list) + 1),
                                    "segment_output_paths": segment_output_paths,
                                    "segments": segments,
                                    "total_frames_used": total_frames,
                                    "frame_rate_used": frame_rate,
                                    "overlap_frames": overlap,
                                    "vfi_bridge_frames": vfi_bridge_frames,
                                    "load_video_node_id": load_nid,
                                    "combine_video_node_id": combine_nid,
                                    "trimmer_node_id": trimmer_nid,
                                    "bridge_trimmer_node_id": bridge_trimmer_nid,
                                    "segment_io_mode": segment_io_mode,
                                    "physical_slice_subdir": physical_slice_subdir,
                                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                                })
                            elapsed = time.time() - _t0
                            frames_done = sum(max(0, lmt - trm) for _, _, lmt, trm in segs_to_run[:run_index + 1])
                            _sur_save_speed_record(elapsed, frames_done)
                            if clear_segment_history:
                                _sur_delete_prompt_history(pid, log=log)
                        else:
                            segment_failed = True
                            log(f"✗ 第{seg_num}段执行出错，已终止后续分段")
                    except Exception as e:
                        segment_failed = True
                        log(f"✗ 第{seg_num}段提交失败: {type(e).__name__}: {e}")
                    finally:
                        wf = None
                        if pid and physical_mode:
                            _sur_post_segment_cache_purge(log=log, trim_working_set=True)

                    if segment_failed:
                        break

                if merge_segments:
                    if not _all_done:
                        log("自动合并已跳过：任务未全部完成，避免生成不完整合并视频。")
                    elif len(segment_output_paths) >= 2:
                        orig_prefix = base_prompt[combine_nid]["inputs"].get("filename_prefix", "") if combine_nid in base_prompt else ""
                        slash = max(orig_prefix.rfind("/"), orig_prefix.rfind("\\"))
                        subfolder = physical_output_subfolder if physical_mode else (orig_prefix[:slash + 1] if slash >= 0 else "")
                        output_root = folder_paths.get_output_directory()
                        output_dir = os.path.join(output_root, subfolder.rstrip("/\\")) if subfolder else output_root
                        os.makedirs(output_dir, exist_ok=True)
                        merged_name = f"{merged_filename_prefix}_{run_stamp}.mp4"
                        merged_path = _sur_unique_filepath(os.path.join(output_dir, merged_name))
                        log(f"开始合并 {len(segment_output_paths)} 段视频...")
                        if _sur_merge_videos(segment_output_paths, merged_path, log=log):
                            rel = (subfolder + os.path.basename(merged_path)) if subfolder else os.path.basename(merged_path)
                            log(f"✓ 合并完成: {rel}")
                        else:
                            log("✗ 合并失败，请手动拼接各段视频")
                    else:
                        log("合并已开启，但可用分段视频少于 2 个，跳过")

                if enable_checkpoint and uid:
                    if _all_done and clear_checkpoint_on_finish:
                        _sur_clear_checkpoint(uid)
                        log("checkpoint 已清除（全部完成）")
                    elif not _all_done:
                        log("任务未全部完成，checkpoint 已保留供续跑")

                log(f"{'═'*20} 全部完成 {'═'*20}")
            finally:
                with _SUR_JOB_LOCK:
                    if _SUR_ACTIVE_JOBS.get(job_key) is threading.current_thread():
                        _SUR_ACTIVE_JOBS.pop(job_key, None)

        thread = threading.Thread(
            target=submit_all,
            daemon=True,
            name=f"SUR-submit-{job_key}",
        )
        with _SUR_JOB_LOCK:
            old = _SUR_ACTIVE_JOBS.get(job_key)
            if old is not None and old.is_alive():
                log("✗ 已有分段任务正在运行，本次不会再启动一个后台队列")
                return {}
            _SUR_ACTIVE_JOBS[job_key] = thread

        _interrupt_current()
        thread.start()
        return {}


# ── HTTP 接口：前端拉取日志 ───────────────────────────────────────



def _sur_vsrfi_stream_path() -> str:
    return os.path.join(os.path.dirname(_SUR_PLUGIN_DIR), "VSRFI-ComfyUI", "vsrfi_stream.py")


def _sur_vsrfi_vfi_methods() -> list[str]:
    base = os.path.dirname(_sur_vsrfi_stream_path())
    parent = os.path.dirname(base)
    methods = ["GIMM-VFI"]
    for name in ("comfyui-frame-interpolation", "ComfyUI-Frame-Interpolation"):
        if os.path.isdir(os.path.join(parent, name)):
            methods.extend(["RIFE", "FILM"])
            break
    return methods


def _sur_load_vsrfi_stream_module():
    path = _sur_vsrfi_stream_path()
    if not os.path.isfile(path):
        raise RuntimeError("VSRFI-ComfyUI was not found next to this plugin. Install neilthefrobot/VSRFI-ComfyUI first.")

    real_path = os.path.realpath(path)
    for module in list(sys.modules.values()):
        try:
            module_file = os.path.realpath(getattr(module, "__file__", ""))
        except Exception:
            continue
        if module_file == real_path and hasattr(module, "VSRFINode"):
            return module

    module_name = "_sur_external_vsrfi_stream"
    module = sys.modules.get(module_name)
    if module is not None and hasattr(module, "VSRFINode"):
        return module

    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load VSRFI stream module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _sur_load_vsrfi_stream_class():
    return _sur_load_vsrfi_stream_module().VSRFINode


def _sur_make_bridge_vsrfi_node(vsrfi_module, base_cls, bridge_frames: int):
    bridge_frames = max(0, int(bridge_frames or 0))

    class _SURBridgeVSRFINode(base_cls):
        def process_video(
            self,
            input_path,
            output_path,
            scale,
            frames_per_chunk,
            max_tile_kilopixels,
            max_gimm_kilopixels,
            interp_factor,
            device,
            vfi_method="GIMM-VFI",
            skip_first_frames=0,
            frame_load_cap=0,
        ):
            if bridge_frames <= 0:
                return super().process_video(
                    input_path,
                    output_path,
                    scale,
                    frames_per_chunk,
                    max_tile_kilopixels,
                    max_gimm_kilopixels,
                    interp_factor,
                    device,
                    vfi_method,
                    skip_first_frames,
                    frame_load_cap,
                )
            return _sur_vsrfi_process_video_with_bridge(
                self,
                vsrfi_module,
                input_path,
                output_path,
                scale,
                frames_per_chunk,
                max_tile_kilopixels,
                max_gimm_kilopixels,
                interp_factor,
                device,
                vfi_method,
                skip_first_frames,
                frame_load_cap,
                bridge_frames,
            )

    return _SURBridgeVSRFINode()


def _sur_vsrfi_process_video_with_bridge(
    node,
    vsrfi_module,
    input_path,
    output_path,
    scale,
    frames_per_chunk,
    max_tile_kilopixels,
    max_gimm_kilopixels,
    interp_factor,
    device,
    vfi_method="GIMM-VFI",
    skip_first_frames=0,
    frame_load_cap=0,
    bridge_frames=1,
):
    cv2 = vsrfi_module.cv2
    np = vsrfi_module.np
    torch = vsrfi_module.torch
    Path = vsrfi_module.Path
    tqdm = vsrfi_module.tqdm
    subprocess_mod = vsrfi_module.subprocess
    comfy_mod = getattr(vsrfi_module, "comfy", None)
    clean_vram = vsrfi_module.clean_vram

    node._vfi_method = vfi_method
    frames_per_chunk = max(1, int(frames_per_chunk or 1))
    bridge_frames = max(0, int(bridge_frames or 0))
    interp_factor = max(1, int(interp_factor or 1))

    cap = cv2.VideoCapture(input_path)
    fps = cap.get(cv2.CAP_PROP_FPS)
    w_orig = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h_orig = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_in_file = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    if skip_first_frames > 0:
        cap.set(cv2.CAP_PROP_POS_FRAMES, skip_first_frames)
        print(f"[SUR Stream] Skipping first {skip_first_frames} frames")

    total = max(0, total_in_file - int(skip_first_frames or 0))
    if frame_load_cap > 0:
        total = min(total, int(frame_load_cap))
        print(f"[SUR Stream] Frame load cap: {frame_load_cap} (will process {total} frames)")

    if total == 0:
        cap.release()
        raise ValueError(f"No frames to process (video has {total_in_file} frames, skip_first_frames={skip_first_frames}, frame_load_cap={frame_load_cap})")

    audio_start_time = skip_first_frames / fps if fps > 0 else 0
    print(f"[SUR Stream] Bridge frames enabled: {bridge_frames}; chunk source frames: {frames_per_chunk}")
    print(f"[DEBUG] Original input: {w_orig}x{h_orig}, processing frames {skip_first_frames}-{skip_first_frames + total - 1} of {total_in_file}")

    has_audio = False
    probe_cmd = [
        "ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
        "stream=codec_type", "-of", "default=noprint_wrappers=1:nokey=1", input_path,
    ]
    try:
        result = subprocess_mod.run(probe_cmd, capture_output=True, text=True, timeout=5)
        has_audio = result.stdout.strip() == "audio"
        print("[INFO] Audio track detected in input video" if has_audio else "[INFO] No audio track found in input video")
    except FileNotFoundError:
        print("[WARNING] ffprobe not found. Please install ffmpeg and ensure it is on your system PATH.")
    except Exception as e:
        print(f"[WARNING] Could not probe audio: {e}")

    if scale > 0:
        out_w = w_orig * scale
        out_h = h_orig * scale
        print(f"[DEBUG] Output: {out_w}x{out_h} (scale={scale})")
    else:
        out_w = w_orig
        out_h = h_orig
        print(f"[DEBUG] Output: {out_w}x{out_h} (no upscaling, VFI only)")

    video_only_path = output_path if not has_audio else str(Path(output_path).with_suffix(".temp_video.mp4"))
    ffmpeg_cmd = [
        "ffmpeg", "-y",
        "-f", "rawvideo",
        "-vcodec", "rawvideo",
        "-pix_fmt", "rgb24",
        "-s", f"{out_w}x{out_h}",
        "-r", str(fps * max(1, interp_factor)),
        "-i", "-",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        video_only_path,
    ]

    try:
        process = subprocess_mod.Popen(ffmpeg_cmd, stdin=subprocess_mod.PIPE, stderr=subprocess_mod.DEVNULL)
    except FileNotFoundError:
        raise RuntimeError("ffmpeg not found. Please install ffmpeg and ensure it is on your system PATH.")

    pbar = comfy_mod.utils.ProgressBar(total) if comfy_mod is not None else None
    buffer = []
    carry = []
    frames_processed = 0
    output_frames_written = 0
    was_cancelled = False
    tqdm_bar = None

    def _check_interrupt():
        if comfy_mod is not None:
            comfy_mod.model_management.throw_exception_if_processing_interrupted()

    def _discard_count(carry_len: int) -> int:
        if carry_len <= 0:
            return 0
        if interp_factor <= 1:
            return carry_len
        return (carry_len - 1) * interp_factor + 1

    def _write_chunk(work_buffer, new_count: int):
        nonlocal carry, frames_processed, output_frames_written
        carry_len = len(carry)
        chunk = node._process_chunk(
            work_buffer,
            scale,
            max_tile_kilopixels,
            max_gimm_kilopixels,
            interp_factor,
            device,
        )
        discard = min(_discard_count(carry_len), max(0, len(chunk) - 1))
        if discard:
            print(f"[SUR Stream] Dropping {discard} bridge output frames; keeping cross-boundary interpolation.")
            chunk = chunk[discard:]

        for frame in chunk:
            frame_out = (frame.detach().cpu().numpy() * 255).clip(0, 255).astype(np.uint8)
            process.stdin.write(frame_out.tobytes())
            output_frames_written += 1

        frames_processed += new_count
        if tqdm_bar is not None:
            tqdm_bar.update(new_count)
        if pbar is not None:
            pbar.update_absolute(frames_processed)

        if bridge_frames > 0:
            carry = [f.copy() for f in work_buffer[-bridge_frames:]]
        else:
            carry = []
        del chunk
        clean_vram()
        gc.collect()

    try:
        with tqdm(total=total, desc="Processing frames") as tqdm_bar:
            for _ in range(total):
                _check_interrupt()
                ret, frame = cap.read()
                if not ret:
                    break

                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                frame = frame.astype(np.float32) / 255.0
                buffer.append(frame)

                if len(buffer) >= frames_per_chunk:
                    _check_interrupt()
                    work_buffer = carry + buffer
                    new_count = len(buffer)
                    _write_chunk(work_buffer, new_count)
                    buffer = []
                    del work_buffer

            if buffer:
                _check_interrupt()
                work_buffer = carry + buffer
                new_count = len(buffer)
                _write_chunk(work_buffer, new_count)
                buffer = []
                del work_buffer

    except Exception as e:
        interrupt_type = getattr(getattr(comfy_mod, "model_management", None), "InterruptProcessingException", None) if comfy_mod is not None else None
        if interrupt_type is not None and isinstance(e, interrupt_type):
            was_cancelled = True
            print(f"\n[INFO] Processing cancelled by user. Saving partial video ({frames_processed}/{total} source frames processed)...")
            if tqdm_bar is not None:
                tqdm_bar.write(f"Cancelled - saving partial video to: {output_path}")
        else:
            raise

    finally:
        cap.release()
        if process.stdin:
            try:
                process.stdin.close()
            except Exception:
                pass
        if process:
            try:
                process.wait(timeout=10)
            except subprocess_mod.TimeoutExpired:
                process.kill()
                process.wait()

        if has_audio and os.path.exists(video_only_path):
            try:
                print("[INFO] Adding audio to output video...")
                mux_cmd = [
                    "ffmpeg", "-y",
                    "-i", video_only_path,
                    "-ss", str(audio_start_time),
                    "-i", input_path,
                    "-map", "0:v:0",
                    "-map", "1:a:0",
                    "-c:v", "copy",
                    "-c:a", "aac",
                    "-shortest",
                    output_path,
                ]
                result = subprocess_mod.run(mux_cmd, capture_output=True, timeout=30)
                if result.returncode == 0 and os.path.exists(output_path):
                    print("[INFO] Audio successfully added to output video")
                    try:
                        os.remove(video_only_path)
                    except Exception:
                        pass
                else:
                    print(f"[WARNING] Audio muxing failed, keeping video-only file at: {video_only_path}")
                    if os.path.exists(video_only_path):
                        try:
                            os.rename(video_only_path, output_path)
                        except Exception:
                            pass
            except Exception as e:
                print(f"[WARNING] Could not add audio: {e}, keeping video-only file")
                if os.path.exists(video_only_path) and not os.path.exists(output_path):
                    try:
                        os.rename(video_only_path, output_path)
                    except Exception:
                        pass

        print(f"[SUR Stream] Source frames processed: {frames_processed}; output frames written: {output_frames_written}")
        if was_cancelled and comfy_mod is not None:
            print("[INFO] Partial video saved successfully.")
            raise comfy_mod.model_management.InterruptProcessingException()

class SegmentVSRFIStreamRunner:
    """Streaming upscale/interpolation runner backed by VSRFI-ComfyUI.

    This mode is for long video upscale + VFI jobs. It avoids the generic
    ComfyUI IMAGE tensor chain by letting VSRFI read, process, and encode
    chunks directly from/to disk.
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "video_path": (
                    "STRING",
                    {
                        "default": "",
                        "placeholder": "input 中的视频文件名，或完整路径",
                        "display_name": "视频文件",
                        "tooltip": "输入视频。可填写 ComfyUI/input 下的文件名、相对路径或完整路径；也可以点节点里的“选择/上传视频”。",
                    },
                ),
                "output_path": (
                    "STRING",
                    {
                        "default": "",
                        "placeholder": "留空自动输出到 output/VSRFI",
                        "display_name": "输出路径",
                        "tooltip": "输出 mp4 路径。留空时自动保存到 output/VSRFI/<输入文件名>_VSRFI.mp4。",
                    },
                ),
                "execute": (
                    "BOOLEAN",
                    {
                        "default": False,
                        "display_name": "开始执行",
                        "tooltip": "False 只预览参数；True 才开始流式放大/插帧。",
                    },
                ),
                "scale": (
                    "INT",
                    {
                        "default": 2,
                        "min": 0,
                        "max": 16,
                        "step": 1,
                        "display_name": "放大倍率",
                        "tooltip": "空间放大倍率。2 表示宽高各放大 2 倍；0 表示不放大，只做插帧。",
                    },
                ),
                "interpolation_factor": (
                    "INT",
                    {
                        "default": 2,
                        "min": 0,
                        "max": 16,
                        "step": 1,
                        "display_name": "插帧倍率",
                        "tooltip": "帧率倍率。2 会把 25fps 变成 50fps；小于 2 时跳过插帧。",
                    },
                ),
                "vfi_method": (
                    _sur_vsrfi_vfi_methods(),
                    {
                        "default": "GIMM-VFI",
                        "display_name": "插帧方法",
                        "tooltip": "插帧模型。RIFE/FILM 需要安装 ComfyUI-Frame-Interpolation；GIMM-VFI 需要对应模型目录。",
                    },
                ),
                "frames_per_chunk": (
                    "INT",
                    {
                        "default": 21,
                        "min": 1,
                        "max": 100000,
                        "step": 1,
                        "display_name": "每块源帧数",
                        "tooltip": "每次处理的新源帧数量。越小越省 RAM/VRAM，但速度更慢；12GB 显存可先用 21。",
                    },
                ),
                "bridge_frames": (
                    "INT",
                    {
                        "default": 1,
                        "min": 0,
                        "max": 64,
                        "step": 1,
                        "display_name": "桥接帧",
                        "tooltip": "把上一块末尾的源帧带入下一块。1 可保留边界插帧；4-8 会给 FlashVSR 更多时序上下文，但会稍慢。",
                    },
                ),
                "max_tile_kilopixels": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "max": 100000,
                        "step": 1,
                        "display_name": "VSR分块上限",
                        "tooltip": "放大模型的 tile 输入像素上限，单位千像素。0 表示让 VSRFI 根据显存自动选择。",
                    },
                ),
                "max_gimm_kilopixels": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "max": 100000,
                        "step": 1,
                        "display_name": "GIMM分块上限",
                        "tooltip": "GIMM-VFI 光流/插帧分块上限，单位千像素。0 表示自动选择。",
                    },
                ),
                "skip_first_frames": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "max": 999999,
                        "step": 1,
                        "display_name": "跳过开头帧",
                        "tooltip": "从输入视频开头跳过多少源帧后再开始处理。",
                    },
                ),
                "frame_load_cap": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "max": 999999,
                        "step": 1,
                        "display_name": "最多处理帧",
                        "tooltip": "最多处理多少源帧。0 表示从 skip_first_frames 开始处理到视频结束。",
                    },
                ),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("output_path",)
    OUTPUT_NODE = True
    FUNCTION = "run"
    CATEGORY = "video/Segment Upscale Runner"

    @classmethod
    def IS_CHANGED(cls, execute=False, **kwargs):
        return time.time() if _sur_bool(execute, False) else "preview"

    def run(
        self,
        video_path="",
        output_path="",
        execute=False,
        scale=2,
        interpolation_factor=2,
        vfi_method="GIMM-VFI",
        frames_per_chunk=21,
        bridge_frames=1,
        max_tile_kilopixels=0,
        max_gimm_kilopixels=0,
        skip_first_frames=0,
        frame_load_cap=0,
        unique_id=None,
    ):
        uid = unique_id

        def log(msg):
            _sur_log(uid, f"[SUR Stream] {msg}")

        video_path = str(video_path or "").strip().strip('"')
        output_path = str(output_path or "").strip().strip('"')
        scale = max(0, int(scale or 0))
        interpolation_factor = max(0, int(interpolation_factor or 0))
        frames_per_chunk = max(1, int(frames_per_chunk or 1))
        bridge_frames = max(0, int(bridge_frames or 0))
        max_tile_kilopixels = max(0, int(max_tile_kilopixels or 0))
        max_gimm_kilopixels = max(0, int(max_gimm_kilopixels or 0))
        skip_first_frames = max(0, int(skip_first_frames or 0))
        frame_load_cap = max(0, int(frame_load_cap or 0))
        vfi_method = str(vfi_method or "GIMM-VFI")

        if not video_path:
            log("No input video was provided.")
            return ("",)

        log("VSRFI stream mode")
        log(f"  video_path={video_path}")
        log(f"  output_path={output_path or '(auto: output/VSRFI)'}")
        log(f"  scale={scale}  interpolation_factor={interpolation_factor}  vfi_method={vfi_method}")
        log(f"  frames_per_chunk={frames_per_chunk}  bridge_frames={bridge_frames}")
        log(f"  max_tile_kilopixels={max_tile_kilopixels}  max_gimm_kilopixels={max_gimm_kilopixels}")
        log(f"  skip_first_frames={skip_first_frames}  frame_load_cap={frame_load_cap}")

        if not _sur_bool(execute, False):
            log("Preview only. Set execute=True to start the stream job.")
            return ("",)

        try:
            vsrfi_module = _sur_load_vsrfi_stream_module()
            vsrfi_cls = vsrfi_module.VSRFINode
            vsrfi_node = _sur_make_bridge_vsrfi_node(vsrfi_module, vsrfi_cls, bridge_frames) if bridge_frames > 0 else vsrfi_cls()
            out = vsrfi_node.process(
                video_path,
                output_path,
                scale,
                frames_per_chunk,
                max_tile_kilopixels,
                max_gimm_kilopixels,
                interpolation_factor,
                vfi_method,
                skip_first_frames,
                frame_load_cap,
            )
            result_path = out[0] if out else ""
            log(f"Done: {result_path}")
            return (result_path,)
        except Exception as e:
            log(f"Failed: {type(e).__name__}: {e}")
            raise
@server.PromptServer.instance.routes.get("/sur/log")
async def sur_log_api(request):
    uid   = request.query.get("node_id", "")
    lines = _sur_log_buf.get(str(uid), [])
    return web.json_response({"lines": lines})

@server.PromptServer.instance.routes.post("/sur/log/clear")
async def sur_log_clear_api(request):
    uid = request.query.get("node_id", "")
    _sur_log_clear(uid)
    return web.json_response({"ok": True})


@server.PromptServer.instance.routes.get("/sur/checkpoint")
async def sur_checkpoint_api(request):
    uid = request.query.get("node_id", "") or request.query.get("uid", "")
    ckpt = _sur_read_checkpoint(uid)
    return web.json_response({"checkpoint": ckpt})


@server.PromptServer.instance.routes.post("/sur/checkpoint/clear")
async def sur_checkpoint_clear_api(request):
    uid = request.query.get("node_id", "") or request.query.get("uid", "")
    _sur_clear_checkpoint(uid)
    return web.json_response({"ok": True})


def _sur_list_media(exts: tuple[str, ...], max_items: int = 500):
    items = []
    for root in _sur_media_roots():
        try:
            for dirpath, _, files in os.walk(root):
                for name in files:
                    if not name.lower().endswith(exts):
                        continue
                    path = os.path.join(dirpath, name)
                    try:
                        st = os.stat(path)
                    except Exception:
                        continue
                    items.append({
                        "name": name,
                        "path": path,
                        "root": root,
                        "size": st.st_size,
                        "mtime": st.st_mtime,
                    })
        except Exception:
            continue
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items[:max_items]


def _sur_safe_upload_name(original: str, default_ext: str) -> str:
    base = os.path.basename(str(original or "")).strip()
    if not base:
        base = f"sur_upload_{_now_stamp()}{default_ext}"
    root, ext = os.path.splitext(base)
    if not ext:
        base = root + default_ext
    return base.replace("/", "_").replace("\\", "_")


async def _sur_upload_file(request, allowed_exts: tuple[str, ...], default_ext: str):
    reader = await request.multipart()
    field = await reader.next()
    if field is None:
        return web.json_response({"ok": False, "error": "missing file"}, status=400)
    name = _sur_safe_upload_name(getattr(field, "filename", ""), default_ext)
    if not name.lower().endswith(allowed_exts):
        return web.json_response({"ok": False, "error": "unsupported extension"}, status=400)
    input_dir = folder_paths.get_input_directory()
    os.makedirs(input_dir, exist_ok=True)
    dst = _sur_unique_filepath(os.path.join(input_dir, name))
    with open(dst, "wb") as f:
        while True:
            chunk = await field.read_chunk()
            if not chunk:
                break
            f.write(chunk)
    return web.json_response({"ok": True, "path": dst, "name": os.path.basename(dst)})


@server.PromptServer.instance.routes.get("/sur/list_images")
async def sur_list_images_api(request):
    return web.json_response({
        "images": _sur_list_media((".png", ".jpg", ".jpeg", ".webp", ".bmp")),
    })


@server.PromptServer.instance.routes.get("/sur/list_videos")
async def sur_list_videos_api(request):
    return web.json_response({
        "videos": _sur_list_media((".mp4", ".mov", ".mkv", ".webm", ".avi")),
    })


@server.PromptServer.instance.routes.post("/sur/upload_image")
async def sur_upload_image_api(request):
    return await _sur_upload_file(request, (".png", ".jpg", ".jpeg", ".webp", ".bmp"), ".png")


@server.PromptServer.instance.routes.post("/sur/upload_video")
async def sur_upload_video_api(request):
    return await _sur_upload_file(request, (".mp4", ".mov", ".mkv", ".webm", ".avi"), ".mp4")


# ── 节点注册 ──────────────────────────────────────────────────────

NODE_CLASS_MAPPINGS = {
    "SegmentUpscaleRunner": SegmentUpscaleRunner,
    "SegmentFrameTrimmer":  SegmentFrameTrimmer,
    "SegmentVfiBridgeTrimmer": SegmentVfiBridgeTrimmer,
    "SegmentRunLogViewer":  SegmentRunLogViewer,
    "SegmentVideoInfoProbe": SegmentVideoInfoProbe,
    "SegmentVSRFIStreamRunner": SegmentVSRFIStreamRunner,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "SegmentUpscaleRunner": "Segment Upscale Runner 🎬",
    "SegmentFrameTrimmer":  "Segment Final Frame Trimmer ✂️",
    "SegmentVfiBridgeTrimmer": "Segment VFI Bridge Trimmer",
    "SegmentRunLogViewer":  "Segment Run Log Viewer 📋",
    "SegmentVideoInfoProbe": "Segment Video Info Probe",
    "SegmentVSRFIStreamRunner": "SUR VSRFI Stream Runner",
}
