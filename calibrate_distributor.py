from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from pathlib import Path

from config.calibration_loader import load_calibration
from hardware.axis import Axis
from hardware.port_discovery import is_controller_response
from hardware.serial_transport import SerialTransport

ROOT = Path(__file__).resolve().parent


def confirm(expected: str) -> None:
    print()
    print("Физический E-stop должен быть доступен оператору.")
    print("Механизм должен быть свободен от деталей и посторонних предметов.")
    entered = input(f"Введите точно {expected!r}: ").strip()
    if entered != expected:
        raise RuntimeError("Подтверждение не совпало; движение отменено")


def home_axis(axis: Axis, label: str) -> None:
    confirm(f"HOME {label}")
    axis.home()
    axis.wait_stop(timeout=30.0)
    status = axis.verify_homed()
    print(
        f"{label}: HOME подтверждён, POS={status['position']}, "
        f"HOMED={status['homed']}, LIM={status['limits_enabled']}"
    )


def calibrate_endpoint(axis: Axis, label: str, maximum: int) -> int:
    home_axis(axis, label)
    while True:
        raw = input(
            f"{label}: введите целевую позицию 1..{maximum} "
            "или ABORT: "
        ).strip()
        if raw.upper() == "ABORT":
            raise RuntimeError("Калибровка отменена оператором")
        try:
            target = int(raw)
        except ValueError:
            print("Нужно ввести целое число или ABORT")
            continue
        if not 1 <= target <= maximum:
            print(f"Допустимый диапазон: 1..{maximum}")
            continue

        confirm(f"MOVE {label} TO {target}")
        axis.move_absolute(target)
        axis.wait_stop(timeout=20.0)
        actual = axis.position
        if actual != target:
            raise RuntimeError(
                f"{label}: контроллер сообщил POS={actual}, ожидался {target}"
            )
        print(f"{label}: POS={actual}. Осмотрите фактическое положение лопасти.")
        decision = input(
            f"Введите ACCEPT {target}, TRY или ABORT: "
        ).strip()
        if decision == f"ACCEPT {target}":
            home_axis(axis, label)
            return target
        if decision.upper() == "ABORT":
            raise RuntimeError("Калибровка отменена оператором")
        if decision.upper() != "TRY":
            print("Решение не принято; используйте TRY для следующей позиции")


def atomic_write_json(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Guarded calibration of DIST1 OPEN and DIST2 CLEANUP positions"
    )
    parser.add_argument(
        "--port",
        default=os.environ.get("SERIAL_PORT", "COM4"),
    )
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--maximum", type=int, default=2000)
    args = parser.parse_args()
    if not 100 <= args.maximum <= 10000:
        parser.error("--maximum должен быть 100..10000")

    calibration_path = ROOT / "calibration.json"
    calibration = load_calibration(calibration_path)
    confirm(f"CALIBRATE DISTRIBUTOR {args.port}")

    transport = None
    try:
        transport = SerialTransport(args.port, args.baudrate)
        transport.send("G1")
        transport.send("G25")
        status = transport.query("I2", delay=0.2)
        if not is_controller_response(status):
            raise RuntimeError(f"Неожиданный ответ контроллера I2: {status!r}")

        safe_speed = min(int(calibration["axis_speed"]), 100)
        safe_accel = min(int(calibration["axis_accel"]), 50)
        axis0 = Axis(
            transport,
            axis_id=0,
            minimum=0,
            maximum=args.maximum,
            speed=safe_speed,
            accel=safe_accel,
        )
        axis1 = Axis(
            transport,
            axis_id=1,
            minimum=0,
            maximum=args.maximum,
            speed=safe_speed,
            accel=safe_accel,
        )

        print()
        print("Калибровка DIST1: положение полностью открытой лопасти.")
        dist1_open = calibrate_endpoint(axis0, "DIST1", args.maximum)
        print()
        print("Калибровка DIST2: положение маршрута CLEANUP; BAD остаётся HOME=0.")
        dist2_cleanup = calibrate_endpoint(axis1, "DIST2", args.maximum)

        candidate = dict(calibration)
        candidate["dist1_open_position"] = dist1_open
        candidate["dist2_bad_position"] = 0
        candidate["dist2_cleanup_position"] = dist2_cleanup
        candidate_path = ROOT / "calibration.distributor_candidate.json"
        atomic_write_json(candidate_path, candidate)
        print(f"Кандидат записан: {candidate_path}")
        print(
            f"DIST1_OPEN={dist1_open}; DIST2_BAD=0; "
            f"DIST2_CLEANUP={dist2_cleanup}"
        )

        apply_text = input(
            "Для применения в calibration.json введите точно "
            "'APPLY DISTRIBUTOR CALIBRATION', иначе Enter: "
        ).strip()
        if apply_text == "APPLY DISTRIBUTOR CALIBRATION":
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            backup = ROOT / f"calibration.before_distributor_{timestamp}.json"
            shutil.copy2(calibration_path, backup)
            atomic_write_json(calibration_path, candidate)
            print(f"Применено. Резервная копия: {backup}")
        else:
            print("Основной calibration.json не изменён.")

        print("DISTRIBUTOR CALIBRATION COMPLETED")
        return 0
    finally:
        if transport is not None:
            stop_errors = []
            try:
                transport.send("G1")
            except Exception as exc:
                stop_errors.append(f"G1: {exc}")
            try:
                transport.send("G25")
            except Exception as exc:
                stop_errors.append(f"G25: {exc}")
            transport.close()
            if stop_errors:
                print("ВНИМАНИЕ: " + "; ".join(stop_errors))
            print("Команды остановки отправлены; COM закрыт")


if __name__ == "__main__":
    raise SystemExit(main())
