# Media pack

This pack contains the local Media Lab catalogue, FFprobe adapter, cut renderer, render supervisor and synthetic tests. It is a Windows-friendly optional capability, not an autonomous video editor. Python 3.11+ is required to run the code. FFprobe is required for real ingestion; FFmpeg and FFprobe are required for rendering and real-media tests. Obtain compatible executables separately from [FFmpeg](https://ffmpeg.org/download.html). No executables, footage, database, edit, review record or account settings are distributed here.

Run commands from this pack directory in PowerShell. The tools use Python's standard library and SQLite; no package installation is needed. Supply executables via PATH or the tester's private environment variables `MEDIA_LAB_FFMPEG` and `MEDIA_LAB_FFPROBE`. An optional private `.tools/tool-paths.json` may contain `ffmpeg` and `ffprobe` executable paths. There are no network calls in the application; FFprobe accepts local file input. That is not an operating-system sandbox for hostile media.

```powershell
python -m media_lab doctor
python -m media_lab init
python -m media_lab ingest "D:\Camera footage\clip.mp4"
python -m media_lab list --search "clip"
python -m media_lab verify "D:\Camera footage\clip.mp4"
```

The example input path is a placeholder. `ingest` uses explicit files, not recursive scanning, and should run only after transfers finish. `init` creates `data/catalogue.sqlite3` in this pack unless you supply `--db` before the command or set `MEDIA_LAB_DB` to a private catalogue path. Keep `data/`, `media/`, `local/`, `.tools/` and `.scratch/` outside any shared archive or Git staging. These runtime directories are not shipped. A local catalogue stores source paths and metadata; it is private even if the video bytes stay elsewhere. Do not use a source media file as the database path.

An edit is a private JSON file with a name, integer fps, source absolute path and SHA-256, and nonempty clips with inclusive `in_frame` and exclusive `out_frame`. Schema 2 uses named `sources` and ordered clips with `source_id`. Both schemas have a narrow supported input contract; read the role instructions before composing one. The renderer requires an output directory separate from originals. Example command, after creating and reviewing an edit:

```powershell
python -m media_lab.render "media/edits/edit-v1.json" --output-directory "media/renders"
```

For a supervised final export, create a review JSON with the exact edit-file `edit_sha256`, `ready_to_render: true`, and nonempty notes covering the actual editorial/audio review. The review is an attestation, not automatic proof of quality. Then run:

```powershell
python -m media_lab.render_job "media/edits/edit-v1.json" --review-record "media/edits/edit-v1-review.json"
```

The supervisor writes private attempts under `local/render-jobs/`. Wait for process exit and inspect that attempt's `result.json`. Only a completed, validated bundle is a deliverable. If a process is forcibly killed, `status.json` can remain `running`; verify whether that process exists before recovery. Full decode and frame/duration/audio checks validate the rendered outputs; they do not prove the creative brief was met.

Run the synthetic suite from this pack directory:

```powershell
python -B -m unittest discover -s tests -v
```

Real-media tests generate tiny disposable clips and skip when FFmpeg/FFprobe are absent. For a renderer release check requiring real tools and zero skips, run `python -B tests/run_required.py`; it fails if required integration coverage is missing. Report executed and skipped counts separately.

The catalogue's cached import can report `already_known_cached` without rehashing bytes. Use `ingest --rehash` or `verify` for a fresh checksum. Source files must remain quiescent during ingest/render. The code does not generate proxies, use LRF automatically, analyze speech, detect highlights, schedule jobs, upload media or support arbitrary timeline formats. Unsupported footage needs an explicitly designed and tested workflow.

Source provenance: copied from the original Media Lab project's `media_lab` and synthetic `tests`, with pack instructions newly written for portable use. No separate third-party code provenance was identified in the selected files; this is an inspection finding, not legal clearance. No third-party FFmpeg binary is included. Users must review rights and privacy for any footage, music, metadata or later assets they add.
