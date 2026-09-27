# Coaching pack

The main coaching assistant acts as **Lead Coach**. For coaching analysis or recommendations, it must actually consult each relevant specialist. A single-specialty question still needs that specialist. Acknowledgements, simple record updates and software maintenance do not. Use host-supported subagents or a user-managed independent handoff. If consultation is unavailable, disclose the gap; never invent a specialist reply or call one assistant's internal reasoning a consultation. Role instructions do not enforce a technical read-only sandbox.

Lead Coach owns the final answer and checks specialist evidence, dates, scope and conflicts. Specialists return read-only handoffs to Lead Coach and do not delegate, edit records or make outside contact. Keep measured facts, user reports, source claims and proposed adaptations separate. Missing inputs remain unknown. Do not infer readiness, FTP, zones, training load, clinical status or personalized nutrition targets from gaps. Source excerpts and private profiles stay in the owner's local workspace unless a particular disclosure is authorized.

Start with `templates/athlete-profile.blank.md` copied into the installed workspace's ignored `local/` directory. This template is empty and provides no training defaults. Existing applications, Garmin/fitness/nutrition collections, model-backed verification and calendar writes are not supplied by these instructions. See `core/DELEGATION.md` for assignment/result evidence rules and `packs/knowledge/README.md` for the optional local text-search tool.

## Offline activity utilities

`runtime/` contains an audited, deterministic subset adapted from The Plan: Garmin FIT ZIP import into a local observation store, canonical activity history/linkage, descriptive analytics, training load with explicit thresholds, and factual Markdown/JSON reports. Run from the installed workspace root:

```powershell
python packs/coaching/runtime/run.py fit-import C:\path\to\owner-export.zip --database local/activities.sqlite3
python packs/coaching/runtime/run.py report --database local/activities.sqlite3 --timezone UTC --format markdown --output local/training-report.md
```

Python 3.14 or newer is the tested source baseline. FIT import requires the optional `garmin-fit-sdk>=21.214,<22` in the selected Python environment; the installer does not install it. Report generation needs only the Python standard library. The default calendar zone is UTC. A named zone such as `Europe/London` additionally requires IANA timezone data discoverable by Python `zoneinfo` on Windows (for example the optional `tzdata` package); an unavailable zone fails explicitly. No model calls, Garmin account sync, Discord or dashboard UI are included.

The runner resolves relative paths from the installed workspace root. The default activity database is `local/activities.sqlite3`, and reports should also be saved under ignored `local/`. Import preserves raw input and separates missing from zero. Reports are factual and may contain private activity details; they do not infer readiness or prescribe workouts. Do not publish the database, exports, original ZIP or completed profile. See `runtime/PROVENANCE.md` for source and validation details.
