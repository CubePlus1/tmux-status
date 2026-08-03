# tmux-status JSON contract v3

This directory is the canonical producer contract for `tmux-status status --json`, `snapshot`, and `recovery`.

Schema v3 is a strict additive successor to v2. Every v2 producer and tmux instance field remains required, while `agent_conversations` records verified Codex/Grok identity separately from tmux names.

Rules:

- `session` and `tmux_session_name` are tmux names, never agent IDs.
- A confirmed conversation requires process-associated file or CLI evidence, a UUID, nonempty `working_directory`, `stable_mapping_key` derived exactly as `<tool>:<conversation_id>`, and `resume_command`.
- `pre_restart` is true exactly for `report_type: "recovery"` and false for status or snapshot reports.
- Every conversation records its own `working_directory`; pane cwd is never substituted for a different agent cwd, and missing process-associated cwd keeps recovery unknown.
- `process_instances` maps each signed 32-bit agent PID to exactly one `<pid>:`-prefixed start identity so PID reuse cannot merge unknown observations; it is never conversation-ID evidence.
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
