# tools/encoder_discovery.py

import serial
import time

PORT = "COM4"
BAUD = 115200

ser = serial.Serial(PORT, BAUD, timeout=0.5)
time.sleep(2)

def query(cmd, delay=0.3):
    ser.reset_input_buffer()
    ser.write(f"{cmd}\n".encode())
    ser.flush()
    time.sleep(delay)
    return ser.read_all().decode(errors="ignore").strip()

print("=" * 60)
print("Опрос всех возможных команд")
print("=" * 60)

commands = [
    "I0", "I1", "I2", "I3", "I4", "I5",
    "I10", "I11", "I12", "I13", "I14", "I15",
    "I20", "I21", "I22",
    "I30", "I31",
    "M114", "M115", "M119",
    "?", "STATUS", "POS", "ENC",
    "GET_POS", "GET_ENC", "ENCODER",
]

results = {}
for cmd in commands:
    resp = query(cmd)
    results[cmd] = resp
    marker = "✓" if resp else " "
    print(f"[{marker}] [{cmd:10s}] → {resp!r}")

print()
print("=" * 60)
print("Теперь тест на изменение — вручную покрути ленту")
print("=" * 60)

input("Нажми Enter когда прокрутишь ленту рукой на 5-10 см...")

changed = []
for cmd in commands:
    if not results[cmd]:
        continue
    new_resp = query(cmd)
    if new_resp != results[cmd]:
        changed.append((cmd, results[cmd], new_resp))

if changed:
    print("\n✅ Найдены команды которые ВИДЯТ движение:")
    for cmd, old, new in changed:
        print(f"  [{cmd}]")
        print(f"     до:    {old!r}")
        print(f"     после: {new!r}")
    print("\n🎉 ЭНКОДЕР УЖЕ ЧИТАЕТСЯ! Осталось только использовать это.")
else:
    print("\n❌ Ни одна команда не изменила ответ.")
    print("   Энкодер не подключён к прошивке.")
    print("   Нужна доработка прошивки.")

ser.close()