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
    @staticmethod
    def pane(path="/tmp/project"):
        return tmux_status.PaneInfo(
            session="work",
            session_attached=True,
            session_activity=0,
            window_index=2,
            window_name="agents",
            pane_index=1,
            pane_id="%3",
            pane_pid=100,
            current_command="grok",
            pane_active=True,
            pane_dead=False,
            pane_dead_status=None,
            current_path=path,
            session_id="$1",
            session_created=1785000000,
            window_id="@2",
            server_pid=500,
            server_started=1784999999,
        )

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
                    "$1",
                    "1785000000",
                    "@2",
                    "500",
                    "1784999999",
                ]
            )
            + "\n"
        )
        panes = tmux_status.parse_panes_output(output)
        self.assertEqual(1, len(panes))
        self.assertEqual("work:0.1", panes[0].locator)
        self.assertEqual("%3", panes[0].pane_id)
        self.assertEqual("500:1784999999", tmux_status.server_instance_id(panes[0]))

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
            session_id="$1",
            session_created=1785000000,
            window_id="@2",
            server_pid=500,
            server_started=1784999999,
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
        self.assertEqual("500:1784999999", statuses[0].server_instance_id)
        self.assertEqual(
            "500:1784999999:$1:1785000000:@2:%3:100",
            statuses[0].pane_instance_id,
        )

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

    def test_tool_detection_supports_versioned_grok_binary(self):
        process = tmux_status.ProcessInfo(
            1,
            0,
            0.0,
            1,
            "S",
            "0:01",
            "/Users/me/.grok/downloads/grok-0.2.118-macos-aarch64",
        )
        self.assertEqual(["grok"], tmux_status.detect_tools([process]))

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

    def test_extracts_only_explicit_uuid_cli_arguments(self):
        codex_id = "019fc5d1-40e4-75a2-89f2-188ae5efb2c4"
        grok_id = "019fc532-c5ba-7b90-a199-5ecd6d99bf69"
        self.assertEqual(
            (codex_id, "cli_resume_argument"),
            tmux_status.session_id_from_command(
                "codex", "codex resume -C '/tmp/my project' {}".format(codex_id)
            ),
        )
        self.assertEqual(
            (grok_id, "cli_resume_argument"),
            tmux_status.session_id_from_command(
                "grok", "grok --cwd /tmp/project --resume={}".format(grok_id)
            ),
        )
        self.assertIsNone(
            tmux_status.session_id_from_command("codex", "codex resume release-task")
        )
        self.assertIsNone(
            tmux_status.session_id_from_command("grok", "grok --resume 12345")
        )

    def test_reads_ids_from_open_codex_and_grok_session_files(self):
        codex_id = "019fc5d1-40e4-75a2-89f2-188ae5efb2c4"
        grok_id = "019fc532-c5ba-7b90-a199-5ecd6d99bf69"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_path = root / "sessions" / "2026" / "08" / "03"
            codex_path.mkdir(parents=True)
            rollout = codex_path / "rollout-2026-08-03-{}.jsonl".format(codex_id)
            rollout.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"session_id": codex_id, "cwd": "/tmp/project"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            grok_path = root / "sessions" / "cwd" / grok_id
            grok_path.mkdir(parents=True)
            events = grok_path / "events.jsonl"
            events.write_text("", encoding="utf-8")
            self.assertEqual(
                codex_id,
                tmux_status.session_id_from_open_file("codex", rollout),
            )
            self.assertEqual(
                grok_id,
                tmux_status.session_id_from_open_file("grok", events),
            )

    def test_collects_stable_mapping_from_open_session_file(self):
        grok_id = "019fc532-c5ba-7b90-a199-5ecd6d99bf69"
        pane = self.pane("/tmp/my project")
        process = tmux_status.ProcessInfo(
            101,
            100,
            0.0,
            1,
            "S",
            "0:01",
            "/Users/me/.grok/downloads/grok-0.2.118-macos-aarch64",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions" / "cwd" / grok_id / "events.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text("", encoding="utf-8")
            conversations = tmux_status.collect_agent_conversations(
                pane,
                [process],
                open_paths=lambda _pid: [path],
                scrollback=lambda _pane_id: "",
            )
        self.assertEqual(1, len(conversations))
        conversation = conversations[0]
        self.assertEqual(grok_id, conversation.conversation_id)
        self.assertEqual("confirmed", conversation.conversation_id_status)
        self.assertEqual("open_session_file", conversation.identity_source)
        self.assertEqual("grok:{}".format(grok_id), conversation.stable_mapping_key)
        self.assertEqual([101], conversation.process_pids)
        self.assertEqual(
            "grok --cwd '/tmp/my project' --resume {}".format(grok_id),
            conversation.resume_command,
        )

    def test_open_session_file_has_priority_over_wrapper_resume_argument(self):
        codex_id = "019fc5d1-40e4-75a2-89f2-188ae5efb2c4"
        pane = self.pane()
        wrapper = tmux_status.ProcessInfo(
            101,
            100,
            0.0,
            1,
            "S",
            "0:01",
            "node /opt/codex resume {}".format(codex_id),
        )
        binary = tmux_status.ProcessInfo(
            102, 101, 0.0, 1, "S", "0:01", "/opt/codex resume {}".format(codex_id)
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions" / "2026" / "08" / "03"
            path.mkdir(parents=True)
            rollout = path / "rollout-{}.jsonl".format(codex_id)
            rollout.write_text(
                json.dumps(
                    {"type": "session_meta", "payload": {"id": codex_id}}
                )
                + "\n",
                encoding="utf-8",
            )
            conversations = tmux_status.collect_agent_conversations(
                pane,
                [wrapper, binary],
                open_paths=lambda pid: [rollout] if pid == 102 else [],
                scrollback=lambda _pane_id: "",
            )
        self.assertEqual(1, len(conversations))
        self.assertEqual("open_session_file", conversations[0].identity_source)
        self.assertEqual(str(rollout), conversations[0].source_path)
        self.assertEqual([101, 102], conversations[0].process_pids)

    def test_unknown_id_is_explicit_and_never_guessed_from_pid(self):
        pane = self.pane()
        process = tmux_status.ProcessInfo(
            98765, 100, 0.0, 1, "S", "0:01", "/usr/local/bin/codex"
        )
        conversations = tmux_status.collect_agent_conversations(
            pane,
            [process],
            open_paths=lambda _pid: [],
            scrollback=lambda _pane_id: "",
        )
        self.assertEqual(1, len(conversations))
        self.assertIsNone(conversations[0].conversation_id)
        self.assertEqual("unknown", conversations[0].conversation_id_status)
        self.assertIsNone(conversations[0].stable_mapping_key)
        self.assertIsNone(conversations[0].resume_command)
        self.assertNotIn("98765", conversations[0].evidence)

    def test_conflicting_open_session_files_are_unknown(self):
        pane = self.pane()
        process = tmux_status.ProcessInfo(
            101,
            100,
            0.0,
            1,
            "S",
            "0:01",
            "/Users/me/.grok/downloads/grok-0.2.118-macos-aarch64",
        )
        ids = (
            "019fc532-c5ba-7b90-a199-5ecd6d99bf69",
            "019fb21f-84c9-7692-8371-1f9aa3e75401",
        )
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for session_id in ids:
                path = (
                    Path(directory)
                    / "sessions"
                    / "cwd"
                    / session_id
                    / "events.jsonl"
                )
                path.parent.mkdir(parents=True)
                path.write_text("", encoding="utf-8")
                paths.append(path)
            conversations = tmux_status.collect_agent_conversations(
                pane,
                [process],
                open_paths=lambda _pid: paths,
                scrollback=lambda _pane_id: "",
            )
        self.assertEqual(1, len(conversations))
        self.assertEqual("unknown", conversations[0].conversation_id_status)
        self.assertIsNone(conversations[0].conversation_id)
        self.assertIn("multiple", conversations[0].evidence)

    def test_uses_unique_resume_uuid_from_scrollback(self):
        codex_id = "019fc5d1-40e4-75a2-89f2-188ae5efb2c4"
        pane = self.pane()
        process = tmux_status.ProcessInfo(
            101, 100, 0.0, 1, "S", "0:01", "/usr/local/bin/codex"
        )
        conversations = tmux_status.collect_agent_conversations(
            pane,
            [process],
            open_paths=lambda _pid: [],
            scrollback=lambda _pane_id: "To continue, run codex resume {}\n".format(
                codex_id
            ),
        )
        self.assertEqual(codex_id, conversations[0].conversation_id)
        self.assertEqual(
            "tmux_scrollback_resume_command", conversations[0].identity_source
        )

    def test_payload_and_markdown_include_explicit_mapping_and_recovery(self):
        codex_id = "019fc5d1-40e4-75a2-89f2-188ae5efb2c4"
        pane = self.pane("/tmp/project")
        processes = {
            100: tmux_status.ProcessInfo(100, 1, 0.0, 1024, "S", "1:00", "zsh"),
            101: tmux_status.ProcessInfo(
                101,
                100,
                1.0,
                2048,
                "S",
                "0:10",
                "codex resume {}".format(codex_id),
            ),
        }
        statuses = tmux_status.build_statuses(
            [pane],
            processes,
            {},
            80.0,
            1024.0,
            conversation_collector=lambda current_pane, tree: tmux_status.collect_agent_conversations(
                current_pane,
                tree,
                open_paths=lambda _pid: [],
                scrollback=lambda _pane_id: "",
            ),
        )
        args = tmux_status.build_parser().parse_args(["recovery"])
        payload = tmux_status.status_payload(statuses, args, "recovery")
        self.assertEqual(3, payload["schema_version"])
        self.assertEqual("0.3.0", payload["tool_version"])
        self.assertEqual(
            {"name": "tmux-status", "version": "0.3.0"}, payload["producer"]
        )
        self.assertEqual("500:1784999999", payload["server_instance_id"])
        self.assertEqual("work", payload["panes"][0]["tmux_session_name"])
        self.assertEqual("%3", payload["panes"][0]["pane_id"])
        self.assertEqual(100, payload["panes"][0]["pane_pid"])
        self.assertEqual(codex_id, payload["recovery"][0]["conversation_id"])
        self.assertTrue(payload["pre_restart"])
        markdown = tmux_status.render_markdown(payload)
        self.assertIn("codex_thread_id", markdown)
        self.assertIn(codex_id, markdown)
        self.assertIn("codex resume -C /tmp/project {}".format(codex_id), markdown)

    def test_canonical_v3_fixtures_preserve_identity_semantics(self):
        root = Path(__file__).resolve().parents[1] / "contracts" / "v3"
        for name in ("confirmed", "unknown", "conflicting", "no-server"):
            with self.subTest(name=name):
                payload = json.loads(
                    (root / "fixtures" / "{}.json".format(name)).read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(3, payload["schema_version"])
                self.assertEqual(payload["tool_version"], payload["producer"]["version"])
                for pane in payload["panes"]:
                    for key in (
                        "session_id",
                        "session_created",
                        "window_id",
                        "server_instance_id",
                        "pane_instance_id",
                    ):
                        self.assertIn(key, pane)
                    for conversation in pane["agent_conversations"]:
                        if conversation["conversation_id_status"] == "unknown":
                            self.assertIsNone(conversation["conversation_id"])
                            self.assertIsNone(conversation["stable_mapping_key"])
                            self.assertIsNone(conversation["resume_command"])


if __name__ == "__main__":
    unittest.main()
