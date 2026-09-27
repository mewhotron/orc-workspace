"""Synthetic ZIP/FIT persistence regression tests; no private fixtures."""

from contextlib import closing, redirect_stdout
from dataclasses import replace
from datetime import timedelta
import hashlib
import io
import json
from pathlib import Path
import shutil
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import warnings
import zipfile

from garmin_fit_sdk import Encoder, Profile
from scripts.import_garmin_fit_activities import main as cli
from scripts.test_garmin_fit import START
from src.ingestion.activity_storage import (
    ActivityStorageError, ObservationConflictError, canonical_observation_payload,
    import_garmin_summary, load_activity_observations, store_activity_observations,
)
from src.ingestion.garmin_fit_import import (
    GarminFitImportError, import_garmin_fit_activities, parse_garmin_fit_archive,
)


def fit(kind='activity', product=1):
    encoder = Encoder()
    encoder.on_mesg(Profile['mesg_num']['FILE_ID'], {
        'type': kind, 'manufacturer': 'development', 'product': product,
        'time_created': START,
    })
    encoder.on_mesg(Profile['mesg_num']['SESSION'], {
        'start_time': START, 'timestamp': START + timedelta(seconds=100),
        'sport': 'cycling', 'sub_sport': 'road',
        'total_elapsed_time': 100, 'total_distance': 1234.56,
    })
    return bytes(encoder.close())


class FitImportTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.archive = self.root / 'synthetic.zip'
        self.database = self.root / 'observations.sqlite3'

    def zip(self, entries):
        with warnings.catch_warnings(), zipfile.ZipFile(self.archive, 'w') as archive:
            warnings.simplefilter('ignore', UserWarning)
            for name, content in entries:
                archive.writestr(name, content)

    def snapshot(self):
        with closing(sqlite3.connect(self.database)) as db:
            return {name: db.execute('SELECT * FROM ' + name).fetchall() for name in (
                'activity_exports', 'activity_imports', 'activity_observations', 'activity_import_members')}

    def test_single_member_lossless_and_no_extraction(self):
        raw = fit()
        self.zip([('folder/private.fit', raw)])
        before = self.archive.read_bytes(), self.archive.stat().st_mtime_ns
        with (patch('zipfile.ZipFile.extract', side_effect=AssertionError('extraction')),
              patch('zipfile.ZipFile.extractall', side_effect=AssertionError('extraction'))):
            batch = parse_garmin_fit_archive(self.archive)
            report = store_activity_observations(self.database, batch)
        self.assertEqual((report.inserted, report.parser_rejected), (1, 0))
        records = load_activity_observations(self.database)
        self.assertEqual(records, batch.member_batches[0].records)
        record = records[0]
        self.assertIsNone(record.source_activity_id)
        self.assertEqual(record.source_record_index, 0)
        self.assertEqual(record.source_file.original_path, self.archive)
        self.assertEqual(record.source_file.source, 'garmin')
        self.assertEqual(record.source_file.checksum_sha256, hashlib.sha256(raw).hexdigest())
        self.assertEqual(record.source_file.archive_checksum_sha256, hashlib.sha256(before[0]).hexdigest())
        self.assertEqual(record.source_file.archive_member, 'folder/private.fit')
        self.assertIsNotNone(record.source_file.discovered_at.utcoffset())
        row = self.snapshot()['activity_observations'][0]
        self.assertEqual(row[5], canonical_observation_payload(record))
        self.assertEqual(row[6], hashlib.sha256(row[5].encode()).hexdigest())
        self.assertEqual((self.archive.read_bytes(), self.archive.stat().st_mtime_ns), before)
        self.assertEqual({p.name for p in self.root.iterdir()}, {'synthetic.zip', 'observations.sqlite3'})

    def test_multiple_members_repeat_and_relocation(self):
        self.zip([('one.fit', fit()), ('two.fit', fit(product=2))])
        first = import_garmin_fit_activities(self.archive, self.database)
        before = self.snapshot()
        records = load_activity_observations(self.database)
        self.assertEqual((first.inserted, first.final_observation_count), (2, 2))
        self.assertEqual([r.source_record_index for r in records], [0, 0])
        self.assertEqual(len({r.source_file.checksum_sha256 for r in records}), 2)
        second = import_garmin_fit_activities(self.archive, self.database)
        moved = self.root / 'moved.zip'
        shutil.copy2(self.archive, moved)
        third = import_garmin_fit_activities(moved, self.database)
        for report in (second, third):
            self.assertEqual((report.inserted, report.already_existing, report.final_observation_count), (0, 2, 2))
        after = self.snapshot()
        self.assertEqual(before['activity_observations'], after['activity_observations'])
        self.assertEqual(load_activity_observations(self.database), records)
        self.assertEqual(len(after['activity_import_members']), 6)
        self.assertEqual(len(after['activity_imports']), 9)  # Three operations, two member receipts each.
        parents = {row[0] for row in after['activity_import_members']}
        self.assertEqual(len(parents), 3)
        self.assertEqual([row[2] for row in after['activity_imports'] if row[0] in parents],
                         [str(self.archive), str(self.archive), str(moved)])

    def test_normalizer_gate_nonactivities_and_malformed_fit(self):
        self.zip([('valid.fit', fit()), ('broken.fit', b'private invalid FIT'),
                  *[(f'type-{kind}.fit', fit(kind)) for kind in (41, 44, 57, 'course')]])
        report = import_garmin_fit_activities(self.archive, self.database)
        self.assertEqual((report.parser_accepted, report.parser_rejected, report.inserted), (1, 5, 1))
        self.assertEqual(len(load_activity_observations(self.database)), 1)
        self.assertNotIn('private', repr(report.rejection_diagnostics))

    def test_duplicate_names_excluded_and_identical_content_idempotent(self):
        self.zip([('ambiguous.fit', fit()), ('ambiguous.fit', fit(product=2)),
                  ('one.fit', fit()), ('copy.fit', fit())])
        report = import_garmin_fit_activities(self.archive, self.database)
        self.assertEqual((report.parser_accepted, report.parser_rejected, report.inserted,
                          report.already_existing), (2, 2, 1, 1))
        self.assertEqual(len(self.snapshot()['activity_import_members']), 2)

    def test_matching_summary_and_fit_are_independent(self):
        summary = self.root / 'summary.json'
        summary.write_text(json.dumps([{'summarizedActivitiesExport': [{
            'activityId': 123, 'activityType': 'road_biking',
            'startTimeGmt': int(START.timestamp() * 1000),
            'distance': 123456, 'elapsedDuration': 100000,
        }]}]))
        import_garmin_summary(summary, self.database)
        before = self.snapshot()['activity_observations']
        self.zip([('one.fit', fit())])
        report = import_garmin_fit_activities(self.archive, self.database)
        self.assertEqual((report.inserted, report.final_observation_count), (1, 2))
        records = load_activity_observations(self.database)
        a, b = records
        self.assertEqual((a.start_time, a.sport, a.metrics.distance_m, a.metrics.elapsed_time_s),
                         (b.start_time, b.sport, b.metrics.distance_m, b.metrics.elapsed_time_s))
        self.assertNotEqual(a.source_file.checksum_sha256, b.source_file.checksum_sha256)
        self.assertIn(before[0], self.snapshot()['activity_observations'])

    def test_conflict_rolls_back_whole_archive_operation(self):
        self.zip([('one.fit', fit())])
        import_garmin_fit_activities(self.archive, self.database)
        before = self.snapshot()
        self.zip([('new.fit', fit(product=2)), ('one.fit', fit())])
        batch = parse_garmin_fit_archive(self.archive)
        new, existing = batch.member_batches
        changed = replace(existing, records=(replace(existing.records[0], name='conflict'),))
        with self.assertRaises(ObservationConflictError):
            store_activity_observations(self.database, replace(batch, member_batches=(new, changed)))
        self.assertEqual(self.snapshot(), before)

    def test_unsafe_paths_fail_before_storage(self):
        for name in ('/private.fit', '../private.fit', 'a/../private.fit',
                     'a\\private.fit', 'C:/private.fit', 'a//private.fit', './private.fit'):
            self.zip([('valid.fit', fit()), (name, fit())])
            if '\\' in name:
                self.archive.write_bytes(self.archive.read_bytes().replace(
                    name.replace('\\', '/').encode(), name.encode()))
            with self.assertRaisesRegex(GarminFitImportError, '^unsafe_fit_archive_member$'):
                import_garmin_fit_activities(self.archive, self.database)
            self.assertFalse(self.database.exists())
        # Patch both ZIP filename copies because writestr truncates NULs itself.
        self.zip([('private.fit', fit())])
        self.archive.write_bytes(self.archive.read_bytes().replace(b'private.fit', b'priv\x00te.fit'))
        with self.assertRaisesRegex(GarminFitImportError, '^unsafe_fit_archive_member$'):
            import_garmin_fit_activities(self.archive, self.database)

    def test_unreadable_encrypted_and_malformed_archive_are_private(self):
        for content in (None, b'private malformed ZIP'):
            if content is not None:
                self.archive.write_bytes(content)
            with self.assertRaisesRegex(GarminFitImportError, '^fit_archive_read_failed$'):
                import_garmin_fit_activities(self.archive, self.database)
        self.zip([('one.fit', fit())])
        with patch('zipfile.ZipFile.open', side_effect=RuntimeError('private encrypted member')):
            with self.assertRaisesRegex(GarminFitImportError, '^fit_archive_read_failed$'):
                import_garmin_fit_activities(self.archive, self.database)
        original = zipfile.ZipFile.infolist
        def encrypted(archive):
            entries = original(archive)
            entries[0].flag_bits |= 1
            return entries
        with patch('zipfile.ZipFile.infolist', encrypted):
            with self.assertRaisesRegex(GarminFitImportError, '^unsupported_fit_archive_member$'):
                import_garmin_fit_activities(self.archive, self.database)
        self.assertFalse(self.database.exists())

    def test_changed_archive_and_size_limits_write_nothing(self):
        self.zip([('one.fit', fit())])
        with patch('src.ingestion.garmin_fit_import.sha256_file', return_value='0' * 64):
            with self.assertRaisesRegex(GarminFitImportError, '^fit_archive_changed_during_read$'):
                import_garmin_fit_activities(self.archive, self.database)
        with patch('src.ingestion.garmin_fit_import.MAX_MEMBER_BYTES', 1):
            with self.assertRaisesRegex(GarminFitImportError, '^fit_archive_member_size_limit$'):
                import_garmin_fit_activities(self.archive, self.database)
        self.assertFalse(self.database.exists())

    def test_empty_accepted_batch_creates_only_operation_receipt(self):
        self.zip([('course.fit', fit('course'))])
        report = import_garmin_fit_activities(self.archive, self.database)
        self.assertEqual((report.inserted, report.parser_rejected), (0, 1))
        self.assertEqual(len(self.snapshot()['activity_imports']), 1)
        self.assertEqual(load_activity_observations(self.database), ())

    def test_generic_group_provenance_is_checked(self):
        self.zip([('one.fit', fit())])
        batch = parse_garmin_fit_archive(self.archive)
        child = batch.member_batches[0]
        bad = replace(child, source_file=replace(child.source_file, archive_checksum_sha256='a' * 64))
        with self.assertRaisesRegex(ActivityStorageError, '^inconsistent_archive_batch_provenance$'):
            store_activity_observations(self.database, replace(batch, member_batches=(bad,)))
        self.assertFalse(self.database.exists())

    def test_stored_archive_receipt_links_are_validated(self):
        self.zip([('one.fit', fit())])
        import_garmin_fit_activities(self.archive, self.database)
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("UPDATE activity_imports SET source_path='private' WHERE import_id=1")
            db.commit()
        with self.assertRaisesRegex(ActivityStorageError, '^invalid_archive_receipt_link$'):
            load_activity_observations(self.database)

    def test_cli_aggregates_help_and_private_failure(self):
        self.zip([('private-name.fit', fit())])
        output = io.StringIO()
        with redirect_stdout(output):
            status = cli([str(self.archive), '--database', str(self.database)])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue())['inserted'], 1)
        self.assertNotIn('private', output.getvalue())
        self.assertNotIn(str(self.root), output.getvalue())
        self.assertNotIn('rejection_diagnostics', output.getvalue())
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as caught:
            cli(['--help'])
        self.assertEqual(caught.exception.code, 0)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli([str(self.root / 'private-missing.zip')]), 1)


if __name__ == '__main__':
    unittest.main()
