# tmux-status JSON contract v3

This directory is the canonical producer contract for `tmux-status status --json`, `snapshot`, and `recovery`.

Schema v3 is a strict additive successor to v2. Every v2 producer and tmux instance field remains required, while `agent_conversations` records verified Codex/Grok identity separately from tmux names.

Rules:

- `session` and `tmux_session_name` are tmux names, never agent IDs.
- `target` and `tmux_target` are derived exactly from the tmux session, window index, and pane index.
- A confirmed conversation requires process-associated file or CLI evidence, a UUID, nonempty `working_directory`, `stable_mapping_key` derived exactly as `<tool>:<conversation_id>`, and `resume_command`.
- `pre_restart` is true exactly for `report_type: "recovery"` and false for status or snapshot reports.
- Indexed tmux identity components use bounded numeric forms. `generated_at` excludes RFC 3339 leap-second `:60` values and permits at most six fractional digits so PostgreSQL preserves snapshot ordering exactly.
- `DEAD` matches pane state exactly. `CPU` and `MEM` match their thresholds whenever the producer's one-decimal rounding makes the result unambiguous; boundary values within half a rounding unit may validly carry either label state.
- Every conversation records its own `working_directory`; pane cwd is never substituted for a different agent cwd, and missing process-associated cwd keeps recovery unknown.
- Dead panes never contain agent conversations or recovery commands.
- `process_instances` maps each signed 32-bit agent PID to exactly one `<pid>:<nonempty-incarnation>` start identity across the whole report, and neither a PID nor a process incarnation can be reused by multiple conversations; it is never conversation-ID evidence.
- `pane_id` and `pane_instance_id` are unique within a report, and a confirmed conversation mapping appears at most once per pane.
- Missing or conflicting evidence is `unknown` with null ID/key/command.
- PID, cwd, pane title, and recency are never identity evidence.
- Payloads contain no prompt, response, reasoning, or pane content.

Validate the contract with:

```sh
cd contracts/v3
shasum -a 256 -c SHA256SUMS
uvx --from check-jsonschema==0.33.3 check-jsonschema \
  --schemafile tmux-status.schema.json fixtures/*.json
python3 validate_semantics.py fixtures/*.json
```

Files under `fixtures-invalid/` must fail schema validation. `validate_semantics.py` enforces ordered recovery projection, summary counts, producer version consistency, legacy/v3 pane aliases, derived pane-instance identity, server identity, stable keys, and resume commands that JSON Schema cannot compare across fields.
