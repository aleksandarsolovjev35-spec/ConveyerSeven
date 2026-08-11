"""
Автоматический поиск COM-порта контроллера.

Перебирает доступные порты, отправляет тестовую команду,
проверяет ответ.
"""

import time

import serial
import serial.tools.list_ports

from core.app_logging import get_logger

log = get_logger("hardware.discovery")


# Проверяем именно формат статуса Conveyor, а не любой непустой ответ.
TEST_COMMAND = "I2"

# Время ожидания ответа
TEST_TIMEOUT = 0.5
TEST_DELAY   = 0.3

# Задержка после открытия порта (Arduino reset)
OPEN_DELAY = 2.0


def is_controller_response(response: str) -> bool:
    if not response:
        return False
    required_tokens = ("MOV=", "WAIT=", "lastErr=")
    return all(token in response for token in required_tokens)


def list_available_ports() -> list[dict]:
    """
    Список всех доступных COM-портов.

    Returns:
        [{"port": "COM4", "description": "USB-SERIAL", "hwid": "..."}, ...]
    """
    ports = []
    for info in serial.tools.list_ports.comports():
        ports.append({
            "port":        info.device,
            "description": info.description or "",
            "hwid":        info.hwid or "",
            "manufacturer": info.manufacturer or "",
        })
    return ports


def try_port(
    port: str,
    baudrate: int = 115200,
    command: str = TEST_COMMAND,
) -> tuple[bool, str]:
    """
    Попробовать открыть порт и отправить тестовую команду.

    Returns:
        (success, response_or_error)
    """
    ser = None
    try:
        ser = serial.Serial(
            port,
            baudrate,
            timeout=TEST_TIMEOUT,
            write_timeout=2.0,
        )
        time.sleep(OPEN_DELAY)

        # Очистить буфер
        ser.reset_input_buffer()

        # Отправить тестовую команду
        ser.write(f"{command}\n".encode())
        ser.flush()

        time.sleep(TEST_DELAY)

        # Прочитать ответ
        response = ser.read_all().decode(errors="ignore").strip()

        if not response:
            return False, "no response"
        if not is_controller_response(response):
            return False, f"unexpected controller response: {response[:120]}"
        return True, response

    except serial.SerialException as e:
        return False, f"serial error: {e}"
    except Exception as e:
        return False, f"error: {e}"
    finally:
        if ser:
            try:
                ser.close()
            except Exception as exc:
                log.warning("Ошибка закрытия порта: %s", exc)


def find_controller(
    baudrate: int = 115200,
    preferred_port: str | None = None,
) -> tuple[str | None, str]:
    """
    Найти порт контроллера автоматически.

    Args:
        baudrate: скорость порта.
        preferred_port: предпочтительный порт (проверяется первым).

    Returns:
        (port, message)
        port = "COM4" или None если не найден.
        message = описание результата.
    """
    available = list_available_ports()

    if not available:
        return None, "No COM ports found on this system"

    port_names = [p["port"] for p in available]
    descriptions = [
        f"{p['port']} ({p['description']})"
        for p in available
    ]

    log.info(
        "Найдено портов: %d: %s", len(available), ", ".join(descriptions),
    )

    # Порядок проверки: preferred первым, потом остальные
    check_order = []
    if preferred_port and preferred_port in port_names:
        check_order.append(preferred_port)
    for p in port_names:
        if p not in check_order:
            check_order.append(p)

    # Проверяем каждый порт
    for port in check_order:
        info = next(
            (p for p in available if p["port"] == port), {}
        )
        desc = info.get("description", "")

        log.debug("Пробуем %s (%s)...", port, desc)

        success, response = try_port(port, baudrate)

        if success:
            log.info(
                "Контроллер найден на %s: %r", port, response[:80],
            )
            return port, (
                f"Found on {port} ({desc})"
            )
        else:
            log.debug("  %s: %s", port, response)

    return None, (
        f"Controller not found. "
        f"Checked: {', '.join(check_order)}"
    )
