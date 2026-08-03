#!/usr/bin/env python3
"""Validate v3 relationships that JSON Schema cannot express."""

import json
import shlex
import sys
from pathlib import Path


RECOVERY_FIELDS = (
    "tool",
    "conversation_id",
    "conversation_id_status",
    "conversation_id_kind",
    "identity_source",
    "source_path",
    "stable_mapping_key",
    "process_instances",
    "working_directory",
    "resume_command",
)
MAX_PROCESS_PID = 2147483647
MAX_SAFE_INTEGER = 9007199254740991


def expected_resume(tool, conversation_id, cwd):
    if tool == "codex":
        return "codex resume -C {} {}".format(shlex.quote(cwd), conversation_id)
    return "grok --cwd {} --resume {}".format(shlex.quote(cwd), conversation_id)


def validate(payload):
    errors = []
    panes = payload.get("panes", [])
    recovery = payload.get("recovery", [])
    producer = payload.get("producer", {})
    if producer.get("version") != payload.get("tool_version"):
        errors.append("producer.version does not match tool_version")
    if payload.get("pane_count") != len(panes):
        errors.append("pane_count does not equal len(panes)")
    anomaly_count = sum(bool(pane.get("anomalies")) for pane in panes)
    if payload.get("anomaly_count") != anomaly_count:
        errors.append("anomaly_count does not equal anomalous pane count")
    confirmed_count = sum(
        entry.get("conversation_id_status") == "confirmed" for entry in recovery
    )
    unknown_count = sum(
        entry.get("conversation_id_status") == "unknown" for entry in recovery
    )
    if payload.get("confirmed_conversation_count") != confirmed_count:
        errors.append("confirmed_conversation_count does not match recovery")
    if payload.get("unknown_conversation_count") != unknown_count:
        errors.append("unknown_conversation_count does not match recovery")

    server_id = payload.get("server_instance_id")
    if panes and any(pane.get("server_instance_id") != server_id for pane in panes):
        errors.append("pane server_instance_id does not match producer")

    projected = []
    for pane in panes:
        if pane.get("session_created", 0) > MAX_SAFE_INTEGER:
            errors.append("session_created exceeds safe database integer range")
        legacy_aliases = (
            ("session", pane.get("tmux_session_name")),
            (
                "window",
                "{}:{}".format(
                    pane.get("tmux_window_index"), pane.get("tmux_window_name")
                ),
            ),
            ("pane", pane.get("pane_id")),
            ("target", pane.get("tmux_target")),
            ("pid", pane.get("pane_pid")),
            ("path", pane.get("working_directory")),
        )
        for legacy_field, v3_value in legacy_aliases:
            if pane.get(legacy_field) != v3_value:
                errors.append(
                    "{} does not match its v3 pane identity field".format(
                        legacy_field
                    )
                )
        expected_pane_instance_id = "{}:{}:{}:{}:{}:{}".format(
            pane.get("server_instance_id"),
            pane.get("session_id"),
            pane.get("session_created"),
            pane.get("window_id"),
            pane.get("pane_id"),
            pane.get("pane_pid"),
        )
        if pane.get("pane_instance_id") != expected_pane_instance_id:
            errors.append("pane_instance_id does not match pane identity fields")
        for conversation in pane.get("agent_conversations", []):
            for pid, instance_key in conversation.get(
                "process_instances", {}
            ).items():
                if int(pid) > MAX_PROCESS_PID:
                    errors.append(
                        "process_instances PID exceeds signed 32-bit range"
                    )
                if not instance_key.startswith(pid + ":"):
                    errors.append("process instance key does not match its PID")
            entry = {field: conversation[field] for field in RECOVERY_FIELDS}
            entry.update(
                tmux_target=pane["tmux_target"],
                tmux_session_name=pane["tmux_session_name"],
                pane_id=pane["pane_id"],
                pane_pid=pane["pane_pid"],
            )
            projected.append(entry)
            status = conversation.get("conversation_id_status")
            if status == "confirmed":
                tool = conversation.get("tool")
                conversation_id = conversation.get("conversation_id")
                cwd = conversation.get("working_directory")
                if conversation.get("stable_mapping_key") != "{}:{}".format(
                    tool, conversation_id
                ):
                    errors.append("stable_mapping_key does not match tool and ID")
                if not isinstance(cwd, str) or not cwd:
                    errors.append("confirmed working_directory must be nonempty")
                elif conversation.get("resume_command") != expected_resume(
                    tool, conversation_id, cwd
                ):
                    errors.append("resume_command does not match tool, ID, and cwd")
    if recovery != projected:
        errors.append("recovery is not the ordered pane conversation projection")
    return errors


def load_payload(path):
    if path == "-":
        return json.load(sys.stdin)
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(arguments):
    failed = False
    for path in arguments:
        errors = validate(load_payload(path))
        if errors:
            failed = True
            for error in errors:
                print("{}: {}".format(path, error), file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
