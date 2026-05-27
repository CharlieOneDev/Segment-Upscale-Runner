"""
ComfyUI 通用视频分段加工队列节点 (Segment Upscale Runner)
- 将大视频拆成多个连续片段，逐段提交到任意自定义 IMAGE/video 工作流
- 每段独立保存为单独视频，可选自动合并
- 支持断点续跑
- 支持重叠帧（Overlap Frames）：用前一段尾部 N 帧作为下一段头部的时序上下文，
  保存时由 SegmentFrameTrimmer 节点自动裁掉，合并后无重叠、无跳帧
"""

import copy, ctypes, gc, hashlib, json, time, os, platform, shutil, subprocess, sys, tempfile, threading, traceback, urllib.request, urllib.error
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
    Runner 会在每段 prompt 完成后再调用清理，所以子 prompt 中只移除
    已选择的清理/调试节点以及它们下游的 show/debug 分支。
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


def _sur_set_load_video_segment(
    wf: dict,
    load_nid: str,
    load_skip: int,
    load_limit: int,
    frame_rate: float,
    base_start_time: float = 0.0,
    log=None,
):
    node = wf.get(str(load_nid))
    if not node:
        return
    inputs = node.setdefault("inputs", {})
    class_type = _node_class(node)
    inputs["frame_load_cap"] = int(load_limit)

    if "skip_first_frames" in inputs:
        inputs["skip_first_frames"] = int(load_skip)
        if log:
            log(f"  LoadVideo: skip_first_frames={load_skip} frame_load_cap={load_limit}")
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

    inputs["skip_first_frames"] = int(load_skip)
    if log:
        log("  ⚠ LoadVideo 未声明 skip_first_frames/start_time，已尝试写入 skip_first_frames")


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
    start_time = max(0.0, float(base_start_time) + saved_start / frame_rate) if frame_rate > 0 else float(base_start_time)
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


def _sur_repair_legacy_widget_shift(
    clear_segment_history,
    unload_models_between_segments,
    merge_segments,
    merged_filename_prefix,
    enable_checkpoint,
    auto_resume_checkpoint,
    clear_checkpoint_on_finish,
    pre_segment_paths,
    reference_image_node_id,
    segment_reference_images,
    audio_mode,
):
    shifted = (
        isinstance(unload_models_between_segments, str)
        and not isinstance(merged_filename_prefix, str)
    )
    if not shifted:
        return (
            clear_segment_history,
            unload_models_between_segments,
            merge_segments,
            merged_filename_prefix,
            enable_checkpoint,
            auto_resume_checkpoint,
            clear_checkpoint_on_finish,
            pre_segment_paths,
            reference_image_node_id,
            segment_reference_images,
            audio_mode,
            False,
        )

    old_merge_segments = clear_segment_history
    old_merged_filename_prefix = unload_models_between_segments
    old_enable_checkpoint = merge_segments
    old_auto_resume_checkpoint = merged_filename_prefix
    old_clear_checkpoint_on_finish = enable_checkpoint
    old_pre_segment_paths = auto_resume_checkpoint
    old_reference_image_node_id = clear_checkpoint_on_finish
    old_segment_reference_images = pre_segment_paths
    old_audio_mode = reference_image_node_id

    return (
        True,
        False,
        old_merge_segments,
        old_merged_filename_prefix,
        old_enable_checkpoint,
        old_auto_resume_checkpoint,
        old_clear_checkpoint_on_finish,
        old_pre_segment_paths,
        old_reference_image_node_id,
        old_segment_reference_images,
        old_audio_mode or audio_mode,
        True,
    )


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


def _run_post_segment_clean(log, deep: bool = True, unload_models: bool = False):
    before_ram = _sur_ram_gb()
    before_vram = _sur_vram_gb()
    if log:
        log(f"  段后清理开始: RAM={before_ram:.2f}GB VRAM={before_vram:.2f}GB")
    if unload_models:
        try:
            from comfy import model_management as _mm
            _mm.unload_all_models()
            log("  模型卸载: 已请求 unload_all_models")
        except Exception as e:
            log(f"  模型卸载失败: {type(e).__name__}: {e}")

    if not deep:
        _clear_comfy_execution_cache(log=log)
        after_ram = _sur_ram_gb()
        after_vram = _sur_vram_gb()
        if log:
            log(
                f"  段后清理结束: RAM={after_ram:.2f}GB VRAM={after_vram:.2f}GB "
                f"(ΔRAM={after_ram - before_ram:+.2f}GB ΔVRAM={after_vram - before_vram:+.2f}GB)"
            )
        return
    try:
        cleaner = SegmentDeepRAMCleanNode()
        _, report = cleaner.deep_clean(
            wait_ffmpeg_subprocess=True,
            clear_executor_cache=True,
            clear_cell_refs=True,
            clear_model_cpu_cache=True,
            os_trim=True,
            forensic_tensor_refs=False,
            forensic_executor=False,
            forensic_models=False,
            forensic_subprocess=False,
            forensic_threads=False,
            forensic_gc_garbage=False,
            any_input=None,
        )
        summary = [
            line.strip()
            for line in str(report).splitlines()
            if line.startswith("RAM:") or line.startswith("VRAM:")
        ]
        log("  深度段后清理完成" + (": " + " / ".join(summary) if summary else ""))
    except Exception as e:
        log(f"  深度段后清理失败: {type(e).__name__}: {e}")
    finally:
        _clear_comfy_execution_cache(log=log)
        after_ram = _sur_ram_gb()
        after_vram = _sur_vram_gb()
        if log:
            log(
                f"  段后清理结束: RAM={after_ram:.2f}GB VRAM={after_vram:.2f}GB "
                f"(ΔRAM={after_ram - before_ram:+.2f}GB ΔVRAM={after_vram - before_vram:+.2f}GB)"
            )


# ── 工具函数 ──────────────────────────────────────────────────────

def _now_stamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")

def _build_plan_text(total_frames, segments, start_from,
                     overlap, load_nid, trimmer_nid,
                     trim_multiplier: int = 1, trim_note: str = "") -> str:
    if total_frames <= 0:
        return "✗ total_frames 必须大于 0"

    seg_list = calc_segments(total_frames, segments, overlap)
    lines = [
        f"LoadVideo 节点 ID : {load_nid}",
        f"Trimmer  节点 ID  : {trimmer_nid or '（未设置，overlap_frames 将被忽略）'}",
        f"总帧数: {total_frames}  共 {segments} 段  重叠帧: {overlap}  从第 {start_from} 段开始",
        f"Trimmer 输出裁剪倍率: x{trim_multiplier}"
        + (f"（{trim_note}）" if trim_note else ""),
        "",
        f"  {'段':>3}  {'load_skip':>9}  {'load_limit':>10}  {'trim_in':>7}  {'trim_out':>8}"
        f"  {'保存范围':>14}  {'保存帧数':>8}  状态",
        f"  {'-'*3}  {'-'*9}  {'-'*10}  {'-'*7}  {'-'*8}  {'-'*14}  {'-'*8}  ----",
    ]
    for i, (skip, limit, trim) in enumerate(seg_list):
        seg_num     = i + 1
        saved_start = skip + trim
        saved_end   = skip + limit - 1
        saved_n     = limit - trim
        out_trim    = _output_trim_for_overlap(trim, trim_multiplier)
        status      = "→ 执行" if seg_num >= start_from else "  跳过"
        lines.append(
            f"  第{seg_num:>2}段  skip={skip:>7}  limit={limit:>8}"
            f"  trim_in={trim:>3}  trim_out={out_trim:>3}"
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


# ── 内置 RAM 深度清理节点 ─────────────────────────────────────────

class SegmentDeepRAMCleanNode:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "wait_ffmpeg_subprocess": (
                    "BOOLEAN",
                    {
                        "default": True,
                        "tooltip": "等待 ffmpeg 子进程结束。Windows + VHS/VideoCombine 场景建议保持开启。",
                    },
                ),
                "clear_executor_cache": (
                    "BOOLEAN",
                    {"default": True, "tooltip": "深度清理 ComfyUI executor/caches 中的输出 tensor。"},
                ),
                "clear_cell_refs": (
                    "BOOLEAN",
                    {"default": True, "tooltip": "清理闭包 cell 中直接持有的大 CPU tensor。"},
                ),
                "clear_model_cpu_cache": (
                    "BOOLEAN",
                    {"default": True, "tooltip": "请求 ComfyUI model_management 释放模型缓存。"},
                ),
                "os_trim": (
                    "BOOLEAN",
                    {"default": True, "tooltip": "Windows 调 EmptyWorkingSet，Linux 调 malloc_trim。"},
                ),
                "forensic_tensor_refs": (
                    "BOOLEAN",
                    {"default": True, "tooltip": "输出大 CPU tensor 的直接引用者。日志会比较长。"},
                ),
                "forensic_executor": (
                    "BOOLEAN",
                    {"default": True, "tooltip": "分析 ComfyUI executor/cache 中的 tensor 占用。"},
                ),
                "forensic_models": (
                    "BOOLEAN",
                    {"default": True, "tooltip": "分析 comfy.model_management 当前加载模型。"},
                ),
                "forensic_subprocess": (
                    "BOOLEAN",
                    {"default": True, "tooltip": "分析当前 ComfyUI 子进程，尤其是 ffmpeg。"},
                ),
                "forensic_threads": (
                    "BOOLEAN",
                    {"default": True, "tooltip": "分析当前线程栈。"},
                ),
                "forensic_gc_garbage": (
                    "BOOLEAN",
                    {"default": True, "tooltip": "检查 gc.garbage。"},
                ),
            },
            "optional": {
                "any_input": ("*", {}),
            },
        }

    RETURN_TYPES = ("*", "STRING")
    RETURN_NAMES = ("passthrough", "report")
    FUNCTION = "deep_clean"
    CATEGORY = "video/utils"
    OUTPUT_NODE = True

    def deep_clean(
        self,
        wait_ffmpeg_subprocess=True,
        clear_executor_cache=True,
        clear_cell_refs=True,
        clear_model_cpu_cache=True,
        os_trim=True,
        forensic_tensor_refs=True,
        forensic_executor=True,
        forensic_models=True,
        forensic_subprocess=True,
        forensic_threads=True,
        forensic_gc_garbage=True,
        any_input=None,
    ):
        input_was_connected = any_input is not None
        any_input = None

        torch = _sur_import_torch()
        process = _sur_process()
        lines = [
            "=" * 60,
            "SUR 内置 RAM 深度取证+清理报告",
            f"   OS: {platform.system()}  Python: {sys.version.split()[0]}",
            "=" * 60,
        ]
        if torch is None:
            lines.append("提示: torch 不可用，tensor/VRAM 相关清理会跳过。")
        if process is None:
            lines.append("提示: psutil 不可用，进程/子进程 RAM 统计会跳过。")
        if input_was_connected:
            lines.append(
                "提示: any_input 已在清理函数开头置空；若它接的是 IMAGE，"
                "ComfyUI 输入缓存仍可能在本次 prompt 结束前持有大 tensor。"
            )

        ram0 = _sur_ram_gb(process)
        vram0 = _sur_vram_gb(torch)
        lines.append(f"初始: RAM={ram0:.2f}GB  VRAM={vram0:.2f}GB")

        lines.append("\n── 取证（清理前）──")
        if forensic_threads:
            lines.append("")
            _sur_analyze_threads(lines)
        if forensic_subprocess:
            lines.append("")
            _sur_analyze_subprocesses(lines)
        if forensic_executor:
            lines.append("")
            _sur_analyze_executor_cache(lines)
        if forensic_models:
            lines.append("")
            _sur_analyze_models(lines)
        if forensic_tensor_refs:
            lines.append("")
            _sur_trace_tensor_holders(lines)
        if forensic_gc_garbage:
            lines.append("")
            _sur_check_gc_garbage(lines)

        lines.append(f"\n取证结束: RAM={_sur_ram_gb(process):.2f}GB")
        lines.append("\n── 清理 ──")

        if wait_ffmpeg_subprocess:
            lines.append("\n[0a] 等待 ffmpeg 子进程...")
            _sur_wait_for_ffmpeg_subprocesses(lines)

        lines.append("\n[0b] 等待 VHS/编码线程...")
        _sur_wait_for_vhs_threads(lines)

        c1 = gc.collect()
        gc.collect()
        lines.append(f"\n[1] GC 第一轮: 回收 {c1} 个  RAM={_sur_ram_gb(process):.2f}GB")

        if clear_executor_cache:
            r0 = _sur_ram_gb(process)
            lines.append("\n[2] executor/cache 深度清理...")
            executors = _sur_get_prompt_executors()
            if executors:
                total_tensors = 0
                for executor, source in executors:
                    total_tensors += _sur_deep_clear_executor(executor, source, torch, lines)
                lines.append(f"  合计 resize tensor: {total_tensors} 个")
            else:
                lines.append("  ✗ executor 未找到")
            gc.collect()
            gc.collect()
            lines.append(f"  清理后释放: {r0 - _sur_ram_gb(process):.2f}GB  RAM={_sur_ram_gb(process):.2f}GB")

        if clear_cell_refs:
            r0 = _sur_ram_gb(process)
            lines.append("\n[3] cell 闭包引用清理...")
            _sur_clear_cell_tensor_refs(lines)
            gc.collect()
            gc.collect()
            lines.append(f"  清理后释放: {r0 - _sur_ram_gb(process):.2f}GB  RAM={_sur_ram_gb(process):.2f}GB")

        if clear_model_cpu_cache:
            r0 = _sur_ram_gb(process)
            try:
                import comfy.model_management as mm
                mm.free_memory(1024**4, mm.get_torch_device())
                gc.collect()
                lines.append(f"\n[4] ComfyUI 模型缓存: 释放 {r0 - _sur_ram_gb(process):.2f}GB  RAM={_sur_ram_gb(process):.2f}GB")
            except Exception as e:
                lines.append(f"\n[4] ComfyUI 模型缓存: 失败 ({type(e).__name__}: {e})")

        if torch is not None:
            try:
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                    torch.cuda.ipc_collect()
                    torch.cuda.synchronize()
                    lines.append("\n[5] CUDA: empty_cache + ipc_collect + synchronize ✓")
            except Exception as e:
                lines.append(f"\n[5] CUDA 清理失败: {type(e).__name__}: {e}")

        c2 = gc.collect()
        gc.collect()
        gc.collect()
        lines.append(f"\n[6] GC 第二轮: 回收 {c2} 个  RAM={_sur_ram_gb(process):.2f}GB")

        if os_trim:
            r0 = _sur_ram_gb(process)
            ok = _sur_trim_ram_os()
            name = "Windows EmptyWorkingSet" if platform.system() == "Windows" else "malloc_trim"
            lines.append(f"\n[7] {name}: {'✓' if ok else '✗'}  释放 {r0 - _sur_ram_gb(process):.2f}GB  RAM={_sur_ram_gb(process):.2f}GB")

        lines.append("\n── 清理后验证 ──")
        if forensic_tensor_refs:
            lines.append("")
            _sur_trace_tensor_holders(lines, max_tensors=5)
        if forensic_subprocess:
            lines.append("")
            _sur_analyze_subprocesses(lines)

        ram1 = _sur_ram_gb(process)
        vram1 = _sur_vram_gb(torch)
        lines.append("\n" + "=" * 60)
        lines.append(f"RAM:  {ram0:.2f}GB -> {ram1:.2f}GB  (释放 {ram0 - ram1:.2f}GB)")
        lines.append(f"VRAM: {vram0:.2f}GB -> {vram1:.2f}GB  (释放 {vram0 - vram1:.2f}GB)")
        if ram0 - ram1 < 0.1:
            lines.append("⚠ RAM 几乎未释放：请重点检查 tensor 引用链、未结束的 ffmpeg、以及仍在图内执行的预览/保存分支。")
        lines.append("=" * 60)

        report = "\n".join(lines)
        print(report)
        return (None, report)


# ── SegmentFrameTrimmer 节点 ──────────────────────────────────────

class SegmentFrameTrimmer:
    """
    插在放大节点输出 和 VHS_VideoCombine 之间。
    SegmentUpscaleRunner 每段执行时会自动设置 trim_frames：
      - 第1段：trim_frames = 0（直通）
      - 后续段：裁掉头部重叠上下文对应的输出帧
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
                        "tooltip": "从帧序列头部裁掉的帧数，由 SegmentUpscaleRunner 自动控制。",
                    },
                ),
                "clone_output": (
                    "BOOLEAN",
                    {
                        "default": False,
                        "tooltip": (
                            "高级选项。开启后会复制裁剪后的输出，避免 PyTorch 切片视图继续引用原始大张量；"
                            "但会在保存前临时增加 RAM 占用。"
                        ),
                    },
                ),
            }
        }

    def trim(self, images, trim_frames: int, clone_output=False):
        if images is None:
            raise ValueError(
                "[SegmentFrameTrimmer] images 为 None，"
                "请将本节点的 images 输入直接连接到上游放大/插帧节点，"
                "不要经过类型为 * 的透传节点中转。"
            )
        n           = images.shape[0]
        trim_frames = max(0, min(int(trim_frames), n - 1))
        if trim_frames > 0:
            out = images[trim_frames:]
            if clone_output:
                out = out.clone()
            return (out,)
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
                            "重叠帧数（建议 4~16，0=不重叠）。\n"
                            "第2段起会向前多读这些帧作为模型时序上下文，\n"
                            "保存时由 SegmentFrameTrimmer 自动裁掉。\n"
                            "启用时必须填写 trimmer_node_id。"
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
                            "SegmentFrameTrimmer 节点 ID。\n"
                            "overlap_frames=0 时可留空；\n"
                            "overlap_frames>0 时必须填写。"
                        ),
                    },
                ),
                "trim_multiplier_override": (
                    "INT",
                    {
                        "default": 0, "min": 0, "max": 16, "step": 1,
                        "tooltip": (
                            "0=自动识别 Trimmer 上游的 RIFE/VFI 倍率。\n"
                            "如果自动识别失败，可手动填 2/4 等；overlap=8 且倍率=2 时会裁 15 输出帧。"
                        ),
                    },
                ),
                "cleanup_between_segments": (
                    "BOOLEAN",
                    {
                        "default": True,
                        "tooltip": "每段完成后清理 ComfyUI 执行缓存、CUDA 缓存和 Python GC。",
                    },
                ),
                "deep_cleanup_between_segments": (
                    "BOOLEAN",
                    {
                        "default": True,
                        "tooltip": "启用时调用本插件内置的 SegmentDeepRAMCleanNode 做更彻底的段后清理。",
                    },
                ),
                "prune_cleanup_debug_nodes": (
                    "BOOLEAN",
                    {
                        "default": True,
                        "tooltip": "从每段子 prompt 中移除清理/调试/预览分支，避免这些节点持有大 IMAGE tensor。",
                    },
                ),
                "cleanup_node_selectors": (
                    "STRING",
                    {
                        "default": "DeepRAMCleanNode,VRAM_Debug,easy showAnything,PreviewImage,SaveImage",
                        "tooltip": (
                            "要从子 prompt 移除的节点 ID 或 class_type，逗号分隔。\n"
                            "下游 show/debug/preview 节点会一起移除。\n"
                            "建议把不需要执行的预览、调试、保存图片节点都加进来。"
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
                "unload_models_between_segments": (
                    "BOOLEAN",
                    {
                        "default": False,
                        "tooltip": "更激进的段间清理：每段后卸载模型。能释放更多内存，但下一段会重新加载模型、速度更慢。",
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
        trim_multiplier_override=0,
        cleanup_between_segments=True,
        deep_cleanup_between_segments=True,
        prune_cleanup_debug_nodes=True,
        cleanup_node_selectors="DeepRAMCleanNode,VRAM_Debug,easy showAnything,PreviewImage,SaveImage",
        clear_segment_history=True,
        unload_models_between_segments=False,
        merge_segments=True,
        merged_filename_prefix="sur_merged",
        enable_checkpoint=True,
        auto_resume_checkpoint=True,
        clear_checkpoint_on_finish=True,
        pre_segment_paths="",
        reference_image_node_id="",
        segment_reference_images="",
        audio_mode="keep_original",
        prompt=None, extra_pnginfo=None, unique_id=None,
        **_legacy,
    ):
        total_frames = int(total_frames or 0)
        frame_rate   = float(frame_rate or 24.0)
        segments     = int(segment_count or 4)
        start_from   = max(1, int(start_segment or 1))
        overlap      = max(0, int(overlap_frames or 0))
        load_nid     = (load_video_node_id or "").strip()
        combine_nid  = (combine_video_node_id or "").strip()
        trimmer_nid  = (trimmer_node_id or "").strip()
        trim_override = max(0, int(trim_multiplier_override or 0))
        (
            clear_segment_history,
            unload_models_between_segments,
            merge_segments,
            merged_filename_prefix,
            enable_checkpoint,
            auto_resume_checkpoint,
            clear_checkpoint_on_finish,
            pre_segment_paths,
            reference_image_node_id,
            segment_reference_images,
            audio_mode,
            legacy_widget_shift,
        ) = _sur_repair_legacy_widget_shift(
            clear_segment_history,
            unload_models_between_segments,
            merge_segments,
            merged_filename_prefix,
            enable_checkpoint,
            auto_resume_checkpoint,
            clear_checkpoint_on_finish,
            pre_segment_paths,
            reference_image_node_id,
            segment_reference_images,
            audio_mode,
        )
        cleanup_between_segments = _sur_bool(cleanup_between_segments, True)
        deep_cleanup_between_segments = _sur_bool(deep_cleanup_between_segments, True)
        prune_cleanup_debug_nodes = _sur_bool(prune_cleanup_debug_nodes, True)
        clear_segment_history = _sur_bool(clear_segment_history, True)
        unload_models_between_segments = _sur_bool(unload_models_between_segments, False)
        merge_segments = _sur_bool(merge_segments, True)
        cleanup_selectors = _parse_selectors(cleanup_node_selectors)
        merged_filename_prefix = str(merged_filename_prefix or "sur_merged").strip() or "sur_merged"
        enable_checkpoint = _sur_bool(enable_checkpoint, True)
        auto_resume_checkpoint = _sur_bool(auto_resume_checkpoint, True)
        clear_checkpoint_on_finish = _sur_bool(clear_checkpoint_on_finish, True)
        pre_segment_paths = str(pre_segment_paths or "").strip()
        reference_image_node_id = str(reference_image_node_id or "").strip()
        segment_reference_images = str(segment_reference_images or "").strip()
        audio_mode = str(audio_mode or "keep_original")
        uid          = unique_id

        def log(msg):
            _sur_log(uid, f"[SUR] {msg}")

        extra_info = extra_pnginfo if isinstance(extra_pnginfo, dict) else {}
        full_prompt = extra_info.get("sur_full_prompt") or prompt
        client_id   = str(extra_info.get("sur_client_id") or _sur_current_client_id() or "")
        load_nid = _sur_auto_node_id(full_prompt, load_nid, ("VHS_LoadVideo", "VHS_LoadVideoFFmpeg"), "load_video_node_id", log=log)
        combine_nid = _sur_auto_node_id(full_prompt, combine_nid, "VHS_VideoCombine", "combine_video_node_id", log=log)
        trimmer_nid = _sur_auto_node_id(full_prompt, trimmer_nid, "SegmentFrameTrimmer", "trimmer_node_id", log=log)
        trim_multiplier, trim_note = _infer_trimmer_trim_multiplier(full_prompt, trimmer_nid)
        if trim_override > 0:
            trim_multiplier, trim_note = trim_override, "手动覆盖"

        # ── 预览模式 ──────────────────────────────────────────────
        if not execute:
            plan = _build_plan_text(
                total_frames, segments, start_from,
                overlap, load_nid, trimmer_nid,
                trim_multiplier, trim_note
            )
            _sur_log(uid, "[预览模式]\n" + plan)
            threading.Thread(
                target=lambda: (time.sleep(0.01), _interrupt_current()),
                daemon=True,
            ).start()
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
        ref_images_list = [
            x.strip()
            for x in segment_reference_images.split(",")
            if x.strip()
        ]
        if ref_images_list:
            ref_images_list = _sur_prepare_reference_images(ref_images_list, unique_id=uid)

        def submit_all():
            try:
                log(f"{'═'*20} 开始执行 stamp={run_stamp} {'═'*20}")
                log(f"LoadVideo [{load_nid}]  VideoCombine [{combine_nid}]"
                    + (f"  Trimmer [{trimmer_nid}]" if trimmer_nid else ""))
                log(f"总帧数={total_frames}  共{segments}段  重叠帧={overlap}"
                    f"  执行第{start_from}~{len(seg_list)}段")
                if resume_note:
                    log(resume_note)
                if pre_paths:
                    log(f"前段素材/已完成分段: {len(pre_paths)} 个")
                if ref_images_list:
                    log(f"分段参考图: {len(ref_images_list)} 张")
                if audio_mode != "keep_original":
                    log(f"音频模式: {audio_mode}" + (f"  源: {audio_filename}" if audio_filename else ""))
                if trimmer_nid:
                    log(f"Trimmer 输出裁剪倍率=x{trim_multiplier}  来源: {trim_note}")
                if legacy_widget_shift:
                    log("检测到旧版工作流 widgets_values 顺序，已按旧参数自动兼容")
                log("前端执行状态转发=" + ("开" if client_id else "关（未取得 client_id）"))
                log(
                    "段间清理="
                    + ("开" if cleanup_between_segments else "关")
                    + ("（深度）" if cleanup_between_segments and deep_cleanup_between_segments else "")
                    + ("  模型卸载=开" if cleanup_between_segments and unload_models_between_segments else "")
                    + f"  history清理={'开' if clear_segment_history else '关'}"
                    + f"  图内清理分支裁剪={'开' if prune_cleanup_debug_nodes else '关'}"
                    + f"  自动合并={'开' if merge_segments else '关'}"
                )

                segment_output_paths: list[str] = list(pre_paths)
                _t0 = time.time()
                _all_done = False

                if not segs_to_run:
                    log("没有需要执行的分段；如果开启合并，将尝试使用 checkpoint/pre_segment_paths 中的视频。")

                for run_index, (seg_num, load_skip, load_limit, trim) in enumerate(segs_to_run):
                    if cleanup_between_segments and run_index > 0:
                        log("  下一段启动前轻清理...")
                        _clear_comfy_execution_cache(log=log)

                    output_trim = _output_trim_for_overlap(trim, trim_multiplier)
                    saved_start = load_skip + trim
                    saved_end   = load_skip + load_limit - 1
                    saved_n     = load_limit - trim
                    log(f"── 第{seg_num}/{len(seg_list)}段  "
                        f"skip={load_skip}  limit={load_limit}  "
                        f"trim_in={trim}  trim_out={output_trim}"
                        f"  保存[{saved_start}~{saved_end}] ──")

                    wf = copy.deepcopy(base_prompt)
                    if prune_cleanup_debug_nodes:
                        pruned, blockers = _prune_in_graph_cleanup_branch(wf, cleanup_selectors)
                        if pruned:
                            log("  已移除图内清理/调试分支: " + ", ".join(pruned))
                        elif blockers:
                            log("  图内清理/调试节点连接到非调试节点，保留: " + ", ".join(blockers))

                    # 1. 修改 VHS_LoadVideo / VHS_LoadVideoFFmpeg
                    _sur_set_load_video_segment(
                        wf, load_nid, load_skip, load_limit, frame_rate,
                        base_start_time=base_start_time, log=log
                    )

                    # 2. 修改 SegmentFrameTrimmer（写入 VFI 后的输出裁剪帧数）
                    if trimmer_nid and trimmer_nid in wf:
                        wf[trimmer_nid]["inputs"]["trim_frames"] = output_trim

                    # 3. 修改 VHS_VideoCombine：唯一文件名前缀，避免覆盖
                    seg_prefix  = f"sur_seg{seg_num:02d}_{run_stamp}_"
                    orig_prefix = wf[combine_nid]["inputs"].get("filename_prefix", "")
                    slash       = max(orig_prefix.rfind("/"), orig_prefix.rfind("\\"))
                    subfolder   = orig_prefix[:slash + 1] if slash >= 0 else ""
                    wf[combine_nid]["inputs"]["filename_prefix"] = subfolder + seg_prefix
                    wf[combine_nid]["inputs"]["save_output"]     = True

                    # 4. 可选：按段替换参考图、按段切音频
                    _sur_set_segment_reference_image(
                        wf, reference_image_node_id, ref_images_list, seg_num - 1, log=log
                    )
                    _sur_set_segment_audio(
                        wf, combine_nid, load_nid, seg_num, saved_start, saved_n,
                        frame_rate, audio_mode, audio_filename,
                        base_start_time=base_start_time, log=log
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
                                    "load_video_node_id": load_nid,
                                    "combine_video_node_id": combine_nid,
                                    "trimmer_node_id": trimmer_nid,
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
                        if cleanup_between_segments:
                            time.sleep(0.5)
                            _run_post_segment_clean(
                                log,
                                deep=deep_cleanup_between_segments,
                                unload_models=unload_models_between_segments,
                            )

                    if segment_failed:
                        break

                if merge_segments:
                    if len(segment_output_paths) >= 2:
                        orig_prefix = base_prompt[combine_nid]["inputs"].get("filename_prefix", "") if combine_nid in base_prompt else ""
                        slash = max(orig_prefix.rfind("/"), orig_prefix.rfind("\\"))
                        subfolder = orig_prefix[:slash + 1] if slash >= 0 else ""
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
    "SegmentRunLogViewer":  SegmentRunLogViewer,
    "SegmentDeepRAMCleanNode": SegmentDeepRAMCleanNode,
    "DeepRAMCleanNode": SegmentDeepRAMCleanNode,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "SegmentUpscaleRunner": "Segment Upscale Runner 🎬",
    "SegmentFrameTrimmer":  "Segment Frame Trimmer ✂️",
    "SegmentRunLogViewer":  "Segment Run Log Viewer 📋",
    "SegmentDeepRAMCleanNode": "Segment Deep RAM Cleaner",
    "DeepRAMCleanNode": "Segment Deep RAM Cleaner",
}
