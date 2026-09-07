"""Замер производительности машины на выбранной модели.

Три числа меряются РАЗДЕЛЬНО — смешивать их бессмысленно:
  * время загрузки модели  — разовая стоимость старта;
  * tokens/sec             — скорость генерации, только после прогрева;
  * пиковая RSS            — максимум за процесс, а не снимок в конце.
"""

import json
import statistics
import sys
import time
import ctypes
from ctypes import wintypes
from pathlib import Path

from src.config import load_params
from src.model import generate, load_model, set_seed


def peak_rss_mb() -> float:
    """Пиковая резидентная память процесса.

    ru_maxrss на macOS в байтах, на Linux в килобайтах.
    """
    if sys.platform == "win32":
        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ProcessMemoryCounters),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        success = psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(),
            ctypes.byref(counters),
            counters.cb,
        )
        if not success:
            raise ctypes.WinError(ctypes.get_last_error())
        return counters.PeakWorkingSetSize / (1024 ** 2)

    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 ** 2) if sys.platform == "darwin" else peak / 1024


def main() -> None:
    params = load_params()
    prompt = params["bench"]["prompt"]
    set_seed(params["generate"]["seed"])

    load_started = time.perf_counter()
    tokenizer, model = load_model(params)
    load_time = time.perf_counter() - load_started

    for _ in range(params["bench"]["warmup_runs"]):
        generate(tokenizer, model, params, prompt)

    speeds = []
    for _ in range(params["bench"]["measure_runs"]):
        started = time.perf_counter()
        _, n_tokens = generate(tokenizer, model, params, prompt)
        elapsed = time.perf_counter() - started
        speeds.append(n_tokens / elapsed)

    # Медиана устойчивее среднего к одиночному выбросу.
    report = {
        "model": params["model"]["name"],
        "device": str(model.device),
        "dtype": params["model"]["dtype"],
        "load_time_sec": round(load_time, 2),
        "tokens_per_sec": round(statistics.median(speeds), 2),
        "tokens_per_sec_all": [round(s, 2) for s in speeds],
        "peak_rss_mb": round(peak_rss_mb(), 1),
    }

    Path("docs").mkdir(exist_ok=True)
    Path("docs/bench.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
