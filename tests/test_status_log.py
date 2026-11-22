import sys
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib" / "src"))

from status_log import StatusReporter
import ducking_hooks
import voice_activity


def printed(log):
    return [call.args[0] for call in log.call_args_list]


class StatusReporterTests(unittest.TestCase):
    def test_only_transitions_are_printed(self):
        with mock.patch("builtins.print") as log:
            status = StatusReporter("[X]")
            status.up(first="ready")
            status.up(first="ready")
            self.assertTrue(status.down("lost"))
            self.assertFalse(status.down("lost"))
            self.assertFalse(status.down("lost again"))
            status.up(first="ready", recovered="back")
            status.up(first="ready", recovered="back")
        self.assertEqual(printed(log), ["[X] ready", "[X] lost", "[X] back"])

    def test_first_success_can_be_silent(self):
        with mock.patch("builtins.print") as log:
            status = StatusReporter("[X]")
            status.up(recovered="back")
            self.assertFalse(status.is_down)
        self.assertEqual(printed(log), [])


class VoxdTransitionTests(unittest.TestCase):
    def test_missing_daemon_is_reported_once_across_takes(self):
        def missing(*_args, **_kwargs):
            sock = mock.Mock()
            sock.connect.side_effect = FileNotFoundError("missing daemon")
            return sock

        with mock.patch("voice_activity.socket.socket", side_effect=missing), \
                mock.patch("voice_activity.threading.Thread"), \
                mock.patch("builtins.print") as log:
            activity = voice_activity.VoiceActivity("/test/socket")
            for _ in range(3):
                session = activity.begin()
                session._renew()  # background retry
                session.close()
            activity.close()
        lines = printed(log)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("Input lease unavailable", lines[0])


class DuckingHookTransitionTests(unittest.TestCase):
    def test_failing_hook_is_reported_once_across_takes(self):
        failing = [sys.executable, "-c", "import sys; sys.stdin.read(); sys.exit(255)"]
        with mock.patch("builtins.print") as log:
            hooks = ducking_hooks.DuckingHooks(
                [{"name": "tab", "command": failing, "restore_timeout": 2}]
            )
            for _ in range(3):
                hooks.set_recording(True)
                time.sleep(0.15)
                hooks.set_recording(False)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and not log.call_args_list:
                    time.sleep(0.02)
                time.sleep(0.3)
            hooks.close()
        lines = printed(log)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("exited with status 255", lines[0])


if __name__ == "__main__":
    unittest.main()
