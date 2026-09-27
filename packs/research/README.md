# Research pack

Search Crawler is a role and a local Markdown record helper. It does not run an autonomous crawler, monitor websites, connect an account or supply a web tool. The AI host must provide browsing for live research. This pack has no account link or private research records.

Start from this pack directory in PowerShell. Python 3.11+ is optional for the record helper and synthetic checks; no Python packages are required.

```powershell
python -B checks/test_record_update.py
```

An AI host can load `roles/search-crawler.md` as a role instruction, or the user can supply it to an assistant manually. The host's actual browsing and file permissions still govern. Create `local/` as private, ignored storage if you choose to keep research records. The pack does not create it automatically. Keep it out of shared archives and Git; ignoring files is not encryption. Recommended record files are `local/websites.md`, `local/search-history.md` and `local/findings.md`.

For a coordinated edit, first inspect `local/.record-control/` and ensure no pending writer owns it. Then use non-sensitive IDs and an owner label. Draft full replacement Markdown files under `local/drafts/` after `begin`; preserve unrelated entries. For example, from the pack directory:

```powershell
python -B tools/record_update.py local begin S-example S-example-A01 writer findings.md
python -B tools/record_update.py local stage S-example-A01 writer findings.md=local/drafts/findings.md
python -B tools/record_update.py local resume S-example-A01 writer
python -B tools/record_update.py local status S-example-A01
```

`local/` must exist before these commands. Stage exactly the planned files. Check current content and the returned status before reporting completion. A pending attempt remains incomplete even if some files already changed. Resume a still-valid attempt with the same ID; on conflict, preserve current files, reconcile, then start a new attempt. Do not take over based on elapsed time. Erasure must cover records, drafts, candidates and other copies in the authorized scope. The helper uses per-file atomic replacement and OS locks; it is not a transaction, security boundary or backup.

`checks/behavior-cases.json`, `behavior-oracle.json` and `evaluate_behavior.py` are synthetic offline evaluation materials. To evaluate an assistant, give it the role file and case stimuli without the oracle, save its **actual** ordered mock decisions in an observation JSON file, then replay them. Run from the pack directory:

```powershell
python -B checks/evaluate_behavior.py local/acceptance/observations.json --output local/acceptance/replay.json
```

The observation document needs `instruction_sha256` for `roles/search-crawler.md`, an `evaluator` description and one `observations` entry for each case, with `id` and ordered `actions`. The evaluator makes no real web or account calls. It checks selected actions against the oracle in disposable files; a pass cannot prove that a future assistant will choose those actions. No prewritten assistant observations are included.

Source provenance: adapted from the original Search Crawler project's research rules, `tools/record_update.py` and synthetic checks. Source code is copied without external dependencies. The original project's private records and setup history are excluded. No separate third-party code provenance was identified in the selected files; this is an inspection finding, not legal clearance. Redistribution of any later user-supplied research content requires its own review.
