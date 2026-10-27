import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tv_scheduler


def fake_run(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class SchedulerTestCase(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.launch_agents_dir = Path(self._temp.name) / "LaunchAgents"
        self.log_path = Path(self._temp.name) / "Logs" / "tv-schedule.log"
        for target, value in (
            ("tv_scheduler.LAUNCH_AGENTS_DIR", self.launch_agents_dir),
            ("tv_scheduler.LOG_PATH", self.log_path),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class ScheduleActionTests(SchedulerTestCase):
    def test_rejects_unknown_action(self):
        with self.assertRaises(tv_scheduler.SchedulerError):
            tv_scheduler.schedule_action("bedtime", "explode", [], 21, 0)

    def test_rejects_empty_name(self):
        with self.assertRaises(tv_scheduler.SchedulerError):
            tv_scheduler.schedule_action("   ", "sleep", [], 21, 0)

    def test_rejects_invalid_hour(self):
        with self.assertRaises(tv_scheduler.SchedulerError):
            tv_scheduler.schedule_action("bedtime", "sleep", [], 24, 0)

    def test_rejects_invalid_minute(self):
        with self.assertRaises(tv_scheduler.SchedulerError):
            tv_scheduler.schedule_action("bedtime", "sleep", [], 21, 60)

    def test_writes_valid_plist_and_calls_launchctl_load(self):
        with patch("subprocess.run", return_value=fake_run()) as mock_run:
            path = tv_scheduler.schedule_action("bedtime", "sleep", [], 21, 30)
        self.assertTrue(path.exists())
        with path.open("rb") as handle:
            plist = plistlib.load(handle)
        self.assertEqual(plist["Label"], "local.desktop-assistant.tv.bedtime")
        self.assertEqual(plist["StartCalendarInterval"], {"Hour": 21, "Minute": 30})
        self.assertIn("sleep", plist["ProgramArguments"])
        load_calls = [c for c in mock_run.call_args_list if "load" in c.args[0]]
        self.assertEqual(len(load_calls), 1)

    def test_includes_action_args_in_plist(self):
        with patch("subprocess.run", return_value=fake_run()):
            path = tv_scheduler.schedule_action("movie_time", "open_app", ["Netflix"], 20, 0)
        with path.open("rb") as handle:
            plist = plistlib.load(handle)
        self.assertEqual(plist["ProgramArguments"][-2:], ["open_app", "Netflix"])

    def test_removes_plist_when_launchctl_load_fails(self):
        with patch("subprocess.run", return_value=fake_run(returncode=1, stderr="boom")):
            with self.assertRaises(tv_scheduler.SchedulerError):
                tv_scheduler.schedule_action("bedtime", "sleep", [], 21, 0)
        self.assertFalse((self.launch_agents_dir / "local.desktop-assistant.tv.bedtime.plist").exists())

    def test_overwriting_an_existing_schedule_unloads_it_first(self):
        with patch("subprocess.run", return_value=fake_run()) as mock_run:
            tv_scheduler.schedule_action("bedtime", "sleep", [], 21, 0)
            mock_run.reset_mock()
            tv_scheduler.schedule_action("bedtime", "sleep", [], 22, 0)
        unload_calls = [c for c in mock_run.call_args_list if "unload" in c.args[0]]
        self.assertEqual(len(unload_calls), 1)


class ListScheduledActionsTests(SchedulerTestCase):
    def test_returns_empty_list_when_no_launch_agents_dir(self):
        self.assertEqual(tv_scheduler.list_scheduled_actions(), [])

    def test_lists_only_helm_tv_schedules(self):
        self.launch_agents_dir.mkdir(parents=True)
        with patch("subprocess.run", return_value=fake_run()):
            tv_scheduler.schedule_action("bedtime", "sleep", [], 21, 30)
        (self.launch_agents_dir / "com.apple.something.else.plist").write_bytes(
            plistlib.dumps({"Label": "com.apple.something.else"})
        )
        schedules = tv_scheduler.list_scheduled_actions()
        self.assertEqual(len(schedules), 1)
        self.assertEqual(schedules[0]["name"], "bedtime")
        self.assertEqual(schedules[0]["action"], "sleep")
        self.assertEqual(schedules[0]["hour"], 21)
        self.assertEqual(schedules[0]["minute"], 30)

    def test_ignores_unreadable_plist(self):
        self.launch_agents_dir.mkdir(parents=True)
        (self.launch_agents_dir / "local.desktop-assistant.tv.broken.plist").write_text("not a plist")
        self.assertEqual(tv_scheduler.list_scheduled_actions(), [])


class RemoveScheduledActionTests(SchedulerTestCase):
    def test_raises_when_not_found(self):
        with self.assertRaises(tv_scheduler.SchedulerError):
            tv_scheduler.remove_scheduled_action("nope")

    def test_unloads_and_deletes_plist(self):
        with patch("subprocess.run", return_value=fake_run()) as mock_run:
            path = tv_scheduler.schedule_action("bedtime", "sleep", [], 21, 0)
            mock_run.reset_mock()
            tv_scheduler.remove_scheduled_action("bedtime")
        self.assertFalse(path.exists())
        unload_calls = [c for c in mock_run.call_args_list if "unload" in c.args[0]]
        self.assertEqual(len(unload_calls), 1)


if __name__ == "__main__":
    unittest.main()
