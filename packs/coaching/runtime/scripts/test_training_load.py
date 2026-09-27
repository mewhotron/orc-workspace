"""Synthetic-only training-load regression tests. No production data or APIs."""

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta, timezone
from decimal import localcontext, ROUND_DOWN
import unittest

from scripts.test_activity_analytics import START, activity, history
from src.training_load import (
    POWER_TSS_VERSION, HR_VERSION, DURATION_VERSION, DurationReference,
    LoadStatus, ThresholdConfiguration, ThresholdReference, TrainingLoadError,
    calculate_activity_load, summarize_load_coverage,
)


def ftp(value=200, start=START, end=None, kind='ftp', unit='W'):
    return ThresholdReference(kind, value, unit, start, end, 'synthetic', 'test')


def config(*values):
    return ThresholdConfiguration(values or (ftp(),))


def ride(**values):
    return activity(**({'elapsed_time_s': 3600, 'normalized_power_w': 200} | values))


def duration(a):
    return DurationReference(a.canonical_id, 'elapsed_time_s', 'synthetic matched period')


def calculate(a=None, thresholds=None, method='power'):
    a = ride() if a is None else a
    return calculate_activity_load(a, config() if thresholds is None else thresholds,
                                   method, duration_reference=duration(a))


class TrainingLoadTests(unittest.TestCase):
    def test_exact_formula(self):
        self.assertEqual(calculate().value, 100)
        self.assertEqual(calculate(ride(normalized_power_w=100, elapsed_time_s=7200)).value, 50)
        self.assertEqual(calculate().unit, 'TSS')

    def test_np_missing_no_average_substitution(self):
        r = calculate(ride(normalized_power_w=None, average_power_w=200))
        self.assertIsNone(r.value)
        self.assertEqual(r.reasons, ('missing_normalized_power_w',))

    def test_missing_ftp(self):
        self.assertEqual(calculate(thresholds=ThresholdConfiguration()).reasons, ('missing_valid_ftp',))

    def test_future_ftp(self):
        self.assertEqual(calculate(thresholds=config(ftp(start=START + timedelta(seconds=1)))).reasons,
                         ('missing_valid_ftp',))

    def test_historical_and_boundaries(self):
        boundary = START + timedelta(days=1)
        old, new = ftp(200, end=boundary), ftp(400, start=boundary)
        thresholds = config(new, old)
        for start, expected in ((START, 100), (boundary-timedelta(microseconds=1), 100),
                                (boundary, 25), (boundary+timedelta(microseconds=1), 25)):
            self.assertEqual(calculate(ride(start=start), thresholds).value, expected)
        self.assertEqual(calculate(thresholds=thresholds).thresholds, (old,))

    def test_equivalent_timezone_boundary(self):
        same = START.astimezone(timezone(timedelta(hours=2)))
        self.assertEqual(calculate(thresholds=config(ftp(start=same))).value, 100)

    def test_expired_gap_and_irrelevant_future_overlap(self):
        expired = ftp(start=START-timedelta(days=2), end=START)
        future = ftp(start=START+timedelta(days=2))
        self.assertEqual(calculate(thresholds=config(expired, future)).reasons, ('missing_valid_ftp',))
        current = ftp(end=START+timedelta(days=1))
        self.assertEqual(calculate(thresholds=config(current, future, future)).value, 100)

    def test_overlap_including_identical(self):
        for thresholds in (config(ftp(), ftp(250)), config(ftp(), ftp())):
            r = calculate(thresholds=thresholds)
            self.assertEqual(r.status, LoadStatus.INVALID_INPUTS)
            self.assertEqual(r.reasons, ('ambiguous_ftp',))
            self.assertEqual(r.thresholds, ())

    def test_invalid_threshold_numbers(self):
        for value in (0, -1, float('nan'), float('inf'), True, 'private', 10**400):
            with self.subTest(value=type(value)), self.assertRaisesRegex(TrainingLoadError, '^invalid_threshold$'):
                calculate(thresholds=config(ftp(value)))

    def test_invalid_threshold_periods_units_metadata(self):
        for threshold in (ftp(end=START), ftp(end=START-timedelta(seconds=1)),
                ftp(start=START.replace(tzinfo=None)), ftp(unit='bpm'),
                replace(ftp(), source='private\x00')):
            with self.assertRaises(TrainingLoadError):
                calculate(thresholds=config(threshold))

    def test_invalid_activity_numbers(self):
        for name in ('elapsed_time_s', 'normalized_power_w'):
            for value in (-1, float('nan'), float('inf'), True, 'private', 10**400):
                r = calculate(ride(**{name: value}))
                self.assertEqual(r.status, LoadStatus.INVALID_INPUTS)
                self.assertEqual(r.reasons, ('invalid_' + name,))
                self.assertIsNone(r.value)

    def test_missing_duration(self):
        self.assertEqual(calculate(ride(elapsed_time_s=None)).reasons, ('missing_elapsed_time_s',))

    def test_duration_basis_required(self):
        r = calculate_activity_load(ride(), config())
        self.assertEqual(r.reasons, ('unverified_duration_basis',))
        self.assertIsNone(r.value)

    def test_duration_reference_scope_and_selection(self):
        a = ride(moving_time_s=1800)
        ref = DurationReference(a.canonical_id, 'moving_time_s', 'synthetic')
        self.assertEqual(calculate_activity_load(a, config(), duration_reference=ref).value, 50)
        for invalid in (replace(ref, canonical_id='other'), replace(ref, field_name='average_power_w'),
                        replace(ref, source='')):
            with self.assertRaisesRegex(TrainingLoadError, '^invalid_duration_reference$'):
                calculate_activity_load(a, config(), duration_reference=invalid)

    def test_hr_unsupported_even_with_thresholds(self):
        r = calculate(ride(average_heart_rate_bpm=130), config(
            ftp(kind='threshold_hr', unit='bpm', value=160)), method='hr')
        self.assertEqual(r.status, LoadStatus.UNSUPPORTED_METHOD)
        self.assertIn('unsupported_method', r.reasons)
        self.assertIsNone(r.value)
        self.assertEqual(r.thresholds[0].kind, 'threshold_hr')

    def test_timer_requires_declaration_and_preserves_provenance(self):
        a = ride(timer_time_s=1800, moving_time_s=900)
        ref = DurationReference(a.canonical_id, 'timer_time_s', 'synthetic matched period')
        self.assertIn('unverified_duration_basis', calculate_activity_load(a, config()).reasons)
        result = calculate_activity_load(a, config(), duration_reference=ref)
        self.assertEqual(result.value, 50)
        self.assertEqual(result.duration_reference, ref)
        self.assertTrue(any(i.canonical_field is a.selected('timer_time_s') for i in result.activity_inputs))
        coverage = summarize_load_coverage(history(a), config(), duration_references=(ref,))
        self.assertEqual(coverage.power_prerequisites_available, 1)

    def test_timer_missing_invalid_or_cross_source_abstains(self):
        for timer, reason in ((None, 'missing_timer_time_s'), (-1, 'invalid_timer_time_s'),
                              (3601, 'timer_time_exceeds_elapsed_time')):
            a = ride(timer_time_s=timer)
            ref = DurationReference(a.canonical_id, 'timer_time_s', 'synthetic')
            result = calculate_activity_load(a, config(), duration_reference=ref)
            self.assertIsNone(result.value)
            self.assertIn(reason, result.reasons)
        a = ride(pair=True, timer_time_s=1800)
        a = replace(a, fields=tuple(replace(f, selected_from=a.contributors[0])
                    if f.name == 'timer_time_s' else f for f in a.fields))
        result = calculate_activity_load(a, config(), duration_reference=
            DurationReference(a.canonical_id, 'timer_time_s', 'synthetic'))
        self.assertEqual(result.status, LoadStatus.INVALID_INPUTS)
        self.assertIn('duration_power_source_mismatch', result.reasons)

    def test_legacy_projection_remains_supported(self):
        a = replace(ride(), projection_version='canonical-activity-v1')
        self.assertEqual(calculate(a).value, 100)

    def test_invalid_hr_and_ambiguous_hr_inventory(self):
        self.assertEqual(calculate(ride(average_heart_rate_bpm=float('nan')), method='hr').status,
                         LoadStatus.INVALID_INPUTS)
        threshold = ftp(160, kind='threshold_hr', unit='bpm')
        c = summarize_load_coverage(history(ride()), config(threshold, threshold))
        self.assertEqual(c.missing_hr_threshold_configuration, 1)
        self.assertIn(('hr', 'ambiguous_threshold_hr', 1), c.abstention_reasons)

    def test_methods_versions_distinct(self):
        results = [calculate(method=m) for m in ('power', 'hr', 'duration')]
        self.assertEqual([r.formula_version for r in results],
                         [POWER_TSS_VERSION, HR_VERSION, DURATION_VERSION])
        self.assertEqual(len({r.method for r in results}), 3)
        self.assertIsNone(results[2].value)
        with self.assertRaises(TrainingLoadError):
            calculate(method='unknown-private-method')

    def test_determinism_immutability_and_decimal_context(self):
        a, thresholds = ride(normalized_power_w=187), config()
        before = deepcopy((a, thresholds))
        expected = calculate(a, thresholds)
        with localcontext() as context:
            context.prec = 2
            context.rounding = ROUND_DOWN
            self.assertEqual(calculate(a, thresholds), expected)
        self.assertEqual(calculate(a, thresholds), expected)
        self.assertEqual((a, thresholds), before)
        with self.assertRaises(FrozenInstanceError):
            expected.value = 1
        with self.assertRaises(FrozenInstanceError):
            thresholds.thresholds = ()

    def test_provenance_preserved(self):
        a = ride(pair=True)
        r = calculate(a)
        np = next(i for i in r.activity_inputs if i.canonical_field.name == 'normalized_power_w')
        self.assertIs(np.canonical_field, a.selected('normalized_power_w'))
        self.assertEqual(r.canonical_id, a.canonical_id)
        self.assertEqual(r.projection_version, a.projection_version)
        self.assertEqual(r.duration_reference, duration(a))

    def test_coverage_singleton_pair_and_hr_separate(self):
        a = ride(pair=True, average_power_w=180, average_heart_rate_bpm=120)
        b = activity('b', elapsed_time_s=100, max_heart_rate_bpm=150)
        h = history(a, b)
        c = summarize_load_coverage(h)
        self.assertEqual((c.activity_count, c.power_metrics_available, c.normalized_power_available,
            c.power_activity_inputs_available, c.power_prerequisites_available), (2, 1, 1, 1, 0))
        self.assertEqual((c.missing_normalized_power, c.missing_valid_ftp, c.hr_data_available,
                          c.missing_hr_threshold_configuration), (1, 2, 2, 2))
        self.assertEqual(dict(c.scored_by_method), {'power': 0, 'hr': 0, 'duration': 0})
        thresholds = config(ftp(), ftp(160, kind='threshold_hr', unit='bpm'))
        scored = summarize_load_coverage(h, thresholds, duration_references=(duration(a),))
        self.assertEqual(scored.power_prerequisites_available, 1)
        self.assertEqual(scored.missing_hr_threshold_configuration, 0)
        self.assertEqual(dict(scored.unsupported_by_method)['hr'], 2)
        self.assertEqual(c, summarize_load_coverage(replace(h, activities=h.activities[::-1])))

    def test_filtered_history(self):
        full = history(ride(), activity('b'))
        filtered = replace(history(full.activities[0]), diagnostics=full.diagnostics,
                           unfiltered_activity_count=2)
        self.assertEqual(summarize_load_coverage(filtered).activity_count, 1)
        empty = replace(history(), diagnostics=full.diagnostics, unfiltered_activity_count=2)
        result = summarize_load_coverage(empty)
        self.assertEqual(result.activity_count, 0)
        self.assertEqual(dict(result.scored_by_method), {'power': 0, 'hr': 0, 'duration': 0})

    def test_privacy_safe_failures(self):
        a = ride()
        for invalid in (replace(a, canonical_id=''), replace(a, fields=a.fields+a.fields),
                replace(a, projection_version='private'), None):
            with self.assertRaises(TrainingLoadError) as caught:
                calculate(invalid) if invalid is not None else calculate_activity_load(None)
            self.assertNotIn('private', str(caught.exception))
        with self.assertRaisesRegex(TrainingLoadError, '^invalid_load_history$'):
            summarize_load_coverage(None)

    def test_zero_and_unrepresentable(self):
        self.assertEqual(calculate(ride(elapsed_time_s=0)).value, 0)
        self.assertEqual(calculate(ride(normalized_power_w=0)).value, 0)
        for np, threshold in ((1e308, 1e-308), (1e-308, 1e308)):
            self.assertEqual(calculate(ride(normalized_power_w=np), config(ftp(threshold))).reasons,
                             ('unrepresentable_load',))

    def test_noncycling_unsupported(self):
        self.assertEqual(calculate(ride(sport='running')).reasons, ('unsupported_sport',))

    def test_coverage_rejects_duplicate_and_outside_duration_references(self):
        a = ride()
        for refs in ((duration(a), duration(a)), (replace(duration(a), canonical_id='other'),)):
            with self.assertRaises(TrainingLoadError):
                summarize_load_coverage(history(a), duration_references=refs)

    def test_prerequisites_distinct_from_numeric_representability(self):
        a = ride(normalized_power_w=1e308)
        c = summarize_load_coverage(history(a), config(ftp(1e-308)),
                                    duration_references=(duration(a),))
        self.assertEqual(c.power_prerequisites_available, 1)
        self.assertEqual(dict(c.scored_by_method)['power'], 0)


if __name__ == '__main__':
    unittest.main()
