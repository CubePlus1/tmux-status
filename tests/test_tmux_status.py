import importlib.util
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "tmux_status.py"
SPEC = importlib.util.spec_from_file_location("tmux_status", MODULE_PATH)
tmux_status = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tmux_status)


class TmuxStatusTests(unittest.TestCase):
    def test_parse_tmux_escaped_field_separator(self):
        separator = r"\037"
        output = (
            separator.join(
                [
                    "work",
                    "1",
                    "1785231314",
                    "0",
                    "code",
                    "1",
                    "%3",
                    "100",
                    "codex",
                    "1",
                    "0",
                    "",
                    "/tmp/project",
                ]
            )
            + "\n"
        )
        panes = tmux_status.parse_panes_output(output)
        self.assertEqual(1, len(panes))
        self.assertEqual("work:0.1", panes[0].locator)
        self.assertEqual("%3", panes[0].pane_id)

    def test_parse_ps_and_descendants(self):
        output = """\
  100     1   0.0   5000 Ss   01:00:00 /bin/zsh
  101   100  75.5 120000 S+      10:03 /opt/homebrew/bin/codex
  102   101  10.0  80000 S       09:50 node helper.js
  200     1   1.0   4000 S       02:00 unrelated
"""
        processes = tmux_status.parse_ps_output(output)
        tree = tmux_status.descendants(100, processes)
        self.assertEqual({100, 101, 102}, {process.pid for process in tree})
        self.assertEqual(["codex"], tmux_status.detect_tools(tree))

    def test_build_status_aggregates_and_flags(self):
        pane = tmux_status.PaneInfo(
            session="work",
            session_attached=True,
            session_activity=0,
            window_index=0,
            window_name="code",
            pane_index=1,
            pane_id="%3",
            pane_pid=100,
            current_command="codex",
            pane_active=True,
            pane_dead=False,
            pane_dead_status=None,
            current_path="/tmp/project",
        )
        processes = {
            100: tmux_status.ProcessInfo(100, 1, 1.0, 1024, "S", "1:00", "zsh"),
            101: tmux_status.ProcessInfo(
                101, 100, 90.0, 2048, "S", "0:10", "/usr/bin/codex"
            ),
        }
        statuses = tmux_status.build_statuses(
            [pane], processes, {}, cpu_threshold=80.0, memory_threshold_mb=10.0
        )
        self.assertEqual(91.0, statuses[0].cpu_percent)
        self.assertEqual(["CPU"], statuses[0].anomalies)
        self.assertEqual(["codex"], statuses[0].tools)
        self.assertEqual("active", statuses[0].activity)

    def test_manual_mark_precedence(self):
        pane = tmux_status.PaneInfo(
            "work", True, 0, 0, "shell", 0, "%1", 100, "zsh", True, False, None, "/tmp"
        )
        statuses = tmux_status.build_statuses(
            [pane],
            {100: tmux_status.ProcessInfo(100, 1, 0.0, 1024, "S", "1:00", "zsh")},
            {"%1": {"state": "inactive", "note": "paused"}},
            80.0,
            1024.0,
        )
        self.assertEqual("inactive", statuses[0].activity)
        self.assertEqual("manual:%1", statuses[0].activity_source)
        self.assertEqual("paused", statuses[0].note)

    def test_marks_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "marks.json"
            marks = {"work": {"state": "active", "note": "release"}}
            tmux_status.save_marks(marks, path)
            self.assertEqual(marks, tmux_status.load_marks(path))
            payload = json.loads(path.read_text())
            self.assertEqual(1, payload["version"])

    def test_invalid_marks_are_reported(self):
        invalid_payloads = [
            [],
            {"marks": []},
            {"marks": {"work": []}},
            {"marks": {"work": {"state": "bogus"}}},
            {"marks": {"work": {"state": "active", "note": []}}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "marks.json"
            for payload in invalid_payloads:
                with self.subTest(payload=payload):
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaises(tmux_status.TmuxStatusError):
                        tmux_status.load_marks(path)

    def test_numeric_options_require_finite_values_in_range(self):
        parser = tmux_status.build_parser()
        invalid_arguments = [
            ["status", "--cpu-threshold", "nan"],
            ["status", "--memory-threshold", "inf"],
            ["status", "--cpu-threshold", "-1"],
            ["watch", "--interval", "nan"],
            ["watch", "--interval", "0"],
        ]
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                with redirect_stderr(StringIO()):
                    with self.assertRaises(SystemExit) as context:
                        parser.parse_args(arguments)
                self.assertEqual(2, context.exception.code)

    def test_tool_detection_supports_runtime_wrappers(self):
        processes = [
            tmux_status.ProcessInfo(
                1, 0, 0.0, 1, "S", "0:01", "/Users/me/.grok/bin/grok chat"
            ),
            tmux_status.ProcessInfo(
                2, 0, 0.0, 1, "S", "0:01", "node /opt/homebrew/bin/codex run"
            ),
            tmux_status.ProcessInfo(3, 0, 0.0, 1, "S", "0:01", "python -m codex run"),
        ]
        self.assertEqual(["codex", "grok"], tmux_status.detect_tools(processes))

    def test_tool_detection_ignores_command_arguments(self):
        processes = [
            tmux_status.ProcessInfo(
                1, 0, 0.0, 1, "S", "0:01", "python app.py ask codex"
            ),
            tmux_status.ProcessInfo(
                2, 0, 0.0, 1, "S", "0:01", "tool tell grok something"
            ),
            tmux_status.ProcessInfo(3, 0, 0.0, 1, "S", "0:01", "python -c codex"),
        ]
        self.assertEqual([], tmux_status.detect_tools(processes))


if __name__ == "__main__":
    unittest.main()
