"""JOG-режим — ручное управление лентой."""
import threading
import time

class JogMode:
    """Управление ручным движением (JOG) и коррекцией ленты."""

    JOG_ALLOWED_STATES = ("IDLE", "STOPPED")
    NUDGE_ALLOWED_STATES = ("PAUSED",)

    def __init__(self, jog):
        self.jog = jog
        self._jog_lock = threading.Lock()
        self.jog_active = False
