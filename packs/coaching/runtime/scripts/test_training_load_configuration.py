"""Synthetic threshold JSON only; temporary files, no athlete configuration."""

from dataclasses import FrozenInstanceError
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.test_training_load import START, ride, duration
from src.training_load import (
    THRESHOLD_CONFIG_VERSION, TrainingLoadError, calculate_activity_load,
    load_threshold_configuration, parse_threshold_configuration,
)
from src.training_load.configuration import MAX_THRESHOLD_CONFIG_BYTES


def entry(**changes):
    return dict(kind='ftp', value=200, unit='W', effective_from='2024-01-01T00:00:00Z',
                source='synthetic', method='test') | changes


def document(*rows):
    return json.dumps({'schema_version': THRESHOLD_CONFIG_VERSION, 'thresholds': list(rows)})


class ThresholdConfigurationTests(unittest.TestCase):
    def test_valid_config_to_exact_synthetic_score(self):
        config = parse_threshold_configuration(document(entry()))
        self.assertEqual(config.thresholds[0].effective_from, START)
        a = ride()
        self.assertEqual(calculate_activity_load(a, config, duration_reference=duration(a)).value, 100)
        with self.assertRaises(FrozenInstanceError):
            config.thresholds[0].value = 250

    def test_empty_has_no_defaults(self):
        config = parse_threshold_configuration(document())
        self.assertEqual(config.thresholds, ())
        self.assertIn('missing_valid_ftp', calculate_activity_load(ride(), config).reasons)

    def test_historical_selection_and_offset_normalization(self):
        config = parse_threshold_configuration(document(
            entry(effective_from='2024-01-01T02:00:00+02:00', effective_to='2024-01-02T00:00:00Z'),
            entry(value=400, effective_from='2024-01-02T00:00:00Z')))
        a = ride()
        self.assertEqual(calculate_activity_load(a, config, duration_reference=duration(a)).value, 100)
        self.assertEqual(config.thresholds[0].effective_from, START)

    def test_overlap_is_not_silently_resolved(self):
        config = parse_threshold_configuration(document(entry(), entry(value=250)))
        self.assertIn('ambiguous_ftp', calculate_activity_load(ride(), config).reasons)

    def test_all_supported_threshold_kinds(self):
        config = parse_threshold_configuration(document(*(entry(kind=kind, unit='bpm', value=100)
            for kind in ('threshold_hr', 'resting_hr', 'maximum_hr'))))
        self.assertEqual(len(config.thresholds), 3)

    def test_missing_unknown_and_duplicate_keys(self):
        for text in ('{}', '[]', 'null', document(entry(unexpected=1)),
                document({'kind': 'ftp'}),
                '{"schema_version":"training-thresholds-v1","thresholds":[],"thresholds":[]}',
                document(entry()).replace('"value": 200', '"value": 200, "value": 400'),
                document().replace('training-thresholds-v1', 'future-version')):
            with self.subTest(text=text), self.assertRaises(TrainingLoadError):
                parse_threshold_configuration(text)

    def test_reject_nonfinite_bad_units_and_values(self):
        for value in (True, 0, -1, '200', float('nan'), float('inf'), 1e309):
            with self.assertRaises(TrainingLoadError):
                parse_threshold_configuration(document(entry(value=value)))
        with self.assertRaises(TrainingLoadError):
            parse_threshold_configuration(document(entry(unit='bpm')))

    def test_explicit_timestamp_contract(self):
        for stamp in ('2024-01-01', '2024-01-01T00:00:00', '2024-01-01 00:00:00Z',
                '2024-13-01T00:00:00Z', '2024-01-01T00:00:00-00:00',
                '2024-01-01T00:00:00+00:60', '2024-01-01T00:00:00+24:00', 1704067200):
            with self.assertRaises(TrainingLoadError):
                parse_threshold_configuration(document(entry(effective_from=stamp)))
        with self.assertRaises(TrainingLoadError):
            parse_threshold_configuration(document(entry(effective_to='2024-01-01T00:00:00Z')))

    def test_read_only_file_and_determinism(self):
        with TemporaryDirectory() as folder:
            p = Path(folder) / 'thresholds.local.json'
            p.write_text(document(entry()), encoding='utf-8')
            before = p.read_bytes(), p.stat().st_mtime_ns
            self.assertEqual(load_threshold_configuration(p), load_threshold_configuration(p))
            self.assertEqual((p.read_bytes(), p.stat().st_mtime_ns), before)

    def test_read_and_parse_failures_do_not_leak_private_content(self):
        with TemporaryDirectory() as folder:
            p = Path(folder) / 'private-person.local.json'
            for payload in (None, b'private secret', b'\xff', b'x'*(MAX_THRESHOLD_CONFIG_BYTES+1)):
                if payload is not None:
                    p.write_bytes(payload)
                with self.assertRaises(TrainingLoadError) as caught:
                    load_threshold_configuration(p)
                self.assertNotIn('private', str(caught.exception))
                self.assertIsNone(caught.exception.__cause__)
        with self.assertRaises(TrainingLoadError):
            load_threshold_configuration('private-path')


if __name__ == '__main__':
    unittest.main()
