# Compatibility

The core is provider-neutral text. Local utilities use documented deterministic inputs rather than calling a model provider. Model quality, tool availability and host behavior still affect outcomes; this is not a guarantee of identical results on every AI product.

| Host | Installed adapter | Verification boundary |
| --- | --- | --- |
| Generic host | AGENTS.md plus explicit-read instructions | Usable through manual file reading/handoffs; capabilities must be checked |
| Codex | AGENTS.md; .codex/agents TOML for selected roles | Official format reviewed; inspect release verification for executed probe scope |
| Claude Code | CLAUDE.md; .claude/agents Markdown | Official format reviewed; live loading/delegation not tested here |
| Gemini CLI | GEMINI.md | Official context format reviewed; no native-agent claim or live test |

Windows setup targets PowerShell 5.1 and 7; see docs/verification.md for versions actually run. Optional pack tools declare Python/FFmpeg requirements in their READMEs. Setup never silently installs them.

No provider-specific model is mandated. Embedding indexes require the matching model/dimensions; the portable knowledge route does not require embeddings. A model endpoint alone does not provide local file access or cross-chat coordination. Account connectors and marketplace skills are not bundled.

Documentation reviewed on 2026-09-27 using the official links in each adapter. Source formats can change; a file-level check does not prove future compatibility. The release manifest supplies file integrity, not a vendor compatibility certification.
