import unittest

from railassist.domain.errors import InvalidTransition
from railassist.domain.models import TaskStatus
from railassist.domain.states import TERMINAL_STATES, is_terminal, require_transition


class StateMachineTests(unittest.TestCase):
    def test_happy_path_from_doc(self):
        require_transition(TaskStatus.READY, TaskStatus.MONITORING)
        require_transition(TaskStatus.MONITORING, TaskStatus.MATCHED)
        require_transition(TaskStatus.MATCHED, TaskStatus.MONITORING)
        require_transition(TaskStatus.READY, TaskStatus.WAITING_SALE)
        require_transition(TaskStatus.WAITING_SALE, TaskStatus.MONITORING)

    def test_pause_resume_stop(self):
        require_transition(TaskStatus.MONITORING, TaskStatus.PAUSED)
        require_transition(TaskStatus.PAUSED, TaskStatus.MONITORING)
        require_transition(TaskStatus.PAUSED, TaskStatus.WAITING_SALE)
        require_transition(TaskStatus.MONITORING, TaskStatus.STOPPED)

    def test_terminal_states_reject_everything(self):
        for status in (TaskStatus.STOPPED, TaskStatus.EXPIRED, TaskStatus.FAILED, TaskStatus.COMPLETED):
            self.assertTrue(is_terminal(status), status)
            for target in TaskStatus:
                if target is status:
                    continue
                with self.assertRaises(InvalidTransition, msg=f"{status} -> {target}"):
                    require_transition(status, target)

    def test_stop_at_cannot_be_stopped_twice(self):
        with self.assertRaises(InvalidTransition):
            require_transition(TaskStatus.STOPPED, TaskStatus.STOPPED)

    def test_terminal_set_matches_transitions(self):
        self.assertEqual(TERMINAL_STATES, {
            TaskStatus.COMPLETED, TaskStatus.STOPPED, TaskStatus.EXPIRED, TaskStatus.FAILED,
        })


if __name__ == "__main__":
    unittest.main()
