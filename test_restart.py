"""Guard against stopping NeuroScope while its device or sessions are in use."""
from pathlib import Path
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

if sys.platform == "linux":
    import restart


@unittest.skipUnless(sys.platform == "linux" and hasattr(signal, "pidfd_send_signal"),
                     "Linux pidfd support is required")
class TestRestart(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.pidfile = self.directory / "server.pid"
        self.pidfile.write_text("123", encoding="ascii")
        self.url = "http://127.0.0.1:8000"
        self.status = {
            "/api/health": {"status": "ok", "device_ready": False, "app_status": "idle",
                            "recording": None, "replay": {"active": False}},
            "/api/device/status": {"status": "idle", "connected": False, "scanning": False,
                                   "calibration_phase": "unavailable", "auto_reconnect": False,
                                   "recording": None},
            "/api/experiments/status": {"state": "idle"},
            "/api/experiment-lab/status": {"active_run_id": None},
        }
        self.command = ["python3", str(restart.ROOT / "run.py"), "--host", "127.0.0.1", "--port", "8000"]
        self.env = {"CAPSULE_SDK_DIR": "/sdk"}
        self.lifecycle = Mock()
        self.enterContext(patch.multiple(
            restart, read_status=lambda url, endpoint: self.status[endpoint],
            listener_url=Mock(return_value=self.url),
            launcher_config=Mock(return_value=(self.command, self.directory, self.env, 8000)),
            check_replacement=self.lifecycle.check_replacement,
            wait_for_start=self.lifecycle.wait_for_start,
        ))
        self.enterContext(patch.object(restart.os, "pidfd_open", return_value=20))
        self.enterContext(patch.object(restart.os, "close"))
        self.enterContext(patch.object(restart.signal, "pidfd_send_signal", self.lifecycle.send))
        self.enterContext(patch.object(restart.select, "poll", return_value=self.lifecycle.poller))
        self.enterContext(patch.object(restart.subprocess, "Popen", self.lifecycle.launch))

    def test_busy_server_is_left_running(self):
        scenarios = [
            ("/api/health", "recording", "session-123"),
            ("/api/device/status", "connected", True),
            ("/api/device/status", "calibration_phase", "running"),
            ("/api/device/status", "status", "connecting"),
            ("/api/device/status", "auto_reconnect", True),
            ("/api/health", "replay", {"active": True}),
            ("/api/experiment-lab/status", "active_run_id", "active-123"),
        ]
        for endpoint, field, value in scenarios:
            with self.subTest(field=field), patch.dict(self.status[endpoint], {field: value}):
                self.assertEqual(restart.restart(self.pidfile, wait=0, check=False), 2)
        self.assertEqual(self.lifecycle.mock_calls, [])

    def test_state_rechecked_after_preparing_replacement(self):
        def new_connection(*args):
            self.status["/api/device/status"]["connected"] = True

        self.lifecycle.check_replacement.side_effect = new_connection
        self.assertEqual(restart.restart(self.pidfile, wait=0, check=False), 2)
        self.lifecycle.send.assert_not_called()
        self.lifecycle.launch.assert_not_called()

    def test_waits_for_graceful_exit_before_launching_replacement(self):
        self.lifecycle.poller.poll.return_value = [(20, 1)]
        self.assertEqual(restart.restart(self.pidfile, wait=0, check=False), 0)
        self.assertEqual([call[0] for call in self.lifecycle.mock_calls[:6]], [
            "check_replacement", "poller.register", "send", "poller.poll", "launch", "wait_for_start",
        ])
        self.lifecycle.send.assert_called_once_with(20, signal.SIGINT)
        self.assertEqual(self.lifecycle.launch.call_args.args, (self.command,))
        self.assertEqual(self.lifecycle.launch.call_args.kwargs["env"], self.env)
        self.assertEqual(self.lifecycle.launch.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_timeout_never_launches_second_server(self):
        self.lifecycle.poller.poll.return_value = []
        with self.assertRaisesRegex(RuntimeError, "ещё завершается"):
            restart.restart(self.pidfile, wait=0, check=False)
        self.lifecycle.launch.assert_not_called()
