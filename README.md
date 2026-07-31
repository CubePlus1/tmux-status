# tmux-status

A small, dependency-free CLI for inspecting every tmux pane and the full process
tree below it.

It shows:

- aggregate CPU and resident memory per pane;
- configurable CPU/memory anomaly flags;
- attached, selected, idle, active, and dead state;
- persistent manual `active` / `inactive` overrides;
- best-effort detection of running Codex and Grok processes;
- human-readable and JSON output.

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

The JSON output uses the additive, versioned
[JSON contract v2](docs/json-contract-v2.md). It includes stable tmux
session/window identity fields and does not include pane content.

## Interpretation

CPU and memory include the pane's root process and every descendant. CPU can
exceed 100% when a process uses more than one core. A pane is inferred as active
when Codex/Grok is running, a descendant process exists, measurable CPU activity
is present, or it is the selected pane in an attached session. Manual marks take
precedence and are shown with `*`.

Codex/Grok detection is process-based. It does not inspect prompts, messages,
network requests, or private application APIs. It reliably answers whether a
matching executable is alive under the pane, but cannot prove that the tool is
currently generating rather than waiting for input.

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
