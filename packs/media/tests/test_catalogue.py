import contextlib
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from media_lab.__main__ import main
from media_lab.catalogue import Catalogue
from media_lab.errors import MediaLabError
from media_lab.identity import source_path
from media_lab.ingest import ingest, verify
from media_lab.probe import FFprobe, normalize


def sample_metadata():
    return {"streams": [{"index": 0, "codec_type": "video", "codec_name": "h264",
                         "width": 1920, "height": 1080, "avg_frame_rate": "60000/1001",
                         "r_frame_rate": "60000/1001", "start_pts": 9000,
                         "start_time": "0.1", "time_base": "1/90000"}],
            "format": {"format_name": "mov,mp4", "duration": "10.5", "start_time": "0.1"}}


class FakeProbe:
    version = "test-probe 1"

    def __init__(self):
        self.calls = 0

    def inspect(self, path):
        self.calls += 1
        raw = sample_metadata()
        return normalize(raw), raw


class CatalogueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.original = self.root / "camera file.mp4"
        # This is a unit-test byte fixture, not real video. Integration tests use FFmpeg.
        self.original.write_bytes(b"original camera bytes")
        self.catalogue = Catalogue(self.root / "catalogue.sqlite3")
        self.addCleanup(self.catalogue.close)
        self.probe = FakeProbe()

    def add(self, path=None, **kwargs):
        return ingest(self.catalogue, path or self.original, probe=self.probe, **kwargs)

    def assertError(self, code, function, *args, **kwargs):
        with self.assertRaises(MediaLabError) as result:
            function(*args, **kwargs)
        self.assertEqual(result.exception.code, code)

    def test_original_unchanged_and_provenance_persists(self):
        before = self.original.stat()
        content = self.original.read_bytes()
        result = self.add()
        self.assertEqual(result["outcome"], "new")
        self.assertEqual(result["media_id"], "sha256:" + hashlib.sha256(content).hexdigest())
        self.assertEqual(self.original.read_bytes(), content)
        self.assertEqual(self.original.stat().st_mtime_ns, before.st_mtime_ns)
        row = self.catalogue.media(hashlib.sha256(content).hexdigest())
        self.assertEqual(json.loads(row["probe_json"]), sample_metadata())
        self.assertEqual(row["probe_version"], self.probe.version)
        self.assertEqual(json.loads(row["metadata_json"])["video"]["time_base"], "1/90000")
        with Catalogue(self.catalogue.path) as reopened:
            self.assertEqual(len(reopened.search()), 1)

    def test_duplicate_uses_explicitly_labelled_cache(self):
        self.add()
        with patch("media_lab.ingest.hash_source", side_effect=AssertionError("unnecessary rehash")):
            self.assertEqual(self.add()["outcome"], "already_known_cached")
        self.assertEqual(self.probe.calls, 1)
        self.assertEqual(len(self.catalogue.search()), 1)

    def test_rehash_reads_bytes_without_reprobing(self):
        self.add()
        result = self.add(rehash=True)
        self.assertEqual(result["outcome"], "already_known_verified")
        self.assertEqual(result["identity_check"], "sha256")
        self.assertEqual(self.probe.calls, 1)

    def test_copy_at_new_path_deduplicates_content(self):
        first = self.add()
        second = self.root / "copied.mp4"
        second.write_bytes(self.original.read_bytes())
        result = self.add(second)
        self.assertEqual(result["outcome"], "known_content_new_path")
        self.assertEqual(result["media_id"], first["media_id"])
        self.assertEqual(len(self.catalogue.search()), 1)
        self.assertEqual(len(self.catalogue.search()[0]["locations"]), 2)

    def test_renamed_source_keeps_identity_and_missing_path_is_reported(self):
        first = self.add()
        renamed = self.root / "renamed.mp4"
        self.original.rename(renamed)
        self.assertEqual(self.add(renamed)["media_id"], first["media_id"])
        self.assertError("missing_source", verify, self.catalogue, self.original)
        self.assertEqual(self.catalogue.location(source_path(self.original))["status"], "missing_source")

    def test_changed_path_keeps_previous_media_and_event(self):
        first = self.add()
        self.original.write_bytes(b"different camera bytes")
        result = self.add()
        self.assertEqual(result["outcome"], "changed")
        self.assertNotEqual(result["media_id"], first["media_id"])
        self.assertEqual(len(self.catalogue.search()), 2)
        self.assertIsNotNone(self.catalogue.media(first["media_id"].split(":")[1]))
        self.assertEqual(len(self.catalogue.search()[0]["locations"]), 0)

    def test_missing_and_directory_inputs(self):
        self.assertError("missing_source", self.add, self.root / "missing.mp4")
        self.assertError("unsupported_source", self.add, self.root)
        self.assertEqual(self.catalogue.search(), [])

    def test_probe_failure_does_not_register_media(self):
        self.probe.inspect = lambda _: (_ for _ in ()).throw(MediaLabError("probe_failed", "unreadable media"))
        self.assertError("probe_failed", self.add)
        self.assertEqual(self.catalogue.search(), [])
        event = self.catalogue.connection.execute("SELECT outcome FROM import_events").fetchone()
        self.assertEqual(event[0], "probe_failed")

    def test_failure_for_changed_file_invalidates_old_location(self):
        self.add()
        self.original.write_bytes(b"bad new contents")
        self.probe.inspect = lambda _: (_ for _ in ()).throw(MediaLabError("probe_failed", "bad file"))
        self.assertError("probe_failed", self.add)
        self.assertEqual(self.catalogue.location(source_path(self.original))["status"], "probe_failed")

    def test_source_changed_during_inspection_is_not_registered(self):
        def mutate(path):
            # Change size deterministically; rapid same-size writes can share filesystem timestamps.
            path.write_bytes(b"still being copied... more bytes arrived")
            raw = sample_metadata()
            return normalize(raw), raw
        self.probe.inspect = mutate
        self.assertError("source_changed", self.add)
        self.assertEqual(self.catalogue.search(), [])

    def test_interrupted_probe_is_restart_safe(self):
        with patch.object(self.probe, "inspect", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.add()
        self.assertEqual(self.catalogue.search(), [])
        self.assertEqual(self.add()["outcome"], "new")

    def test_transaction_rolls_back_if_event_write_fails(self):
        with patch.object(self.catalogue, "event", side_effect=sqlite3.OperationalError("simulated full disk")):
            with self.assertRaises(sqlite3.OperationalError):
                self.add()
        self.assertEqual(self.catalogue.search(), [])
        self.assertEqual(self.add()["outcome"], "new")

    def test_verify_reads_all_bytes_and_detects_same_size_replacement(self):
        self.add()
        before = self.original.stat()
        self.original.write_bytes(b"x" * before.st_size)
        os.utime(self.original, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertError("checksum_mismatch", verify, self.catalogue, self.original)
        self.assertEqual(self.catalogue.location(source_path(self.original))["status"], "checksum_mismatch")
        self.assertEqual(len(self.catalogue.search()), 1)

    def test_verify_success_does_not_claim_decode_integrity(self):
        self.add()
        result = verify(self.catalogue, self.original)
        self.assertEqual(result["outcome"], "checksum_verified")
        self.assertEqual(result["decode_integrity"], "not_tested")

    def test_verify_unknown_does_not_import(self):
        self.assertError("unknown_source", verify, self.catalogue, self.original)

    def test_search_is_literal_and_parameterized(self):
        self.add()
        self.assertEqual(len(self.catalogue.search("camera file")), 1)
        self.assertEqual(self.catalogue.search("%"), [])
        self.assertEqual(self.catalogue.search("' OR 1=1 --"), [])

    def test_newer_schema_is_not_modified(self):
        newer = self.root / "newer.sqlite3"
        with contextlib.closing(sqlite3.connect(newer)) as db:
            db.execute("PRAGMA user_version=99")
        before = newer.read_bytes()
        self.assertError("catalogue_version", Catalogue, newer)
        self.assertEqual(newer.read_bytes(), before)

    def test_unrelated_database_is_not_modified(self):
        other = self.root / "unrelated.sqlite3"
        with contextlib.closing(sqlite3.connect(other)) as db:
            db.execute("CREATE TABLE other_project(value TEXT)")
        before = other.read_bytes()
        self.assertError("unrelated_database", Catalogue, other)
        self.assertEqual(other.read_bytes(), before)

    def test_cli_rejects_source_as_database_before_opening(self):
        source = self.root / "footage.db"
        source.write_bytes(b"original")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(["--db", str(source), "ingest", str(source)])
        self.assertEqual(code, 1)
        self.assertIn("invalid_database_path", output.getvalue())
        self.assertEqual(source.read_bytes(), b"original")

    def test_cli_missing_dependency_is_actionable(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(["--ffprobe", str(self.root / "absent-ffprobe"), "doctor"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())["error"], "invalid_tool_config")


class ProbeTests(unittest.TestCase):
    def test_unknown_fields_remain_null(self):
        raw = sample_metadata()
        del raw["format"]["duration"]
        raw["streams"][0]["avg_frame_rate"] = "0/0"
        value = normalize(raw)
        self.assertIsNone(value["duration_seconds"])
        self.assertIsNone(value["video"]["average_frame_rate"])
        self.assertIsNone(value["creation_time_tag"])
        self.assertEqual(value["audio_streams"], [])

    def test_rational_rates_and_nonzero_start_are_preserved(self):
        value = normalize(sample_metadata())
        self.assertEqual(value["video"]["average_frame_rate"], "60000/1001")
        self.assertEqual(value["video"]["start_pts"], 9000)
        self.assertEqual(value["container_start_seconds"], 0.1)

    def test_nan_and_infinite_duration_are_unknown(self):
        for value in ("NaN", "inf", "-1", "N/A", None):
            raw = sample_metadata()
            raw["format"]["duration"] = value
            self.assertIsNone(normalize(raw)["duration_seconds"])

    def test_embedded_time_is_not_claimed_as_verified(self):
        raw = sample_metadata()
        raw["format"]["tags"] = {"creation_time": "2026-09-25T12:00:00Z"}
        self.assertEqual(normalize(raw)["creation_time_tag"], "2026-09-25T12:00:00Z")
        self.assertFalse(normalize(raw)["creation_time_verified"])

    def test_audio_metadata_is_retained(self):
        raw = sample_metadata()
        raw["streams"].append({"index": 1, "codec_type": "audio", "codec_name": "aac",
                               "channels": 2, "sample_rate": "48000", "start_time": "0.02"})
        value = normalize(raw)["audio_streams"][0]
        self.assertEqual(value["channels"], 2)
        self.assertEqual(value["start_seconds"], 0.02)

    def test_audio_only_and_cover_art_are_not_video(self):
        for streams in ([{"codec_type": "audio"}], [{"codec_type": "video", "disposition": {"attached_pic": 1}}]):
            with self.assertRaises(MediaLabError) as result:
                normalize({"streams": streams})
            self.assertEqual(result.exception.code, "unsupported_media")

    def test_malformed_metadata(self):
        for raw in (None, [], {}, {"streams": [{"codec_type": "video", "disposition": "bad"}]}, {"streams": [None]}, {"streams": [], "format": []},
                    {"streams": [{"codec_type": "video", "width": 0, "height": 1080}]}):
            with self.assertRaises(MediaLabError):
                normalize(raw)

    def test_probe_is_structured_and_disables_network_protocols(self):
        with patch("media_lab.probe.find_tool", return_value="ffprobe"), patch("media_lab.probe.run_tool") as run:
            run.side_effect = [subprocess.CompletedProcess([], 0, "ffprobe test\n", ""),
                               subprocess.CompletedProcess([], 0, json.dumps(sample_metadata()), "")]
            probe = FFprobe()
            source = Path('file with spaces & symbols.mp4')
            probe.inspect(source)
            args = run.call_args.args[0]
            self.assertEqual(args[-1], str(source))
            self.assertEqual(args[args.index("-protocol_whitelist") + 1], "file")


if __name__ == "__main__":
    unittest.main()
