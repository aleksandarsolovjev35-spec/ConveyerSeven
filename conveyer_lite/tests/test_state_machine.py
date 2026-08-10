"""Тесты StateMachine: переходы и колбэки."""

import unittest

from core.state_machine import State, StateMachine


class StateMachineTest(unittest.TestCase):
    def test_initial_state(self):
        sm = StateMachine()
        self.assertEqual(sm.state, State.IDLE)
        self.assertFalse(sm.exit_requested)
        self.assertFalse(sm.is_active)

    def test_start_transitions(self):
        sm = StateMachine()
        self.assertTrue(sm.request_start())
        self.assertEqual(sm.state, State.RUNNING)
        self.assertTrue(sm.is_active)
        self.assertTrue(sm.accepts_new_parts)

    def test_stop_drain_empty(self):
        sm = StateMachine()
        sm.request_start()
        self.assertTrue(sm.request_stop())
        self.assertEqual(sm.state, State.STOPPING)
        self.assertTrue(sm.notify_line_empty())
        self.assertEqual(sm.state, State.STOPPED)

    def test_pause_resume(self):
        sm = StateMachine()
        sm.request_start()
        self.assertTrue(sm.request_pause())
        self.assertEqual(sm.state, State.PAUSED)
        self.assertFalse(sm.accepts_new_parts)
        self.assertTrue(sm.request_resume())
        self.assertEqual(sm.state, State.RUNNING)

    def test_invalid_transition_ignored(self):
        sm = StateMachine()
        self.assertFalse(sm.request_stop())   # IDLE -> STOP недопустим
        self.assertEqual(sm.state, State.IDLE)
        self.assertFalse(sm.request_pause())

    def test_fault_from_running(self):
        sm = StateMachine()
        sm.request_start()
        sm.notify_fault()
        self.assertEqual(sm.state, State.FAULT)
        # Из FAULT нельзя стартовать
        self.assertFalse(sm.request_start())

    def test_exit_requested_during_running(self):
        sm = StateMachine()
        sm.request_start()
        sm.request_exit()
        self.assertTrue(sm.exit_requested)
        self.assertEqual(sm.state, State.STOPPING)

    def test_on_transition_callback(self):
        seen = []
        sm = StateMachine(on_transition=lambda old, new, action: seen.append(
            (old.value, new.value, action),
        ))
        sm.request_start()
        self.assertEqual(seen, [("IDLE", "RUNNING", "START")])

    def test_force_exit(self):
        sm = StateMachine()
        sm.request_start()
        sm.request_force_exit()
        self.assertTrue(sm.force_exit)
        self.assertTrue(sm.exit_requested)


if __name__ == "__main__":
    unittest.main()
