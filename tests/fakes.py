"""Фейки транспорта и осей, повторяющие контракт прошивки convey15 v2.4.0.

FakeTransport имитирует ответы контроллера:
- I1 -> "1" в движении, "0" в остановке (плюс возможные сервисные строки);
- I2 -> строка MOV=/WAIT=/POS=/TGT=/lastErr=.

FakeAxis притворяется осью распределителя с записью всех операций.
"""

# I2-строки в формате прошивки convey15.
STATUS_MOVING = "MOV=1 WAIT=0 POS=1234 TGT=38096 lastErr=0"
STATUS_STOPPED = "MOV=0 WAIT=0 POS=0 TGT=0 lastErr=0"


class FakeTransport:
    """Транспорт со скриптами ответов на I1/I2.

    Каждый скрипт читается слева направо; последний элемент повторяется
    бесконечно (устоявшееся состояние контроллера).
    """

    def __init__(self, i1_script=("0",), i2_script=(STATUS_STOPPED,)):
        self.sent = []
        self.queries = []
        self.i1 = list(i1_script)
        self.i2 = list(i2_script)

    def send(self, cmd):
        self.sent.append(cmd)

    def query(self, cmd, delay=0.0):
        self.queries.append(cmd)
        if cmd == "I1":
            script = self.i1
        elif cmd == "I2":
            script = self.i2
        else:
            return ""
        if len(script) > 1:
            return script.pop(0)
        return script[0]


class MovingFakeTransport(FakeTransport):
    """Транспорт, который сам моделирует движение по командам G3/G1."""

    def __init__(self):
        super().__init__()
        self.moving = False

    def send(self, cmd):
        super().send(cmd)
        if cmd == "G3":
            self.moving = True
        elif cmd == "G1":
            self.moving = False

    def query(self, cmd, delay=0.0):
        self.queries.append(cmd)
        if cmd == "I1":
            return "1" if self.moving else "0"
        if cmd == "I2":
            return STATUS_MOVING if self.moving else STATUS_STOPPED
        return ""


class FakeAxis:
    """Ось распределителя с журналом операций в общем потоке ``shared_ops``.

    ``shared_ops`` получает кортежи (имя оси, операция, аргумент), что
    позволяет проверять межосевой порядок команд.
    """

    def __init__(self, transport, name, shared_ops, pos=0, fail_home=False):
        self.transport = transport
        self.name = name
        self._pos = pos
        self.fail_home = fail_home
        self.ops = []
        self._shared = shared_ops

    def _log(self, op, arg=None):
        entry = (self.name, op, arg)
        self.ops.append(entry)
        self._shared.append(entry)

    def home(self):
        self._log("home")

    def wait_stop(self, timeout=10.0, progress_callback=None):
        if self.fail_home:
            raise TimeoutError(f"Axis {self.name} не остановилась")
        if progress_callback is not None:
            progress_callback(self._pos, 0)

    def move_absolute(self, position):
        self._log("move", position)
        self._pos = position

    def verify_homed(self):
        return {"position": 0, "moving": 0, "homed": 1, "limits_enabled": 1}

    @property
    def position(self):
        return self._pos
