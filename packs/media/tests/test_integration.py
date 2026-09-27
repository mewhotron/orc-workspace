"""Generated media only: no private source footage is used or uploaded."""

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from media_lab.catalogue import Catalogue
from media_lab.errors import MediaLabError
from media_lab.ingest import ingest, verify
from media_lab.probe import FFprobe, PROJECT_ROOT, find_tool
from integration_support import handle_tool_error


class RealMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.ffmpeg = find_tool("ffmpeg")
            cls.probe = FFprobe()
        except MediaLabError as exc:
            handle_tool_error(exc)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.catalogue = Catalogue(self.root / "catalogue.sqlite3")
        self.addCleanup(self.catalogue.close)

    def generate(self, name, codec="libx264", audio=False):
        path = self.root / name
        args = [self.ffmpeg, "-nostdin", "-v", "error", "-n", "-f", "lavfi",
                "-i", "testsrc2=size=160x90:rate=30000/1001:duration=1"]
        if audio:
            args += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1", "-c:a", "aac"]
        args += ["-c:v", codec, "-threads", "1", "-pix_fmt", "yuv420p"]
        if codec == "libx265":
            args += ["-x265-params", "pools=none:frame-threads=1:log-level=error"]
        args += [str(path)]
        subprocess.run(args, capture_output=True, check=True, timeout=60)
        return path

    def test_h264_audio_identity_metadata_and_rename(self):
        original = self.generate("cycling test & [1].mp4", audio=True)
        before = hashlib.sha256(original.read_bytes()).hexdigest()
        mtime = original.stat().st_mtime_ns
        first = ingest(self.catalogue, original, probe=self.probe)
        self.assertEqual(first["outcome"], "new")
        self.assertEqual(ingest(self.catalogue, original)["outcome"], "already_known_cached")
        self.assertEqual(verify(self.catalogue, original)["outcome"], "checksum_verified")
        record = self.catalogue.search()[0]
        self.assertEqual(record["metadata"]["video"]["width"], 160)
        self.assertEqual(record["metadata"]["video"]["codec"], "h264")
        self.assertEqual(record["metadata"]["video"]["average_frame_rate"], "30000/1001")
        self.assertEqual(record["metadata"]["audio_streams"][0]["codec"], "aac")
        self.assertAlmostEqual(record["metadata"]["duration_seconds"], 1.001, delta=0.05)
        self.assertEqual(hashlib.sha256(original.read_bytes()).hexdigest(), before)
        self.assertEqual(original.stat().st_mtime_ns, mtime)
        renamed = original.with_name("renamed.mp4")
        original.rename(renamed)
        result = ingest(self.catalogue, renamed)
        self.assertEqual(result["outcome"], "known_content_new_path")
        self.assertEqual(result["media_id"], first["media_id"])

    def test_hevc_without_audio(self):
        original = self.generate("hevc.mp4", codec="libx265")
        ingest(self.catalogue, original, probe=self.probe)
        metadata = self.catalogue.search()[0]["metadata"]
        self.assertEqual(metadata["video"]["codec"], "hevc")
        self.assertEqual(metadata["audio_streams"], [])

    def test_invalid_video_fails_without_cataloguing(self):
        invalid = self.root / "broken.mp4"
        invalid.write_bytes(b"not a video")
        with self.assertRaises(MediaLabError) as result:
            ingest(self.catalogue, invalid, probe=self.probe)
        self.assertEqual(result.exception.code, "probe_failed")
        self.assertEqual(self.catalogue.search(), [])
        self.assertEqual(invalid.read_bytes(), b"not a video")

    def test_cli_batch_continues_after_failure_and_reports_nonzero(self):
        original = self.generate("batch.mp4")
        result = subprocess.run(
            [sys.executable, "-m", "media_lab", "--db", str(self.catalogue.path),
             "--ffprobe", self.probe.executable, "ingest", str(self.root / "absent.mp4"), str(original)],
            cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["failures"], 1)
        self.assertEqual(payload["results"][1]["outcome"], "new")
        self.assertEqual(len(self.catalogue.search()), 1)


if __name__ == "__main__":
    unittest.main()
