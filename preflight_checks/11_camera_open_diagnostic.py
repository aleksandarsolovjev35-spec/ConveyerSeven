"""Диагностика открытия камер: почему конкретная роль не стартует.

Скрипт отвечает на один вопрос: камера действительно неисправна или ей
просто не хватает полосы USB / не подходит backend. Для этого он
проверяет каждую камеру тремя способами:

1. по одной, изолированно — так камера получает всю полосу шины;
2. под каждым доступным backend отдельно;
3. все семь одновременно — так же, как это делает production-запуск.

Если камера проходит проверку 1, но набор из семи падает на проверке 3,
проблема в пропускной способности USB-контроллера, а не в камере.

Движение не выполняется, COM-порт не открывается, модели не грузятся.
"""

from __future__ import annotations

import time

import numpy as np

from common import ROOT, print_header
from config.camera_mapping import load_camera_mapping
from vision.camera_manager import (
    CameraManager,
    _backend_label,
    _default_backends,
)


def _describe(capture) -> str:
    return CameraManager._negotiated_format(capture)


def _probe_single(camera_id: int, backend) -> tuple[bool, str, str]:
    """Открыть одну камеру под конкретным backend и дождаться кадра."""
    capture = None
    try:
        capture = CameraManager._open_capture(camera_id, backend)
        if capture is None or not capture.isOpened():
            return False, "устройство не открылось", "-"
        CameraManager._configure_capture(capture)
        negotiated = _describe(capture)
        error = CameraManager._wait_for_stable_preflight(capture)
        if error is not None:
            return False, error, negotiated
        return True, "OK", negotiated
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}", "-"
    finally:
        if capture is not None:
            try:
                capture.release()
            except Exception as exc:
                print(f"  release error: {exc}")


def main() -> int:
    print_header("11 — CAMERA OPEN DIAGNOSTIC (NO MODELS, NO COM, NO MOTION)")
    mapping = load_camera_mapping(ROOT / "camera_mapping.json")
    backends = _default_backends() or (None,)
    print(f"backends: {', '.join(_backend_label(b) for b in backends)}")

    print()
    print("--- 1. Каждая камера отдельно (полная полоса USB) ---")
    isolated_ok: dict[str, list[str]] = {}
    for role, camera_id in mapping.items():
        working = []
        for backend in backends:
            label = _backend_label(backend)
            started = time.monotonic()
            ok, message, negotiated = _probe_single(camera_id, backend)
            elapsed = time.monotonic() - started
            status = "OK  " if ok else "FAIL"
            print(
                f"{status} {role:<13} id={camera_id} [{label:<5}] "
                f"{elapsed:5.1f}s {negotiated:<22} {message}"
            )
            if ok:
                working.append(label)
            # Драйверу нужно время отпустить устройство.
            time.sleep(0.3)
        isolated_ok[role] = working

    print()
    print("--- 2. Все семь одновременно (как в production) ---")
    manager = None
    combined_error = None
    try:
        manager = CameraManager(config_file=ROOT / "camera_mapping.json")
        frames = manager.capture_all()
        for role, frame in frames.items():
            array = np.asarray(frame)
            height, width = array.shape[:2]
            mean = float(array[:, :, :3].mean())
            print(f"OK   {role:<13} {width}x{height} mean={mean:.2f}")
    except Exception as exc:
        combined_error = f"{type(exc).__name__}: {exc}"
        print(f"FAIL набор из семи камер: {combined_error}")
    finally:
        if manager is not None:
            manager.release()

    print()
    print("--- ИТОГ ---")
    dead = [role for role, working in isolated_ok.items() if not working]
    if dead:
        print(f"Не отвечают даже поодиночке: {', '.join(sorted(dead))}")
        print("Причина на стороне камеры/кабеля/драйвера:")
        print("  - переподключить камеру и проверить кабель;")
        print("  - убедиться, что камеру не держит другая программа;")
        print("  - проверить камеру в 'Камера' Windows на 1280x720.")
    elif combined_error:
        print("Каждая камера исправна поодиночке, но набор из семи падает.")
        print("Это нехватка пропускной способности USB, а не поломка:")
        print("  - развести камеры по разным USB-контроллерам,")
        print("    не через один хаб;")
        print("  - использовать порты USB 3.0 (разные корневые хабы);")
        print("  - при необходимости увеличить паузы старта:")
        print("      set CAMERA_OPEN_CONCURRENCY=1")
        print("      set CAMERA_PREFLIGHT_TIMEOUT=8")
        print("      set CAMERA_OPEN_ATTEMPTS=3")
    else:
        print("Все семь камер открылись и поодиночке, и вместе.")

    for role, working in sorted(isolated_ok.items()):
        if working and len(working) < len(backends):
            print(
                f"Примечание: {role} работает только под "
                f"{', '.join(working)}."
            )

    print()
    print("CAMERA OPEN DIAGNOSTIC COMPLETED")
    # Скрипт диагностический: он сообщает состояние, а не блокирует запуск.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
