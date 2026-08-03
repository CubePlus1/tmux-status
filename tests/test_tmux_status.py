import importlib.util
import json
import os
import shlex
import struct
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
        self.instance_key_patch = patch.object(
            tmux_status,
            "process_instance_key",
            side_effect=lambda pid: "{}:test-process-start".format(pid),
        )
        self.instance_key_patch.start()
        self.addCleanup(self.instance_key_patch.stop)

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
        self.assertEqual(
            ["codex"], tmux_status.detect_tools(tree, arguments=lambda _pid: None)
        )

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

    def test_dead_pane_does_not_reuse_pid_processes_or_collect_recovery(self):
        pane = self.pane()
        pane.pane_dead = True
        pane.pane_dead_status = 0
        collected = []
        statuses = tmux_status.build_statuses(
            [pane],
            {
                100: tmux_status.ProcessInfo(
                    100, 1, 25.0, 2048, "S", "0:10", "/usr/local/bin/codex"
                )
            },
            {},
            80.0,
            1024.0,
            conversation_collector=lambda current_pane, tree: collected.append(
                (current_pane, tree)
            ),
        )

        self.assertEqual([], collected)
        self.assertEqual(0, statuses[0].process_count)
        self.assertEqual([], statuses[0].tools)
        self.assertEqual([], statuses[0].agent_conversations)
        self.assertIn("DEAD", statuses[0].anomalies)

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
        self.assertEqual(
            ["codex", "grok"],
            tmux_status.detect_tools(processes, arguments=lambda _pid: None),
        )
        self.assertTrue(
            tmux_status.is_runtime_wrapper_process(
                tmux_status.ProcessInfo(
                    4,
                    0,
                    0.0,
                    1,
                    "S",
                    "0:01",
                    "python3.11 -m codex resume thread-id",
                ),
                "codex",
            )
        )

    def test_python_wrapper_consumes_value_taking_interpreter_options(self):
        codex_id = "019fc5d1-40e4-75a2-89f2-188ae5efb2c4"
        arguments = [
            "python3",
            "-X",
            "dev",
            "-W",
            "ignore",
            "-m",
            "codex",
            "resume",
            codex_id,
        ]
        self.assertEqual(
            (codex_id, "cli_resume_argument"),
            tmux_status.session_id_from_arguments("codex", arguments),
        )
        self.assertEqual("codex", tmux_status.tool_for_arguments(arguments))

    def test_node_wrapper_consumes_value_taking_runtime_options(self):
        codex_id = "019fc5d1-40e4-75a2-89f2-188ae5efb2c4"
        arguments = [
            "node",
            "-r",
            "preload",
            "--import=loader",
            "--env-file-if-exists",
            "/tmp/agent.env",
            "--experimental-config-file",
            "/tmp/node.json",
            "--cpu-prof-dir",
            "/tmp/profiles",
            "--watch-path=/tmp/project",
            "/tmp/my tools/codex",
            "resume",
            codex_id,
        ]
        self.assertEqual(
            (codex_id, "cli_resume_argument"),
            tmux_status.session_id_from_arguments("codex", arguments),
        )
        self.assertEqual("codex", tmux_status.tool_for_arguments(arguments))
        process = tmux_status.ProcessInfo(
            101, 100, 0.0, 1, "S", "0:01", "node -r preload /tmp/my tools/codex"
        )
        self.assertTrue(
            tmux_status.is_runtime_wrapper_process(process, "codex", arguments)
        )

    def test_bun_wrapper_consumes_value_taking_runtime_options(self):
        codex_id = "019fc5d1-40e4-75a2-89f2-188ae5efb2c4"
        arguments = [
            "bun",
            "--cwd",
            "/tmp/project",
            "--config=/tmp/bunfig.toml",
            "--preload",
            "/tmp/register.ts",
            "/opt/codex",
            "resume",
            codex_id,
        ]
        self.assertEqual(
            (codex_id, "cli_resume_argument"),
            tmux_status.session_id_from_arguments("codex", arguments),
        )
        self.assertEqual("codex", tmux_status.tool_for_arguments(arguments))
        process = tmux_status.ProcessInfo(
            101, 100, 0.0, 1, "S", "0:01", "bun --cwd /tmp/project /opt/codex"
        )
        self.assertTrue(
            tmux_status.is_runtime_wrapper_process(process, "codex", arguments)
        )

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
        self.assertEqual(
            ["grok"],
            tmux_status.detect_tools([process], arguments=lambda _pid: None),
        )

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
        self.assertEqual(
            [], tmux_status.detect_tools(processes, arguments=lambda _pid: None)
        )

    def test_lossless_argv_classifies_agent_paths_with_spaces(self):
        codex_id = "019fc5d1-40e4-75a2-89f2-188ae5efb2c4"
        process = tmux_status.ProcessInfo(
            101,
            100,
            0.0,
            1,
            "S",
            "0:01",
            "/tmp/tmux status/codex resume {}".format(codex_id),
        )
        argv = ["/tmp/tmux status/codex", "resume", codex_id]
        self.assertEqual(
            ["codex"],
            tmux_status.detect_tools([process], arguments=lambda _pid: argv),
        )
        conversations = tmux_status.collect_agent_conversations(
            self.pane(),
            [process],
            open_paths=lambda _pid: [],
            scrollback=lambda _pane_id: "",
            working_directory=lambda _pid: "/tmp/project",
            arguments=lambda _pid: argv,
        )
        self.assertEqual(codex_id, conversations[0].conversation_id)

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
        image_id = "019fb21f-84c9-7692-8371-1f9aa3e75401"
        commands = (
            "codex resume -i image.png {}".format(codex_id),
            "codex resume -i one.png two.png {}".format(codex_id),
            "codex -i one.png two.png resume {}".format(codex_id),
            "codex resume -i {} {}".format(image_id, codex_id),
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
        self.assertIsNone(
            tmux_status.session_id_from_command(
                "codex", "codex resume -i {}".format(image_id)
            )
        )
        self.assertEqual(
            (codex_id, "cli_resume_argument"),
            tmux_status.session_id_from_command(
                "codex",
                "codex resume -i image.png {} --enable feature {}".format(
                    image_id, codex_id
                ),
            ),
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
                tmux_status.session_id_from_open_file(
                    "codex", rollout, {"codex": root / "sessions"}
                ),
            )
            self.assertEqual(
                grok_id,
                tmux_status.session_id_from_open_file(
                    "grok", events, {"grok": root / "sessions"}
                ),
            )

            unrelated = root / "project" / "sessions" / rollout.name
            unrelated.parent.mkdir(parents=True)
            unrelated.write_text(
                rollout.read_text(encoding="utf-8"), encoding="utf-8"
            )
            self.assertIsNone(
                tmux_status.session_id_from_open_file(
                    "codex", unrelated, {"codex": root / "sessions"}
                )
            )
            unrelated_grok = (
                root / "project" / "sessions" / "fixture" / grok_id / "events.jsonl"
            )
            unrelated_grok.parent.mkdir(parents=True)
            unrelated_grok.write_text("", encoding="utf-8")
            self.assertIsNone(
                tmux_status.session_id_from_open_file(
                    "grok", unrelated_grok, {"grok": root / "sessions"}
                )
            )

    def test_reads_codex_metadata_from_the_held_descriptor(self):
        original_id = "019fc5d1-40e4-75a2-89f2-188ae5efb2c4"
        replacement_id = "019fb21f-84c9-7692-8371-1f9aa3e75401"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rollout_dir = root / "sessions" / "2026" / "08" / "03"
            rollout_dir.mkdir(parents=True)
            rollout = rollout_dir / "rollout-{}.jsonl".format(original_id)
            rollout.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": original_id}})
                + "\n",
                encoding="utf-8",
            )
            with rollout.open("r", encoding="utf-8") as held_file:
                held_stat = os.fstat(held_file.fileno())
                evidence = tmux_status.OpenProcessFile(
                    source_path=rollout,
                    read_path=Path("/dev/fd") / str(held_file.fileno()),
                    inode=held_stat.st_ino,
                    device=held_stat.st_dev,
                )
                replacement = rollout.with_suffix(".replacement")
                replacement.write_text(
                    json.dumps(
                        {"type": "session_meta", "payload": {"id": replacement_id}}
                    )
                    + "\n",
                    encoding="utf-8",
                )
                replacement.replace(rollout)

                self.assertEqual(
                    original_id,
                    tmux_status.session_id_from_open_file(
                        "codex", evidence, {"codex": root / "sessions"}
                    ),
                )

    def test_grok_metadata_requires_the_captured_descriptor_identity(self):
        grok_id = "019fc532-c5ba-7b90-a199-5ecd6d99bf69"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events = root / "sessions" / grok_id / "events.jsonl"
            events.parent.mkdir(parents=True)
            events.write_text("", encoding="utf-8")
            original_inode = events.stat().st_ino
            replacement = root / "replacement.jsonl"
            replacement.write_text("", encoding="utf-8")
            switched_descriptor = tmux_status.OpenProcessFile(
                source_path=events,
                read_path=replacement,
                inode=original_inode,
            )

            self.assertIsNone(
                tmux_status.session_id_from_open_file(
                    "grok",
                    switched_descriptor,
                    {"grok": root / "sessions"},
                )
            )

    def test_grok_metadata_requires_captured_device_and_inode(self):
        grok_id = "019fc532-c5ba-7b90-a199-5ecd6d99bf69"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events = root / "sessions" / grok_id / "events.jsonl"
            events.parent.mkdir(parents=True)
            events.write_text("", encoding="utf-8")
            event_stat = events.stat()
            wrong_device = tmux_status.OpenProcessFile(
                source_path=events,
                read_path=events,
                inode=event_stat.st_ino,
                device=event_stat.st_dev + 1,
            )

            self.assertIsNone(
                tmux_status.session_id_from_open_file(
                    "grok", wrong_device, {"grok": root / "sessions"}
                )
            )

    def test_lsof_evidence_revalidates_the_target_descriptor(self):
        codex_id = "019fc5d1-40e4-75a2-89f2-188ae5efb2c4"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rollout_dir = root / "sessions" / "2026" / "08" / "03"
            rollout_dir.mkdir(parents=True)
            rollout = rollout_dir / "rollout-{}.jsonl".format(codex_id)
            rollout.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": codex_id}})
                + "\n",
                encoding="utf-8",
            )
            rollout_stat = rollout.stat()
            evidence = tmux_status.OpenProcessFile(
                source_path=rollout,
                read_path=rollout,
                inode=rollout_stat.st_ino,
                device=rollout_stat.st_dev,
                process_id=101,
                descriptor="15",
            )
            matching_lsof = "f15u\nD{}\ni{}\nn{}\n".format(
                hex(rollout_stat.st_dev), rollout_stat.st_ino, rollout
            )
            switched_lsof = "f15u\nD{}\ni{}\nn{}\n".format(
                hex(rollout_stat.st_dev), rollout_stat.st_ino + 1, rollout
            )
            results = [
                tmux_status.subprocess.CompletedProcess(
                    [], 0, stdout=matching_lsof, stderr=""
                ),
                tmux_status.subprocess.CompletedProcess(
                    [], 0, stdout=switched_lsof, stderr=""
                ),
            ]
            with patch.object(tmux_status, "run_command", side_effect=results):
                self.assertIsNone(
                    tmux_status.session_id_from_open_file(
                        "codex", evidence, {"codex": root / "sessions"}
                    )
                )

    def test_lsof_parser_keeps_numeric_descriptor_identity(self):
        parsed = tmux_status.parse_lsof_open_files(
            "f7u\nD0x100\ni42\nn/tmp/session.jsonl\nfcwd\nn/tmp\n",
            101,
        )
        self.assertEqual(1, len(parsed))
        self.assertEqual("7", parsed[0].descriptor)
        self.assertEqual(101, parsed[0].process_id)
        self.assertEqual(0x100, parsed[0].device)
        self.assertEqual(42, parsed[0].inode)

    def test_descriptor_capture_rejects_a_changed_fd_target(self):
        descriptor = Path("/proc/101/fd/7")
        descriptor_stat = os.stat(__file__)
        with patch.object(
            tmux_status.os,
            "readlink",
            side_effect=["/tmp/old-session", "/tmp/new-session"],
        ):
            with patch.object(
                tmux_status.Path, "stat", return_value=descriptor_stat
            ):
                self.assertIsNone(
                    tmux_status.capture_open_descriptor(descriptor)
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
                session_roots={"grok": Path(directory) / "sessions"},
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

    def test_uses_the_inspected_process_codex_home(self):
        codex_id = "019fc5d1-40e4-75a2-89f2-188ae5efb2c4"
        pane = self.pane()
        process = tmux_status.ProcessInfo(
            101, 100, 0.0, 1, "S", "0:01", "/usr/local/bin/codex"
        )
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "custom-codex"
            rollout_dir = codex_home / "sessions" / "2026" / "08" / "03"
            rollout_dir.mkdir(parents=True)
            rollout = rollout_dir / "rollout-{}.jsonl".format(codex_id)
            rollout.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": codex_id}})
                + "\n",
                encoding="utf-8",
            )
            conversations = tmux_status.collect_agent_conversations(
                pane,
                [process],
                open_paths=lambda _pid: [rollout],
                scrollback=lambda _pane_id: "",
                working_directory=lambda _pid: "/tmp/project",
                environment=lambda _pid: {"CODEX_HOME": str(codex_home)},
            )

        self.assertEqual("confirmed", conversations[0].conversation_id_status)
        self.assertEqual(codex_id, conversations[0].conversation_id)

    def test_uses_inspected_process_home_for_default_codex_root(self):
        codex_id = "019fc5d1-40e4-75a2-89f2-188ae5efb2c4"
        pane = self.pane()
        process = tmux_status.ProcessInfo(
            101, 100, 0.0, 1, "S", "0:01", "/usr/local/bin/codex"
        )
        with tempfile.TemporaryDirectory() as directory:
            agent_home = Path(directory) / "agent-home"
            rollout_dir = agent_home / ".codex" / "sessions" / "2026" / "08" / "03"
            rollout_dir.mkdir(parents=True)
            rollout = rollout_dir / "rollout-{}.jsonl".format(codex_id)
            rollout.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": codex_id}})
                + "\n",
                encoding="utf-8",
            )
            conversations = tmux_status.collect_agent_conversations(
                pane,
                [process],
                open_paths=lambda _pid: [rollout],
                scrollback=lambda _pane_id: "",
                working_directory=lambda _pid: "/tmp/project",
                environment=lambda _pid: {"HOME": str(agent_home)},
            )

        self.assertEqual("confirmed", conversations[0].conversation_id_status)
        self.assertEqual(codex_id, conversations[0].conversation_id)

    def test_reads_agent_home_from_darwin_process_environment(self):
        raw_procargs = (
            struct.pack("=i", 1)
            + b"/usr/local/bin/codex\0\0"
            + b"codex\0"
            + b"UNRELATED=discard-me\0"
            + b"CODEX_HOME=/tmp/Custom Codex\0"
            + b"HOME=/Users/agent\0"
        )
        with patch.object(tmux_status.sys, "platform", "darwin"):
            with patch.object(
                tmux_status.Path, "read_bytes", side_effect=OSError
            ):
                with patch.object(
                    tmux_status,
                    "darwin_process_arguments_and_environment",
                    return_value=raw_procargs,
                ):
                    environment = tmux_status.process_agent_home_environment(101)

        self.assertEqual(
            {"CODEX_HOME": "/tmp/Custom Codex", "HOME": "/Users/agent"},
            environment,
        )

    def test_reads_lossless_arguments_from_darwin_procargs(self):
        codex_id = "019fc5d1-40e4-75a2-89f2-188ae5efb2c4"
        expected_arguments = [
            "/Applications/Codex Agent/codex",
            "resume",
            codex_id,
        ]
        raw_procargs = (
            struct.pack("=i", len(expected_arguments))
            + b"/Applications/Codex Agent/codex\0\0"
            + b"\0".join(os.fsencode(value) for value in expected_arguments)
            + b"\0CODEX_HOME=/tmp/Custom Codex\0"
        )
        with patch.object(tmux_status.sys, "platform", "darwin"):
            with patch.object(
                tmux_status.Path, "read_bytes", side_effect=OSError
            ):
                with patch.object(
                    tmux_status,
                    "darwin_process_arguments_and_environment",
                    return_value=raw_procargs,
                ):
                    arguments = tmux_status.process_arguments(101)

        self.assertEqual(expected_arguments, arguments)
        self.assertEqual("codex", tmux_status.tool_for_arguments(arguments))

    def test_empty_process_environment_uses_agent_default_home(self):
        with patch.dict(
            tmux_status.os.environ,
            {"CODEX_HOME": "/reporter/custom-codex"},
        ):
            with patch.object(
                tmux_status.Path, "home", return_value=Path("/agent-home")
            ):
                self.assertEqual(
                    Path("/agent-home/.codex/sessions"),
                    tmux_status.configured_session_root("codex", {}),
                )
                self.assertEqual(
                    Path("/reporter/custom-codex/sessions"),
                    tmux_status.configured_session_root("codex", None),
                )

    def test_process_home_controls_default_agent_data_root(self):
        with patch.object(
            tmux_status.Path, "home", return_value=Path("/reporter-home")
        ):
            self.assertEqual(
                Path("/agent-home/.codex/sessions"),
                tmux_status.configured_session_root(
                    "codex", {"HOME": "/agent-home"}
                ),
            )
            self.assertEqual(
                Path("/agent-home/.grok/sessions"),
                tmux_status.configured_session_root(
                    "grok", {"HOME": "/agent-home"}
                ),
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
                session_roots={"codex": Path(directory) / "sessions"},
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
        with tempfile.TemporaryDirectory() as directory:
            cwd = str(Path(directory) / "my project")
            Path(cwd).mkdir()
            process = tmux_status.ProcessInfo(
                101,
                100,
                0.0,
                1,
                "S",
                "0:01",
                "codex -C {} resume {}".format(cwd, codex_id),
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
                    cwd,
                    "resume",
                    codex_id,
                ],
            )

        self.assertEqual("confirmed", conversations[0].conversation_id_status)
        self.assertEqual(cwd, conversations[0].working_directory)
        self.assertEqual(
            "codex resume -C {} {}".format(shlex.quote(cwd), codex_id),
            conversations[0].resume_command,
        )

    def test_unusable_absolute_cli_cwd_keeps_mapping_unknown(self):
        codex_id = "019fc5d1-40e4-75a2-89f2-188ae5efb2c4"
        pane = self.pane("/pane/project")
        missing_cwd = "/tmp/definitely-missing-codex-working-directory"
        process = tmux_status.ProcessInfo(
            101,
            100,
            0.0,
            1,
            "S",
            "0:01",
            "codex -C {} resume {}".format(missing_cwd, codex_id),
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
                missing_cwd,
                "resume",
                codex_id,
            ],
        )

        self.assertEqual("unknown", conversations[0].conversation_id_status)
        self.assertIsNone(conversations[0].conversation_id)
        self.assertIsNone(conversations[0].resume_command)

    def test_same_session_with_different_process_cwds_is_conflicting(self):
        codex_id = "019fc5d1-40e4-75a2-89f2-188ae5efb2c4"
        pane = self.pane("/pane/project")
        processes = [
            tmux_status.ProcessInfo(
                pid,
                100,
                0.0,
                1,
                "S",
                "0:01",
                "codex resume {}".format(codex_id),
            )
            for pid in (101, 102)
        ]
        with tempfile.TemporaryDirectory() as directory:
            process_cwds = {
                101: str(Path(directory) / "first"),
                102: str(Path(directory) / "second"),
            }
            for cwd in process_cwds.values():
                Path(cwd).mkdir()
            conversations = tmux_status.collect_agent_conversations(
                pane,
                processes,
                open_paths=lambda _pid: [],
                scrollback=lambda _pane_id: "",
                working_directory=lambda pid: process_cwds[pid],
                arguments=lambda _pid: ["codex", "resume", codex_id],
            )

        self.assertTrue(conversations)
        self.assertTrue(
            all(
                conversation.conversation_id_status == "unknown"
                and conversation.identity_source == "conflicting_evidence"
                and conversation.conversation_id is None
                for conversation in conversations
            )
        )
        self.assertEqual(
            {"101", "102"},
            {
                pid
                for conversation in conversations
                for pid in conversation.process_instances
            },
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
                session_roots={"codex": Path(directory) / "sessions"},
            )

        self.assertEqual("confirmed", conversations[0].conversation_id_status)
        self.assertEqual("/new", conversations[0].working_directory)
        self.assertIn("-C /new", conversations[0].resume_command)

    def test_unusable_session_metadata_cwd_keeps_mapping_unknown(self):
        codex_id = "019fc5d1-40e4-75a2-89f2-188ae5efb2c4"
        pane = self.pane("/pane/project")
        process = tmux_status.ProcessInfo(
            101, 100, 0.0, 1, "S", "0:01", "/usr/local/bin/codex"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions" / "2026" / "08" / "03"
            path.mkdir(parents=True)
            rollout = path / "rollout-{}.jsonl".format(codex_id)
            rollout.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": codex_id,
                            "cwd": str(Path(directory) / "deleted-project"),
                        },
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
                working_directory=lambda _pid: None,
                session_roots={"codex": Path(directory) / "sessions"},
            )

        self.assertEqual(1, len(conversations))
        self.assertEqual("unknown", conversations[0].conversation_id_status)
        self.assertIsNone(conversations[0].conversation_id)
        self.assertIsNone(conversations[0].resume_command)
        self.assertIn(
            "metadata working directory is unavailable",
            conversations[0].evidence,
        )

    def test_relative_session_metadata_cwd_keeps_mapping_unknown(self):
        codex_id = "019fc5d1-40e4-75a2-89f2-188ae5efb2c4"
        process = tmux_status.ProcessInfo(
            101, 100, 0.0, 1, "S", "0:01", "/usr/local/bin/codex"
        )
        with tempfile.TemporaryDirectory() as directory:
            pane = self.pane(directory)
            (Path(directory) / "project").mkdir()
            path = Path(directory) / "sessions" / "2026" / "08" / "03"
            path.mkdir(parents=True)
            rollout = path / "rollout-{}.jsonl".format(codex_id)
            rollout.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"id": codex_id, "cwd": "project"},
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
                working_directory=lambda _pid: None,
                session_roots={"codex": Path(directory) / "sessions"},
            )

        self.assertEqual(1, len(conversations))
        self.assertEqual("unknown", conversations[0].conversation_id_status)
        self.assertIsNone(conversations[0].conversation_id)
        self.assertIsNone(conversations[0].resume_command)
        self.assertIn(
            "metadata working directory is unavailable",
            conversations[0].evidence,
        )

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
                session_roots={"codex": Path(directory) / "sessions"},
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
                session_roots={"codex": Path(directory) / "sessions"},
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

    def test_wrapper_without_cwd_folds_into_confirmed_child(self):
        codex_id = "019fc5d1-40e4-75a2-89f2-188ae5efb2c4"
        wrapper = tmux_status.ProcessInfo(
            101, 100, 0.0, 1, "S", "0:01", "node /opt/codex"
        )
        child = tmux_status.ProcessInfo(
            102, 101, 0.0, 1, "S", "0:01", "/usr/local/bin/codex"
        )
        arguments = {
            101: ["node", "/opt/codex", "resume", codex_id],
            102: ["codex", "resume", codex_id],
        }
        conversations = tmux_status.collect_agent_conversations(
            self.pane(),
            [wrapper, child],
            open_paths=lambda _pid: [],
            scrollback=lambda _pane_id: "",
            working_directory=lambda pid: None if pid == 101 else "/project",
            arguments=lambda pid: arguments[pid],
        )

        self.assertEqual(1, len(conversations))
        self.assertEqual("confirmed", conversations[0].conversation_id_status)
        self.assertEqual({"101", "102"}, set(conversations[0].process_instances))
        self.assertEqual("/project", conversations[0].working_directory)

    def test_same_identity_wrappers_merge_only_into_their_own_children(self):
        codex_id = "019fc5d1-40e4-75a2-89f2-188ae5efb2c4"
        processes = [
            tmux_status.ProcessInfo(
                101, 100, 0.0, 1, "S", "0:01", "node /opt/codex"
            ),
            tmux_status.ProcessInfo(
                102, 101, 0.0, 1, "S", "0:01", "/usr/local/bin/codex"
            ),
            tmux_status.ProcessInfo(
                201, 100, 0.0, 1, "S", "0:01", "node /opt/codex"
            ),
            tmux_status.ProcessInfo(
                202, 201, 0.0, 1, "S", "0:01", "/usr/local/bin/codex"
            ),
        ]
        arguments = {
            101: ["node", "/opt/codex", "resume", codex_id],
            102: ["codex", "resume", codex_id],
            201: ["node", "/opt/codex", "resume", codex_id],
            202: ["codex", "resume", codex_id],
        }
        working_directories = {
            101: "/base",
            102: "/project-one",
            201: "/base",
            202: "/project-two",
        }

        conversations = tmux_status.collect_agent_conversations(
            self.pane(),
            processes,
            open_paths=lambda _pid: [],
            scrollback=lambda _pane_id: "",
            working_directory=lambda pid: working_directories[pid],
            arguments=lambda pid: arguments[pid],
        )

        pids_by_cwd = {
            conversation.working_directory: set(conversation.process_instances)
            for conversation in conversations
        }
        self.assertEqual(
            {
                "/project-one": {"101", "102"},
                "/project-two": {"201", "202"},
            },
            pids_by_cwd,
        )

    def test_lossless_wrapper_argv_folds_child_when_tool_path_has_spaces(self):
        codex_id = "019fc5d1-40e4-75a2-89f2-188ae5efb2c4"
        wrapper = tmux_status.ProcessInfo(
            101,
            100,
            0.0,
            1,
            "S",
            "0:01",
            "node /tmp/my tools/codex resume {}".format(codex_id),
        )
        child = tmux_status.ProcessInfo(
            102, 101, 0.0, 1, "S", "0:01", "/usr/local/bin/codex"
        )
        arguments = {
            101: ["node", "/tmp/my tools/codex", "resume", codex_id],
            102: ["codex"],
        }

        conversations = tmux_status.collect_agent_conversations(
            self.pane(),
            [wrapper, child],
            open_paths=lambda _pid: [],
            scrollback=lambda _pane_id: "",
            working_directory=lambda _pid: "/project",
            arguments=lambda pid: arguments[pid],
        )

        self.assertEqual(1, len(conversations))
        self.assertEqual("confirmed", conversations[0].conversation_id_status)
        self.assertEqual({"101", "102"}, set(conversations[0].process_instances))

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
        self.assertIsNone(conversations[0].working_directory)
        self.assertIn(
            "no process-associated working directory", conversations[0].evidence
        )

    def test_deleted_process_working_directory_is_unavailable(self):
        with patch.object(
            tmux_status.os, "readlink", return_value="/tmp/project (deleted)"
        ):
            with patch.object(tmux_status.os.path, "isdir", return_value=False):
                with patch.object(tmux_status.shutil, "which", return_value=None):
                    self.assertIsNone(tmux_status.process_working_directory(101))

    def test_linux_process_start_time_handles_spaces_in_comm(self):
        fields_after_comm = ["S"] + [str(field) for field in range(4, 23)]
        stat_text = "101 (codex worker) {}".format(" ".join(fields_after_comm))
        self.assertEqual("22", tmux_status.linux_process_start_time(stat_text))

    def test_non_proc_process_keys_require_subsecond_start_identity(self):
        pid = 987654
        self.instance_key_patch.stop()
        try:
            with patch.object(tmux_status.sys, "platform", "darwin"):
                with patch.object(
                    tmux_status,
                    "darwin_process_start_time",
                    return_value="1785783501:123456",
                ):
                    self.assertEqual(
                        "987654:darwin:1785783501:123456",
                        tmux_status.process_instance_key(pid),
                    )
                with patch.object(
                    tmux_status, "darwin_process_start_time", return_value=None
                ):
                    first = tmux_status.process_instance_key(pid)
                    second = tmux_status.process_instance_key(pid)
                    self.assertNotEqual(first, second)
                    self.assertIn(":unverified:", first)
        finally:
            self.instance_key_patch.start()

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
                session_roots={"grok": Path(directory) / "sessions"},
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
                session_roots={"codex": Path(directory) / "sessions"},
            )

        self.assertEqual(1, len(conversations))
        self.assertEqual("unknown", conversations[0].conversation_id_status)
        self.assertEqual("conflicting_evidence", conversations[0].identity_source)
        self.assertIsNone(conversations[0].conversation_id)

    def test_conflict_status_is_scoped_to_the_affected_invocation(self):
        file_id = "019fc5d1-40e4-75a2-89f2-188ae5efb2c4"
        cli_id = "019fb21f-84c9-7692-8371-1f9aa3e75401"
        pane = self.pane()
        conflicting = tmux_status.ProcessInfo(
            101, 100, 0.0, 1, "S", "0:01", "codex resume {}".format(cli_id)
        )
        unavailable = tmux_status.ProcessInfo(
            102, 100, 0.0, 1, "S", "0:01", "/usr/local/bin/codex"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rollout_dir = root / "sessions" / "2026" / "08" / "03"
            rollout_dir.mkdir(parents=True)
            rollout = rollout_dir / "rollout-{}.jsonl".format(file_id)
            rollout.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": file_id}})
                + "\n",
                encoding="utf-8",
            )
            conversations = tmux_status.collect_agent_conversations(
                pane,
                [conflicting, unavailable],
                open_paths=lambda pid: [rollout] if pid == conflicting.pid else [],
                scrollback=lambda _pane_id: "",
                working_directory=lambda _pid: "/tmp/project",
                arguments=lambda pid: (
                    ["codex", "resume", cli_id]
                    if pid == conflicting.pid
                    else ["/usr/local/bin/codex"]
                ),
                session_roots={"codex": root / "sessions"},
            )

        by_pid = {
            next(iter(conversation.process_instances)): conversation
            for conversation in conversations
        }
        self.assertEqual("conflicting_evidence", by_pid["101"].identity_source)
        self.assertEqual("unavailable", by_pid["102"].identity_source)
        self.assertNotIn("disagrees", by_pid["102"].evidence)

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

        self.assertEqual(2, len(conversations))
        self.assertTrue(
            all(
                conversation.conversation_id_status == "unknown"
                and len(conversation.process_instances) == 1
                and conversation.conversation_id is None
                for conversation in conversations
            )
        )

    def test_unknown_runtime_wrapper_and_native_child_stay_one_invocation(self):
        pane = self.pane()
        wrapper = tmux_status.ProcessInfo(
            101, 100, 0.0, 1, "S", "0:01", "python3.11 -m codex"
        )
        child = tmux_status.ProcessInfo(
            102, 101, 0.0, 1, "S", "0:01", "/usr/local/bin/codex"
        )
        conversations = tmux_status.collect_agent_conversations(
            pane,
            [wrapper, child],
            open_paths=lambda _pid: [],
            scrollback=lambda _pane_id: "",
            working_directory=lambda _pid: "/tmp/project",
        )

        self.assertEqual(1, len(conversations))
        self.assertEqual({"101", "102"}, set(conversations[0].process_instances))

    def test_revalidates_pane_after_process_snapshot_before_recovery(self):
        live_pane = self.pane()
        dead_pane = self.pane()
        dead_pane.pane_dead = True
        dead_pane.pane_dead_status = 0
        replacement = tmux_status.ProcessInfo(
            100, 1, 0.0, 1, "S", "0:01", "/usr/local/bin/codex"
        )
        args = tmux_status.build_parser().parse_args(["status", "--json"])
        with patch.object(
            tmux_status, "collect_panes", side_effect=[[live_pane], [dead_pane]]
        ) as collect_panes:
            with patch.object(
                tmux_status, "collect_processes", return_value={100: replacement}
            ):
                with patch.object(
                    tmux_status, "collect_agent_conversations"
                ) as collect_conversations:
                    with patch.object(tmux_status, "load_marks", return_value={}):
                        statuses = tmux_status.collect_statuses(
                            args, include_conversations=True
                        )

        self.assertEqual(2, collect_panes.call_count)
        collect_conversations.assert_not_called()
        self.assertTrue(statuses[0].dead)
        self.assertEqual([], statuses[0].agent_conversations)

    def test_refreshes_process_tree_for_a_replacement_pane(self):
        old_pane = self.pane()
        replacement_pane = self.pane()
        replacement_pane.pane_id = "%4"
        replacement = tmux_status.ProcessInfo(
            100, 1, 0.0, 1, "S", "0:01", "/usr/local/bin/codex"
        )
        args = tmux_status.build_parser().parse_args(["status", "--json"])
        with patch.object(
            tmux_status,
            "collect_panes",
            side_effect=[[old_pane], [replacement_pane], [replacement_pane]],
        ) as collect_panes:
            with patch.object(
                tmux_status, "collect_processes", return_value={100: replacement}
            ) as collect_processes:
                with patch.object(
                    tmux_status, "collect_agent_conversations", return_value=[]
                ) as collect_conversations:
                    with patch.object(tmux_status, "load_marks", return_value={}):
                        statuses = tmux_status.collect_statuses(
                            args, include_conversations=True
                        )

        self.assertEqual(3, collect_panes.call_count)
        self.assertEqual(2, collect_processes.call_count)
        collect_conversations.assert_called_once()
        self.assertEqual("%4", statuses[0].pane_id)
        self.assertEqual([], statuses[0].agent_conversations)

    def test_fails_recovery_when_pane_instances_never_stabilize(self):
        panes = []
        for pane_id in ("%3", "%4", "%5", "%6"):
            pane = self.pane()
            pane.pane_id = pane_id
            panes.append([pane])
        args = tmux_status.build_parser().parse_args(["status", "--json"])
        with patch.object(tmux_status, "collect_panes", side_effect=panes):
            with patch.object(tmux_status, "collect_processes", return_value={}):
                with self.assertRaisesRegex(
                    tmux_status.TmuxStatusError,
                    "changed repeatedly",
                ):
                    tmux_status.collect_statuses(args, include_conversations=True)

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

    def test_payload_rejects_surrogateescaped_paths(self):
        pane = self.pane("/tmp/project-\udcff")
        statuses = tmux_status.build_statuses(
            [pane],
            {},
            {},
            cpu_threshold=80.0,
            memory_threshold_mb=1024.0,
        )
        args = tmux_status.build_parser().parse_args(["snapshot"])
        with self.assertRaisesRegex(
            tmux_status.TmuxStatusError,
            "not valid UTF-8",
        ):
            tmux_status.status_payload(statuses, args, "snapshot")

    def test_markdown_code_uses_a_safe_backtick_delimiter(self):
        self.assertEqual("``a`b``", tmux_status.markdown_code("a`b"))
        self.assertEqual("`` `quoted` ``", tmux_status.markdown_code("`quoted`"))
        self.assertEqual("```a``b```", tmux_status.markdown_code("a``b"))

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
                        else:
                            self.assertEqual(
                                "{}:{}".format(
                                    conversation["tool"],
                                    conversation["conversation_id"],
                                ),
                                conversation["stable_mapping_key"],
                            )


if __name__ == "__main__":
    unittest.main()
