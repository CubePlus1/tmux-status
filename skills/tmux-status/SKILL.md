---
name: tmux-status
description: Inspect existing tmux sessions and panes, aggregate CPU and memory across each pane process tree, flag resource anomalies, detect running Codex or Grok CLIs, preserve verified Codex thread IDs and Grok session IDs with pane mappings, produce restart recovery reports, and manage manual active/inactive marks. Use when Codex needs to diagnose tmux activity, find expensive panes, monitor terminal agents, preserve resumable agent sessions, or report tmux status from the command line.
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

When recording tmux work for later use, create a durable snapshot rather than
copying only the `tools` array:

```sh
python3 <skill-dir>/scripts/tmux_status.py snapshot \
  --format json --output tmux-snapshot.json
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

## Preserve Agent Conversations

For every detected Codex or Grok process, keep the full mapping found in
`agent_conversations` together with these pane fields:

- `tmux_session_name`, `tmux_window_index`, and `tmux_pane_index`;
- `pane_id` and `pane_pid`;
- agent `process_pids`, one process-incarnation `process_instance_keys` entry
  per PID, and the conversation-specific `working_directory`;
- `conversation_id_kind`, `conversation_id`, `conversation_id_status`,
  `identity_source`, and `source_path`;
- `stable_mapping_key` and `resume_command`.

Treat the tmux session name and agent conversation/thread ID as different
identities. Never report only `tools: ["codex"]` or `tools: ["grok"]` when the
task is to record or recover agent work.

Only accept an explicit UUID confirmed from an open rollout/session file or a
live CLI resume/session-ID argument associated with the current process. A UUID
found only in tmux scrollback remains diagnostic and unknown. Do not infer an ID from a
PID, working directory, title, or most-recent session. If
`conversation_id_status` is `unknown`, preserve `unknown` in the result and tell
the user that automatic resume is unavailable for that entry.
Never emit a confirmed recovery command using pane cwd as a fallback when the
process, CLI, and session metadata do not provide an associated cwd.

## Prepare for Restart

Before a restart or shutdown, create a recovery report. Markdown is the default:

```sh
python3 <skill-dir>/scripts/tmux_status.py recovery \
  --output tmux-recovery.md
```

Also create JSON when another tool will consume the report:

```sh
python3 <skill-dir>/scripts/tmux_status.py recovery \
  --format json --output tmux-recovery.json
```

The report includes executable commands for confirmed IDs:

```sh
codex resume -C /path/to/project <codex-thread-uuid>
grok --cwd /path/to/project --resume <grok-session-uuid>
```

Do not invent a command for an `unknown` ID. Ask the user to resolve those
entries manually before shutdown if recovery is required.

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
- `agent_conversations` reports verified conversation IDs separately from tmux
  session names; a null ID with status `unknown` is an intentional result.
- A process match proves the CLI is alive, not that it is currently generating.
- Manual activity has precedence and uses an `activity_source` beginning with
  `manual:`.

Do not kill tmux sessions or send input to panes as part of status inspection.
