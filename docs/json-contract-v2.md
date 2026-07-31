# tmux-status JSON contract v2

`tmux-status status --json` emits a single JSON object. Version 2 is additive:
all version 1 status and pane fields remain present.

## Envelope

| Field | Type | Meaning |
| --- | --- | --- |
| `schema_version` | integer | JSON contract version, currently `2` |
| `tool_version` | string | `tmux-status` semantic version |
| `server_instance_id` | string or null | Server PID/start tuple; null with no server |
| `producer.name` | string | Always `tmux-status` |
| `producer.version` | string | CLI semantic version |
| `generated_at` | RFC 3339 string | UTC snapshot generation time |
| `thresholds.cpu_percent` | number | Aggregate pane CPU anomaly threshold |
| `thresholds.memory_mb` | number | Aggregate pane RSS anomaly threshold |
| `pane_count` | integer | Number of visible panes |
| `anomaly_count` | integer | Panes with at least one anomaly |
| `panes` | array | Pane observations |

## Pane identity

Version 2 adds these fields to each pane:

| Field | Type | Meaning |
| --- | --- | --- |
| `session_id` | string | tmux server-scoped session ID, such as `$1` |
| `session_created` | integer | Session creation time as Unix seconds |
| `window_id` | string | tmux server-scoped window ID, such as `@2` |
| `server_instance_id` | string | Server PID/start tuple |
| `pane_instance_id` | string | Stable composite for this pane process instance |

Consumers must use `pane_instance_id` for pane links and
`server_instance_id` to detect tmux server replacement. They must not treat
`pane`, `pid`, or a selected/active flag alone as a durable identity.

No prompt, reply, scrollback, pane content, command arguments, or environment
variables are included. The existing `command` field is tmux's
`pane_current_command`, not captured pane text.

## Compatibility

Consumers should reject an unsupported greater `schema_version`, ignore unknown
fields, and continue accepting the unversioned version 1 envelope during a
migration window. An empty `panes` array is a valid snapshot and represents the
normal no-server/no-pane state.
