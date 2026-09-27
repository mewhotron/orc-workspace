"""Synthetic report regressions; temporary storage only, no live API calls."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
import hashlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts.personal_training_report import main
from scripts.test_activity_analytics import START, activity, history
from scripts.test_activity_linkage import observation
from src.history import HistoryFilter, ProjectionConflict
from src.ingestion.activity_storage import ObservationBatch, store_activity_observations
from src.reports import (
    REPORT_VERSION, TrainingReportError, build_personal_training_report,
    read_personal_training_report, render_training_report_json, render_training_report_markdown,
)
from src.training_load import DurationReference, ThresholdConfiguration, ThresholdReference


class TrainingReportTests(unittest.TestCase):
    def test_empty_missing_and_zero_remain_distinct(self):
        for rows, expected in (((), 'unavailable (0/0)'), ((activity(),), 'unavailable (0/1)'),
                              ((activity(distance_m=0),), '0.00 (1/1)')):
            report = build_personal_training_report(history(*rows))
            metric = report.analytics.totals.distance_m
            if rows and rows[0].selected('distance_m').value == 0:
                self.assertEqual(metric.value, 0)
            else:
                self.assertIsNone(metric.value)
            from src.reports.rendering import _total
            self.assertEqual(_total(metric), expected)
            text = render_training_report_markdown(report)
            self.assertIn('Missing scores are not zero', text)
            self.assertNotIn('nan', render_training_report_json(report))
        self.assertIn('No canonical activities', render_training_report_markdown(
            build_personal_training_report(history())))

    def test_partial_totals_linked_pair_and_no_average_imputation(self):
        report = build_personal_training_report(history(
            activity(pair=True, distance_m=1250, average_power_w=200), activity('b')))
        self.assertEqual(report.analytics.activity_count, 2)
        self.assertEqual(report.analytics.global_diagnostics.source_observation_count, 3)
        text = render_training_report_markdown(report)
        self.assertIn('| Distance (km) | 1.25 | 1/2 |', text)
        self.assertIn('missing normalized power w', text)
        self.assertEqual(dict(report.load_coverage.scored_by_method)['power'], 0)

    def test_gate_threshold_period_and_zero_score(self):
        a = activity(elapsed_time_s=3600, normalized_power_w=200)
        b = activity('b', start=START + timedelta(days=1), elapsed_time_s=3600, normalized_power_w=0)
        c = activity('c', start=START - timedelta(days=1), elapsed_time_s=3600, normalized_power_w=200)
        thresholds = ThresholdConfiguration((ThresholdReference('ftp', 200, 'W', START),))
        report = build_personal_training_report(history(a), thresholds=thresholds)
        self.assertEqual(report.power_loads[0].reasons, ('unverified_duration_basis',))
        refs = tuple(DurationReference(row.canonical_id, 'elapsed_time_s', 'synthetic interval evidence')
                     for row in (a, b, c))
        report = build_personal_training_report(history(a, b, c), thresholds=thresholds,
                                               duration_references=refs)
        by_id = {load.canonical_id: load for load in report.power_loads}
        self.assertEqual((by_id['a'].value, by_id['b'].value), (100, 0))
        self.assertEqual(by_id['c'].reasons, ('missing_valid_ftp',))
        self.assertEqual(dict(report.load_coverage.scored_by_method)['power'], 2)
        self.assertIn('| 0.00 | computed |', render_training_report_markdown(report))
        self.assertIsNone(by_id['c'].value)

    def test_timer_source_mismatch_is_preserved(self):
        a = activity(pair=True, elapsed_time_s=3600, timer_time_s=3000, normalized_power_w=200)
        a = replace(a, fields=tuple(replace(f, selected_from=a.contributors[0])
                                   if f.name == 'timer_time_s' else f for f in a.fields))
        report = build_personal_training_report(history(a),
            thresholds=ThresholdConfiguration((ThresholdReference('ftp', 200, 'W', START),)),
            duration_references=(DurationReference('a', 'timer_time_s', 'synthetic'),))
        self.assertEqual(report.power_loads[0].reasons, ('duration_power_source_mismatch',))
        self.assertIsNone(report.power_loads[0].value)

    def test_provenance_precision_and_input_immutability(self):
        source = history(activity(pair=True, distance_m=1234.56789))
        before = deepcopy(source)
        report = build_personal_training_report(source)
        encoded = json.loads(render_training_report_json(report))
        self.assertEqual(encoded['report_version'], REPORT_VERSION)
        self.assertEqual(encoded['analytics']['totals']['distance_m']['value'], 1234.56789)
        selected = next(f for f in encoded['history']['activities'][0]['fields'] if f['name'] == 'distance_m')
        self.assertEqual(selected['selected_from']['record_id'], 'a1')
        self.assertEqual(selected['selection_rule'], 'synthetic')
        self.assertEqual(encoded['history']['activities'][0]['canonical_id'], 'a')
        self.assertEqual(encoded['power_loads'][0]['canonical_id'], 'a')
        self.assertEqual(before, source)
        with self.assertRaises(FrozenInstanceError):
            report.report_version = 'changed'

    def test_shuffled_input_is_deterministic(self):
        a, b = activity('a'), activity('b', start=START + timedelta(days=7))
        one = build_personal_training_report(history(a, b))
        two = build_personal_training_report(history(b, a))
        self.assertEqual(render_training_report_json(one), render_training_report_json(two))
        self.assertEqual(render_training_report_markdown(one), render_training_report_markdown(two))

    def test_global_quality_is_visible_for_empty_filtered_snapshot(self):
        unresolved, conflict = activity('unresolved'), activity('conflict')
        source = history(activity())
        source = replace(history(), ambiguous_groups=(unresolved.contributors,),
            conflicts=(ProjectionConflict(conflict.contributors, 'synthetic'),),
            diagnostics=replace(source.diagnostics, source_observation_count=3,
                ambiguous_group_count=1, projection_conflict_count=1), unfiltered_activity_count=1)
        text = render_training_report_markdown(build_personal_training_report(source))
        self.assertIn('No canonical activities', text)
        self.assertIn('Unresolved ambiguous groups: 1', text)
        self.assertIn('Projection conflicts: 1', text)
        self.assertIn('Canonical activities before filtering: 1', text)

    def test_local_week_boundaries_and_dst(self):
        # 22:30 UTC Sunday is Monday in Bucharest. March 25 week contains DST.
        rows = history(activity(start=START + timedelta(days=83, hours=22, minutes=30)),
                       activity('b', start=START + timedelta(days=90, hours=21)))
        report = build_personal_training_report(rows, aggregation_timezone='Europe/Bucharest')
        weeks = report.analytics.weeks
        self.assertEqual(str(weeks[0].week_start.date()), '2024-03-25')
        self.assertEqual(str(weeks[1].week_start.date()), '2024-04-01')
        self.assertEqual(weeks[0].week_end.timestamp() - weeks[0].week_start.timestamp(), 167 * 3600)
        self.assertIn('2024-03-25 | 2024-04-01 | 1', render_training_report_markdown(report))

    def test_bad_inputs_fail_with_fixed_codes(self):
        for source in (None, history(activity(distance_m=-1)), history(activity(normalized_power_w=float('nan')))):
            with self.assertRaisesRegex(TrainingReportError, '^report_analytics_failed$'):
                build_personal_training_report(source)
        for refs in ((DurationReference('unknown', 'elapsed_time_s', 'private'),), [],
                     (DurationReference('a', 'elapsed_time_s', 'private'),) * 2):
            with self.assertRaisesRegex(TrainingReportError, '^report_load_failed$'):
                build_personal_training_report(history(activity()), duration_references=refs)
        with self.assertRaisesRegex(TrainingReportError, '^report_load_failed$'):
            build_personal_training_report(history(), thresholds='private value')

    def test_render_source_text_as_data(self):
        tag = '<img src="x">|[link](https://example.invalid)\'&'
        report = build_personal_training_report(history(activity(tag, sport=tag)))
        text = render_training_report_markdown(report)
        self.assertNotIn('<img', text)
        self.assertNotIn('[link]', text)
        self.assertIn('&#124;', text)
        self.assertIn('&quot;x&quot;', text)
        self.assertIn('&#x27;&amp;', text)
        self.assertEqual(json.loads(render_training_report_json(report))['history']['activities'][0]['canonical_id'], tag)

    def test_stage_errors_do_not_echo_upstream_private_text(self):
        with patch('src.reports.service.read_activity_history', side_effect=ValueError('secret')):
            with self.assertRaisesRegex(TrainingReportError, '^report_history_failed$'):
                read_personal_training_report(Path('private'))
        with patch('src.reports.service.summarize_load_coverage', side_effect=ValueError('secret')):
            with self.assertRaisesRegex(TrainingReportError, '^report_load_failed$'):
                build_personal_training_report(history())


class TrainingReportStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / 'activities.sqlite3'
        for tag, start, sport in (('first', START, 'cycling'),
                                 ('second', START + timedelta(days=1), 'running')):
            row = observation('summary', tag, start_time=start, sport=sport, subtype=None)
            row = replace(row, source_file=replace(row.source_file,
                checksum_sha256=hashlib.sha256(tag.encode()).hexdigest()))
            store_activity_observations(self.database, ObservationBatch((row,), row.source_file, {}))

    def fingerprint(self):
        return (hashlib.sha256(self.database.read_bytes()).hexdigest(), self.database.stat().st_mtime_ns,
                tuple(sorted(p.name for p in self.root.glob('activities.sqlite3*'))))

    def test_actual_reader_filters_and_preserves_database(self):
        before = self.fingerprint()
        filters = HistoryFilter(start=START, end=START + timedelta(days=1), sport='cycling')
        report = read_personal_training_report(self.database, filters)
        self.assertEqual(report.analytics.activity_count, 1)
        self.assertEqual(report.analytics.unfiltered_activity_count, 2)
        self.assertEqual(report.selection, filters)
        self.assertEqual(json.loads(render_training_report_json(report))['selection']['end'], filters.end.isoformat())
        self.assertEqual(self.fingerprint(), before)
        empty = read_personal_training_report(self.database, HistoryFilter(start=START, end=START))
        self.assertEqual(empty.analytics.activity_count, 0)

    def test_cli_both_exports_and_refuses_existing_output(self):
        before = self.fingerprint()
        for fmt in ('markdown', 'json'):
            output = self.root / fmt
            args = ['--database', str(self.database), '--format', fmt, '--output', str(output),
                    '--sport', 'cycling', '--start', START.isoformat()]
            self.assertEqual(main(args), 0)
            saved = output.read_bytes()
            with redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(main(args), 1)
            self.assertEqual(stderr.getvalue().strip(), 'report_output_failed')
            self.assertEqual(output.read_bytes(), saved)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(['--database', str(self.database), '--output', str(self.database)]), 1)
        self.assertEqual(self.fingerprint(), before)

    def test_cli_missing_database_and_invalid_timestamp(self):
        missing = self.root / 'missing.sqlite3'
        with redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(main(['--database', str(missing)]), 1)
        self.assertEqual(stderr.getvalue().strip(), 'report_history_failed')
        self.assertFalse(missing.exists())
        for stamp in ('2024-01-01', '2024-01-01T10:00:00', '2024-01-01T10:00:00-00:00'):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                main(['--start', stamp])
            self.assertEqual(error.exception.code, 2)

    def test_cli_stdout_json_and_explicit_threshold_loading(self):
        config = self.root / 'thresholds.json'
        config.write_text(json.dumps({'schema_version': 'training-thresholds-v1', 'thresholds': [
            {'kind': 'ftp', 'value': 200, 'unit': 'W', 'effective_from': START.isoformat()}]}), encoding='utf-8')
        with redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(main(['--database', str(self.database), '--format', 'json',
                                   '--thresholds', str(config)]), 0)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report['power_loads'][0]['thresholds'][0]['value'], 200)
        self.assertIn('unverified_duration_basis', report['power_loads'][0]['reasons'])


if __name__ == '__main__':
    unittest.main()
