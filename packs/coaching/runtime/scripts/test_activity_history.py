"""Synthetic temporary-database tests for the public history boundary."""

from contextlib import closing
from dataclasses import replace
from datetime import timedelta, timezone
import hashlib
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts.test_activity_linkage import START, observation
from src.history import ActivityHistoryError, HistoryFilter, read_activity_history
from src.ingestion.activity_storage import ObservationBatch, store_activity_observations
from src.linkage.activity import LINKAGE_VERSION
from src.projection.activity import PROJECTION_VERSION


class ActivityHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / 'history.sqlite3'
        self.summary, self.fit = observation('summary'), observation('fit')

    def persist(self, records, database=None):
        database = self.database if database is None else database
        if not records:
            store_activity_observations(database, ObservationBatch((), self.summary.source_file, {}))
        for record in records:
            source = replace(record.source_file,
                             checksum_sha256=hashlib.sha256(record.record_id.encode()).hexdigest())
            record = replace(record, source_file=source)
            store_activity_observations(database, ObservationBatch((record,), source, {}))

    def history(self, filters=None):
        return read_activity_history(self.database, filters)

    def test_empty_history(self):
        self.persist([])
        result = self.history()
        self.assertEqual(result.activities, ())
        self.assertEqual(result.inventory.total_activities, 0)
        self.assertIsNone(result.inventory.earliest_start)
        self.assertIsNone(result.inventory.latest_start)
        self.assertEqual(result.inventory.counts_by_sport, ())
        self.assertEqual(result.diagnostics.source_observation_count, 0)

    def test_singleton(self):
        self.persist([self.summary])
        result = self.history()
        self.assertEqual(len(result.activities), 1)
        self.assertEqual(result.diagnostics.singleton_count, 1)
        self.assertEqual(result.conflicts, ())
        self.assertEqual(result.inventory.earliest_start, START)
        self.assertEqual(result.inventory.counts_by_sport, (('cycling', 1),))
        self.assertEqual(result.inventory.counts_by_subtype, (('road_biking', 1),))

    def test_linked_pair_diagnostics_versions_and_provenance(self):
        self.persist([self.summary, self.fit])
        result = self.history()
        self.assertEqual(len(result.activities), 1)
        self.assertEqual(result.unfiltered_activity_count, 1)
        diag = result.diagnostics
        self.assertEqual((diag.source_observation_count, diag.strong_link_count,
                          diag.authoritative_link_count, diag.linked_pair_count, diag.singleton_count),
                         (2, 1, 0, 1, 0))
        self.assertEqual((diag.ambiguous_group_count, diag.projection_conflict_count), (0, 0))
        self.assertEqual(result.linkage_version, LINKAGE_VERSION)
        self.assertEqual(result.projection_version, PROJECTION_VERSION)
        activity = result.activities[0]
        self.assertEqual(len(activity.contributors), 2)
        selected = activity.selected('distance_m')
        self.assertEqual(selected.selected_from.record_id, self.fit.record_id)
        self.assertTrue(selected.selection_rule)
        self.assertEqual(len(selected.corroborated_by), 1)
        self.assertEqual(len(activity.external_ids), 2)

    def test_chronological_and_tie_ordering(self):
        records = [observation('summary', 'later', start_time=START + timedelta(days=2)),
                   self.summary, observation('summary', 'tie')]
        self.persist(records)
        result = self.history()
        keys = [(a.selected('start_time').value, a.canonical_id) for a in result.activities]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(result.inventory.latest_start, START + timedelta(days=2))

    def test_date_bounds_and_timezone_equivalence(self):
        self.persist([self.summary, observation('summary', 'second', start_time=START + timedelta(days=1)),
                      observation('summary', 'third', start_time=START + timedelta(days=2))])
        filtered = self.history(HistoryFilter(start=START, end=START + timedelta(days=1)))
        self.assertEqual(len(filtered.activities), 1)
        self.assertEqual(filtered.inventory.earliest_start, START)
        self.assertEqual(len(self.history(HistoryFilter(start=START + timedelta(days=1))).activities), 2)
        self.assertEqual(len(self.history(HistoryFilter(end=START)).activities), 0)
        self.assertEqual(len(self.history(HistoryFilter(start=START, end=START)).activities), 0)
        local = START.astimezone(timezone(timedelta(hours=3)))
        self.assertEqual(self.history(HistoryFilter(start=local, end=local + timedelta(days=1))), filtered)

    def test_sport_subtype_and_combined_filters(self):
        mountain = observation('summary', 'mountain', subtype='mountain_biking',
                               source_activity_type='mountain_biking', start_time=START + timedelta(days=1))
        strength = observation('summary', 'strength', sport='strength_training', subtype=None,
                               source_activity_type='strength_training', start_time=START + timedelta(days=2))
        self.persist([self.summary, mountain, strength])
        self.assertEqual(len(self.history(HistoryFilter(sport='cycling')).activities), 2)
        self.assertEqual(len(self.history(HistoryFilter(subtype='mountain_biking')).activities), 1)
        result = self.history(HistoryFilter(start=START, end=START + timedelta(days=2),
                                          sport='cycling', subtype='mountain_biking'))
        self.assertEqual(len(result.activities), 1)
        self.assertEqual(result.inventory.earliest_start, mountain.start_time)
        self.assertEqual(result.inventory.counts_by_subtype, (('mountain_biking', 1),))
        self.assertEqual(self.history().inventory.counts_by_subtype,
                         ((None, 1), ('mountain_biking', 1), ('road_biking', 1)))

    def test_filter_uses_projected_subtype_and_diagnostics_stay_global(self):
        summary = replace(self.summary, subtype=None, source_activity_type='cycling')
        fit = replace(self.fit, subtype='commuting',
                      source_activity_type='{"sport":"cycling","sub_sport":"commuting"}')
        self.persist([summary, fit])
        result = self.history(HistoryFilter(subtype='commuting'))
        self.assertEqual(len(result.activities[0].contributors), 2)
        empty = self.history(HistoryFilter(sport='swimming'))
        self.assertEqual(empty.activities, ())
        self.assertEqual(empty.inventory.total_activities, 0)
        self.assertEqual(empty.unfiltered_activity_count, 1)
        self.assertEqual(empty.diagnostics, result.diagnostics)

    def test_invalid_filters_fail_before_database_access(self):
        cases = [HistoryFilter(start=START.replace(tzinfo=None)),
                 HistoryFilter(end=START.replace(tzinfo=None)), HistoryFilter(start='private'),
                 HistoryFilter(start=START + timedelta(days=1), end=START),
                 HistoryFilter(sport=''), HistoryFilter(subtype=' '), HistoryFilter(sport=42),
                 HistoryFilter(subtype='private\x00'), HistoryFilter(sport=' cycling'), 'private']
        with patch('src.history.service.load_activity_observations') as loader:
            for filters in cases:
                with self.subTest(filters=filters), self.assertRaises(ActivityHistoryError) as caught:
                    self.history(filters)
                self.assertNotIn('private', str(caught.exception))
            loader.assert_not_called()

    def test_ambiguity_is_surfaced_even_when_filter_matches_nothing(self):
        self.persist([self.summary, self.fit, observation('fit', 'second')])
        result = self.history(HistoryFilter(sport='swimming'))
        self.assertEqual(result.activities, ())
        self.assertEqual(len(result.ambiguous_groups), 1)
        self.assertEqual(len(result.ambiguous_groups[0]), 3)
        self.assertEqual(result.diagnostics.linkage_ambiguous_group_count, 1)
        self.assertEqual(result.diagnostics.ambiguous_group_count, 1)

    def test_projection_conflict_is_surfaced(self):
        self.persist([self.summary, replace(self.summary, source_record_index=1)])
        result = self.history()
        self.assertEqual(result.activities, ())
        self.assertEqual(result.conflicts[0].code, 'canonical_id_collision')
        self.assertEqual(result.diagnostics.projection_conflict_count, 1)
        self.assertEqual(result.diagnostics.source_observation_count, 2)

    def test_database_insertion_and_input_order_independence(self):
        records = [self.summary, self.fit, observation('summary', 'later', start_time=START + timedelta(days=1))]
        second = self.root / 'reversed.sqlite3'
        self.persist(records)
        self.persist(list(reversed(records)), second)
        expected = self.history()
        self.assertEqual(read_activity_history(second), expected)
        from src.ingestion.activity_storage import load_activity_observations
        loaded = load_activity_observations(self.database)
        with patch('src.history.service.load_activity_observations', return_value=tuple(reversed(loaded))):
            self.assertEqual(self.history(), expected)

    def test_read_only_bytes_mtime_and_sidecars(self):
        self.persist([self.summary, self.fit])
        before = self.database.read_bytes(), self.database.stat().st_mtime_ns, list(self.root.iterdir())
        self.history()
        self.history(HistoryFilter(sport='cycling'))
        self.assertEqual((self.database.read_bytes(), self.database.stat().st_mtime_ns,
                          list(self.root.iterdir())), before)

    def test_v1_reads_without_migration(self):
        self.persist([self.summary])
        with closing(sqlite3.connect(self.database)) as db:
            db.execute('DROP TABLE activity_import_members')
            db.execute('ALTER TABLE activity_imports DROP COLUMN archive_member')
            db.execute('ALTER TABLE activity_imports DROP COLUMN archive_checksum_sha256')
            db.execute('PRAGMA user_version=1')
            db.commit()
        before = self.database.read_bytes(), self.database.stat().st_mtime_ns
        self.assertEqual(len(self.history().activities), 1)
        self.assertEqual((self.database.read_bytes(), self.database.stat().st_mtime_ns), before)

    def test_sidecars_are_refused_without_opening_or_altering_files(self):
        self.persist([self.summary])
        for suffix in ('-wal', '-shm', '-journal'):
            sidecar = Path(str(self.database) + suffix)
            sidecar.write_bytes(b'synthetic sidecar')
            before = self.database.read_bytes(), sidecar.read_bytes(), sidecar.stat().st_mtime_ns
            with patch('src.history.service.load_activity_observations') as loader:
                with self.assertRaisesRegex(ActivityHistoryError, '^history_database_has_sidecars$'):
                    self.history()
                loader.assert_not_called()
            self.assertEqual((self.database.read_bytes(), sidecar.read_bytes(), sidecar.stat().st_mtime_ns), before)
            sidecar.unlink()

    def test_missing_database_is_not_created(self):
        with self.assertRaisesRegex(ActivityHistoryError, '^history_storage_failed$') as caught:
            self.history()
        self.assertIsNone(caught.exception.__cause__)
        self.assertFalse(self.database.exists())

    def test_clean_wal_database_cannot_create_sidecars_on_read(self):
        self.persist([self.summary])
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
        self.assertEqual(list(self.root.iterdir()), [self.database])
        before = self.database.read_bytes(), self.database.stat().st_mtime_ns
        with patch('src.history.service.load_activity_observations') as loader:
            with self.assertRaisesRegex(ActivityHistoryError, '^history_database_uses_wal$'):
                self.history()
            loader.assert_not_called()
        self.assertEqual((self.database.read_bytes(), self.database.stat().st_mtime_ns), before)
        self.assertEqual(list(self.root.iterdir()), [self.database])

    def test_corrupt_unsupported_and_unreadable_fail_privately(self):
        self.database.write_bytes(b'private corrupt database')
        with self.assertRaisesRegex(ActivityHistoryError, '^history_storage_failed$'):
            self.history()
        self.database.unlink()
        self.persist([self.summary])
        with closing(sqlite3.connect(self.database)) as db:
            db.execute('PRAGMA user_version=999')
        with self.assertRaisesRegex(ActivityHistoryError, '^history_storage_failed$'):
            self.history()
        with patch('src.history.service.load_activity_observations', side_effect=PermissionError('private path')):
            with self.assertRaisesRegex(ActivityHistoryError, '^history_storage_failed$'):
                self.history()

    def test_linkage_and_projection_failures_are_private(self):
        self.persist([self.summary])
        for target, code in [('link_activity_observations', 'history_linkage_failed'),
                             ('project_canonical_activities', 'history_projection_failed')]:
            with patch('src.history.service.' + target, side_effect=RuntimeError('private payload')):
                with self.assertRaisesRegex(ActivityHistoryError, '^' + code + '$') as caught:
                    self.history()
                self.assertIsNone(caught.exception.__cause__)


if __name__ == '__main__':
    unittest.main()
