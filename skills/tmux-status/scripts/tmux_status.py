#!/usr/bin/env python3
"""Inspect tmux panes, their process trees, and persisted activity marks."""

import argparse
import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

VERSION = "0.1.0"
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
RUNTIME_NAMES = {
    "bun",
    "deno",
    "node",
    "nodejs",
    "python",
    "python2",
    "python3",
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

    @property
    def locator(self) -> str:
        return "{}:{}.{}".format(self.session, self.window_index, self.pane_index)


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


def executable_names(command: str) -> List[str]:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = re.findall(r"[^\s\"']+", command)
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


def detect_tools(processes: Iterable[ProcessInfo]) -> List[str]:
    found: Set[str] = set()
    for process in processes:
        if "Z" in process.state.upper():
            continue
        for name in executable_names(process.command):
            for tool, aliases in TOOL_NAMES.items():
                if name in aliases:
                    found.add(tool)
    return sorted(found)


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
) -> List[PaneStatus]:
    statuses = []
    for pane in panes:
        tree = descendants(pane.pane_pid, processes)
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


def collect_statuses(args: argparse.Namespace) -> List[PaneStatus]:
    panes = collect_panes()
    processes = collect_processes() if panes else {}
    return build_statuses(
        panes,
        processes,
        load_marks(),
        args.cpu_threshold,
        args.memory_threshold,
    )


def status_payload(statuses: List[PaneStatus], args: argparse.Namespace) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "thresholds": {
            "cpu_percent": args.cpu_threshold,
            "memory_mb": args.memory_threshold,
        },
        "pane_count": len(statuses),
        "anomaly_count": sum(bool(status.anomalies) for status in statuses),
        "panes": [asdict(status) for status in statuses],
    }


def cmd_status(args: argparse.Namespace) -> int:
    statuses = collect_statuses(args)
    if args.json:
        print(json.dumps(status_payload(statuses, args), ensure_ascii=False, indent=2))
    else:
        print(render_table(statuses, not args.no_color and sys.stdout.isatty()))
    return 2 if args.fail_on_anomaly and any(s.anomalies for s in statuses) else 0


def cmd_watch(args: argparse.Namespace) -> int:
    first = True
    try:
        while True:
            statuses = collect_statuses(args)
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
