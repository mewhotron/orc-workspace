"""Synthetic FIT activity normalization tests; no athlete fixtures or writes."""

import copy
import hashlib
import io
import json
import unittest
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.test_garmin_fit import fixture
from src.ingestion.garmin_fit import FitDecodeResult, FitProvenance
from src.ingestion.garmin_fit_activity import normalize_fit_activity, parse_fit_activity
from src.models.provenance import SourceFile


START = datetime(2024, 1, 1, tzinfo=timezone.utc)


def decoded(**session):
    row = {'start_time': START, 'timestamp': START + timedelta(seconds=100),
           'sport': 'cycling', 'sub_sport': 'road'}
    row.update(session)
    return FitDecodeResult(
        FitProvenance('a' * 64, Path('synthetic.fit'), decoded_at=START),
        {'file_id_mesgs': [{'type': 'activity', 'manufacturer': 'garmin',
                            'garmin_product': 'edge_530', 'product': 3121,
                            'time_created': START - timedelta(seconds=200)}],
         'session_mesgs': [row]}, 'synthetic', (), (), (),
    )


class FitActivityTests(unittest.TestCase):
    def record(self, source):
        result = normalize_fit_activity(source)
        self.assertEqual(result.rejected, ())
        self.assertEqual(len(result.records), 1)
        return result.records[0]

    def test_observed_classifications(self):
        cases = [('cycling', 'road', 'cycling', 'road_biking'),
                 ('cycling', 'mountain', 'cycling', 'mountain_biking'),
                 ('cycling', 'commuting', 'cycling', 'commuting'),
                 ('cycling', 'e_bike_fitness', 'cycling', 'e_bike_fitness'),
                 ('fitness_equipment', 'strength_training', 'strength_training', None),
                 ('training', 'strength_training', 'strength_training', None),
                 ('cycling', 'generic', 'cycling', None)]
        for sport, sub, expected_sport, expected_sub in cases:
            with self.subTest(sport=sport, sub=sub):
                record = self.record(decoded(sport=sport, sub_sport=sub))
                self.assertEqual((record.sport, record.subtype), (expected_sport, expected_sub))
                self.assertEqual(json.loads(record.source_activity_type), {'sport': sport, 'sub_sport': sub})

    def test_unknown_classifications_remain_observable(self):
        for sport, sub, expected in [('cycling', 'future_subsport', 'cycling'),
                                     (250, 240, 'unknown'), ('running', 'future', 'running')]:
            result = normalize_fit_activity(decoded(sport=sport, sub_sport=sub))
            self.assertEqual(result.records[0].sport, expected)
            self.assertIsNone(result.records[0].subtype)
            self.assertEqual(result.warnings['unknown_sport_mapping'], 1)
            self.assertEqual(json.loads(result.records[0].source_activity_type)['sub_sport'], sub)

    def test_optional_values_not_fabricated(self):
        record = self.record(decoded())
        self.assertTrue(all(v is None for v in asdict(record.metrics).values()))
        for name in ['source_activity_id', 'origin_platform', 'is_indoor', 'is_manual', 'name']:
            self.assertIsNone(getattr(record, name))
        self.assertEqual(record.source_record_index, 0)

    def test_session_times_not_creation_or_save_time(self):
        source = decoded()
        source.messages['activity_mesgs'] = [{'timestamp': START + timedelta(seconds=999), 'num_sessions': 1}]
        record = self.record(source)
        self.assertEqual(record.start_time, START)
        self.assertEqual(record.end_time, START + timedelta(seconds=100))
        source.messages['session_mesgs'][0].pop('start_time')
        self.assertEqual(normalize_fit_activity(source).rejected[0].field, 'start_time')

    def test_missing_session_endpoint_is_not_invented(self):
        source = decoded(timestamp=None)
        source.messages['activity_mesgs'] = [{'timestamp': START + timedelta(seconds=999)}]
        self.assertIsNone(self.record(source).end_time)

    def test_elapsed_timer_and_moving_semantics(self):
        record = self.record(decoded(total_elapsed_time=100, total_timer_time=90, total_moving_time=80))
        self.assertEqual(record.metrics.elapsed_time_s, 100)
        self.assertEqual(record.metrics.moving_time_s, 80)
        self.assertEqual(record.metrics.timer_time_s, 90)
        result = normalize_fit_activity(decoded(total_timer_time=90))
        self.assertIsNone(result.records[0].metrics.elapsed_time_s)
        self.assertIsNone(result.records[0].metrics.moving_time_s)
        self.assertEqual(result.records[0].metrics.timer_time_s, 90)
        self.assertNotIn('timer_time_unmapped', result.warnings)

    def test_timezone_validation_and_utc_conversion(self):
        local = START.astimezone(timezone(timedelta(hours=2)))
        record = self.record(decoded(start_time=local))
        self.assertEqual(record.start_time.tzinfo, timezone.utc)
        for kwargs in ({'start_time': START.replace(tzinfo=None)},
                       {'timestamp': START.replace(tzinfo=None)},
                       {'timestamp': START - timedelta(seconds=1)}):
            self.assertTrue(normalize_fit_activity(decoded(**kwargs)).rejected)

    def test_timer_invalid_values_and_elapsed_bound(self):
        for timer in (-1, True, float('nan'), float('inf'), 'private', 101):
            with self.subTest(timer=type(timer)):
                result = normalize_fit_activity(decoded(total_elapsed_time=100, total_timer_time=timer))
                self.assertEqual(result.records, ())
                self.assertTrue(result.rejected)
        self.assertEqual(self.record(decoded(total_timer_time=0)).metrics.timer_time_s, 0)

    def test_activity_timer_is_not_a_session_fallback(self):
        source = decoded()
        source.messages['activity_mesgs'] = [{'num_sessions': 1, 'total_timer_time': 90}]
        result = normalize_fit_activity(source)
        self.assertIsNone(result.records[0].metrics.timer_time_s)
        self.assertEqual(result.warnings['activity_timer_time_not_used'], 1)

    def test_established_metric_units_and_precision(self):
        record = self.record(decoded(total_distance=1234.56, total_ascent=20, total_descent=15,
            avg_speed=2, enhanced_avg_speed=3, max_speed=4, enhanced_max_speed=5,
            avg_heart_rate=130, max_heart_rate=160, avg_cadence=80, avg_fractional_cadence=0.5,
            max_cadence=100, max_fractional_cadence=0.25, avg_power=150, max_power=500,
            normalized_power=170, total_work=123456, total_calories=345))
        self.assertEqual(asdict(record.metrics), dict(
            elapsed_time_s=None, moving_time_s=None, distance_m=1234.56,
            elevation_gain_m=20, elevation_loss_m=15, average_speed_mps=3, max_speed_mps=5,
            average_heart_rate_bpm=130, max_heart_rate_bpm=160, average_cadence_rpm=80.5,
            max_cadence_rpm=100.25, average_power_w=150, max_power_w=500,
            normalized_power_w=170, work_kj=123.456, calories_kcal=345, timer_time_s=None))
        self.assertEqual(self.record(decoded(avg_speed=0)).metrics.average_speed_mps, 0)

    def test_fractional_cadence_without_base_is_not_whole_cadence(self):
        result = normalize_fit_activity(decoded(avg_fractional_cadence=0.5))
        self.assertIsNone(result.records[0].metrics.average_cadence_rpm)
        self.assertEqual(result.warnings['fractional_cadence_without_base'], 1)

    def test_invalid_metrics_rejected_privately(self):
        for value in [-1, True, float('nan'), float('inf'), 'private payload', 10 ** 1000]:
            result = normalize_fit_activity(decoded(total_distance=value))
            self.assertEqual(result.records, ())
            self.assertNotIn('private payload', repr(result))

    def test_device_values_are_not_guessed(self):
        record = self.record(decoded())
        self.assertEqual((record.device_manufacturer, record.device_model), ('garmin', 'edge_530'))
        source = decoded()
        source.messages['file_id_mesgs'][0]['garmin_product'] = 9999
        self.assertIsNone(self.record(source).device_model)
        source.messages['file_id_mesgs'][0]['garmin_product'] = 'connect'
        record = self.record(source)
        self.assertIsNone(record.device_model)
        self.assertIsNone(record.device_manufacturer)

    def test_profile_name_and_gps_absence_do_not_invent_metadata(self):
        record = self.record(decoded(sport='fitness_equipment', sub_sport='strength_training',
                                     total_distance=0, sport_profile_name='private profile'))
        self.assertIsNone(record.name)
        self.assertIsNone(record.is_indoor)
        self.assertIsNone(record.is_manual)

    def test_unknown_messages_do_not_change_records(self):
        source = decoded()
        expected = self.record(source)
        source.messages['60000'] = [{0: 'private', 'distance': 9999999}]
        source = replace(source, unknown_message_numbers=(60000,), unknown_fields=((60000, 0),))
        result = normalize_fit_activity(source)
        self.assertEqual(result.records[0], expected)
        self.assertEqual(result.warnings, {'unknown_fit_messages': 1, 'unknown_fit_fields': 1})

    def test_archive_provenance_and_duplicate_copy_identity(self):
        source = decoded()
        source = replace(source, provenance=replace(source.provenance,
            original_path=Path('synthetic.zip'), archive_member='folder/a.fit', archive_checksum_sha256='b' * 64))
        first = self.record(source)
        self.assertEqual(first.source_file.original_path, Path('synthetic.zip'))
        self.assertEqual(first.source_file.archive_member, 'folder/a.fit')
        self.assertEqual(first.source_file.archive_checksum_sha256, 'b' * 64)
        self.assertEqual(first.source_file.checksum_sha256, 'a' * 64)
        self.assertEqual(SourceFile(**asdict(first.source_file)), first.source_file)
        second = self.record(replace(source, provenance=replace(source.provenance,
            original_path=Path('relocated.zip'), archive_member='folder/copy.fit',
            decoded_at=START + timedelta(days=1))))
        self.assertEqual(first.record_id, second.record_id)
        self.assertNotEqual(first.source_file, second.source_file)
        changed = self.record(replace(source, provenance=replace(source.provenance, checksum_sha256='c' * 64)))
        self.assertNotEqual(first.record_id, changed.record_id)

    def test_missing_and_malformed_provenance(self):
        source = decoded()
        for changes in [dict(original_path=None), dict(checksum_sha256='invalid'),
                        dict(decoded_at=START.replace(tzinfo=None)), dict(archive_member='a.fit'),
                        dict(archive_member='../escape.fit', archive_checksum_sha256='b' * 64)]:
            self.assertTrue(normalize_fit_activity(replace(source, provenance=replace(source.provenance, **changes))).rejected)

    def test_nonactivity_missing_and_multiple_sessions(self):
        for kind in [41, 44, 57, 'course', None]:
            source = decoded()
            source.messages['file_id_mesgs'][0]['type'] = kind
            self.assertTrue(normalize_fit_activity(source).rejected)
        for rows in [[], [{}, {}]]:
            source = decoded()
            source.messages['session_mesgs'] = rows
            self.assertTrue(normalize_fit_activity(source).rejected)
        self.assertTrue(normalize_fit_activity(None).rejected)

    def test_activity_session_count_consistency(self):
        source = decoded()
        source.messages['activity_mesgs'] = [{'num_sessions': 2}]
        self.assertTrue(normalize_fit_activity(source).rejected)

    def test_determinism_and_decoded_input_immutability(self):
        source = decoded(total_distance=1)
        before = copy.deepcopy(source)
        self.assertEqual(normalize_fit_activity(source), normalize_fit_activity(source))
        self.assertEqual(source, before)

    def test_sdk_to_normalized_record_and_source_immutability(self):
        content = fixture()
        checksum = hashlib.sha256(content).hexdigest()
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / 'synthetic.fit'
            path.write_bytes(content)
            mtime = path.stat().st_mtime_ns
            first = parse_fit_activity(path)
            second = parse_fit_activity(content, source_path=path)
            self.assertEqual(first.rejected, ())
            self.assertEqual(first.records[0].record_id, second.records[0].record_id)
            self.assertEqual(first.records[0].metrics.elapsed_time_s, 100.125)
            self.assertEqual(first.records[0].metrics.average_power_w, 150)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), checksum)
            self.assertEqual(path.stat().st_mtime_ns, mtime)

    def test_decode_failures_are_private(self):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            result = parse_fit_activity(b'private invalid FIT', source_path=Path('private.fit'))
        self.assertEqual(result.records, ())
        self.assertNotIn('private', repr(result))
        self.assertEqual(output.getvalue(), '')


if __name__ == '__main__':
    unittest.main()
