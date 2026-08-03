# tmux-status JSON contract v3

This directory is the canonical producer contract for `tmux-status status --json`, `snapshot`, and `recovery`.

Schema v3 is a strict additive successor to v2. Every v2 producer and tmux instance field remains required, while `agent_conversations` records verified Codex/Grok identity separately from tmux names.

Rules:

- `session` and `tmux_session_name` are tmux names, never agent IDs.
- A confirmed conversation requires process-associated file or CLI evidence, a UUID, `stable_mapping_key`, and `resume_command`.
- Every conversation records its own `working_directory`; pane cwd is never substituted for a different agent cwd.
- `process_instance_keys` records one PID-plus-start identity per `process_pids` entry so PID reuse cannot merge unknown observations; it is never conversation-ID evidence.
- Missing or conflicting evidence is `unknown` with null ID/key/command.
- PID, cwd, pane title, and recency are never identity evidence.
- Payloads contain no prompt, response, reasoning, or pane content.

Validate the contract with:

```sh
cd contracts/v3
shasum -a 256 -c SHA256SUMS
uvx --from check-jsonschema==0.33.3 check-jsonschema \
  --schemafile tmux-status.schema.json fixtures/*.json
```

Files under `fixtures-invalid/` must fail schema validation.
