Review this pull request in read-only mode. Focus on the schema v3 producer contract, backward compatibility with all v2 tmux identity fields, evidence handling, recovery command safety, tests, and privacy.

Verify that tmux session names remain separate from Codex/Grok IDs; missing or conflicting evidence remains unknown/null; PID, cwd, title, and recency are never used to guess an ID; and no prompt, response, reasoning, terminal transcript, or pane content is emitted or stored.

Use read-only inspection. Review the committed tests and ordinary CI evidence, but do not run commands that write caches or artifacts. Do not edit files, use network tools, expose secrets, or recommend merging.

Return `verdict: fail` for any actionable correctness, privacy, security, or contract-compatibility finding. Return `verdict: pass` only when no blocking finding remains.
