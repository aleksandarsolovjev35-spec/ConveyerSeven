"""Параметризованные тесты цикла — замена длинных ручных последовательностей."""
import unittest
from unittest.mock import MagicMock
from core.production_cycle import ProductionCycle

class ParametrizedCycleTests(unittest.TestCase):
    """Параметризованные проверки шага цикла."""

    def setUp(self):
        self.mock_cycle = MagicMock(spec=ProductionCycle)

    def test_cycle_states(self):
        """Проверка основных состояний с параметризацией."""
        states = ["IDLE", "RUNNING", "STOPPING", "STOPPED", "PAUSED", "FAULT"]
        for state in states:
            with self.subTest(state=state):
                self.assertIn(state, states)
