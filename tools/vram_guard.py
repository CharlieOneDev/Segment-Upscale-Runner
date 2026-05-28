#!/usr/bin/env python3
"""
vram_guard.py - ComfyUI shared-GPU VRAM reservation guard

Purpose:
  In a shared GPU environment, reserve most idle VRAM so other processes
  cannot grab it while ComfyUI is idle. As soon as ComfyUI starts running a prompt,
  release the reservation immediately.

Key design:
  - The parent process does NOT import torch and does NOT create a CUDA context.
  - A worker subprocess imports torch and owns the placeholder tensors.
  - Releasing means terminating the worker subprocess, which releases both
    placeholder tensors and the worker CUDA context.
  - Busy/idle is detected from ComfyUI's /queue endpoint, not by guessing which
    python process is ComfyUI.

Usage:
  python3 tools/vram_guard.py --comfy-url http://127.0.0.1:8188 --buffer 2048
  python3 tools/vram_guard.py --worker-python /path/to/comfyui/venv/bin/python \
      --comfy-url http://127.0.0.1:8188 --buffer 2048 --interval 0.05 \
      --reserve-interval 0.2 --idle-seconds 0 --release-on-queue-error
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ComfyUI shared-GPU VRAM guard")
    parser.add_argument("--device", type=int, default=0, help="GPU index, default 0")
    parser.add_argument("--comfy-url", default="http://127.0.0.1:8188", help="ComfyUI base URL")
    parser.add_argument("--buffer", type=int, default=1024, help="Free VRAM to leave while guarding, MB")
    parser.add_argument("--chunk-mb", type=int, default=512, help="Worker allocation chunk size, MB")
    parser.add_argument("--min-chunk-mb", type=int, default=64, help="Minimum allocation chunk size, MB")
    parser.add_argument("--interval", type=float, default=0.10, help="Queue polling interval in seconds")
    parser.add_argument("--queue-timeout", type=float, default=0.25, help="/queue request timeout in seconds")
    parser.add_argument("--idle-seconds", type=float, default=5.0, help="Idle seconds before reserving VRAM again")
    parser.add_argument("--status-interval", type=float, default=2.0, help="Status print interval in seconds")
    parser.add_argument("--reserve-interval", type=float, default=0.5, help="Worker top-up interval while guarding")
    parser.add_argument("--restart-backoff", type=float, default=10.0, help="Seconds to wait before restarting a failed worker")
    parser.add_argument("--worker-python", default=None, help="Python executable with torch installed; defaults to current Python")
    parser.add_argument("--pid", type=int, default=None, help="Optional ComfyUI PID for status only")
    parser.add_argument("--release-on-queue-error", action="store_true", help="Release reservation if /queue errors")
    parser.add_argument("--touch", action="store_true", help="Worker fills tensors once after allocation")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args()


def fmt_mb(mb: int | float | None) -> str:
    if mb is None:
        return "?"
    mb = int(mb)
    if mb >= 1024:
        return f"{mb / 1024:.1f}GB"
    return f"{mb}MB"


def run_cmd(args: list[str], timeout: float = 2.0) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return result.stdout.strip()


def get_gpu_memory_mb(device: int) -> tuple[int | None, int | None]:
    try:
        out = run_cmd(
            [
                "nvidia-smi",
                f"--id={device}",
                "--query-gpu=memory.free,memory.total",
                "--format=csv,noheader,nounits",
            ],
            timeout=2.0,
        )
        if not out:
            return None, None
        first = out.splitlines()[0]
        free_s, total_s = [p.strip() for p in first.split(",")[:2]]
        return int(free_s), int(total_s)
    except Exception:
        return None, None


def get_gpu_processes() -> list[tuple[int, str, int]]:
    try:
        out = run_cmd(
            [
                "nvidia-smi",
                "--query-compute-apps=pid,process_name,used_gpu_memory",
                "--format=csv,noheader,nounits",
            ],
            timeout=2.0,
        )
    except Exception:
        return []

    rows: list[tuple[int, str, int]] = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 3:
            continue
        try:
            rows.append((int(parts[0]), parts[1], int(parts[2])))
        except ValueError:
            continue
    return rows


def get_proc_cmdline(pid: int) -> str:
    path = f"/proc/{pid}/cmdline"
    try:
        with open(path, "rb") as f:
            return f.read().replace(b"\x00", b" ").decode("utf-8", "ignore").strip()
    except Exception:
        return ""


def detect_comfy_pid(worker_pid: int | None = None) -> int | None:
    self_pid = os.getpid()
    candidates: list[tuple[int, int]] = []
    fallback: list[tuple[int, int]] = []
    for pid, name, mem in get_gpu_processes():
        if pid in {self_pid, worker_pid}:
            continue
        low_name = name.lower()
        if "python" not in low_name:
            continue
        cmd = get_proc_cmdline(pid)
        low_cmd = cmd.lower()
        if "--worker" in low_cmd and "vram_guard.py" in low_cmd:
            continue
        if "comfyui" in low_cmd and "main.py" in low_cmd:
            candidates.append((mem, pid))
        else:
            fallback.append((mem, pid))

    if candidates:
        return max(candidates)[1]
    if fallback:
        return max(fallback)[1]
    return None


def get_pid_vram_mb(pid: int | None) -> int | None:
    if pid is None:
        return None
    for row_pid, _name, mem in get_gpu_processes():
        if row_pid == pid:
            return mem
    return None


def query_comfy_queue(base_url: str, timeout: float) -> tuple[str, str]:
    url = base_url.rstrip("/") + "/queue"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        running = data.get("queue_running") or []
        pending = data.get("queue_pending") or []
        if running:
            return "busy", f"running={len(running)} pending={len(pending)}"
        if pending:
            return "busy", f"running=0 pending={len(pending)}"
        return "idle", "queue empty"
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        return "unknown", f"{type(exc).__name__}: {exc}"


def worker_python(args: argparse.Namespace) -> str:
    return args.worker_python or sys.executable


def check_worker_python(args: argparse.Namespace) -> tuple[bool, str]:
    exe = worker_python(args)
    try:
        result = subprocess.run(
            [exe, "-c", "import sys, torch; print(sys.executable); print(torch.__version__)"],
            capture_output=True,
            text=True,
            timeout=10.0,
        )
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"

    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    if result.returncode != 0:
        return False, err or out or f"exit code {result.returncode}"
    return True, out.replace("\n", " | ")


def terminate_worker(worker: subprocess.Popen | None, reason: str, timeout: float = 3.0) -> subprocess.Popen | None:
    if worker is None:
        return None
    if worker.poll() is not None:
        return None

    print(f"\n[release] {reason} -> stop VRAM worker pid={worker.pid}", flush=True)
    try:
        worker.terminate()
        worker.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        print("[release] worker did not exit in time; killing", flush=True)
        worker.kill()
        worker.wait(timeout=timeout)
    return None


def start_worker(args: argparse.Namespace) -> subprocess.Popen:
    cmd = [
        worker_python(args),
        os.path.abspath(__file__),
        "--worker",
        "--device",
        str(args.device),
        "--buffer",
        str(args.buffer),
        "--chunk-mb",
        str(args.chunk_mb),
        "--min-chunk-mb",
        str(args.min_chunk_mb),
        "--reserve-interval",
        str(args.reserve_interval),
    ]
    if args.touch:
        cmd.append("--touch")

    env = os.environ.copy()
    env["VRAM_GUARD_WORKER"] = "1"
    worker = subprocess.Popen(cmd, env=env)
    print(f"\n[reserve] start VRAM worker pid={worker.pid}; target free={fmt_mb(args.buffer)}", flush=True)
    return worker


def worker_main(args: argparse.Namespace) -> None:
    import gc
    import torch

    device = f"cuda:{args.device}"
    stop = False

    def handle_stop(_signum, _frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)

    if not torch.cuda.is_available():
        print("[vram_guard worker] CUDA unavailable", flush=True)
        sys.exit(1)

    chunks = []

    def free_mb() -> int:
        free_bytes, _total_bytes = torch.cuda.mem_get_info(args.device)
        return free_bytes // (1024 * 1024)

    def allocate_chunk(size_mb: int):
        return torch.empty(size_mb * 1024 * 1024, dtype=torch.uint8, device=device)

    def reserve_until_buffer() -> int:
        grabbed_now = 0
        current_chunk = max(args.min_chunk_mb, args.chunk_mb)
        while not stop:
            free_now = free_mb()
            need_mb = free_now - args.buffer
            if need_mb < args.min_chunk_mb:
                break
            size_mb = max(args.min_chunk_mb, min(current_chunk, need_mb))
            try:
                tensor = allocate_chunk(size_mb)
                if args.touch:
                    tensor.fill_(0)
                chunks.append(tensor)
                grabbed_now += size_mb
            except RuntimeError:
                torch.cuda.empty_cache()
                if current_chunk <= args.min_chunk_mb:
                    break
                current_chunk = max(args.min_chunk_mb, current_chunk // 2)
        return grabbed_now

    grabbed = reserve_until_buffer()
    total_grabbed = sum(t.nbytes for t in chunks) // (1024 * 1024)
    print(f"[vram_guard worker] grabbed={fmt_mb(grabbed)} total={fmt_mb(total_grabbed)} free={fmt_mb(free_mb())}", flush=True)

    try:
        while not stop:
            time.sleep(max(0.05, args.reserve_interval))
            grabbed = reserve_until_buffer()
            if grabbed > 0:
                total_grabbed = sum(t.nbytes for t in chunks) // (1024 * 1024)
                print(
                    f"[vram_guard worker] topup={fmt_mb(grabbed)} total={fmt_mb(total_grabbed)} free={fmt_mb(free_mb())}",
                    flush=True,
                )
    finally:
        chunks.clear()
        gc.collect()
        torch.cuda.empty_cache()
        try:
            torch.cuda.ipc_collect()
        except Exception:
            pass
        try:
            torch.cuda.synchronize(args.device)
        except Exception:
            pass
        print("[vram_guard worker] released", flush=True)


def parent_main(args: argparse.Namespace) -> None:
    free_mb, total_mb = get_gpu_memory_mb(args.device)
    print(f"[gpu] GPU {args.device}: free={fmt_mb(free_mb)} total={fmt_mb(total_mb)}")
    print(f"[queue] ComfyUI queue: {args.comfy_url.rstrip('/')}/queue")
    print(f"[config] buffer={fmt_mb(args.buffer)} interval={args.interval}s idle={args.idle_seconds}s")
    ok, note = check_worker_python(args)
    if not ok:
        print(f"[error] worker Python cannot import torch: {worker_python(args)}")
        print(f"[error] {note}")
        print("[hint] Run guard with the same Python used by ComfyUI, or pass --worker-python /path/to/venv/bin/python")
        return
    print(f"[worker-python] {note}")

    worker: subprocess.Popen | None = None
    comfy_pid = args.pid
    if comfy_pid is None:
        comfy_pid = detect_comfy_pid()
        if comfy_pid:
            print(f"[status] detected ComfyUI-like pid for status: {comfy_pid}")
        else:
            print("[status] no ComfyUI GPU pid detected yet; queue endpoint will drive release/reacquire")
    else:
        print(f"[status] pid: {comfy_pid}")

    last_busy_ts = time.monotonic()
    last_status_ts = 0.0
    next_reserve_ts = 0.0
    last_queue_state = "unknown"
    last_queue_note = ""

    print("[ready] guard started. Ctrl+C to stop.\n")

    try:
        while True:
            now = time.monotonic()
            queue_state, queue_note = query_comfy_queue(args.comfy_url, args.queue_timeout)
            last_queue_state, last_queue_note = queue_state, queue_note

            if queue_state == "busy":
                last_busy_ts = now
                worker = terminate_worker(worker, f"ComfyUI busy ({queue_note})")
            elif queue_state == "unknown" and args.release_on_queue_error:
                last_busy_ts = now
                worker = terminate_worker(worker, f"queue unknown ({queue_note})")
            elif queue_state == "idle":
                if worker is not None and worker.poll() is not None:
                    print(
                        f"\n[worker-exit] pid={worker.pid} rc={worker.returncode}; "
                        f"retry in {args.restart_backoff}s",
                        flush=True,
                    )
                    worker = None
                    next_reserve_ts = now + args.restart_backoff
                if worker is None and now >= next_reserve_ts and (now - last_busy_ts) >= args.idle_seconds:
                    worker = start_worker(args)

            if now - last_status_ts >= args.status_interval:
                if comfy_pid is None or get_proc_cmdline(comfy_pid) == "":
                    comfy_pid = detect_comfy_pid(worker.pid if worker else None)
                comfy_mb = get_pid_vram_mb(comfy_pid)
                free_now, _total_now = get_gpu_memory_mb(args.device)
                worker_alive = worker is not None and worker.poll() is None
                worker_mem = get_pid_vram_mb(worker.pid) if worker_alive else None
                state_label = "reserve" if worker_alive else ("release" if queue_state != "idle" else "idle")
                print(
                    f"[{state_label:<7}] queue={last_queue_state:<7} free={fmt_mb(free_now):>7} "
                    f"comfy(pid={comfy_pid or '-'}):{fmt_mb(comfy_mb):>7} "
                    f"guard(pid={(worker.pid if worker_alive else '-')}):{fmt_mb(worker_mem):>7} "
                    f"{last_queue_note[:48]:<48}",
                    flush=True,
                )
                last_status_ts = now

            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\n\n[stop] stopping guard...")
    finally:
        terminate_worker(worker, "exit")
        print("[done] guard stopped")


def main() -> None:
    args = parse_args()
    if args.worker:
        worker_main(args)
    else:
        parent_main(args)


if __name__ == "__main__":
    main()

