"""Тесты StateMachine: переходы и колбэки."""

import unittest

from core.state_machine import State, StateMachine


class StateMachineTest(unittest.TestCase):
    def test_initial_state(self):
        sm = StateMachine()
        self.assertEqual(sm.state, State.IDLE)

    def test_start_transitions(self):
        sm = StateMachine()
        sm.request_start()
        self.assertEqual(sm.state, State.RUNNING)
        sm.request_stop()
        self.assertIn(sm.state, (State.IDLE, State.STOPPING))

    def test_pause_resume(self):
        sm = StateMachine()
        sm.request_start()
        self.assertTrue(sm.request_pause())
        self.assertEqual(sm.state, State.PAUSED)
        self.assertTrue(sm.request_resume())
        self.assertEqual(sm.state, State.RUNNING)


if __name__ == "__main__":
    unittest.main()
