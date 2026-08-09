"""Terminating inference worker primitive.

A timed-out model call is terminated rather than left as a thread that can
later mutate a production transaction.  The helper is dependency-free and is
also useful in HIL/emulator tests.
"""

from __future__ import annotations

import multiprocessing as mp
import queue
import traceback


class WorkerTimeout(TimeoutError):
    pass


class WorkerCrashed(RuntimeError):
    pass


def _entry(result_queue, function, args, kwargs):
    try:
        result_queue.put((True, function(*args, **kwargs)))
    except BaseException as exc:  # return a serialisable diagnostic
        result_queue.put((False, {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()}))


def run_in_terminating_worker(function, *args, timeout: float = 5.0, context=None, **kwargs):
    """Run one call and terminate its process on timeout/crash."""
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    ctx = context or mp.get_context("fork" if "fork" in mp.get_all_start_methods() else "spawn")
    result_queue = ctx.Queue(maxsize=1)
    process = ctx.Process(target=_entry, args=(result_queue, function, args, kwargs), daemon=True)
    process.start()
    process.join(timeout)
    if process.is_alive():
        process.terminate()
        process.join(1.0)
        raise WorkerTimeout(f"inference worker exceeded {timeout}s")
    try:
        ok, value = result_queue.get_nowait()
    except queue.Empty as exc:
        raise WorkerCrashed(f"inference worker exited with code {process.exitcode}") from exc
    if not ok:
        raise WorkerCrashed(
            f"inference worker failed: {value.get('type')}: {value.get('message')}"
        )
    return value
