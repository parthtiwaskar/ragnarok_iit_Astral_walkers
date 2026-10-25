# AGENTS.md

Instructions for AI coding assistants (Codex, Cursor, Copilot, Claude Code, etc.) working in this repository.

**Read [`CLAUDE.md`](CLAUDE.md). It's the single source of rules for coding agents**: invariants, stack, commands, workflow and definition of done. The two files are kept in sync by pointing here rather than duplicating.

> Not to be confused with the four *runtime* agents of the system (Heimdall, Thor, Loki, Odin), which are specified in [`docs/AGENT_SPECS.md`](docs/AGENT_SPECS.md).

Quick reminders:
- Exactly four LLM agents. The orchestrator ("the Thing") has **no LLM**.
- The as-of date is enforced in the tool layer, documents are untrusted data, numbers come from code.
- Every tool call goes through the guard chain (allowlist → budget → clock → dedupe → execute → log).
- Run `make lint test` before you finish. Follow `TASKS.md` in order.
