"""Reviewable manifest/ZIP builder. No network, credentials, Git or source imports."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.1.0"
ROOT_FILES = {"README.md", "AGENTS.md", "BOOTSTRAP.md", "LICENSE", "THIRD_PARTY_NOTICES.md",
              "CONTRIBUTING.md", "CHANGELOG.md", ".gitignore", ".gitattributes"}
SOURCE_DIRS = {"core", "packs", "adapters", "templates", "scripts", "docs", "examples", "tests", "knowledge", "integrations"}
EXCLUDE_DIRS = {".git", "local", "__pycache__", ".venv", "venv", "dist"}
EXTENSIONS = {".md", ".json", ".py", ".ps1", ".toml", ".txt", ".csv"}
ROLES = {"lead_coach": "coaching", "cycling_trainer": "coaching", "strength_conditioning_trainer": "coaching",
         "nutritionist_specialist": "coaching", "knowledge_curator": "knowledge", "search-crawler": "research", "media-lab": "media"}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def category(relative):
    parts = relative.split("/")
    if parts[0] == "packs":
        if len(parts) < 3 or parts[1] not in ("coaching", "knowledge", "research", "media"):
            raise ValueError("unrecognized pack: " + relative)
        return parts[1]
    if parts[0] == "knowledge" or relative == "tests/test_knowledge.py":
        return "knowledge"
    if parts[0] == "adapters" and "overlay" in parts and "agents" in parts:
        identifier = Path(relative).stem
        return ROLES.get(identifier) or ROLES[identifier.replace("-", "_")]
    return "core"


def files(root=ROOT):
    result = []
    for directory, dirs, names in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in list(dirs):
            path = base / name
            if name in EXCLUDE_DIRS:
                dirs.remove(name)
                continue
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                raise ValueError("link/junction in source: " + str(path.relative_to(root)))
            if base == root and name not in SOURCE_DIRS:
                raise ValueError("unreviewed root directory: " + name)
        for name in sorted(names):
            path = base / name
            relative = path.relative_to(root).as_posix()
            if relative == "release-manifest.json" or path.suffix == ".pyc":
                continue
            if path.is_symlink():
                raise ValueError("linked source file: " + relative)
            if base == root:
                if name not in ROOT_FILES:
                    raise ValueError("unreviewed root file: " + relative)
            elif path.suffix not in EXTENSIONS and path.name != ".gitignore" and relative != "knowledge/starter.sqlite3":
                raise ValueError("unapproved file type: " + relative)
            if any(p.lower().startswith(".env") or p.endswith(".local.json") for p in path.relative_to(root).parts):
                raise ValueError("private file: " + relative)
            result.append((relative, path))
    missing = ROOT_FILES - {r for r, _ in result}
    if missing:
        raise ValueError("missing release files: " + ", ".join(sorted(missing)))
    return sorted(result)


def review(items, deny_text=()):
    problems = []
    secrets = re.compile(r"\b(?:ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|sk-[A-Za-z0-9_-]{30,})\b|-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----")
    for relative, path in items:
        if relative == "knowledge/starter.sqlite3":
            db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    problems.append(relative + ": integrity check failed")
                text = "\n".join(db.iterdump())
            finally:
                db.close()
        else:
            try:
                text = path.read_text(encoding="utf-8-sig")
            except UnicodeError:
                problems.append(relative + ": not UTF-8 text")
                continue
        if secrets.search(text):
            problems.append(relative + ": credential-like content")
        for value in deny_text:
            if value.casefold() in text.casefold():
                problems.append(relative + ": private identifier match (value suppressed)")
        if any(line.rstrip() != line for line in text.splitlines()):
            problems.append(relative + ": trailing whitespace")
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-manifest", action="store_true")
    parser.add_argument("--output", type=Path, help="Build ZIP here; output must be outside source root")
    parser.add_argument("--deny-text", action="append", default=[], help="Local-only private identifier check; values are never printed")
    args = parser.parse_args()
    items = files()
    problems = review(items, args.deny_text)
    if problems:
        raise SystemExit("Release review failed:\n" + "\n".join(problems))
    manifest = {"schema_version": 1, "version": VERSION,
                "files": [dict(path=r, sha256=sha(p), pack=category(r)) for r, p in items]}
    target = ROOT / "release-manifest.json"
    if args.write_manifest:
        target.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    elif not target.exists() or json.loads(target.read_text(encoding="utf-8")) != manifest:
        raise SystemExit("Manifest missing/stale. Review changes, then use --write-manifest.")
    if args.output:
        output = args.output.resolve()
        if output == ROOT or ROOT in output.parents:
            raise SystemExit("Release output must be outside the source root")
        output.mkdir(parents=True, exist_ok=True)
        archive = output / ("orc-workspace-" + VERSION + ".zip")
        if archive.exists():
            raise SystemExit("Archive exists; choose a fresh output directory")
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as stream:
            for relative, path in items + [("release-manifest.json", target)]:
                info = zipfile.ZipInfo("orc-workspace/" + relative, date_time=(2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                stream.writestr(info, path.read_bytes())
        checksum = sha(archive)
        (output / "SHA256SUMS.txt").write_text(checksum + "  " + archive.name + "\n", encoding="ascii", newline="\n")
        print(json.dumps({"files": len(items), "archive": str(archive), "sha256": checksum}))
    else:
        print(json.dumps({"status": "verified", "files": len(items), "version": VERSION}))


if __name__ == "__main__":
    main()
