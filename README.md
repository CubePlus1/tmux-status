# tmux-status

A small, dependency-free CLI for inspecting every tmux pane and the full process
tree below it.

It shows:

- aggregate CPU and resident memory per pane;
- configurable CPU/memory anomaly flags;
- attached, selected, idle, active, and dead state;
- persistent manual `active` / `inactive` overrides;
- process-based detection of running Codex and Grok CLIs;
- verified Codex thread IDs and Grok session IDs when evidence is available;
- stable conversation-to-pane mappings and executable resume commands;
- human-readable, JSON, and Markdown snapshots.

## Run

```sh
git clone https://github.com/CubePlus1/tmux-status.git
cd tmux-status
./tmux-status doctor
./tmux-status
./tmux-status watch
```

No tmux server is a normal state and produces `No tmux server or panes found.`

## Commands

```sh
# One snapshot; defaults are 80% aggregate CPU and 1024 MB RSS.
./tmux-status status

# Refresh every second with custom anomaly thresholds.
./tmux-status watch --interval 1 \
  --cpu-threshold 120 --memory-threshold 2048

# Machine-readable monitoring output.
./tmux-status status --json

# Save a JSON snapshot with tmux and agent-conversation mappings.
./tmux-status snapshot --format json --output tmux-snapshot.json

# Before restarting, save a Markdown recovery report and a JSON copy.
./tmux-status recovery --output tmux-recovery.md
./tmux-status recovery --format json --output tmux-recovery.json

# Return exit code 2 when an anomaly is present.
./tmux-status status --fail-on-anomaly

# A pane ID, exact locator, or whole session can be marked.
./tmux-status mark %3 active --note "release task"
./tmux-status mark work:0.1 inactive
./tmux-status mark work active

# Remove an override and return to automatic activity inference.
./tmux-status mark %3 auto
./tmux-status marks
```

Manual marks are stored at `~/.config/tmux-status/marks.json`. Set
`TMUX_STATUS_MARKS_FILE` to use another path.

`snapshot` defaults to JSON and `recovery` defaults to Markdown. Both print to
stdout unless `--output PATH` is supplied. File output uses an atomic replace so
a partially written recovery report is not left behind.

## Conversation IDs and recovery

For every detected Codex or Grok process, JSON and Markdown reports record the
agent conversation separately from tmux identity:

- `tmux_session_name`, `tmux_window_index`, `tmux_pane_index`, and `pane_id`;
- the pane root `pane_pid`, a `process_instances` map from each agent PID to
  exactly one process-incarnation key, and each conversation's own
  `working_directory`;
- `codex_thread_id` or `grok_session_id` in `conversation_id_kind`;
- the verified UUID in `conversation_id`, its evidence source, and source path;
- a stable mapping key such as `codex:<UUID>` or `grok:<UUID>`;
- an executable `resume_command`.

The legacy `session`, `window`, `pane`, `pid`, and `path` fields remain in JSON
for compatibility. A tmux session name is never used as a Codex/Grok ID.

Machine-readable output uses the canonical
[`schema_version: 3`](contracts/v3/README.md) contract. It preserves the full
v2 producer and tmux instance identity fields while adding strict conversation
mapping semantics. Consumers may verify the controlled schema and fixtures
with the committed `SHA256SUMS` manifest.

Evidence is checked in this order:

1. a rollout/session file currently opened by the live agent process;
2. an explicit UUID supplied to the live CLI through `resume`/`--resume` (or
   Grok `--session-id` when creating a named new session);
3. pane scrollback only as diagnostic context; an unassociated historical UUID
   never confirms the current live process.

Recovery commands use the matched process cwd, an explicit CLI cwd, or session
metadata cwd; they do not substitute the pane cwd for a different agent cwd.
If none of those process-associated sources is available, recovery stays
`unknown` and no executable command is emitted.

Codex rollout metadata is read only from its first `session_meta` record. Grok
IDs are read from the UUID session directory containing the process's open
`events.jsonl`/session file. Prompts and conversation messages are not parsed.
The CLI does not select a session merely because it is recent or shares a
working directory.

If the evidence is missing or conflicting, the report records:

```json
{
  "conversation_id": null,
  "conversation_id_status": "unknown",
  "stable_mapping_key": null,
  "resume_command": null
}
```

Only file or CLI evidence associated with the current process can confirm an
ID. A PID is never converted into or used to guess a conversation ID. Resolve
every `unknown` entry manually before shutdown. Confirmed recovery commands
have this form and are included verbatim in the pre-restart report:

```sh
codex resume -C /path/to/project 019fc5d1-40e4-75a2-89f2-188ae5efb2c4
grok --cwd /path/to/project --resume 019fc532-c5ba-7b90-a199-5ecd6d99bf69
```

## Interpretation

CPU and memory include the pane's root process and every descendant. CPU can
exceed 100% when a process uses more than one core. A pane is inferred as active
when Codex/Grok is running, a descendant process exists, measurable CPU activity
is present, or it is the selected pane in an attached session. Manual marks take
precedence and are shown with `*`.

Codex/Grok activity detection is process-based. It does not inspect prompts,
messages, network requests, or private application APIs. It reliably answers
whether a matching executable is alive under the pane, but cannot prove that the
tool is currently generating rather than waiting for input. Conversation ID
capture uses only local evidence exposed by the running CLI; it is independent
of activity inference.

## Install as a Codex skill

Ask Codex to install the skill from this repository:

```text
Use $skill-installer to install the skill from
https://github.com/CubePlus1/tmux-status/tree/main/skills/tmux-status
```

Or run the bundled Codex skill installer directly:

```sh
python3 ~/.codex/skills/.system/skill-installer/scripts/install-skill-from-github.py \
  --repo CubePlus1/tmux-status \
  --path skills/tmux-status
```

Restart Codex after installation. The skill can then be invoked with:

```text
Use $tmux-status to inspect my current tmux panes and flag anomalies.
```
