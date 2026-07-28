"""Ручная утилита: поиск команды контроллера, отражающей движение ленты.

Скрипт опрашивает список кандидатов, просит оператора повернуть ленту руками
и повторяет опрос. Команды, ответ которых изменился, читают энкодер.
"""

import argparse
import time

import serial

DEFAULT_PORT = "COM4"
DEFAULT_BAUD = 115200
QUERY_DELAY = 0.3
PORT_SETTLE_DELAY = 2.0

COMMANDS = (
    "I0", "I1", "I2", "I3", "I4", "I5",
    "I10", "I11", "I12", "I13", "I14", "I15",
    "I20", "I21", "I22",
    "I30", "I31",
    "M114", "M115", "M119",
    "?", "STATUS", "POS", "ENC",
    "GET_POS", "GET_ENC", "ENCODER",
)


def query(ser: serial.Serial, command: str, delay: float = QUERY_DELAY) -> str:
    ser.reset_input_buffer()
    ser.write(f"{command}\n".encode())
    ser.flush()
    time.sleep(delay)
    return ser.read_all().decode(errors="ignore").strip()


def collect_responses(ser: serial.Serial) -> dict:
    return {command: query(ser, command) for command in COMMANDS}


def find_changed(ser: serial.Serial, baseline: dict) -> list:
    changed = []
    for command, old_response in baseline.items():
        if not old_response:
            continue
        new_response = query(ser, command)
        if new_response != old_response:
            changed.append((command, old_response, new_response))
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default=DEFAULT_PORT)
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    args = parser.parse_args()

    with serial.Serial(args.port, args.baud, timeout=0.5) as ser:
        time.sleep(PORT_SETTLE_DELAY)

        print("=" * 60)
        print("Опрос всех возможных команд")
        print("=" * 60)

        baseline = collect_responses(ser)
        for command, response in baseline.items():
            marker = "+" if response else " "
            print(f"[{marker}] [{command:10s}] -> {response!r}")

        print()
        print("=" * 60)
        print("Тест на изменение: вручную прокрутите ленту")
        print("=" * 60)
        input("Нажмите Enter после прокрутки ленты рукой на 5-10 см...")

        changed = find_changed(ser, baseline)

    if not changed:
        print("\nНи одна команда не изменила ответ.")
        print("Энкодер не подключён к прошивке; нужна доработка прошивки.")
        return 1

    print("\nКоманды, которые видят движение:")
    for command, old_response, new_response in changed:
        print(f"  [{command}]")
        print(f"     до:    {old_response!r}")
        print(f"     после: {new_response!r}")
    print("\nЭнкодер читается: эти команды можно использовать.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
