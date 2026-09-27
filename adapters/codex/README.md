# Codex adapter

The installed root AGENTS.md is the project entrypoint. This adapter adds project-local custom-agent TOML files under .codex/agents for selected packs. Each points to its canonical role instructions. No global configuration, model name, account or integration is installed.

Start a fresh session in the destination, inspect loaded guidance and available roles, and verify an actual bounded dispatch. Role configuration may be affected by runtime overrides; do not claim a read-only technical boundary solely from a prompt or file. If named roles are unavailable, pass the canonical instructions to a supported temporary worker and disclose the fallback.

Adapter format reviewed against official documentation; native auto-loading is not established by file generation alone.

- [Project guidance](https://learn.chatgpt.com/docs/agent-configuration/agents-md)
- [Custom agents and subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents)
