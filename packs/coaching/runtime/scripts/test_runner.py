"""Offline end-to-end report runner check using fictional observation data."""

import hashlib
import json
from dataclasses import replace
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.test_activity_linkage import START, observation
from src.ingestion.activity_storage import ObservationBatch, store_activity_observations


class RunnerTests(unittest.TestCase):
    def test_utc_report_cli_from_workspace_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "activities.sqlite3"
            output = root / "report.json"
            row = observation("summary", "fictional", start_time=START, sport="cycling", subtype=None)
            row = replace(row, source_file=replace(row.source_file,
                checksum_sha256=hashlib.sha256(b"fictional").hexdigest()))
            store_activity_observations(db, ObservationBatch((row,), row.source_file, {}))
            runner = Path(__file__).resolve().parents[1] / "run.py"
            workspace = runner.parents[3]
            command = [sys.executable, "-B", str(runner), "report", "--database", str(db),
                       "--timezone", "UTC", "--format", "json", "--output", str(output)]
            result = subprocess.run(command, cwd=workspace, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["analytics"]["activity_count"], 1)
            again = subprocess.run(command, cwd=workspace, capture_output=True, text=True)
            self.assertEqual(again.returncode, 1)
            self.assertEqual(again.stderr.strip(), "report_output_failed")


if __name__ == "__main__":
    unittest.main()
