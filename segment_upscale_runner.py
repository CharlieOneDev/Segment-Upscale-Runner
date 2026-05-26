"""
ComfyUI 分段放大队列节点 (Segment Upscale Runner)
- 专为高清放大场景设计
- 每段独立保存为单独视频
- 支持断点续跑
- 支持重叠帧（Overlap Frames）：用前一段尾部 N 帧作为下一段头部的时序上下文，
  保存时由 SegmentFrameTrimmer 节点自动裁掉，合并后无重叠、无跳帧
- 适合 CNB 容器环境
"""

import copy, gc, hashlib, json, time, os, shutil, subprocess, tempfile, threading, urllib.request, urllib.error
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


def _sur_find_audio_filename(prompt: dict, load_nid: str) -> str | None:
    node = (prompt or {}).get(str(load_nid), {})
    video = _node_inputs(node).get("video", "")
    if video and isinstance(video, str):
        return video
    return None


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
    start_time = max(0.0, saved_start / frame_rate) if frame_rate > 0 else 0.0
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
                f.write("file " + repr(p) + "\n")
        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", output_path]
        result = subprocess.run(cmd, capture_output=True, text=True)
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


def _clear_cache_obj(cache, lines: list[str], label: str, depth: int = 0) -> int:
    if cache is None or depth > 8:
        return 0
    removed = 0
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


def _clear_comfy_execution_cache(log=None):
    lines: list[str] = []
    cleared_entries = 0
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
        log(f"  Comfy cache 轻清理完成: {cleared_entries} 个 entry")
    return cleared_entries


def _load_malloc_trim_module():
    try:
        import malloc_trim_node
        return malloc_trim_node
    except Exception:
        pass

    try:
        import importlib.util
        path = os.path.join(folder_paths.base_path, "custom_nodes", "malloc_trim_node.py")
        spec = importlib.util.spec_from_file_location("malloc_trim_node", path)
        if spec and spec.loader:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    except Exception:
        return None
    return None


def _run_post_segment_clean(log, deep: bool = True):
    _clear_comfy_execution_cache(log=log)
    if not deep:
        return
    module = _load_malloc_trim_module()
    if module is None or not hasattr(module, "DeepRAMCleanNode"):
        log("  深度段后清理: 未找到 malloc_trim_node.DeepRAMCleanNode，已完成轻清理")
        return
    try:
        cleaner = module.DeepRAMCleanNode()
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
            }
        }

    def trim(self, images, trim_frames: int):
        if images is None:
            raise ValueError(
                "[SegmentFrameTrimmer] images 为 None，"
                "请将本节点的 images 输入直接连接到上游放大/插帧节点，"
                "不要经过类型为 * 的透传节点中转。"
            )
        n           = images.shape[0]
        trim_frames = max(0, min(int(trim_frames), n - 1))
        if trim_frames > 0:
            return (images[trim_frames:],)
        return (images,)

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
                        "tooltip": "每段完成后清理 ComfyUI 执行缓存、CUDA 缓存和 Python GC，复刻 Queue Runner 的段间释放节奏。",
                    },
                ),
                "deep_cleanup_between_segments": (
                    "BOOLEAN",
                    {
                        "default": True,
                        "tooltip": "启用时会调用 custom_nodes/malloc_trim_node.py 的 DeepRAMCleanNode 做更彻底的段后清理。",
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
                        "default": "DeepRAMCleanNode,VRAM_Debug,easy showAnything",
                        "tooltip": (
                            "要从子 prompt 移除的节点 ID 或 class_type，逗号分隔。\n"
                            "下游 show/debug/preview 节点会一起移除。"
                        ),
                    },
                ),
                "merge_segments": (
                    "BOOLEAN",
                    {
                        "default": False,
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
        cleanup_node_selectors="DeepRAMCleanNode,VRAM_Debug,easy showAnything",
        merge_segments=False,
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
        cleanup_between_segments = bool(cleanup_between_segments)
        deep_cleanup_between_segments = bool(deep_cleanup_between_segments)
        prune_cleanup_debug_nodes = bool(prune_cleanup_debug_nodes)
        merge_segments = bool(merge_segments)
        cleanup_selectors = _parse_selectors(cleanup_node_selectors)
        merged_filename_prefix = (merged_filename_prefix or "sur_merged").strip() or "sur_merged"
        enable_checkpoint = bool(enable_checkpoint)
        auto_resume_checkpoint = bool(auto_resume_checkpoint)
        clear_checkpoint_on_finish = bool(clear_checkpoint_on_finish)
        pre_segment_paths = str(pre_segment_paths or "").strip()
        reference_image_node_id = str(reference_image_node_id or "").strip()
        segment_reference_images = str(segment_reference_images or "").strip()
        audio_mode = str(audio_mode or "keep_original")
        uid          = unique_id

        def log(msg):
            _sur_log(uid, f"[SUR] {msg}")

        full_prompt = (extra_pnginfo or {}).get("sqr_full_prompt") or prompt
        client_id   = str((extra_pnginfo or {}).get("sqr_client_id") or "")
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
                return {}

        nid_checks = [(load_nid, "VHS_LoadVideo"), (combine_nid, "VHS_VideoCombine")]
        if trimmer_nid:
            nid_checks.append((trimmer_nid, "SegmentFrameTrimmer"))
        for nid, label in nid_checks:
            if nid not in (full_prompt or {}):
                log(f"✗ 找不到 {label} 节点 ID「{nid}」")
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
                log(
                    "段间清理="
                    + ("开" if cleanup_between_segments else "关")
                    + ("（深度）" if cleanup_between_segments and deep_cleanup_between_segments else "")
                    + f"  图内清理分支裁剪={'开' if prune_cleanup_debug_nodes else '关'}"
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

                    # 1. 修改 VHS_LoadVideo
                    wf[load_nid]["inputs"]["skip_first_frames"] = load_skip
                    wf[load_nid]["inputs"]["frame_load_cap"]    = load_limit

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
                        frame_rate, audio_mode, audio_filename, log=log
                    )

                    # 5. 删除本节点自身，避免递归触发
                    if uid and str(uid) in wf:
                        del wf[str(uid)]

                    # 6. 提交、等待、段后清理
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
                        else:
                            log(f"✗ 第{seg_num}段执行出错，已跳过")
                        if cleanup_between_segments:
                            time.sleep(0.5)
                            _run_post_segment_clean(log, deep=deep_cleanup_between_segments)
                    except Exception as e:
                        log(f"✗ 第{seg_num}段提交失败: {type(e).__name__}: {e}")

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
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "SegmentUpscaleRunner": "Segment Upscale Runner 🎬",
    "SegmentFrameTrimmer":  "Segment Frame Trimmer ✂️",
}
