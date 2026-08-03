#!/usr/bin/env python3
"""Inspect tmux panes, their process trees, and persisted activity marks."""

import argparse
import ctypes
import ctypes.util
import json
import math
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

VERSION = "0.3.0"
FIELD_SEPARATOR = "\x1f"
TMUX_ESCAPED_FIELD_SEPARATOR = r"\037"
DEFAULT_CPU_THRESHOLD = 80.0
DEFAULT_MEMORY_THRESHOLD_MB = 1024.0
SHELL_NAMES = {
    "bash",
    "dash",
    "fish",
    "ksh",
    "sh",
    "tcsh",
    "zsh",
}
TOOL_NAMES = {
    "codex": ("codex", "codex-cli"),
    "grok": ("grok", "grok-cli"),
}
SESSION_FILE_NAMES = {
    "grok": {
        "events.jsonl",
        "updates.jsonl",
        "chat_history.jsonl",
        "summary.json",
        "signals.json",
    }
}
UUID_PATTERN = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
)
RUNTIME_NAMES = {
    "bun",
    "deno",
    "node",
    "nodejs",
    "python",
    "python2",
    "python3",
}
PYTHON_VALUE_OPTIONS = {"-W", "-X", "--check-hash-based-pycs"}
NODE_VALUE_OPTIONS = {
    "-C",
    "-r",
    "--conditions",
    "--diagnostic-dir",
    "--env-file",
    "--env-file-if-exists",
    "--experimental-loader",
    "--heapsnapshot-signal",
    "--icu-data-dir",
    "--import",
    "--inspect-port",
    "--loader",
    "--openssl-config",
    "--redirect-warnings",
    "--require",
    "--snapshot-blob",
    "--title",
}
ANSI = {
    "red": "\033[31m",
    "yellow": "\033[33m",
    "green": "\033[32m",
    "dim": "\033[2m",
    "bold": "\033[1m",
    "reset": "\033[0m",
}


class TmuxStatusError(RuntimeError):
    pass


@dataclass
class ProcessInfo:
    pid: int
    ppid: int
    cpu_percent: float
    rss_kb: int
    state: str
    elapsed: str
    command: str


@dataclass(frozen=True)
class OpenProcessFile:
    source_path: Path
    read_path: Path
    inode: Optional[int]


@dataclass
class PaneInfo:
    session: str
    session_attached: bool
    session_activity: int
    window_index: int
    window_name: str
    pane_index: int
    pane_id: str
    pane_pid: int
    current_command: str
    pane_active: bool
    pane_dead: bool
    pane_dead_status: Optional[int]
    current_path: str
    session_id: str = ""
    session_created: int = 0
    window_id: str = ""
    server_pid: int = 0
    server_started: int = 0

    @property
    def locator(self) -> str:
        return "{}:{}.{}".format(self.session, self.window_index, self.pane_index)


@dataclass
class AgentConversation:
    tool: str
    conversation_id: Optional[str]
    conversation_id_status: str
    conversation_id_kind: str
    identity_source: str
    source_path: Optional[str]
    working_directory: Optional[str]
    process_instances: Dict[str, str]
    stable_mapping_key: Optional[str]
    resume_command: Optional[str]
    evidence: str


@dataclass
class PaneStatus:
    session: str
    window: str
    pane: str
    target: str
    pid: int
    command: str
    path: str
    attached: bool
    selected: bool
    dead: bool
    cpu_percent: float
    memory_mb: float
    process_count: int
    tools: List[str]
    activity: str
    activity_source: str
    note: str
    anomalies: List[str]
    session_id: str
    session_created: int
    window_id: str
    server_instance_id: str
    tmux_target: str
    tmux_session_name: str
    tmux_window_index: int
    tmux_window_name: str
    tmux_pane_index: int
    pane_id: str
    pane_pid: int
    working_directory: str
    pane_instance_id: str
    agent_conversations: List[AgentConversation]


def config_path() -> Path:
    override = os.environ.get("TMUX_STATUS_MARKS_FILE")
    if override:
        return Path(override).expanduser()
    config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return base / "tmux-status" / "marks.json"


def run_command(args: Sequence[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            list(args),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        raise TmuxStatusError("{}: {}".format(args[0], exc)) from exc


PANE_FIELDS = (
    "#{session_name}",
    "#{session_attached}",
    "#{session_activity}",
    "#{window_index}",
    "#{window_name}",
    "#{pane_index}",
    "#{pane_id}",
    "#{pane_pid}",
    "#{pane_current_command}",
    "#{pane_active}",
    "#{pane_dead}",
    "#{pane_dead_status}",
    "#{pane_current_path}",
    "#{session_id}",
    "#{session_created}",
    "#{window_id}",
    "#{pid}",
    "#{start_time}",
)


def parse_panes_output(output: str) -> List[PaneInfo]:
    panes = []
    for line in output.splitlines():
        if not line:
            continue
        # tmux escapes the ASCII unit separator in format output as "\\037".
        values = line.split(TMUX_ESCAPED_FIELD_SEPARATOR)
        if len(values) == 1:
            values = line.split(FIELD_SEPARATOR)
        if len(values) != len(PANE_FIELDS):
            continue
        dead_status = int(values[11]) if values[11].strip() else None
        panes.append(
            PaneInfo(
                session=values[0],
                session_attached=values[1] == "1",
                session_activity=int(values[2] or 0),
                window_index=int(values[3]),
                window_name=values[4],
                pane_index=int(values[5]),
                pane_id=values[6],
                pane_pid=int(values[7]),
                current_command=values[8],
                pane_active=values[9] == "1",
                pane_dead=values[10] == "1",
                pane_dead_status=dead_status,
                current_path=values[12],
                session_id=values[13],
                session_created=int(values[14]),
                window_id=values[15],
                server_pid=int(values[16]),
                server_started=int(values[17]),
            )
        )
    return panes


def collect_panes() -> List[PaneInfo]:
    format_string = FIELD_SEPARATOR.join(PANE_FIELDS)
    result = run_command(["tmux", "list-panes", "-a", "-F", format_string])
    if result.returncode != 0:
        message = result.stderr.strip()
        if "no server running" in message or "error connecting to" in message:
            return []
        raise TmuxStatusError(message or "tmux list-panes failed")

    return parse_panes_output(result.stdout)


PS_LINE = re.compile(r"^\s*(\d+)\s+(\d+)\s+([\d.]+)\s+(\d+)\s+(\S+)\s+(\S+)\s*(.*)$")


def parse_ps_output(output: str) -> Dict[int, ProcessInfo]:
    processes = {}
    for line in output.splitlines():
        match = PS_LINE.match(line)
        if not match:
            continue
        pid, ppid, cpu, rss, state, elapsed, command = match.groups()
        process = ProcessInfo(
            pid=int(pid),
            ppid=int(ppid),
            cpu_percent=float(cpu),
            rss_kb=int(rss),
            state=state,
            elapsed=elapsed,
            command=command.strip(),
        )
        processes[process.pid] = process
    return processes


def collect_processes() -> Dict[int, ProcessInfo]:
    result = run_command(
        [
            "ps",
            "-axo",
            "pid=,ppid=,%cpu=,rss=,state=,etime=,command=",
        ]
    )
    if result.returncode != 0:
        raise TmuxStatusError(result.stderr.strip() or "ps failed")
    return parse_ps_output(result.stdout)


def descendants(root_pid: int, processes: Dict[int, ProcessInfo]) -> List[ProcessInfo]:
    children: Dict[int, List[int]] = {}
    for process in processes.values():
        children.setdefault(process.ppid, []).append(process.pid)

    found = []
    pending = [root_pid]
    seen: Set[int] = set()
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        process = processes.get(pid)
        if process is not None:
            found.append(process)
        pending.extend(children.get(pid, []))
    return found


def runtime_option_next_index(
    runtime_name: str, tokens: Sequence[str], index: int
) -> Optional[int]:
    token = tokens[index]
    if runtime_name.startswith("python"):
        options = PYTHON_VALUE_OPTIONS
        short_options = ("-W", "-X")
    elif runtime_name in ("node", "nodejs"):
        options = NODE_VALUE_OPTIONS
        short_options = ("-C", "-r")
    else:
        return None
    if token in options:
        return index + 2
    if any(token.startswith(option) and token != option for option in short_options):
        return index + 1
    if any(
        option.startswith("--") and token.startswith(option + "=")
        for option in options
    ):
        return index + 1
    return None


def executable_names_from_tokens(tokens: Sequence[str]) -> List[str]:
    if not tokens:
        return []

    index = 0
    first_name = os.path.basename(tokens[index]).lower().lstrip("-")
    if first_name == "env":
        index += 1
        while index < len(tokens):
            token = tokens[index]
            if token == "--":
                index += 1
                break
            if token in ("-u", "--unset", "-C", "--chdir", "-S", "--split-string"):
                index += 2
                continue
            if token.startswith("-") or "=" in token:
                index += 1
                continue
            break
        if index >= len(tokens):
            return []

    candidates = [tokens[index]]
    runtime_name = os.path.basename(tokens[index]).lower().lstrip("-")
    if runtime_name in RUNTIME_NAMES or re.fullmatch(r"python\d+\.\d+", runtime_name):
        index += 1
        while index < len(tokens):
            token = tokens[index]
            next_index = runtime_option_next_index(runtime_name, tokens, index)
            if next_index is not None:
                index = next_index
                continue
            if token == "-m" and index + 1 < len(tokens):
                candidates.append(tokens[index + 1])
                break
            if token in ("-c", "-e", "--eval", "-p", "--print"):
                break
            if not token.startswith("-"):
                candidates.append(token)
                break
            index += 1

    names = []
    for candidate in candidates:
        name = os.path.basename(candidate).lower().lstrip("-")
        names.append(re.sub(r"[^a-z0-9._-].*$", "", name))
    return names


def executable_names(command: str) -> List[str]:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = re.findall(r"[^\s\"']+", command)
    return executable_names_from_tokens(tokens)


def tool_for_process(process: ProcessInfo) -> Optional[str]:
    for name in executable_names(process.command):
        for tool, aliases in TOOL_NAMES.items():
            if name in aliases:
                return tool
        if re.fullmatch(r"codex(?:-cli)?", name):
            return "codex"
        if re.fullmatch(
            r"grok(?:-cli)?(?:-\d+(?:\.\d+)+(?:-[a-z0-9._-]+)*)?", name
        ):
            return "grok"
    return None


def tool_for_arguments(tokens: Optional[Sequence[str]]) -> Optional[str]:
    if tokens is None:
        return None
    for tool in sorted(TOOL_NAMES):
        if tool_arguments_from_tokens(tool, tokens) is not None:
            return tool
    return None


def detect_tools(
    processes: Iterable[ProcessInfo],
    arguments: Optional[Callable[[int], Optional[List[str]]]] = None,
) -> List[str]:
    argument_reader = arguments or process_arguments
    found: Set[str] = set()
    for process in processes:
        if "Z" in process.state.upper():
            continue
        lossless_arguments = argument_reader(process.pid)
        tool = (
            tool_for_arguments(lossless_arguments)
            if lossless_arguments is not None
            else tool_for_process(process)
        )
        if tool:
            found.add(tool)
    return sorted(found)


def executable_name_matches_tool(name: str, tool: str) -> bool:
    if name in TOOL_NAMES.get(tool, ()):
        return True
    if tool == "codex":
        return bool(re.fullmatch(r"codex(?:-cli)?", name))
    if tool == "grok":
        return bool(
            re.fullmatch(
                r"grok(?:-cli)?(?:-\d+(?:\.\d+)+(?:-[a-z0-9._-]+)*)?",
                name,
            )
        )
    return False


def is_runtime_name(name: str) -> bool:
    return name in RUNTIME_NAMES or bool(re.fullmatch(r"python\d+(?:\.\d+)+", name))


def is_runtime_wrapper_process(
    process: ProcessInfo,
    tool: str,
    arguments: Optional[Sequence[str]] = None,
) -> bool:
    names = (
        executable_names_from_tokens(arguments)
        if arguments is not None
        else executable_names(process.command)
    )
    return (
        len(names) > 1
        and is_runtime_name(names[0])
        and any(executable_name_matches_tool(name, tool) for name in names[1:])
    )


def is_native_tool_process(
    process: ProcessInfo,
    tool: str,
    arguments: Optional[Sequence[str]] = None,
) -> bool:
    names = (
        executable_names_from_tokens(arguments)
        if arguments is not None
        else executable_names(process.command)
    )
    return bool(names and executable_name_matches_tool(names[0], tool))


def is_runtime_wrapper_child_pair(
    left_pid: int,
    right_pid: int,
    tool: str,
    processes_by_pid: Dict[int, ProcessInfo],
    arguments_by_pid: Optional[Dict[int, Optional[List[str]]]] = None,
) -> bool:
    left = processes_by_pid.get(left_pid)
    right = processes_by_pid.get(right_pid)
    if left is None or right is None:
        return False
    left_arguments = (
        arguments_by_pid.get(left_pid) if arguments_by_pid is not None else None
    )
    right_arguments = (
        arguments_by_pid.get(right_pid) if arguments_by_pid is not None else None
    )
    if right.ppid == left.pid:
        return is_runtime_wrapper_process(
            left, tool, left_arguments
        ) and is_native_tool_process(
            right, tool, right_arguments
        )
    if left.ppid == right.pid:
        return is_runtime_wrapper_process(
            right, tool, right_arguments
        ) and is_native_tool_process(
            left, tool, left_arguments
        )
    return False


def validated_uuid(value: str) -> Optional[str]:
    value = value.strip().strip("'\"`.,;:()[]{}<>")
    if not UUID_PATTERN.fullmatch(value):
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def command_tokens(command: str) -> List[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return re.findall(r"[^\s\"']+", command)


def tool_arguments_from_tokens(
    tool: str, tokens: Sequence[str]
) -> Optional[List[str]]:
    """Return arguments after the actual tool executable, unwrapping known runtimes."""
    if not tokens:
        return None

    index = 0
    first_name = os.path.basename(tokens[index]).lower().lstrip("-")
    if first_name == "env":
        index += 1
        while index < len(tokens):
            token = tokens[index]
            if token == "--":
                index += 1
                break
            if token in ("-u", "--unset", "-C", "--chdir", "-S", "--split-string"):
                index += 2
                continue
            if token.startswith("-") or "=" in token:
                index += 1
                continue
            break
        if index >= len(tokens):
            return None

    executable_index = index
    runtime_name = os.path.basename(tokens[index]).lower().lstrip("-")
    if is_runtime_name(runtime_name):
        index += 1
        while index < len(tokens):
            token = tokens[index]
            next_index = runtime_option_next_index(runtime_name, tokens, index)
            if next_index is not None:
                index = next_index
                continue
            if token == "-m" and index + 1 < len(tokens):
                executable_index = index + 1
                break
            if token in ("-c", "-e", "--eval", "-p", "--print"):
                return None
            if not token.startswith("-"):
                executable_index = index
                break
            index += 1
        else:
            return None

    executable_name = os.path.basename(tokens[executable_index]).lower().lstrip("-")
    aliases = TOOL_NAMES.get(tool, ())
    if executable_name not in aliases:
        if tool == "codex" and not re.fullmatch(r"codex(?:-cli)?", executable_name):
            return None
        if tool == "grok" and not re.fullmatch(
            r"grok(?:-cli)?(?:-\d+(?:\.\d+)+(?:-[a-z0-9._-]+)*)?",
            executable_name,
        ):
            return None
    return list(tokens[executable_index + 1 :])


def tool_arguments(tool: str, command: str) -> Optional[List[str]]:
    return tool_arguments_from_tokens(tool, command_tokens(command))


def working_directory_from_arguments(
    tool: str, tokens: Sequence[str]
) -> Optional[str]:
    arguments = tool_arguments_from_tokens(tool, tokens)
    if arguments is None:
        return None
    options = ("-C", "--cd") if tool == "codex" else ("--cwd",)
    found = None
    for index, token in enumerate(arguments):
        for option in options:
            if token == option and index + 1 < len(arguments):
                found = arguments[index + 1]
            elif token.startswith(option + "="):
                found = token[len(option) + 1 :]
    return found or None


def working_directory_from_command(tool: str, command: str) -> Optional[str]:
    return working_directory_from_arguments(tool, command_tokens(command))


CODEX_VALUE_OPTIONS = {
    "-a",
    "--add-dir",
    "--ask-for-approval",
    "-c",
    "--cd",
    "--config",
    "-C",
    "--disable",
    "--enable",
    "--local-provider",
    "-m",
    "--model",
    "-p",
    "--profile",
    "--remote",
    "--remote-auth-token-env",
    "-s",
    "--sandbox",
}
CODEX_MULTI_VALUE_OPTIONS = {"-i", "--image"}


def codex_subcommand_arguments(arguments: List[str], subcommand: str) -> Optional[List[str]]:
    """Return subcommand argv only when it occupies Codex's command position."""
    index = 0
    while index < len(arguments):
        token = arguments[index]
        if token == "--":
            return None
        if token in CODEX_MULTI_VALUE_OPTIONS or any(
            token.startswith(option + "=") for option in CODEX_MULTI_VALUE_OPTIONS
        ):
            index += 1
            while index < len(arguments):
                candidate = arguments[index]
                if candidate == subcommand:
                    return arguments[index + 1 :]
                if candidate.startswith("-"):
                    break
                index += 1
            continue
        if token in CODEX_VALUE_OPTIONS:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        if token != subcommand:
            return None
        return arguments[index + 1 :]
    return None


def session_id_from_command(tool: str, command: str) -> Optional[Tuple[str, str]]:
    """Return an explicit UUID and its CLI evidence type, never a title or PID."""
    return session_id_from_arguments(tool, command_tokens(command))


def session_id_from_arguments(
    tool: str, tokens: Sequence[str]
) -> Optional[Tuple[str, str]]:
    """Return explicit CLI identity from an already-tokenized argv."""
    if not tokens:
        return None

    if tool == "grok":
        for index, token in enumerate(tokens):
            for option in ("--resume=", "-r=", "--session-id=", "-s="):
                if token.startswith(option):
                    session_id = validated_uuid(token[len(option) :])
                    if session_id:
                        source = (
                            "cli_resume_argument"
                            if "resume" in option or option.startswith("-r")
                            else "cli_session_id_argument"
                        )
                        return session_id, source
            if token in ("--resume", "-r", "--session-id", "-s"):
                if index + 1 < len(tokens):
                    session_id = validated_uuid(tokens[index + 1])
                    if session_id:
                        source = (
                            "cli_resume_argument"
                            if token in ("--resume", "-r")
                            else "cli_session_id_argument"
                        )
                        return session_id, source
        return None

    if tool != "codex":
        return None
    arguments = tool_arguments_from_tokens(tool, tokens)
    if arguments is None:
        return None
    resume_arguments = codex_subcommand_arguments(arguments, "resume")
    if resume_arguments is None:
        return None
    index = 0
    while index < len(resume_arguments):
        token = resume_arguments[index]
        if token in ("--last", "--all"):
            return None
        if token == "--":
            return None
        if token in CODEX_MULTI_VALUE_OPTIONS or any(
            token.startswith(option + "=") for option in CODEX_MULTI_VALUE_OPTIONS
        ):
            index += 1
            value_count = 0
            last_session_id = None
            while index < len(resume_arguments):
                candidate = resume_arguments[index]
                if candidate.startswith("-"):
                    break
                value_count += 1
                session_id = validated_uuid(candidate)
                if session_id:
                    last_session_id = session_id
                index += 1
            if index == len(resume_arguments) and value_count > 1 and last_session_id:
                return last_session_id, "cli_resume_argument"
            continue
        if token in CODEX_VALUE_OPTIONS:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        session_id = validated_uuid(token)
        return (session_id, "cli_resume_argument") if session_id else None
    return None


def configured_session_root(
    tool: str, process_environment: Optional[Dict[str, str]] = None
) -> Optional[Path]:
    environment_key = "CODEX_HOME" if tool == "codex" else "GROK_HOME"
    configured_home = (process_environment or {}).get(environment_key)
    if not configured_home:
        configured_home = os.environ.get(environment_key)
    if configured_home:
        data_root = Path(configured_home).expanduser()
    elif tool == "codex":
        data_root = Path.home() / ".codex"
    elif tool == "grok":
        data_root = Path.home() / ".grok"
    else:
        return None
    return data_root / "sessions"


def path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def session_metadata_from_open_file(
    tool: str,
    path: Union[Path, OpenProcessFile],
    session_roots: Optional[Dict[str, Path]] = None,
    process_environment: Optional[Dict[str, str]] = None,
) -> Optional[Tuple[str, Optional[str]]]:
    """Read only identity metadata from a session file opened by the process."""
    opened_file = (
        path
        if isinstance(path, OpenProcessFile)
        else OpenProcessFile(path, path, None)
    )
    source_path = opened_file.source_path
    session_root = (session_roots or {}).get(tool) or configured_session_root(
        tool, process_environment
    )
    if session_root is None or not path_is_within(source_path, session_root):
        return None
    if tool == "grok":
        if source_path.name not in SESSION_FILE_NAMES["grok"]:
            return None
        try:
            with opened_file.read_path.open("rb") as session_file:
                opened_stat = os.fstat(session_file.fileno())
                if (
                    opened_file.inode is not None
                    and opened_stat.st_ino != opened_file.inode
                ):
                    return None
                if os.fstat(session_file.fileno()).st_ino != opened_stat.st_ino:
                    return None
        except OSError:
            return None
        session_id = validated_uuid(source_path.parent.name)
        return (session_id, None) if session_id else None

    if tool != "codex":
        return None
    if not source_path.name.startswith("rollout-") or source_path.suffix != ".jsonl":
        return None
    try:
        with opened_file.read_path.open("r", encoding="utf-8") as session_file:
            opened_stat = os.fstat(session_file.fileno())
            if opened_file.inode is not None and opened_stat.st_ino != opened_file.inode:
                return None
            first_line = session_file.readline()
            if os.fstat(session_file.fileno()).st_ino != opened_stat.st_ino:
                return None
        event = json.loads(first_line)
    except (OSError, ValueError):
        return None
    if event.get("type") != "session_meta" or not isinstance(event.get("payload"), dict):
        return None
    payload = event["payload"]
    for key in ("session_id", "id"):
        value = payload.get(key)
        if isinstance(value, str):
            session_id = validated_uuid(value)
            if session_id:
                cwd = payload.get("cwd")
                return session_id, cwd if isinstance(cwd, str) and cwd else None
    return None


def session_id_from_open_file(
    tool: str,
    path: Path,
    session_roots: Optional[Dict[str, Path]] = None,
) -> Optional[str]:
    metadata = session_metadata_from_open_file(tool, path, session_roots)
    return metadata[0] if metadata else None


def list_open_paths(pid: int) -> List[OpenProcessFile]:
    proc_directory = Path("/proc") / str(pid) / "fd"
    if proc_directory.is_dir():
        paths = []
        try:
            for descriptor in proc_directory.iterdir():
                opened_file = capture_open_descriptor(descriptor)
                if opened_file is not None:
                    paths.append(opened_file)
        except OSError:
            return []
        return paths

    if not shutil.which("lsof"):
        return []
    result = run_command(["lsof", "-Ffin", "-p", str(pid)])
    paths = []
    descriptor_inode = None
    for line in result.stdout.splitlines():
        if line.startswith("f"):
            descriptor_inode = None
            continue
        if line.startswith("i"):
            try:
                descriptor_inode = int(line[1:])
            except ValueError:
                descriptor_inode = None
            continue
        if not line.startswith("n/"):
            continue
        path = Path(line[1:])
        if path.is_absolute():
            paths.append(OpenProcessFile(path, path, descriptor_inode))
    return paths


def capture_open_descriptor(descriptor: Path) -> Optional[OpenProcessFile]:
    try:
        first_target = Path(os.readlink(str(descriptor)))
        first_stat = descriptor.stat()
        second_target = Path(os.readlink(str(descriptor)))
        second_stat = descriptor.stat()
    except OSError:
        return None
    if (
        first_target != second_target
        or first_stat.st_ino != second_stat.st_ino
        or first_stat.st_dev != second_stat.st_dev
        or not first_target.is_absolute()
    ):
        return None
    return OpenProcessFile(first_target, descriptor, first_stat.st_ino)


def process_working_directory(pid: int) -> Optional[str]:
    proc_cwd = Path("/proc") / str(pid) / "cwd"
    try:
        target = os.readlink(str(proc_cwd))
        if os.path.isabs(target) and os.path.isdir(target):
            return os.path.normpath(target)
    except OSError:
        pass

    if not shutil.which("lsof"):
        return None
    result = run_command(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"])
    for line in result.stdout.splitlines():
        if line.startswith("n/"):
            target = os.path.normpath(line[1:])
            return target if os.path.isdir(target) else None
    return None


def process_arguments(pid: int) -> Optional[List[str]]:
    cmdline_path = Path("/proc") / str(pid) / "cmdline"
    try:
        raw_arguments = cmdline_path.read_bytes().split(b"\0")
    except OSError:
        return None
    arguments = [os.fsdecode(argument) for argument in raw_arguments if argument]
    return arguments or None


def process_agent_home_environment(pid: int) -> Dict[str, str]:
    """Read only agent data-root variables, never retain unrelated process secrets."""
    environ_path = Path("/proc") / str(pid) / "environ"
    try:
        entries = environ_path.read_bytes().split(b"\0")
    except OSError:
        return {}
    selected = {}
    for entry in entries:
        for key in ("CODEX_HOME", "GROK_HOME"):
            prefix = (key + "=").encode()
            if entry.startswith(prefix):
                selected[key] = os.fsdecode(entry[len(prefix) :])
    return selected


def linux_process_start_time(stat_text: str) -> Optional[str]:
    comm_end = stat_text.rfind(")")
    if comm_end < 0:
        return None
    fields_after_comm = stat_text[comm_end + 1 :].split()
    return fields_after_comm[19] if len(fields_after_comm) > 19 else None


class DarwinProcessInfo(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint32),
        ("status", ctypes.c_uint32),
        ("xstatus", ctypes.c_uint32),
        ("pid", ctypes.c_uint32),
        ("ppid", ctypes.c_uint32),
        ("uid", ctypes.c_uint32),
        ("gid", ctypes.c_uint32),
        ("ruid", ctypes.c_uint32),
        ("rgid", ctypes.c_uint32),
        ("svuid", ctypes.c_uint32),
        ("svgid", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32),
        ("comm", ctypes.c_char * 16),
        ("name", ctypes.c_char * 32),
        ("nfiles", ctypes.c_uint32),
        ("pgid", ctypes.c_uint32),
        ("pjobc", ctypes.c_uint32),
        ("tdev", ctypes.c_uint32),
        ("tpgid", ctypes.c_uint32),
        ("nice", ctypes.c_int32),
        ("start_seconds", ctypes.c_uint64),
        ("start_microseconds", ctypes.c_uint64),
    ]


def darwin_process_start_time(pid: int) -> Optional[str]:
    library_path = ctypes.util.find_library("proc")
    if not library_path:
        return None
    try:
        libproc = ctypes.CDLL(library_path)
        process_info = DarwinProcessInfo()
        written = libproc.proc_pidinfo(
            pid,
            3,  # PROC_PIDTBSDINFO
            0,
            ctypes.byref(process_info),
            ctypes.sizeof(process_info),
        )
    except (AttributeError, OSError):
        return None
    if written != ctypes.sizeof(process_info) or process_info.start_seconds <= 0:
        return None
    return "{}:{}".format(
        process_info.start_seconds, process_info.start_microseconds
    )


def process_instance_key(pid: int) -> str:
    proc_stat = Path("/proc") / str(pid) / "stat"
    boot_id_path = Path("/proc/sys/kernel/random/boot_id")
    try:
        start_time = linux_process_start_time(proc_stat.read_text(encoding="utf-8"))
        boot_id = boot_id_path.read_text(encoding="utf-8").strip()
        if start_time and boot_id:
            return "{}:{}:{}".format(pid, boot_id, start_time)
    except OSError:
        pass

    if sys.platform == "darwin":
        started_at = darwin_process_start_time(pid)
        if started_at:
            return "{}:darwin:{}".format(pid, started_at)
    return "{}:unverified:{}".format(pid, uuid.uuid4())


def resolve_working_directory(value: Optional[str], fallback: str) -> str:
    if not value:
        return fallback
    expanded = os.path.expanduser(value)
    if os.path.isabs(expanded):
        return os.path.normpath(expanded)
    return os.path.normpath(os.path.join(fallback, expanded))


def capture_pane_scrollback(pane_id: str) -> str:
    result = run_command(
        ["tmux", "capture-pane", "-p", "-J", "-t", pane_id, "-S", "-300"]
    )
    return result.stdout if result.returncode == 0 else ""


def session_ids_from_scrollback(tool: str, scrollback: str) -> List[str]:
    found: Set[str] = set()
    for raw_line in scrollback.splitlines():
        line = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", raw_line)
        match = re.search(r"\b{}\b".format(re.escape(tool)), line, re.IGNORECASE)
        if not match:
            continue
        parsed = session_id_from_command(tool, line[match.start() :])
        if parsed:
            found.add(parsed[0])
    return sorted(found)


def resume_command(tool: str, conversation_id: str, cwd: str) -> str:
    if tool == "codex":
        return "codex resume -C {} {}".format(
            shlex.quote(cwd), shlex.quote(conversation_id)
        )
    return "grok --cwd {} --resume {}".format(
        shlex.quote(cwd), shlex.quote(conversation_id)
    )


def conversation_kind(tool: str) -> str:
    return "codex_thread_id" if tool == "codex" else "grok_session_id"


def confirmed_conversation(
    tool: str,
    conversation_id: str,
    source: str,
    process_pids: List[int],
    process_instance_keys: List[str],
    cwd: str,
    source_path: Optional[str] = None,
) -> AgentConversation:
    processes = sorted(zip(process_pids, process_instance_keys))
    return AgentConversation(
        tool=tool,
        conversation_id=conversation_id,
        conversation_id_status="confirmed",
        conversation_id_kind=conversation_kind(tool),
        identity_source=source,
        source_path=source_path,
        working_directory=cwd,
        process_instances={str(pid): key for pid, key in processes},
        stable_mapping_key="{}:{}".format(tool, conversation_id),
        resume_command=resume_command(tool, conversation_id, cwd),
        evidence="explicit UUID from {}".format(source),
    )


def unknown_conversation(
    tool: str,
    process_pids: List[int],
    process_instance_keys: List[str],
    evidence: str,
    cwd: Optional[str],
    identity_source: str = "unavailable",
) -> AgentConversation:
    processes = sorted(zip(process_pids, process_instance_keys))
    return AgentConversation(
        tool=tool,
        conversation_id=None,
        conversation_id_status="unknown",
        conversation_id_kind=conversation_kind(tool),
        identity_source=identity_source,
        source_path=None,
        working_directory=cwd,
        process_instances={str(pid): key for pid, key in processes},
        stable_mapping_key=None,
        resume_command=None,
        evidence=evidence,
    )


def append_unknown_conversations(
    conversations: List[AgentConversation],
    tool: str,
    process_pids: List[int],
    process_cwds: Dict[int, Optional[str]],
    process_keys: Dict[int, str],
    processes_by_pid: Dict[int, ProcessInfo],
    evidence: str,
    identity_source: str = "unavailable",
    conflicting_process_pids: Optional[Set[int]] = None,
    unavailable_evidence: Optional[str] = None,
    arguments_by_pid: Optional[Dict[int, Optional[List[str]]]] = None,
) -> None:
    remaining = set(process_pids)
    invocation_groups = []
    while remaining:
        group = {min(remaining)}
        changed = True
        while changed:
            changed = False
            for candidate in sorted(remaining - group):
                if any(
                    is_runtime_wrapper_child_pair(
                        candidate,
                        member,
                        tool,
                        processes_by_pid,
                        arguments_by_pid,
                    )
                    for member in group
                ):
                    group.add(candidate)
                    changed = True
        remaining -= group
        invocation_groups.append(sorted(group))
    for group_pids in invocation_groups:
        group_has_conflict = bool(
            set(group_pids) & (conflicting_process_pids or set())
        )
        native_pids = [
            pid
            for pid in group_pids
            if is_native_tool_process(
                processes_by_pid[pid],
                tool,
                arguments_by_pid.get(pid) if arguments_by_pid is not None else None,
            )
        ]
        cwd = process_cwds[native_pids[0] if native_pids else group_pids[0]]
        conversations.append(
            unknown_conversation(
                tool,
                group_pids,
                [process_keys[pid] for pid in group_pids],
                evidence if group_has_conflict else unavailable_evidence or evidence,
                cwd,
                "conflicting_evidence" if group_has_conflict else identity_source,
            )
        )


def collect_agent_conversations(
    pane: PaneInfo,
    tree: List[ProcessInfo],
    open_paths: Callable[[int], List[Union[Path, OpenProcessFile]]] = list_open_paths,
    scrollback: Callable[[str], str] = capture_pane_scrollback,
    working_directory: Callable[[int], Optional[str]] = process_working_directory,
    arguments: Callable[[int], Optional[List[str]]] = process_arguments,
    instance_key: Optional[Callable[[int], str]] = None,
    session_roots: Optional[Dict[str, Path]] = None,
    environment: Callable[[int], Dict[str, str]] = process_agent_home_environment,
) -> List[AgentConversation]:
    instance_key = instance_key or process_instance_key
    tool_processes: Dict[str, List[ProcessInfo]] = {}
    observed_arguments: Dict[int, Optional[List[str]]] = {}
    for process in tree:
        if "Z" in process.state.upper():
            continue
        lossless_arguments = arguments(process.pid)
        observed_arguments[process.pid] = lossless_arguments
        tool = (
            tool_for_arguments(lossless_arguments)
            if lossless_arguments is not None
            else tool_for_process(process)
        )
        if tool:
            tool_processes.setdefault(tool, []).append(process)

    conversations = []
    pane_scrollback: Optional[str] = None
    processes_by_pid = {process.pid: process for process in tree}
    for tool, matching_processes in sorted(tool_processes.items()):
        process_cwds = {}
        process_cwds_confirmed = {}
        process_argvs = {}
        process_environments = {}
        process_keys = {}
        for process in matching_processes:
            process_keys[process.pid] = instance_key(process.pid)
            observed_cwd = working_directory(process.pid)
            lossless_arguments = observed_arguments[process.pid]
            process_argvs[process.pid] = lossless_arguments
            process_environments[process.pid] = environment(process.pid)
            command_cwd = (
                working_directory_from_arguments(tool, lossless_arguments)
                if lossless_arguments is not None
                else None
            )
            absolute_command_cwd = (
                os.path.normpath(os.path.expanduser(command_cwd))
                if command_cwd and os.path.isabs(os.path.expanduser(command_cwd))
                else None
            )
            process_cwds[process.pid] = (
                os.path.normpath(observed_cwd)
                if observed_cwd
                else absolute_command_cwd
            )
            process_cwds_confirmed[process.pid] = bool(
                observed_cwd or absolute_command_cwd
            )

        confirmed: Dict[Tuple[str, Optional[str]], dict] = {}
        conflicts = []
        unavailable_reasons = []
        unresolved_pids = []
        conflicting_pids = []
        unavailable_pids = []
        for process in matching_processes:
            command_evidence = (
                session_id_from_arguments(tool, process_argvs[process.pid])
                if process_argvs[process.pid] is not None
                else session_id_from_command(tool, process.command)
            )
            file_evidence: Dict[str, Tuple[str, Optional[str]]] = {}
            for path in open_paths(process.pid):
                metadata = session_metadata_from_open_file(
                    tool,
                    path,
                    session_roots,
                    process_environments[process.pid],
                )
                if metadata:
                    session_id, metadata_cwd = metadata
                    source_path = (
                        path.source_path
                        if isinstance(path, OpenProcessFile)
                        else path
                    )
                    file_evidence[session_id] = (str(source_path), metadata_cwd)
            ending_arguments = arguments(process.pid)
            ending_instance_key = instance_key(process.pid)
            process_still_matches_tool = (
                ending_arguments is None
                or tool_arguments_from_tokens(tool, ending_arguments) is not None
            )
            if (
                ending_instance_key != process_keys[process.pid]
                or ending_arguments != process_argvs[process.pid]
                or not process_still_matches_tool
            ):
                unavailable_reasons.append(
                    "PID {} changed incarnation or command during evidence collection".format(
                        process.pid
                    )
                )
                unavailable_pids.append(process.pid)
                process_keys[process.pid] = "{}:collection-race:{}".format(
                    process.pid, uuid.uuid4()
                )
                continue
            if len(file_evidence) == 1:
                session_id, (source_path, metadata_cwd) = next(
                    iter(file_evidence.items())
                )
                if command_evidence and command_evidence[0] != session_id:
                    conflicts.append(
                        "PID {} file UUID disagrees with explicit CLI UUID".format(
                            process.pid
                        )
                    )
                    conflicting_pids.append(process.pid)
                    continue
                if process_cwds_confirmed[process.pid]:
                    cwd = process_cwds[process.pid]
                elif metadata_cwd:
                    cwd = resolve_working_directory(metadata_cwd, pane.current_path)
                else:
                    unavailable_reasons.append(
                        "PID {} has a session UUID but no process-associated working directory".format(
                            process.pid
                        )
                    )
                    unavailable_pids.append(process.pid)
                    continue
                entry = confirmed.setdefault(
                    (session_id, cwd),
                    {
                        "pids": [],
                        "source": "open_session_file",
                        "path": source_path,
                        "cwd": cwd,
                    },
                )
                entry["pids"].append(process.pid)
                entry["source"] = "open_session_file"
                entry["path"] = source_path
                continue
            if len(file_evidence) > 1:
                conflicts.append(
                    "PID {} opened multiple {} session files".format(process.pid, tool)
                )
                conflicting_pids.append(process.pid)
                continue

            if command_evidence:
                if not process_cwds_confirmed[process.pid]:
                    if is_runtime_wrapper_process(
                        process, tool, process_argvs[process.pid]
                    ):
                        session_id, source = command_evidence
                        entry = confirmed.setdefault(
                            (session_id, None),
                            {
                                "pids": [],
                                "source": source,
                                "path": None,
                                "cwd": None,
                            },
                        )
                        entry["pids"].append(process.pid)
                        continue
                    unavailable_reasons.append(
                        "PID {} has a CLI UUID but no process-associated working directory".format(
                            process.pid
                        )
                    )
                    unavailable_pids.append(process.pid)
                    continue
                session_id, source = command_evidence
                cwd = process_cwds[process.pid]
                entry = confirmed.setdefault(
                    (session_id, cwd),
                    {"pids": [], "source": source, "path": None, "cwd": cwd},
                )
                entry["pids"].append(process.pid)
                continue
            unresolved_pids.append(process.pid)

        confirmed_by_pid = {
            pid: key
            for key, entry in confirmed.items()
            for pid in entry["pids"]
        }
        conflicting_pairs = []
        wrapper_merges = []
        confirmed_items = sorted(confirmed_by_pid.items())
        for index, (left_pid, left_key) in enumerate(confirmed_items):
            for right_pid, right_key in confirmed_items[index + 1 :]:
                if left_key == right_key:
                    continue
                if is_runtime_wrapper_child_pair(
                    left_pid,
                    right_pid,
                    tool,
                    processes_by_pid,
                    process_argvs,
                ):
                    if left_key[0] != right_key[0]:
                        conflicting_pairs.append((left_pid, right_pid))
                        conflicts.append(
                            "runtime wrapper PID {} and native child PID {} disagree on session identity".format(
                                left_pid, right_pid
                            )
                        )
                    elif is_runtime_wrapper_process(
                        processes_by_pid[left_pid],
                        tool,
                        process_argvs[left_pid],
                    ):
                        wrapper_merges.append((left_pid, left_key, right_key))
                    else:
                        wrapper_merges.append((right_pid, right_key, left_key))
        conflicting_pair_pids = {
            pid for pair in conflicting_pairs for pid in pair
        }
        for pid in conflicting_pair_pids:
            key = confirmed_by_pid[pid]
            if key not in confirmed:
                continue
            confirmed[key]["pids"].remove(pid)
            conflicting_pids.append(pid)
            if not confirmed[key]["pids"]:
                del confirmed[key]
        for wrapper_pid, wrapper_key, child_key in wrapper_merges:
            if wrapper_pid in conflicting_pair_pids:
                continue
            if wrapper_key not in confirmed or child_key not in confirmed:
                continue
            if wrapper_pid not in confirmed[wrapper_key]["pids"]:
                continue
            confirmed[wrapper_key]["pids"].remove(wrapper_pid)
            if wrapper_pid not in confirmed[child_key]["pids"]:
                confirmed[child_key]["pids"].append(wrapper_pid)
            if not confirmed[wrapper_key]["pids"]:
                del confirmed[wrapper_key]

        remaining_unresolved = []
        for unresolved_pid in unresolved_pids:
            related_entries = []
            for key, entry in confirmed.items():
                for confirmed_pid in entry["pids"]:
                    if is_runtime_wrapper_child_pair(
                        unresolved_pid,
                        confirmed_pid,
                        tool,
                        processes_by_pid,
                        process_argvs,
                    ):
                        related_entries.append(key)
                    if key in related_entries:
                        break
            unique_entries = set(related_entries)
            if len(unique_entries) == 1:
                confirmed[next(iter(unique_entries))]["pids"].append(unresolved_pid)
            else:
                remaining_unresolved.append(unresolved_pid)
        unresolved_pids = remaining_unresolved

        for key in [key for key in confirmed if key[1] is None]:
            entry = confirmed.pop(key)
            unavailable_pids.extend(entry["pids"])
            unavailable_reasons.append(
                "runtime wrapper has a session UUID but no recoverable child cwd"
            )

        if confirmed:
            for (session_id, _cwd), evidence in sorted(confirmed.items()):
                conversations.append(
                    confirmed_conversation(
                        tool,
                        session_id,
                        evidence["source"],
                        evidence["pids"],
                        [process_keys[pid] for pid in evidence["pids"]],
                        evidence["cwd"],
                        evidence["path"],
                    )
                )
            if conflicts or unavailable_reasons or unresolved_pids:
                evidence_parts = list(conflicts) + unavailable_reasons
                unavailable_parts = list(unavailable_reasons)
                if unresolved_pids:
                    unresolved_reason = (
                        "no explicit UUID found for {} process(es)".format(
                            len(unresolved_pids)
                        )
                    )
                    evidence_parts.append(unresolved_reason)
                    unavailable_parts.append(unresolved_reason)
                append_unknown_conversations(
                    conversations,
                    tool,
                    sorted(
                        set(conflicting_pids + unavailable_pids + unresolved_pids)
                    ),
                    process_cwds,
                    process_keys,
                    processes_by_pid,
                    "; ".join(conflicts) or "; ".join(evidence_parts),
                    "unavailable",
                    set(conflicting_pids),
                    "; ".join(unavailable_parts)
                    or "no explicit UUID found for this process",
                    arguments_by_pid=process_argvs,
                )
            continue

        if conflicts or unavailable_reasons:
            if unresolved_pids:
                unavailable_reasons.append(
                    "no explicit UUID found for {} process(es)".format(
                        len(unresolved_pids)
                    )
                )
            append_unknown_conversations(
                conversations,
                tool,
                sorted(set(conflicting_pids + unavailable_pids + unresolved_pids)),
                process_cwds,
                process_keys,
                processes_by_pid,
                "; ".join(conflicts) or "; ".join(unavailable_reasons),
                "unavailable",
                set(conflicting_pids),
                "; ".join(unavailable_reasons)
                or "no explicit UUID found for this process",
                arguments_by_pid=process_argvs,
            )
            continue

        process_pids = [process.pid for process in matching_processes]
        if len(matching_processes) != 1:
            append_unknown_conversations(
                conversations,
                tool,
                process_pids,
                process_cwds,
                process_keys,
                processes_by_pid,
                "cannot associate one scrollback UUID with multiple tool processes",
                arguments_by_pid=process_argvs,
            )
            continue

        if pane_scrollback is None:
            pane_scrollback = scrollback(pane.pane_id)
        scrollback_ids = session_ids_from_scrollback(tool, pane_scrollback)
        if len(scrollback_ids) == 1:
            append_unknown_conversations(
                conversations,
                tool,
                process_pids,
                process_cwds,
                process_keys,
                processes_by_pid,
                "one resume UUID exists in scrollback but cannot be associated with the live process",
                arguments_by_pid=process_argvs,
            )
        elif len(scrollback_ids) > 1:
            append_unknown_conversations(
                conversations,
                tool,
                process_pids,
                process_cwds,
                process_keys,
                processes_by_pid,
                "multiple distinct resume UUIDs found in tmux scrollback",
                "conflicting_evidence",
                arguments_by_pid=process_argvs,
            )
        else:
            append_unknown_conversations(
                conversations,
                tool,
                process_pids,
                process_cwds,
                process_keys,
                processes_by_pid,
                "no explicit UUID found in open session files, CLI arguments, or tmux scrollback",
                arguments_by_pid=process_argvs,
            )
    return conversations


def pane_instance_id(pane: PaneInfo) -> str:
    return ":".join(
        (
            server_instance_id(pane),
            pane.session_id,
            str(pane.session_created),
            pane.window_id,
            pane.pane_id,
            str(pane.pane_pid),
        )
    )


def server_instance_id(pane: PaneInfo) -> str:
    return "{}:{}".format(pane.server_pid, pane.server_started)


def load_marks(path: Optional[Path] = None) -> Dict[str, dict]:
    path = path or config_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TmuxStatusError("cannot read marks file {}: {}".format(path, exc))
    if not isinstance(data, dict):
        raise TmuxStatusError("invalid marks file: {}".format(path))
    marks = data.get("marks", {})
    if not isinstance(marks, dict):
        raise TmuxStatusError("invalid marks file: {}".format(path))
    for target, mark in marks.items():
        if (
            not isinstance(target, str)
            or not isinstance(mark, dict)
            or mark.get("state") not in ("active", "inactive")
            or not isinstance(mark.get("note", ""), str)
        ):
            raise TmuxStatusError("invalid mark for {!r} in {}".format(target, path))
    return marks


def save_marks(marks: Dict[str, dict], path: Optional[Path] = None) -> None:
    path = path or config_path()
    temporary_name = ""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "marks": marks,
        }
        handle, temporary_name = tempfile.mkstemp(
            prefix=".marks-", suffix=".json", dir=str(path.parent)
        )
        with os.fdopen(handle, "w", encoding="utf-8") as temporary:
            json.dump(payload, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
        os.replace(temporary_name, path)
    except OSError as exc:
        raise TmuxStatusError("cannot write marks file {}: {}".format(path, exc))
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)


def matching_mark(pane: PaneInfo, marks: Dict[str, dict]) -> Tuple[Optional[dict], str]:
    for target in (pane.pane_id, pane.locator, pane.session):
        if target in marks:
            return marks[target], target
    return None, ""


def automatic_activity(
    pane: PaneInfo, tree: List[ProcessInfo], tools: List[str]
) -> str:
    if pane.pane_dead:
        return "dead"
    if tools:
        return "active"
    non_shell = []
    for process in tree:
        executable = process.command.split(None, 1)[0] if process.command else ""
        name = os.path.basename(executable).lower().lstrip("-")
        if process.pid != pane.pane_pid and name not in SHELL_NAMES:
            non_shell.append(process)
    if any(process.cpu_percent >= 0.5 for process in tree) or non_shell:
        return "active"
    if pane.session_attached and pane.pane_active:
        return "active"
    return "idle"


def build_statuses(
    panes: List[PaneInfo],
    processes: Dict[int, ProcessInfo],
    marks: Dict[str, dict],
    cpu_threshold: float,
    memory_threshold_mb: float,
    conversation_collector: Optional[
        Callable[[PaneInfo, List[ProcessInfo]], List[AgentConversation]]
    ] = None,
) -> List[PaneStatus]:
    statuses = []
    for pane in panes:
        tree = [] if pane.pane_dead else descendants(pane.pane_pid, processes)
        cpu = sum(process.cpu_percent for process in tree)
        memory_mb = sum(process.rss_kb for process in tree) / 1024.0
        tools = detect_tools(tree)
        anomalies = []
        if cpu >= cpu_threshold:
            anomalies.append("CPU")
        if memory_mb >= memory_threshold_mb:
            anomalies.append("MEM")
        if pane.pane_dead:
            anomalies.append("DEAD")

        mark, mark_target = matching_mark(pane, marks)
        if mark:
            activity = str(mark.get("state", "active"))
            source = "manual:{}".format(mark_target)
            note = str(mark.get("note", ""))
        else:
            activity = automatic_activity(pane, tree, tools)
            source = "auto"
            note = ""
        agent_conversations = (
            conversation_collector(pane, tree)
            if conversation_collector and not pane.pane_dead
            else []
        )

        statuses.append(
            PaneStatus(
                session=pane.session,
                window="{}:{}".format(pane.window_index, pane.window_name),
                pane=pane.pane_id,
                target=pane.locator,
                pid=pane.pane_pid,
                command=pane.current_command,
                path=pane.current_path,
                attached=pane.session_attached,
                selected=pane.pane_active,
                dead=pane.pane_dead,
                cpu_percent=round(cpu, 1),
                memory_mb=round(memory_mb, 1),
                process_count=len(tree),
                tools=tools,
                activity=activity,
                activity_source=source,
                note=note,
                anomalies=anomalies,
                session_id=pane.session_id,
                session_created=pane.session_created,
                window_id=pane.window_id,
                server_instance_id=server_instance_id(pane),
                tmux_target=pane.locator,
                tmux_session_name=pane.session,
                tmux_window_index=pane.window_index,
                tmux_window_name=pane.window_name,
                tmux_pane_index=pane.pane_index,
                pane_id=pane.pane_id,
                pane_pid=pane.pane_pid,
                working_directory=pane.current_path,
                pane_instance_id=pane_instance_id(pane),
                agent_conversations=agent_conversations,
            )
        )
    return statuses


def color(text: str, name: str, enabled: bool) -> str:
    if not enabled:
        return text
    return "{}{}{}".format(ANSI[name], text, ANSI["reset"])


def truncate(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return text[:1]
    return text[: width - 1] + "…"


def render_table(statuses: List[PaneStatus], use_color: bool) -> str:
    if not statuses:
        return color("No tmux server or panes found.", "dim", use_color)

    terminal_width = shutil.get_terminal_size((120, 24)).columns
    fixed_width = 69
    session_width = max(8, min(18, terminal_width - fixed_width))
    command_width = max(8, terminal_width - fixed_width - session_width)
    header = ("{:<8} {:>7} {:>9} {:<10} {:<11} {:<{sw}} {:<8} {:<{cw}}").format(
        "HEALTH",
        "CPU%",
        "MEM",
        "ACTIVITY",
        "TOOL",
        "SESSION",
        "PANE",
        "COMMAND",
        sw=session_width,
        cw=command_width,
    )
    lines = [color(header, "bold", use_color)]
    for status in statuses:
        health = "!{}".format("+".join(status.anomalies)) if status.anomalies else "ok"
        health_color = "red" if status.anomalies else "green"
        activity = status.activity + ("*" if status.activity_source != "auto" else "")
        activity_color = (
            "green"
            if status.activity == "active"
            else "yellow"
            if status.activity == "idle"
            else "red"
        )
        tool = ",".join(status.tools) or "-"
        session = status.session + ("+" if status.attached else "")
        row = (
            "{:<8} {:>7.1f} {:>7.1f}MB {:<10} {:<11} {:<{sw}} {:<8} {:<{cw}}"
        ).format(
            health,
            status.cpu_percent,
            status.memory_mb,
            activity,
            tool,
            truncate(session, session_width),
            status.pane,
            truncate(status.command, command_width),
            sw=session_width,
            cw=command_width,
        )
        if use_color:
            row = row.replace(health, color(health, health_color, True), 1)
            row = row.replace(activity, color(activity, activity_color, True), 1)
            if tool != "-":
                row = row.replace(tool, color(tool, "green", True), 1)
        lines.append(row.rstrip())
        if status.note:
            lines.append(
                color(
                    "  note {}: {}".format(status.target, status.note),
                    "dim",
                    use_color,
                )
            )
    lines.append(
        color(
            "* manual mark; + attached session; resources include pane descendants",
            "dim",
            use_color,
        )
    )
    return "\n".join(lines)


def collect_statuses(
    args: argparse.Namespace, *, include_conversations: bool
) -> List[PaneStatus]:
    panes = collect_panes()
    processes = collect_processes() if panes else {}
    # A second tmux snapshot after the process snapshot proves that recovery
    # evidence still belongs to the listed pane/root rather than a reused PID.
    if include_conversations and panes:
        panes = collect_panes()
    return build_statuses(
        panes,
        processes,
        load_marks(),
        args.cpu_threshold,
        args.memory_threshold,
        conversation_collector=(
            collect_agent_conversations if include_conversations else None
        ),
    )


def recovery_entries(statuses: List[PaneStatus]) -> List[dict]:
    entries = []
    for status in statuses:
        for conversation in status.agent_conversations:
            entries.append(
                {
                    "tool": conversation.tool,
                    "conversation_id": conversation.conversation_id,
                    "conversation_id_status": conversation.conversation_id_status,
                    "conversation_id_kind": conversation.conversation_id_kind,
                    "identity_source": conversation.identity_source,
                    "source_path": conversation.source_path,
                    "stable_mapping_key": conversation.stable_mapping_key,
                    "tmux_target": status.target,
                    "tmux_session_name": status.tmux_session_name,
                    "pane_id": status.pane_id,
                    "pane_pid": status.pane_pid,
                    "process_instances": conversation.process_instances,
                    "working_directory": conversation.working_directory,
                    "resume_command": conversation.resume_command,
                }
            )
    return entries


def status_payload(
    statuses: List[PaneStatus], args: argparse.Namespace, report_type: str = "status"
) -> dict:
    recovery = recovery_entries(statuses)
    producer_server_id = statuses[0].server_instance_id if statuses else None
    if statuses and any(
        status.server_instance_id != producer_server_id for status in statuses
    ):
        raise TmuxStatusError("tmux panes reported multiple server instances")
    return {
        "schema_version": 3,
        "tool_version": VERSION,
        "producer": {"name": "tmux-status", "version": VERSION},
        "server_instance_id": producer_server_id,
        "report_type": report_type,
        "pre_restart": report_type == "recovery",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "host": socket.gethostname(),
        "thresholds": {
            "cpu_percent": args.cpu_threshold,
            "memory_mb": args.memory_threshold,
        },
        "pane_count": len(statuses),
        "anomaly_count": sum(bool(status.anomalies) for status in statuses),
        "confirmed_conversation_count": sum(
            entry["conversation_id_status"] == "confirmed" for entry in recovery
        ),
        "unknown_conversation_count": sum(
            entry["conversation_id_status"] == "unknown" for entry in recovery
        ),
        "recovery": recovery,
        "panes": [asdict(status) for status in statuses],
    }


def markdown_code(value: object) -> str:
    return "`{}`".format(str(value).replace("`", "\\`"))


def render_markdown(payload: dict) -> str:
    report_type = payload["report_type"]
    title = (
        "tmux-status pre-restart recovery report"
        if report_type == "recovery"
        else "tmux-status snapshot"
    )
    lines = [
        "# {}".format(title),
        "",
        "- Generated: {}".format(markdown_code(payload["generated_at"])),
        "- Host: {}".format(markdown_code(payload["host"])),
        "- Panes: {}".format(payload["pane_count"]),
        "- Anomalies: {}".format(payload["anomaly_count"]),
        "- Confirmed conversations: {}".format(
            payload["confirmed_conversation_count"]
        ),
        "- Unknown conversations: {}".format(payload["unknown_conversation_count"]),
        "",
    ]
    if not payload["panes"]:
        lines.extend(["No tmux panes were visible.", ""])
    for pane in payload["panes"]:
        lines.extend(
            [
                "## {} ({})".format(
                    pane["target"].replace("#", "\\#"),
                    markdown_code(pane["pane_id"]),
                ),
                "",
                "- tmux session name: {}".format(
                    markdown_code(pane["tmux_session_name"])
                ),
                "- tmux window/pane: {}.{}".format(
                    pane["tmux_window_index"], pane["tmux_pane_index"]
                ),
                "- pane ID / pane PID: {} / {}".format(
                    markdown_code(pane["pane_id"]), pane["pane_pid"]
                ),
                "- pane instance ID: {}".format(
                    markdown_code(pane["pane_instance_id"])
                ),
                "- working directory: {}".format(
                    markdown_code(pane["working_directory"])
                ),
                "- resources: CPU {:.1f}%, memory {:.1f} MB".format(
                    pane["cpu_percent"], pane["memory_mb"]
                ),
                "- activity: {} ({})".format(
                    markdown_code(pane["activity"]),
                    markdown_code(pane["activity_source"]),
                ),
                "- anomalies: {}".format(
                    ", ".join(pane["anomalies"]) if pane["anomalies"] else "none"
                ),
                "",
                "### Agent conversation mapping",
                "",
            ]
        )
        conversations = pane["agent_conversations"]
        if not conversations:
            lines.extend(["No Codex or Grok process was detected in this pane.", ""])
            continue
        for conversation in conversations:
            conversation_id = conversation["conversation_id"] or "unknown"
            lines.extend(
                [
                    "- tool: {}".format(markdown_code(conversation["tool"])),
                    "  - ID kind: {}".format(
                        markdown_code(conversation["conversation_id_kind"])
                    ),
                    "  - conversation/thread ID: {}".format(
                        markdown_code(conversation_id)
                    ),
                    "  - ID status: {}".format(
                        markdown_code(conversation["conversation_id_status"])
                    ),
                    "  - agent PID(s): {}".format(
                        ", ".join(conversation["process_instances"].keys())
                    ),
                    "  - process instance key(s): {}".format(
                        ", ".join(conversation["process_instances"].values())
                    ),
                    "  - identity source: {}".format(
                        markdown_code(conversation["identity_source"])
                    ),
                    "  - source path: {}".format(
                        markdown_code(conversation["source_path"] or "unknown")
                    ),
                    "  - working directory: {}".format(
                        markdown_code(conversation["working_directory"] or "unknown")
                    ),
                    "  - stable mapping key: {}".format(
                        markdown_code(conversation["stable_mapping_key"] or "unknown")
                    ),
                    "  - evidence: {}".format(conversation["evidence"]),
                    "  - resume command: {}".format(
                        markdown_code(conversation["resume_command"] or "unknown")
                    ),
                ]
            )
        lines.append("")

    commands = [
        entry["resume_command"]
        for entry in payload["recovery"]
        if entry["resume_command"]
    ]
    lines.extend(["## Recovery commands", ""])
    if commands:
        lines.extend(["```sh", *dict.fromkeys(commands), "```", ""])
    else:
        lines.extend(
            [
                "No verified resume command is available. Resolve every `unknown` ID manually before shutdown.",
                "",
            ]
        )
    return "\n".join(lines)


def save_report(text: str, output: str) -> None:
    if output == "-":
        print(text)
        return
    path = Path(output).expanduser()
    temporary_name = ""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary_name = tempfile.mkstemp(
            prefix=".tmux-status-", suffix=".tmp", dir=str(path.parent)
        )
        with os.fdopen(handle, "w", encoding="utf-8") as temporary:
            temporary.write(text)
            if not text.endswith("\n"):
                temporary.write("\n")
        os.replace(temporary_name, path)
    except OSError as exc:
        raise TmuxStatusError("cannot write report {}: {}".format(path, exc))
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)


def cmd_report(args: argparse.Namespace) -> int:
    statuses = collect_statuses(args, include_conversations=True)
    payload = status_payload(statuses, args, report_type=args.report_type)
    if args.format == "json":
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    else:
        text = render_markdown(payload)
    save_report(text, args.output)
    if args.output != "-":
        print("Wrote {} {} to {}".format(args.report_type, args.format, args.output))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    statuses = collect_statuses(args, include_conversations=args.json)
    if args.json:
        print(json.dumps(status_payload(statuses, args), ensure_ascii=False, indent=2))
    else:
        print(render_table(statuses, not args.no_color and sys.stdout.isatty()))
    return 2 if args.fail_on_anomaly and any(s.anomalies for s in statuses) else 0


def cmd_watch(args: argparse.Namespace) -> int:
    first = True
    try:
        while True:
            statuses = collect_statuses(args, include_conversations=False)
            if not first:
                sys.stdout.write("\033[H\033[2J")
            first = False
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print("tmux-status {}  refresh={}s".format(timestamp, args.interval))
            print(render_table(statuses, not args.no_color and sys.stdout.isatty()))
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def cmd_mark(args: argparse.Namespace) -> int:
    marks = load_marks()
    if args.state == "auto":
        existed = marks.pop(args.target, None) is not None
        save_marks(marks)
        print(
            "{} manual mark for {}".format("Removed" if existed else "No", args.target)
        )
        return 0
    marks[args.target] = {
        "state": args.state,
        "note": args.note or "",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    save_marks(marks)
    print("Marked {} as {}.".format(args.target, args.state))
    return 0


def cmd_marks(args: argparse.Namespace) -> int:
    marks = load_marks()
    if args.json:
        print(json.dumps({"marks": marks}, ensure_ascii=False, indent=2))
        return 0
    if not marks:
        print("No manual marks.")
        return 0
    print("{:<24} {:<9} {}".format("TARGET", "STATE", "NOTE"))
    for target, mark in sorted(marks.items()):
        print(
            "{:<24} {:<9} {}".format(
                truncate(target, 24),
                mark.get("state", ""),
                mark.get("note", ""),
            )
        )
    return 0


def cmd_doctor(_args: argparse.Namespace) -> int:
    ok = True
    for command in ("tmux", "ps"):
        path = shutil.which(command)
        if path:
            print("[ok] {}: {}".format(command, path))
        else:
            ok = False
            print("[missing] {}".format(command))
    if shutil.which("tmux"):
        result = run_command(["tmux", "-V"])
        version = result.stdout.strip() or result.stderr.strip()
        if version:
            print("[info] {}".format(version))
    if Path("/proc").is_dir():
        print("[ok] process file evidence: /proc")
    elif shutil.which("lsof"):
        print("[ok] process file evidence: {}".format(shutil.which("lsof")))
    else:
        print(
            "[info] lsof unavailable; conversation IDs can only use CLI arguments or scrollback"
        )
    print("[info] marks: {}".format(config_path()))
    try:
        panes = collect_panes() if shutil.which("tmux") else []
        print("[ok] visible panes: {}".format(len(panes)))
    except TmuxStatusError as exc:
        ok = False
        print("[error] {}".format(exc))
    return 0 if ok else 1


def finite_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(number):
        raise argparse.ArgumentTypeError("must be finite")
    return number


def nonnegative_float(value: str) -> float:
    number = finite_float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return number


def positive_float(value: str) -> float:
    number = finite_float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def add_threshold_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--cpu-threshold",
        type=nonnegative_float,
        default=DEFAULT_CPU_THRESHOLD,
        metavar="PERCENT",
        help="flag a pane at or above this aggregate CPU%% (default: %(default)s)",
    )
    parser.add_argument(
        "--memory-threshold",
        type=nonnegative_float,
        default=DEFAULT_MEMORY_THRESHOLD_MB,
        metavar="MB",
        help="flag a pane at or above this aggregate memory (default: %(default)s)",
    )
    parser.add_argument("--no-color", action="store_true", help="disable ANSI colors")


def add_report_options(
    parser: argparse.ArgumentParser, report_type: str, default_format: str
) -> None:
    add_threshold_options(parser)
    parser.add_argument(
        "--format",
        choices=("json", "markdown"),
        default=default_format,
        help="report format (default: %(default)s)",
    )
    parser.add_argument(
        "--output",
        default="-",
        metavar="PATH",
        help="write atomically to PATH; '-' prints to stdout (default: '-')",
    )
    parser.set_defaults(handler=cmd_report, report_type=report_type)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tmux-status",
        description="Inspect tmux pane activity and process-tree resource usage.",
    )
    parser.add_argument("--version", action="version", version=VERSION)
    subparsers = parser.add_subparsers(dest="command")

    status = subparsers.add_parser("status", help="show one status snapshot")
    add_threshold_options(status)
    status.add_argument("--json", action="store_true", help="emit structured JSON")
    status.add_argument(
        "--fail-on-anomaly",
        action="store_true",
        help="exit 2 when any pane exceeds a threshold or is dead",
    )
    status.set_defaults(handler=cmd_status)

    snapshot = subparsers.add_parser(
        "snapshot", help="write a JSON or Markdown snapshot with conversation mappings"
    )
    add_report_options(snapshot, "snapshot", "json")

    recovery = subparsers.add_parser(
        "recovery", help="write a pre-restart report with verified resume commands"
    )
    add_report_options(recovery, "recovery", "markdown")

    watch = subparsers.add_parser("watch", help="continuously refresh status")
    add_threshold_options(watch)
    watch.add_argument(
        "--interval",
        type=positive_float,
        default=2.0,
        metavar="SECONDS",
        help="refresh interval (default: %(default)s)",
    )
    watch.set_defaults(handler=cmd_watch)

    mark = subparsers.add_parser("mark", help="set or remove a manual activity mark")
    mark.add_argument(
        "target",
        help="pane ID (%%3), locator (session:0.1), or session name",
    )
    mark.add_argument(
        "state",
        choices=("active", "inactive", "auto"),
        help="manual state; auto removes the mark",
    )
    mark.add_argument("--note", default="", help="optional annotation")
    mark.set_defaults(handler=cmd_mark)

    marks = subparsers.add_parser("marks", help="list manual marks")
    marks.add_argument("--json", action="store_true", help="emit structured JSON")
    marks.set_defaults(handler=cmd_marks)

    doctor = subparsers.add_parser("doctor", help="check local prerequisites")
    doctor.set_defaults(handler=cmd_doctor)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    arguments = list(argv) if argv is not None else sys.argv[1:]
    if not arguments:
        arguments = ["status"]
    args = parser.parse_args(arguments)
    if getattr(args, "command", None) is None:
        parser.print_help()
        return 0
    try:
        return args.handler(args)
    except TmuxStatusError as exc:
        print("tmux-status: {}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    sys.exit(main())
