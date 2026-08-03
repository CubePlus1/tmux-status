import importlib.util
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "tmux_status.py"
SPEC = importlib.util.spec_from_file_location("tmux_status", MODULE_PATH)
tmux_status = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tmux_status)


class TmuxStatusTests(unittest.TestCase):
    def setUp(self):
        instance_key_patch = patch.object(
            tmux_status,
            "process_instance_key",
            side_effect=lambda pid: "{}:test-process-start".format(pid),
        )
        instance_key_patch.start()
        self.addCleanup(instance_key_patch.stop)

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

    def test_codex_resume_must_be_the_actual_subcommand(self):
        codex_id = "019fc5d1-40e4-75a2-89f2-188ae5efb2c4"
        misleading_commands = (
            "codex exec please resume {}".format(codex_id),
            "codex please resume {}".format(codex_id),
            "node /opt/codex exec resume {}".format(codex_id),
        )
        for command in misleading_commands:
            with self.subTest(command=command):
                self.assertIsNone(
                    tmux_status.session_id_from_command("codex", command)
                )

    def test_codex_resume_skips_supported_value_options(self):
        codex_id = "019fc5d1-40e4-75a2-89f2-188ae5efb2c4"
        commands = (
            "codex resume -i image.png {}".format(codex_id),
            "codex resume -i one.png two.png {}".format(codex_id),
            "codex -i one.png two.png resume {}".format(codex_id),
            "codex resume --enable feature {}".format(codex_id),
            "codex resume --add-dir /tmp/extra {}".format(codex_id),
            "codex --model gpt-test resume --profile work {}".format(codex_id),
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(
                    (codex_id, "cli_resume_argument"),
                    tmux_status.session_id_from_command("codex", command),
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
                working_directory=lambda _pid: "/tmp/my project",
            )
        self.assertEqual(1, len(conversations))
        conversation = conversations[0]
        self.assertEqual(grok_id, conversation.conversation_id)
        self.assertEqual("confirmed", conversation.conversation_id_status)
        self.assertEqual("open_session_file", conversation.identity_source)
        self.assertEqual("grok:{}".format(grok_id), conversation.stable_mapping_key)
        self.assertEqual({"101"}, set(conversation.process_instances))
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
                working_directory=lambda _pid: "/tmp/project",
            )
        self.assertEqual(1, len(conversations))
        self.assertEqual("open_session_file", conversations[0].identity_source)
        self.assertEqual(str(rollout), conversations[0].source_path)
        self.assertEqual({"101", "102"}, set(conversations[0].process_instances))

    def test_confirmed_conversation_uses_agent_working_directory(self):
        codex_id = "019fc5d1-40e4-75a2-89f2-188ae5efb2c4"
        pane = self.pane("/pane/project")
        process = tmux_status.ProcessInfo(
            101,
            100,
            0.0,
            1,
            "S",
            "0:01",
            "codex -C /agent/project resume {}".format(codex_id),
        )
        conversations = tmux_status.collect_agent_conversations(
            pane,
            [process],
            open_paths=lambda _pid: [],
            scrollback=lambda _pane_id: "",
            working_directory=lambda _pid: "/agent/project",
            arguments=lambda _pid: [
                "codex",
                "-C",
                "/agent/project",
                "resume",
                codex_id,
            ],
        )

        self.assertEqual("/agent/project", conversations[0].working_directory)
        self.assertEqual(
            "codex resume -C /agent/project {}".format(codex_id),
            conversations[0].resume_command,
        )

    def test_lossless_argv_preserves_explicit_cwd_with_spaces(self):
        codex_id = "019fc5d1-40e4-75a2-89f2-188ae5efb2c4"
        pane = self.pane("/pane/project")
        process = tmux_status.ProcessInfo(
            101,
            100,
            0.0,
            1,
            "S",
            "0:01",
            "codex -C /tmp/my project resume {}".format(codex_id),
        )
        conversations = tmux_status.collect_agent_conversations(
            pane,
            [process],
            open_paths=lambda _pid: [],
            scrollback=lambda _pane_id: "",
            working_directory=lambda _pid: None,
            arguments=lambda _pid: [
                "codex",
                "-C",
                "/tmp/my project",
                "resume",
                codex_id,
            ],
        )

        self.assertEqual("confirmed", conversations[0].conversation_id_status)
        self.assertEqual("/tmp/my project", conversations[0].working_directory)
        self.assertEqual(
            "codex resume -C '/tmp/my project' {}".format(codex_id),
            conversations[0].resume_command,
        )

    def test_observed_cwd_prevents_reapplying_relative_cli_cwd(self):
        codex_id = "019fc5d1-40e4-75a2-89f2-188ae5efb2c4"
        pane = self.pane("/base")
        process = tmux_status.ProcessInfo(
            101,
            100,
            0.0,
            1,
            "S",
            "0:01",
            "codex -C sub resume {}".format(codex_id),
        )
        conversations = tmux_status.collect_agent_conversations(
            pane,
            [process],
            open_paths=lambda _pid: [],
            scrollback=lambda _pane_id: "",
            working_directory=lambda _pid: "/base/sub",
            arguments=lambda _pid: ["codex", "-C", "sub", "resume", codex_id],
        )

        self.assertEqual("confirmed", conversations[0].conversation_id_status)
        self.assertEqual("/base/sub", conversations[0].working_directory)

    def test_current_process_cwd_wins_over_stale_session_metadata(self):
        codex_id = "019fc5d1-40e4-75a2-89f2-188ae5efb2c4"
        pane = self.pane("/pane/project")
        process = tmux_status.ProcessInfo(
            101,
            100,
            0.0,
            1,
            "S",
            "0:01",
            "codex -C /new resume {}".format(codex_id),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions" / "2026" / "08" / "03"
            path.mkdir(parents=True)
            rollout = path / "rollout-{}.jsonl".format(codex_id)
            rollout.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"id": codex_id, "cwd": "/old"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            conversations = tmux_status.collect_agent_conversations(
                pane,
                [process],
                open_paths=lambda _pid: [rollout],
                scrollback=lambda _pane_id: "",
                working_directory=lambda _pid: "/new",
                arguments=lambda _pid: [
                    "codex",
                    "-C",
                    "/new",
                    "resume",
                    codex_id,
                ],
            )

        self.assertEqual("confirmed", conversations[0].conversation_id_status)
        self.assertEqual("/new", conversations[0].working_directory)
        self.assertIn("-C /new", conversations[0].resume_command)

    def test_runtime_wrapper_folds_into_confirmed_child_invocation(self):
        codex_id = "019fc5d1-40e4-75a2-89f2-188ae5efb2c4"
        pane = self.pane()
        wrapper = tmux_status.ProcessInfo(
            101, 100, 0.0, 1, "S", "0:01", "node /opt/codex"
        )
        child = tmux_status.ProcessInfo(
            102, 101, 0.0, 1, "S", "0:01", "/usr/local/bin/codex"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions" / "2026" / "08" / "03"
            path.mkdir(parents=True)
            rollout = path / "rollout-{}.jsonl".format(codex_id)
            rollout.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"id": codex_id, "cwd": "/agent/project"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            conversations = tmux_status.collect_agent_conversations(
                pane,
                [wrapper, child],
                open_paths=lambda pid: [rollout] if pid == child.pid else [],
                scrollback=lambda _pane_id: "",
                working_directory=lambda _pid: "/process/project",
            )

        self.assertEqual(1, len(conversations))
        self.assertEqual("confirmed", conversations[0].conversation_id_status)
        self.assertEqual({"101", "102"}, set(conversations[0].process_instances))
        self.assertEqual("/process/project", conversations[0].working_directory)

    def test_unresolved_child_folds_into_confirmed_wrapper_invocation(self):
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
        child = tmux_status.ProcessInfo(
            102, 101, 0.0, 1, "S", "0:01", "/usr/local/bin/codex"
        )
        conversations = tmux_status.collect_agent_conversations(
            pane,
            [wrapper, child],
            open_paths=lambda _pid: [],
            scrollback=lambda _pane_id: "",
            working_directory=lambda _pid: "/agent/project",
            arguments=lambda pid: (
                ["node", "/opt/codex", "resume", codex_id]
                if pid == wrapper.pid
                else ["codex"]
            ),
        )

        self.assertEqual(1, len(conversations))
        self.assertEqual("confirmed", conversations[0].conversation_id_status)
        self.assertEqual(codex_id, conversations[0].conversation_id)
        self.assertEqual({"101", "102"}, set(conversations[0].process_instances))

    def test_related_wrapper_and_child_identity_disagreement_is_conflicting(self):
        wrapper_id = "019fc5d1-40e4-75a2-89f2-188ae5efb2c4"
        child_id = "019fb21f-84c9-7692-8371-1f9aa3e75401"
        pane = self.pane()
        wrapper = tmux_status.ProcessInfo(
            101,
            100,
            0.0,
            1,
            "S",
            "0:01",
            "node /opt/codex resume {}".format(wrapper_id),
        )
        child = tmux_status.ProcessInfo(
            102, 101, 0.0, 1, "S", "0:01", "/usr/local/bin/codex"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions" / "2026" / "08" / "03"
            path.mkdir(parents=True)
            rollout = path / "rollout-{}.jsonl".format(child_id)
            rollout.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"id": child_id, "cwd": "/agent/project"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            conversations = tmux_status.collect_agent_conversations(
                pane,
                [wrapper, child],
                open_paths=lambda pid: [rollout] if pid == child.pid else [],
                scrollback=lambda _pane_id: "",
                working_directory=lambda _pid: "/agent/project",
            )

        self.assertEqual(1, len(conversations))
        self.assertEqual("unknown", conversations[0].conversation_id_status)
        self.assertEqual("conflicting_evidence", conversations[0].identity_source)
        self.assertEqual({"101", "102"}, set(conversations[0].process_instances))
        self.assertIn("disagree on session identity", conversations[0].evidence)

    def test_wrapper_and_child_same_identity_use_native_child_cwd(self):
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
        child = tmux_status.ProcessInfo(
            102,
            101,
            0.0,
            1,
            "S",
            "0:01",
            "codex resume {}".format(codex_id),
        )
        conversations = tmux_status.collect_agent_conversations(
            pane,
            [wrapper, child],
            open_paths=lambda _pid: [],
            scrollback=lambda _pane_id: "",
            working_directory=lambda pid: "/base" if pid == 101 else "/project",
            arguments=lambda pid: (
                ["node", "/opt/codex", "resume", codex_id]
                if pid == wrapper.pid
                else ["codex", "resume", codex_id]
            ),
        )

        self.assertEqual(1, len(conversations))
        self.assertEqual("confirmed", conversations[0].conversation_id_status)
        self.assertEqual({"101", "102"}, set(conversations[0].process_instances))
        self.assertEqual("/project", conversations[0].working_directory)

    def test_nested_native_tool_processes_keep_independent_identities(self):
        parent_id = "019fc5d1-40e4-75a2-89f2-188ae5efb2c4"
        child_id = "019fb21f-84c9-7692-8371-1f9aa3e75401"
        pane = self.pane()
        parent = tmux_status.ProcessInfo(
            101,
            100,
            0.0,
            1,
            "S",
            "0:01",
            "codex resume {}".format(parent_id),
        )
        child = tmux_status.ProcessInfo(
            102,
            101,
            0.0,
            1,
            "S",
            "0:01",
            "codex resume {}".format(child_id),
        )
        conversations = tmux_status.collect_agent_conversations(
            pane,
            [parent, child],
            open_paths=lambda _pid: [],
            scrollback=lambda _pane_id: "",
            working_directory=lambda pid: "/project/{}".format(pid),
            arguments=lambda pid: [
                "codex",
                "resume",
                parent_id if pid == parent.pid else child_id,
            ],
        )

        self.assertEqual(2, len(conversations))
        self.assertEqual(
            {parent_id, child_id},
            {conversation.conversation_id for conversation in conversations},
        )
        self.assertTrue(
            all(
                conversation.conversation_id_status == "confirmed"
                for conversation in conversations
            )
        )

    def test_identity_without_process_associated_cwd_remains_unknown(self):
        codex_id = "019fc5d1-40e4-75a2-89f2-188ae5efb2c4"
        pane = self.pane("/pane/project")
        process = tmux_status.ProcessInfo(
            101,
            100,
            0.0,
            1,
            "S",
            "0:01",
            "codex resume {}".format(codex_id),
        )
        conversations = tmux_status.collect_agent_conversations(
            pane,
            [process],
            open_paths=lambda _pid: [],
            scrollback=lambda _pane_id: "",
            working_directory=lambda _pid: None,
        )

        self.assertEqual(1, len(conversations))
        self.assertEqual("unknown", conversations[0].conversation_id_status)
        self.assertEqual("unavailable", conversations[0].identity_source)
        self.assertIsNone(conversations[0].conversation_id)
        self.assertIsNone(conversations[0].resume_command)
        self.assertIn(
            "no process-associated working directory", conversations[0].evidence
        )

    def test_linux_process_start_time_handles_spaces_in_comm(self):
        fields_after_comm = ["S"] + [str(field) for field in range(4, 23)]
        stat_text = "101 (codex worker) {}".format(" ".join(fields_after_comm))
        self.assertEqual("22", tmux_status.linux_process_start_time(stat_text))

    def test_unmatched_process_remains_unknown_beside_confirmed_conversation(self):
        codex_id = "019fc5d1-40e4-75a2-89f2-188ae5efb2c4"
        pane = self.pane()
        confirmed = tmux_status.ProcessInfo(
            101,
            100,
            0.0,
            1,
            "S",
            "0:01",
            "codex resume {}".format(codex_id),
        )
        unmatched = tmux_status.ProcessInfo(
            102, 100, 0.0, 1, "S", "0:01", "/usr/local/bin/codex"
        )
        conversations = tmux_status.collect_agent_conversations(
            pane,
            [confirmed, unmatched],
            open_paths=lambda _pid: [],
            scrollback=lambda _pane_id: "",
            working_directory=lambda _pid: "/tmp/project",
        )

        self.assertEqual(2, len(conversations))
        self.assertEqual("confirmed", conversations[0].conversation_id_status)
        self.assertEqual({"101"}, set(conversations[0].process_instances))
        self.assertEqual("unknown", conversations[1].conversation_id_status)
        self.assertEqual({"102"}, set(conversations[1].process_instances))
        self.assertIsNone(conversations[1].conversation_id)

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
            instance_key=lambda pid: "{}:process-start-a".format(pid),
        )
        self.assertEqual(1, len(conversations))
        self.assertIsNone(conversations[0].conversation_id)
        self.assertEqual("unknown", conversations[0].conversation_id_status)
        self.assertIsNone(conversations[0].stable_mapping_key)
        self.assertIsNone(conversations[0].resume_command)
        self.assertEqual(
            {"98765": "98765:process-start-a"},
            conversations[0].process_instances,
        )
        self.assertNotIn("98765", conversations[0].evidence)

    def test_process_incarnation_change_discards_collected_evidence(self):
        codex_id = "019fc5d1-40e4-75a2-89f2-188ae5efb2c4"
        pane = self.pane()
        process = tmux_status.ProcessInfo(
            101,
            100,
            0.0,
            1,
            "S",
            "0:01",
            "codex resume {}".format(codex_id),
        )
        keys = iter(["101:first-start", "101:replacement-start"])
        conversations = tmux_status.collect_agent_conversations(
            pane,
            [process],
            open_paths=lambda _pid: [],
            scrollback=lambda _pane_id: "",
            working_directory=lambda _pid: "/project",
            arguments=lambda _pid: ["codex", "resume", codex_id],
            instance_key=lambda _pid: next(keys),
        )

        self.assertEqual(1, len(conversations))
        self.assertEqual("unknown", conversations[0].conversation_id_status)
        self.assertEqual("unavailable", conversations[0].identity_source)
        self.assertIsNone(conversations[0].resume_command)
        self.assertIn("changed incarnation", conversations[0].evidence)

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

    def test_does_not_confirm_unassociated_scrollback_uuid(self):
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
        self.assertIsNone(conversations[0].conversation_id)
        self.assertEqual("unknown", conversations[0].conversation_id_status)
        self.assertIn("cannot be associated", conversations[0].evidence)

    def test_file_and_cli_identity_disagreement_is_conflicting(self):
        file_id = "019fc5d1-40e4-75a2-89f2-188ae5efb2c4"
        cli_id = "019fb21f-84c9-7692-8371-1f9aa3e75401"
        pane = self.pane()
        process = tmux_status.ProcessInfo(
            101,
            100,
            0.0,
            1,
            "S",
            "0:01",
            "codex resume {}".format(cli_id),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions" / "2026" / "08" / "03"
            path.mkdir(parents=True)
            rollout = path / "rollout-{}.jsonl".format(file_id)
            rollout.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": file_id}})
                + "\n",
                encoding="utf-8",
            )
            conversations = tmux_status.collect_agent_conversations(
                pane,
                [process],
                open_paths=lambda _pid: [rollout],
                scrollback=lambda _pane_id: "",
                working_directory=lambda _pid: "/tmp/project",
            )

        self.assertEqual(1, len(conversations))
        self.assertEqual("unknown", conversations[0].conversation_id_status)
        self.assertEqual("conflicting_evidence", conversations[0].identity_source)
        self.assertIsNone(conversations[0].conversation_id)

    def test_multiple_processes_remain_unknown_despite_one_scrollback_uuid(self):
        codex_id = "019fc5d1-40e4-75a2-89f2-188ae5efb2c4"
        pane = self.pane()
        processes = [
            tmux_status.ProcessInfo(
                pid, 100, 0.0, 1, "S", "0:01", "/usr/local/bin/codex"
            )
            for pid in (101, 102)
        ]
        conversations = tmux_status.collect_agent_conversations(
            pane,
            processes,
            open_paths=lambda _pid: [],
            scrollback=lambda _pane_id: "codex resume {}\n".format(codex_id),
        )

        self.assertEqual(1, len(conversations))
        self.assertEqual("unknown", conversations[0].conversation_id_status)
        self.assertEqual({"101", "102"}, set(conversations[0].process_instances))
        self.assertIsNone(conversations[0].conversation_id)

    def test_human_status_and_watch_skip_conversation_collection(self):
        status_args = tmux_status.build_parser().parse_args(["status"])
        with patch.object(tmux_status, "collect_statuses", return_value=[]) as collect:
            with patch.object(tmux_status, "render_table", return_value="table"):
                with redirect_stdout(StringIO()):
                    tmux_status.cmd_status(status_args)
        collect.assert_called_once_with(status_args, include_conversations=False)

        watch_args = tmux_status.build_parser().parse_args(["watch"])
        with patch.object(
            tmux_status, "collect_statuses", side_effect=KeyboardInterrupt
        ) as collect:
            self.assertEqual(0, tmux_status.cmd_watch(watch_args))
        collect.assert_called_once_with(watch_args, include_conversations=False)

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
                working_directory=lambda _pid: "/tmp/project",
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
                        self.assertIsInstance(conversation["working_directory"], str)
                        self.assertTrue(conversation["process_instances"])
                        for pid, instance_key in conversation[
                            "process_instances"
                        ].items():
                            self.assertGreater(int(pid), 0)
                            self.assertTrue(instance_key)
                        if conversation["conversation_id_status"] == "unknown":
                            self.assertIsNone(conversation["conversation_id"])
                            self.assertIsNone(conversation["stable_mapping_key"])
                            self.assertIsNone(conversation["resume_command"])


if __name__ == "__main__":
    unittest.main()
