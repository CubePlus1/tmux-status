---
name: tmux-status
description: Inspect existing tmux sessions and panes, aggregate CPU and memory across each pane process tree, flag resource anomalies, detect running Codex or Grok CLIs, and manage manual active/inactive marks. Use when Codex needs to diagnose tmux activity, find expensive panes, monitor terminal agents, or report tmux status from the command line.
---

# Tmux Status

Use the bundled `scripts/tmux_status.py` CLI. Resolve its absolute path relative
to this `SKILL.md`; do not assume the skill was installed in a fixed home
directory.

## Inspect

Check prerequisites before the first use on a machine:

```sh
python3 <skill-dir>/scripts/tmux_status.py doctor
```

Collect one machine-readable snapshot for analysis:

```sh
python3 <skill-dir>/scripts/tmux_status.py status --json
```

Use the human-readable table when showing results directly:

```sh
python3 <skill-dir>/scripts/tmux_status.py status
```

Treat `No tmux server or panes found.` as a normal empty state. Report that no
tmux panes are currently visible.

## Monitor

Use a bounded one-shot query by default. Run continuous refresh only when the
user explicitly asks to watch or monitor:

```sh
python3 <skill-dir>/scripts/tmux_status.py watch --interval 2
```

Override anomaly thresholds when the user supplies limits:

```sh
python3 <skill-dir>/scripts/tmux_status.py status \
  --cpu-threshold 120 --memory-threshold 2048
```

Use `--fail-on-anomaly` for automation. Exit code `2` means at least one pane
exceeded a threshold or is dead; exit code `1` means collection failed.

## Manage Activity Marks

Write a manual mark only when the user asks to label a pane or session:

```sh
python3 <skill-dir>/scripts/tmux_status.py mark %3 active --note "release task"
python3 <skill-dir>/scripts/tmux_status.py mark work:0.1 inactive
python3 <skill-dir>/scripts/tmux_status.py mark work auto
python3 <skill-dir>/scripts/tmux_status.py marks
```

Targets may be pane IDs such as `%3`, exact locators such as `work:0.1`, or
session names. `auto` removes the manual override. Marks persist in
`~/.config/tmux-status/marks.json`; honor `TMUX_STATUS_MARKS_FILE` when it is set.

## Interpret Results

- CPU and memory are sums for the pane root process and all descendants.
- CPU may exceed 100 percent when work spans multiple cores.
- `anomalies` contains `CPU`, `MEM`, or `DEAD`.
- `tools` reports live process-based detection of `codex` or `grok`.
- A process match proves the CLI is alive, not that it is currently generating.
- Manual activity has precedence and uses an `activity_source` beginning with
  `manual:`.

Do not kill tmux sessions or send input to panes as part of status inspection.
