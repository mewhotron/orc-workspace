# Verification and limits

Version 0.1.0 was prepared on Windows on 27 September 2026.

## Executed checks

- Installer synthetic suites passed in Windows PowerShell 5.1 and PowerShell 7. They cover repeated setup, conflicts, path traversal, reparse points, ownership inventories, uninstall preservation, source tampering, and injected IO/rollback failures. Failed restoration preserves the recovery directory and reports its location.
- A full Codex-adapter workspace installation/update, a Claude installation with knowledge/research selected, and a Gemini core-only installation passed their inventory checks. These checks verify copied files and hashes; they do not prove that an AI host loaded the roles.
- Core handoff tests: 8 passed. Knowledge import/search tests: 7 passed, including Unicode, punctuation-only queries, missing database refusal and changed citation metadata.
- Coaching runtime tests: 171 passed in an existing Python environment with the optional FIT SDK. A fresh synthetic local report also passed without that SDK. No personal activity data was used.
- Research helper tests: 17 passed. Media tests: 75 passed with zero skips using existing FFmpeg/FFprobe. Media fixtures are synthetic; no original media is included.
- An independent instruction review exercised four fictional scenarios: research prompt injection, missing coaching evidence, a late mismatched result after cancellation, and document-command injection. The reviewer returned bounded decisions. These are mock decisions, not live specialist consultations or proof of injection immunity.
- Release checks inspect allowed file paths, credentials resembling known token formats, locally supplied private identifiers, whitespace, starter SQLite integrity and the file hash manifest.

## What remains unverified

- Native role discovery and live multi-agent execution in Codex, Claude Code, Gemini CLI or other hosts. Provider adapters describe host-specific conventions; generic manual role use remains available.
- Every AI provider or model. Model quality, tool access, context size, pricing and sandbox enforcement are properties of the user's host.
- Live accounts, Garmin synchronization, Notion/calendar writes, cloud APIs and original application services. Integration recipes require separate configuration and validation.
- Real book ingestion and the original embedding/RAG pipeline. The bundled SQLite contains fictional starter material. Original book databases and provider-specific embeddings are excluded.

Privacy scanning is a bounded release check, not a guarantee that arbitrary future contributions contain no secrets. Review changes before publishing.

## Repeat locally

From the source package, run:

```powershell
python -B -m unittest discover -s tests -p 'test_*.py' -v
& .\tests\test_installer.ps1
python -B scripts\build_release.py
```

Each pack README gives its own optional runtime checks and requirements. After installation, run `scripts/Test-Orc.ps1 -Destination <workspace>` to inspect the installed inventory.
