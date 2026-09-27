# Orc Workspace

A reusable, Windows-first workspace for coordinating AI agents inside an AI tool you already use. Orc is your main contact; optional packs add coaching, knowledge curation, research and local media tools. You choose the models, projects and accounts. There is no background service or required model-provider API.

This is **v0.1.0, a private testing distribution**. It contains reusable source and synthetic examples, not the original owner's profile, credentials, history or books. See [capabilities and limits](docs/capabilities.md) and [executed verification](docs/verification.md) before relying on a feature.

## Install on Windows

1. Use your invited GitHub account to download the release ZIP from this private repository. Extract it to a temporary source folder. Keep the checksum file if provided; compare the ZIP with `Get-FileHash -Algorithm SHA256` before extraction.
2. Open PowerShell in the extracted `orc-workspace` folder. Read the installer, or give your AI assistant the [bootstrap prompt](BOOTSTRAP.md).
3. Preview the files, then install into a **different** destination:

```powershell
.\scripts\Install-Orc.ps1 -Destination "$env:USERPROFILE\Orc Workspace" -Adapter generic -WhatIf
.\scripts\Install-Orc.ps1 -Destination "$env:USERPROFILE\Orc Workspace" -Adapter generic
.\scripts\Test-Orc.ps1 -Destination "$env:USERPROFILE\Orc Workspace"
```

All four packs are selected by default. For selected packs use `-Packs coaching,knowledge`; for the core only, use `-Packs @()`. Choose `-Adapter codex`, `claude` or `gemini` for the corresponding host files. Use the generic route for other tools. You need an already available AI host; setup does not install it, sign in, or change execution policy.

The installer checks every release file hash and refuses conflicting/user-modified files. It creates private records under the destination's `local/`, preserving them on updates. Do not install over the source folder, another application, or an existing project with conflicting files. If script execution is blocked, follow your organization's approved policy rather than bypassing it.

4. Open a fresh AI session **in the destination folder**. Ask it to read `AGENTS.md` and report the instruction files and actual tools it has. Configure `local/preferences.md` with your timezone, approved project folders and model choices. No external project is registered automatically.

## First use

Ask: "Orc, inspect this installation. List the installed roles and distinguish available tools from tools you have actually tested. Do not connect accounts or import personal data."

Then choose a task. Research needs the host's browsing capability; coaching needs your own evidence and context; media tools need Python/FFmpeg. The knowledge pack has a small fictional starter database for an immediate offline check; see its README for exact commands.

| Pack | Roles and tools |
| --- | --- |
| [Coaching](packs/coaching/README.md) | Lead Coach, Cycling trainer, Strength & conditioning trainer, Nutritionist specialist; owner context and offline tooling described in the pack |
| [Knowledge](packs/knowledge/README.md) | Knowledge Curator; local SQLite import/search with source citations |
| [Research](packs/research/README.md) | Search Crawler; evidence rules and a local record-update helper |
| [Media](packs/media/README.md) | Media Lab Agent; catalogue, hashing, supported cuts/rendering and output validation |

Unselected pack links describe source-package options and may not exist in a subset installation. Role files are instructions, not automatically running services. The host must actually support delegation; manual handoffs and a disclosed single-assistant fallback are documented. Do not simulate specialist participation.

## Knowledge and personal data

The starter SQLite contains only newly authored fictional test material. The original book-derived databases are not included while their sharing status remains unresolved. This is an explicit release limit, not a claim that the starter reproduces their coverage. Use the local import/read-only legacy adapter with sources you are entitled to use; original EPUB/PDF books are not bundled.

Keep your context, source documents, imported databases, outputs and customizations in ignored `local/` paths. Never commit credentials. A private repository and ignore rules are not encryption or legal clearance. Review [privacy](docs/privacy.md).

## Updates, verification and removal

Download a fresh release into a separate source folder and run its installer with the same destination/packs/adapter. Unchanged owned files can update; modified files produce a conflict instead of silent overwrite. Owner records remain intact. After a change, rerun `Test-Orc.ps1`; its Ready status means installed-file integrity, not that external accounts or AI behavior were tested.

To preview and remove unchanged installer-owned files, run these from the extracted source package:

```powershell
.\scripts\Uninstall-Orc.ps1 -Destination "$env:USERPROFILE\Orc Workspace" -WhatIf
.\scripts\Uninstall-Orc.ps1 -Destination "$env:USERPROFILE\Orc Workspace"
```

Private records and unowned files remain. Review them yourself if you want to erase your data. See [troubleshooting](docs/troubleshooting.md).

## Extend and contribute

Read [architecture](docs/architecture.md), [host compatibility](docs/compatibility.md), [extension guide](docs/extending.md), [integration recipes](integrations/README.md) and [contribution guide](CONTRIBUTING.md). Add a new domain without inheriting the original author's preferences or accounts.

The core has no Python dependency. Optional tools use Python and other requirements listed per pack. No dependencies or binaries are vendored. MIT applies to eligible original source/instructions; see [LICENSE](LICENSE) and [third-party notices](THIRD_PARTY_NOTICES.md). The private repository is for invited testing, not a public launch.
