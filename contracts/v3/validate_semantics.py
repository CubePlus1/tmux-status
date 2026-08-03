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


def expected_resume(tool, conversation_id, cwd):
    if tool == "codex":
        return "codex resume -C {} {}".format(shlex.quote(cwd), conversation_id)
    return "grok --cwd {} --resume {}".format(shlex.quote(cwd), conversation_id)


def validate(payload):
    errors = []
    panes = payload.get("panes", [])
    recovery = payload.get("recovery", [])
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
        for conversation in pane.get("agent_conversations", []):
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
                if not isinstance(cwd, str) or conversation.get(
                    "resume_command"
                ) != expected_resume(tool, conversation_id, cwd):
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
