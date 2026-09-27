"""Small cooperative Markdown writer. No network, service, or multi-file transaction.

Use only for authorized local records. The CLI prints metadata, never record text.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import uuid


class RecordError(RuntimeError):
    pass


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def identifier(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value):
        raise RecordError("IDs and owner labels must be 1-80 non-sensitive letters/digits/_/-")
    return value


def safe(root, relative):
    p = Path(relative)
    if p.is_absolute() or not p.parts or any(x in (".", "..") for x in p.parts):
        raise RecordError("Only contained relative paths are permitted")
    result = root / p
    for component in (result, *result.parents):
        if component == root:
            break
        if component.is_symlink() or getattr(component, "is_junction", lambda: False)():
            raise RecordError("Linked record paths are not supported")
    if not result.resolve().is_relative_to(root):
        raise RecordError("Path escapes record root")
    return result


def atomic(path, data, temp_owner=None):
    """Same-directory replace; interruption before replace leaves original intact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = path.name + ".pending-"
    if temp_owner is not None:
        prefix += identifier(temp_owner) + "--"
    temporary = path.with_name(prefix + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class Records:
    def __init__(self, root):
        supplied = Path(root).absolute()
        if supplied.is_symlink() or getattr(supplied, "is_junction", lambda: False)():
            raise RecordError("Linked record roots are not supported")
        self.root = supplied.resolve()
        if not self.root.is_dir():
            raise RecordError("Record root must already exist")
        self.control = safe(self.root, ".record-control")
        self.control.mkdir(exist_ok=True)

    @contextmanager
    def guard(self):
        # The short OS lock serializes commands, including takeover and resume.
        mutex = safe(self.root, ".record-control/mutex")
        with mutex.open("a+b") as stream:
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RecordError("Another record command is active; defer") from exc
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)

    def manifest_path(self, attempt):
        return safe(self.root, f".record-control/{identifier(attempt)}.json")

    def read(self, attempt):
        path = self.manifest_path(attempt)
        if not path.exists():
            raise RecordError("Unknown attempt")
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("attempt_id") != attempt:
            raise RecordError("Attempt identity mismatch")
        return value

    def save(self, value):
        atomic(self.manifest_path(value["attempt_id"]),
               (json.dumps(value, indent=2) + "\n").encode())

    def owned(self, attempt, owner):
        value = self.read(attempt)
        if value["owner"] != owner:
            raise RecordError("Not this attempt's write owner")
        return value

    def begin(self, research, attempt, owner, names):
        for item in (research, attempt, owner):
            identifier(item)
        if not names or len(names) != len(set(names)):
            raise RecordError("Supply distinct record filenames")
        with self.guard():
            for existing in self.control.glob("*.json"):
                active = json.loads(existing.read_text(encoding="utf-8"))
                if active["status"] == "pending":
                    raise RecordError("A pending writer exists; resume/reconcile or defer")
            if self.manifest_path(attempt).exists():
                raise RecordError("Attempt ID already exists; never reuse it")
            files = {}
            for name in names:
                if not re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*\.md", name):
                    raise RecordError("Records must be Markdown outside control metadata")
                target = safe(self.root, name)
                files[name] = {"base": digest(target), "wanted": None}
            value = {"research_id": research, "attempt_id": attempt,
                     "owner": owner, "status": "pending", "files": files}
            self.save(value)
            return value

    def stage(self, attempt, owner, replacements):
        with self.guard():
            value = self.owned(attempt, owner)
            if value["status"] != "pending" or set(replacements) != set(value["files"]):
                raise RecordError("Stage exactly the pending attempt's files")
            if any(info["wanted"] is not None for info in value["files"].values()):
                raise RecordError("Already staged; reconcile instead of changing the plan")
            # No originals/backups are retained; only prospective replacements.
            for number, (name, info) in enumerate(value["files"].items()):
                data = replacements[name].encode("utf-8")
                candidate = safe(self.root, f".record-control/{attempt}-{number}.md")
                atomic(candidate, data)
                info["candidate"] = candidate.name
                info["wanted"] = hashlib.sha256(data).hexdigest()
            self.save(value)

    def inspect(self, value):
        states = {}
        for name, info in value["files"].items():
            actual = digest(safe(self.root, name))
            states[name] = ("applied" if info["wanted"] is not None and actual == info["wanted"]
                            else "ready" if actual == info["base"] else "conflict")
        return states

    def status(self, attempt):
        with self.guard():
            value = self.read(attempt)
            return {**value, "file_states": self.inspect(value)}

    def cleanup(self, value):
        # Only this attempt's candidate/temporary files, never another draft.
        attempt = value["attempt_id"]
        for number in range(len(value["files"])):
            candidate = safe(self.root, f".record-control/{attempt}-{number}.md")
            candidate.unlink(missing_ok=True)
            for temporary in candidate.parent.glob(candidate.name + ".pending-*"):
                nonce = temporary.name[len(candidate.name + ".pending-"):]
                if re.fullmatch(r"[0-9a-f]{32}", nonce):
                    safe(self.root, temporary.relative_to(self.root)).unlink()
        for name in value["files"]:
            target = safe(self.root, name)
            prefix = target.name + ".pending-" + attempt + "--"
            for temporary in target.parent.iterdir():
                if temporary.name.startswith(prefix) and re.fullmatch(r"[0-9a-f]{32}", temporary.name[len(prefix):]):
                    safe(self.root, temporary.relative_to(self.root)).unlink()

    def resume(self, attempt, owner, fault=None):
        with self.guard():
            value = self.owned(attempt, owner)
            if value["status"] == "completed":
                if set(self.inspect(value).values()) != {"applied"}:
                    raise RecordError("Completed records changed; start a new attempt")
                self.cleanup(value)
                return value
            if value["status"] != "pending":
                raise RecordError("Abandoned attempt cannot resume")
            if any(info["wanted"] is None for info in value["files"].values()):
                raise RecordError("Stage all replacements before resume")
            # Preflight every record and candidate before changing any record.
            if "conflict" in self.inspect(value).values():
                raise RecordError("Conflict: preserve current files and reconcile")
            for info in value["files"].values():
                candidate = safe(self.root, ".record-control/" + info["candidate"])
                if digest(candidate) != info["wanted"]:
                    raise RecordError("Candidate missing/changed; do not publish")
            for number, (name, info) in enumerate(value["files"].items(), 1):
                target = safe(self.root, name)
                current = digest(target)
                if current == info["wanted"]:
                    continue
                if current != info["base"]:
                    raise RecordError("Conflict before replacement; preserve current file")
                if fault:
                    fault("before_replace", number)
                candidate = safe(self.root, ".record-control/" + info["candidate"])
                atomic(target, candidate.read_bytes(), temp_owner=attempt)
                if digest(target) != info["wanted"]:
                    raise RecordError("Read-back mismatch; attempt remains pending")
                if fault:
                    fault("after_replace", number)
            if set(self.inspect(value).values()) != {"applied"}:
                raise RecordError("Final reconciliation failed")
            value["status"] = "completed"
            self.save(value)
            if fault:
                fault("after_complete", 0)
            self.cleanup(value)
            return value

    def takeover(self, attempt, previous_owner, new_owner, previous_stopped=False):
        identifier(new_owner)
        if not previous_stopped:
            raise RecordError("First establish that the prior owner stopped; never expire by age")
        with self.guard():
            value = self.owned(attempt, previous_owner)
            if value["status"] != "pending":
                raise RecordError("Only pending work can change owner")
            value["owner"] = new_owner
            self.save(value)

    def abandon(self, attempt, owner):
        with self.guard():
            value = self.owned(attempt, owner)
            if value["status"] != "pending":
                raise RecordError("Only pending attempts can be abandoned")
            states = self.inspect(value)
            value["status"] = "abandoned_incomplete"
            self.save(value)
            self.cleanup(value)
            return states  # Already applied records are never rolled back blindly.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    begin = sub.add_parser("begin")
    begin.add_argument("research"); begin.add_argument("attempt"); begin.add_argument("owner")
    begin.add_argument("files", nargs="+")
    for operation in ("stage", "resume", "status", "abandon", "takeover"):
        cmd = sub.add_parser(operation); cmd.add_argument("attempt")
        if operation != "status":
            cmd.add_argument("owner")
        if operation == "stage":
            cmd.add_argument("replacements", nargs="+", help="record.md=local/draft.md")
        if operation == "takeover":
            cmd.add_argument("new_owner")
            cmd.add_argument("--previous-owner-stopped", action="store_true")
    args = parser.parse_args()
    records = Records(args.root)
    try:
        if args.command == "begin":
            result = records.begin(args.research, args.attempt, args.owner, args.files)
        elif args.command == "stage":
            replacement = {}
            for entry in args.replacements:
                name, filename = entry.split("=", 1)
                source = Path(filename).resolve()
                if not source.is_relative_to(records.root):
                    raise RecordError("Draft must stay inside record root")
                if name in replacement:
                    raise RecordError("Duplicate replacement")
                replacement[name] = source.read_text(encoding="utf-8")
            records.stage(args.attempt, args.owner, replacement)
            result = {"status": "staged", "attempt_id": args.attempt}
        elif args.command == "takeover":
            records.takeover(args.attempt, args.owner, args.new_owner, args.previous_owner_stopped)
            result = {"status": "owner_changed", "attempt_id": args.attempt}
        else:
            method = getattr(records, args.command)
            result = method(args.attempt) if args.command == "status" else method(args.attempt, args.owner)
        print(json.dumps(result, indent=2))
    except (RecordError, OSError, ValueError) as exc:
        detail = str(exc) if isinstance(exc, RecordError) else type(exc).__name__
        parser.exit(2, f"Record update stopped: {detail}; reconcile before retry.\n")


if __name__ == "__main__":
    main()
