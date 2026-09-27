"""Disposable file fault tests; never read local/ research records."""
import multiprocessing
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from record_update import Records, RecordError, atomic


def competing_writer(root, queue):
    try:
        Records(root).begin("S-other", "A-other", "other-owner", ["findings.md"])
        queue.put("unexpected_success")
    except RecordError:
        queue.put("deferred")


def crashed_writer(root, point, number):
    import os
    def crash(actual_point, actual_number):
        if (actual_point, actual_number) == (point, number):
            os._exit(73)
    Records(root).resume("A01", "owner", fault=crash)


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="search-crawler-record-test-")
        self.root = Path(self.temp.name).resolve()
        self.addCleanup(self.temp.cleanup)
        self.names = ["websites.md", "history.md", "findings.md"]
        self.old = {name: f"KEEP {name}\n" for name in self.names}
        self.new = {name: text + "S-test A01 new entry\n" for name, text in self.old.items()}
        for name, text in self.old.items():
            (self.root / name).write_text(text, encoding="utf-8")
        self.records = Records(self.root)

    def prepare(self):
        self.records.begin("S-test", "A01", "owner", self.names)
        self.records.stage("A01", "owner", self.new)

    def assert_completed(self):
        self.assertEqual(self.records.status("A01")["status"], "completed")
        for name, text in self.new.items():
            self.assertEqual((self.root / name).read_text(encoding="utf-8"), text)
        self.assertFalse(list(self.records.control.glob("A01-*.md*")))

    def test_success_readback_cleanup_idempotence(self):
        self.prepare()
        self.records.resume("A01", "owner")
        self.records.resume("A01", "owner")
        self.assert_completed()

    def test_atomic_replace_failure_preserves_original(self):
        path = self.root / "findings.md"
        with patch("record_update.os.replace", side_effect=OSError("synthetic replace failure")):
            with self.assertRaises(OSError):
                atomic(path, b"replacement")
        self.assertEqual(path.read_text(), self.old["findings.md"])
        self.assertFalse(list(self.root.glob("*.pending-*")))

    def test_crash_before_first_replacement(self):
        self.prepare()
        self.crash_and_resume("before_replace", 1)

    def crash_and_resume(self, point, number):
        process = multiprocessing.Process(target=crashed_writer, args=(str(self.root), point, number))
        process.start(); process.join(15)
        if process.is_alive():
            process.terminate(); process.join()
            self.fail("Synthetic child did not exit")
        self.assertEqual(process.exitcode, 73)
        self.assertEqual(self.records.status("A01")["status"],
                         "completed" if point == "after_complete" else "pending")
        if point == "before_replace":
            for name in self.names:
                self.assertEqual((self.root / name).read_text(), self.old[name])
        elif point == "after_replace":
            states = list(self.records.status("A01")["file_states"].values())
            self.assertEqual(states, ["applied"] * number + ["ready"] * (3 - number))
        self.records.resume("A01", "owner")
        self.assert_completed()

    def test_crash_after_each_of_three_replacements(self):
        for number in (1, 2, 3):
            with self.subTest(number=number):
                # Fresh disposable sub-root for each interruption boundary.
                root = self.root / str(number); root.mkdir()
                for name, text in self.old.items():
                    (root / name).write_text(text)
                previous_root, previous_records = self.root, self.records
                try:
                    self.root, self.records = root, Records(root)
                    self.prepare(); self.crash_and_resume("after_replace", number)
                finally:
                    self.root, self.records = previous_root, previous_records

    def test_crash_after_completion_before_cleanup(self):
        self.prepare(); self.crash_and_resume("after_complete", 0)

    def test_competing_process_defers(self):
        self.prepare()
        self.check_competitor()

    def test_active_os_lock_defers_competing_process(self):
        with self.records.guard():
            self.check_competitor()

    def check_competitor(self):
        queue = multiprocessing.Queue()
        process = multiprocessing.Process(target=competing_writer, args=(str(self.root), queue))
        process.start(); process.join(15)
        if process.is_alive():
            process.terminate(); process.join(); self.fail("Child timed out")
        self.assertEqual(process.exitcode, 0)
        self.assertEqual(queue.get(timeout=2), "deferred")
        queue.close(); queue.join_thread()

    def test_cleanup_cannot_remove_prefix_named_attempt(self):
        self.prepare(); self.records.resume("A01", "owner")
        self.records.begin("S-other", "A01-other", "other", self.names)
        self.records.stage("A01-other", "other", self.new)
        self.records.resume("A01", "owner")
        self.assertTrue((self.records.control / "A01-other-0.md").exists())
        self.records.resume("A01-other", "other")

    def test_cleanup_preserves_unowned_and_other_attempt_temporary_files(self):
        self.prepare()
        names = ["findings.md.pending-" + "a" * 32,
                 "findings.md.pending-A01-other--" + "b" * 32,
                 "findings.md.pending-A01--other--" + "c" * 32]
        for name in names:
            (self.root / name).write_text("UNRELATED draft")
        owned = self.root / ("findings.md.pending-A01--" + "d" * 32)
        owned.write_text("owned interrupted draft")
        self.records.resume("A01", "owner")
        self.assertFalse(owned.exists())
        for name in names:
            self.assertEqual((self.root / name).read_text(), "UNRELATED draft")

    def test_changed_destination_conflicts_before_any_replacement(self):
        self.prepare()
        (self.root / "findings.md").write_text("UNRELATED concurrent content\n")
        with self.assertRaises(RecordError):
            self.records.resume("A01", "owner")
        self.assertEqual((self.root / "websites.md").read_text(), self.old["websites.md"])
        self.assertEqual((self.root / "findings.md").read_text(), "UNRELATED concurrent content\n")
        self.assertEqual(self.records.status("A01")["status"], "pending")

    def test_partial_conflict_abandon_and_reconcile_new_attempt(self):
        self.prepare()
        def interrupt(point, number):
            if (point, number) == ("after_replace", 1):
                raise RuntimeError("synthetic interruption")
        with self.assertRaises(RuntimeError):
            self.records.resume("A01", "owner", fault=interrupt)
        (self.root / "findings.md").write_text("UNRELATED\n")
        with self.assertRaises(RecordError):
            self.records.resume("A01", "owner")
        self.records.abandon("A01", "owner")
        self.assertEqual(self.records.status("A01")["status"], "abandoned_incomplete")
        self.assertFalse(list(self.records.control.glob("A01-*.md*")))
        merged = {name: text.replace("A01", "A02") for name, text in self.new.items()}
        merged["findings.md"] = "UNRELATED\n" + merged["findings.md"]
        self.records.begin("S-test", "A02", "owner", self.names)
        self.records.stage("A02", "owner", merged)
        self.records.resume("A02", "owner")
        for name, text in merged.items():
            self.assertEqual((self.root / name).read_text(), text)
            self.assertEqual(text.count("S-test"), 1)

    def test_owner_change_requires_stopped_assertion(self):
        self.prepare()
        with self.assertRaises(RecordError):
            self.records.takeover("A01", "owner", "new-owner")
        self.records.takeover("A01", "owner", "new-owner", previous_stopped=True)
        with self.assertRaises(RecordError):
            self.records.resume("A01", "owner")
        self.records.resume("A01", "new-owner"); self.assert_completed()

    def test_tampered_candidate_stops_without_publication(self):
        self.prepare()
        (self.records.control / "A01-0.md").write_text("tampered")
        with self.assertRaises(RecordError):
            self.records.resume("A01", "owner")
        for name, text in self.old.items():
            self.assertEqual((self.root / name).read_text(), text)

    def test_path_escape_and_duplicate_attempt_rejected(self):
        with self.assertRaises(RecordError):
            self.records.begin("S-test", "A01", "owner", ["../outside.md"])
        self.prepare(); self.records.resume("A01", "owner")
        with self.assertRaises(RecordError):
            self.records.begin("S-test", "A01", "owner", self.names)

    def test_malformed_control_fails_closed(self):
        (self.records.control / "broken.json").write_text("broken")
        with self.assertRaises(ValueError):
            self.records.begin("S-test", "A01", "owner", self.names)

    def test_completion_marker_failure_leaves_pending_recoverable_attempt(self):
        self.prepare()
        manifest = self.records.manifest_path("A01")
        def fail_marker(path, data, **kwargs):
            if path == manifest:
                raise OSError("synthetic marker write failure")
            return atomic(path, data, **kwargs)
        with patch("record_update.atomic", side_effect=fail_marker):
            with self.assertRaises(OSError):
                self.records.resume("A01", "owner")
        self.assertEqual(self.records.status("A01")["status"], "pending")
        self.assertEqual(set(self.records.status("A01")["file_states"].values()), {"applied"})
        self.records.resume("A01", "owner"); self.assert_completed()

    def test_cli_roundtrip_uses_only_disposable_records(self):
        import json
        tool = Path(__file__).resolve().parents[1] / "tools/record_update.py"
        def command(*args):
            return subprocess.run([sys.executable, "-B", str(tool), str(self.root), *args],
                                  text=True, capture_output=True, timeout=15)
        self.assertEqual(command("begin", "S-cli", "A-cli", "cli", "findings.md").returncode, 0)
        draft = self.root / "replacement.md"
        draft.write_text("KEEP CLI\nS-cli A-cli entry\n")
        result = command("stage", "A-cli", "cli", f"findings.md={draft}")
        self.assertEqual(result.returncode, 0, result.stderr)
        wrong_owner = command("resume", "A-cli", "other")
        self.assertEqual(wrong_owner.returncode, 2)
        self.assertIn("write owner", wrong_owner.stderr)
        self.assertEqual(command("resume", "A-cli", "cli").returncode, 0)
        result = command("status", "A-cli")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["status"], "completed")
        self.assertEqual((self.root / "findings.md").read_text(), draft.read_text())


if __name__ == "__main__":
    multiprocessing.freeze_support()
    unittest.main(verbosity=2)
