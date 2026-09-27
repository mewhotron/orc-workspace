"""Synthetic canonical/history fixtures only; no databases or source records.

DST tests require IANA data discoverable by zoneinfo (PYTHONTZPATH on Windows).
Missing timezone data is a failed prerequisite, never a silently skipped test.
"""

from collections import Counter
from copy import deepcopy
from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timedelta, timezone
import math
import random
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError

from src.analytics import ANALYTICS_VERSION, ActivityAnalyticsError, summarize_activity_history
from src.history import (
    ActivityHistory, CanonicalActivity, HistoryDiagnostics, HistoryInventory,
    ObservationReference, ProjectionConflict,
)
from src.linkage.activity import LINKAGE_VERSION, LinkStatus
from src.projection.activity import CanonicalField, PROJECTION_VERSION


START = datetime(2024, 1, 1, tzinfo=timezone.utc)
ADDITIVE = ('elapsed_time_s', 'moving_time_s', 'distance_m', 'elevation_gain_m',
            'elevation_loss_m', 'work_kj', 'calories_kcal')
NUMERIC = ADDITIVE + ('average_speed_mps', 'max_speed_mps', 'average_heart_rate_bpm',
    'max_heart_rate_bpm', 'average_cadence_rpm', 'max_cadence_rpm', 'average_power_w',
    'max_power_w', 'normalized_power_w')


def activity(tag='a', *, start=START, pair=False, **values):
    refs = tuple(ObservationReference(tag + str(i), 0, tag + str(i), kind)
                 for i, kind in enumerate(('summary', 'fit') if pair else ('summary',)))
    selected = dict.fromkeys(NUMERIC + ('device_manufacturer', 'device_model'))
    selected.update(start_time=start, sport='cycling', subtype='road_biking')
    selected.update(values)
    return CanonicalActivity(tag, refs, tuple(CanonicalField(name, value, refs[-1], 'synthetic')
        for name, value in selected.items()), (), (),
        LinkStatus.STRONG_CANDIDATE if pair else LinkStatus.UNLINKED)


def history(*activities):
    starts = [a.selected('start_time').value.astimezone(timezone.utc) for a in activities]
    sports = Counter(a.selected('sport').value for a in activities)
    subtypes = Counter(a.selected('subtype').value for a in activities)
    inventory = HistoryInventory(len(activities), min(starts) if starts else None,
        max(starts) if starts else None, tuple(sorted(sports.items())),
        tuple(sorted(subtypes.items(), key=lambda item: (item[0] is not None, item[0] or ''))))
    pairs = sum(len(a.contributors) == 2 for a in activities)
    diag = HistoryDiagnostics(sum(len(a.contributors) for a in activities), pairs, 0, pairs,
                              0, 0, 0, len(activities) - pairs)
    return ActivityHistory(tuple(activities), (), (), inventory, diag, len(activities),
                           LINKAGE_VERSION, PROJECTION_VERSION)


def coverage(result, name):
    return next(item for item in result.coverage if item.metric == name)


class ActivityAnalyticsTests(unittest.TestCase):
    def summarize(self, *activities, zone='UTC'):
        return summarize_activity_history(history(*activities), aggregation_timezone=zone)

    def test_empty(self):
        result = self.summarize()
        self.assertEqual((result.activity_count, result.active_days, result.weeks_represented), (0, 0, 0))
        self.assertEqual(result.weeks, ())
        self.assertEqual(result.counts_by_sport, ())
        for name in ADDITIVE:
            total = getattr(result.totals, name)
            self.assertIsNone(total.value)
            self.assertEqual((total.coverage.available_count, total.coverage.fraction), (0, 0.0))

    def test_one_and_singleton(self):
        result = self.summarize(activity())
        self.assertEqual((result.activity_count, result.active_days, result.weeks_represented), (1, 1, 1))
        self.assertEqual(result.global_diagnostics.singleton_count, 1)
        self.assertEqual(result.counts_by_sport, (('cycling', 1),))

    def test_two_source_pair_counted_once(self):
        result = self.summarize(activity(pair=True, distance_m=123))
        self.assertEqual(result.global_diagnostics.source_observation_count, 2)
        self.assertEqual(result.global_diagnostics.strong_link_count, 1)
        self.assertEqual(result.activity_count, 1)
        self.assertEqual(result.totals.distance_m.value, 123)
        self.assertEqual(result.weeks[0].activity_count, 1)

    def test_each_additive_metric_and_weekly_coverage(self):
        for name in ADDITIVE:
            with self.subTest(metric=name):
                result = self.summarize(activity(**{name: 10}), activity('b', **{name: 25}))
                for totals in (result.totals, result.weeks[0].totals):
                    total = getattr(totals, name)
                    self.assertEqual(total.value, 35)
                    self.assertEqual(total.coverage.available_count, 2)
                    self.assertEqual(total.coverage.fraction, 1)

    def test_elapsed_and_moving_are_separate(self):
        result = self.summarize(activity(elapsed_time_s=50), activity('b', moving_time_s=20))
        self.assertEqual(result.totals.elapsed_time_s.value, 50)
        self.assertEqual(result.totals.moving_time_s.value, 20)
        self.assertEqual(result.totals.moving_time_s.coverage.fraction, .5)

    def test_only_selected_values_contribute(self):
        original = activity(pair=True, distance_m=123)
        selected = tuple(replace(f, corroborated_by=(original.contributors[0],))
                         if f.name == 'distance_m' else f for f in original.fields)
        result = self.summarize(replace(original, fields=selected))
        self.assertEqual(result.totals.distance_m.value, 123)
        self.assertEqual(result.totals.distance_m.coverage.available_count, 1)

    def test_missing_is_not_zero(self):
        result = self.summarize(activity())
        for name in ADDITIVE:
            self.assertIsNone(getattr(result.totals, name).value)
            self.assertEqual(coverage(result, name).fraction, 0)

    def test_zero_is_present(self):
        result = self.summarize(activity(**dict.fromkeys(NUMERIC, 0)))
        for name in ADDITIVE:
            self.assertEqual(getattr(result.totals, name).value, 0)
        for name in NUMERIC:
            self.assertEqual(coverage(result, name).fraction, 1)

    def test_partial_coverage(self):
        result = self.summarize(activity(distance_m=15), activity('b'), activity('c', distance_m=0))
        total = result.totals.distance_m
        self.assertEqual(total.value, 15)
        self.assertEqual((total.coverage.available_count, total.coverage.activity_count), (2, 3))
        self.assertEqual(total.coverage.fraction, 2 / 3)

    def test_hr_cadence_power_are_coverage_only(self):
        result = self.summarize(activity(average_heart_rate_bpm=120, max_cadence_rpm=90,
            average_power_w=100, normalized_power_w=150), activity('b', max_heart_rate_bpm=160))
        for name, expected in [('heart_rate', 2), ('cadence', 1), ('power', 1),
                               ('normalized_power_w', 1), ('average_heart_rate_bpm', 1)]:
            self.assertEqual(coverage(result, name).available_count, expected)
        self.assertEqual(tuple(f.name for f in fields(result.totals)), ADDITIVE)
        self.assertFalse(hasattr(result, 'average_heart_rate_bpm'))
        self.assertFalse(hasattr(result, 'average_cadence_rpm'))
        self.assertFalse(hasattr(result, 'average_power_w'))

    def test_device_family_and_individual_coverage(self):
        result = self.summarize(activity(device_manufacturer='synthetic'),
                                activity('b', device_model='synthetic'), activity('c'))
        self.assertEqual(coverage(result, 'device_metadata').available_count, 2)
        self.assertEqual(coverage(result, 'device_model').fraction, 1 / 3)

    def test_same_date_one_active_day(self):
        result = self.summarize(activity(), activity('b', start=START + timedelta(hours=23)))
        self.assertEqual(result.active_days, 1)
        self.assertEqual(result.weeks[0].active_days, 1)

    def test_monday_exclusive_boundary(self):
        monday = START + timedelta(days=7)
        result = self.summarize(activity(start=monday - timedelta(microseconds=1)),
                                activity('b', start=monday))
        self.assertEqual([w.activity_count for w in result.weeks], [1, 1])
        self.assertEqual(result.weeks[0].week_end, result.weeks[1].week_start)
        self.assertEqual(result.weeks[1].week_start, monday)

    def test_chronological_nonempty_weeks_year_boundary(self):
        result = self.summarize(activity(start=START + timedelta(days=70)),
                                activity('b', start=START - timedelta(days=1)))
        self.assertEqual(result.weeks_represented, 2)
        self.assertLess(result.weeks[0].week_start, result.weeks[1].week_start)
        self.assertEqual(result.weeks[0].week_start.date().isoformat(), '2023-12-25')

    def test_non_utc_date_and_week(self):
        start = datetime(2024, 1, 7, 23, tzinfo=timezone.utc)
        utc = self.summarize(activity(start=start))
        local = self.summarize(activity(start=start), zone='Europe/Bucharest')
        self.assertEqual(utc.weeks[0].week_start.day, 1)
        self.assertEqual(local.weeks[0].week_start.day, 8)
        self.assertEqual(local.aggregation_timezone, 'Europe/Bucharest')

    def test_timezone_changes_active_day_count(self):
        a = activity(start=START + timedelta(hours=21))
        b = activity('b', start=START + timedelta(hours=23))
        self.assertEqual(self.summarize(a, b).active_days, 1)
        self.assertEqual(self.summarize(a, b, zone='Europe/Bucharest').active_days, 2)

    def test_spring_dst_week_is_167_absolute_hours(self):
        result = self.summarize(activity(start=datetime(2024, 3, 31, 0, 30, tzinfo=timezone.utc)),
            activity('b', start=datetime(2024, 3, 31, 1, 30, tzinfo=timezone.utc)), zone='Europe/Bucharest')
        week = result.weeks[0]
        self.assertEqual(result.active_days, 1)
        self.assertEqual((week.week_end.astimezone(timezone.utc) -
                          week.week_start.astimezone(timezone.utc)).total_seconds(), 167 * 3600)

    def test_autumn_dst_fold_week_is_169_absolute_hours(self):
        result = self.summarize(activity(start=datetime(2024, 10, 27, 0, 30, tzinfo=timezone.utc)),
            activity('b', start=datetime(2024, 10, 27, 1, 30, tzinfo=timezone.utc)), zone='Europe/Bucharest')
        week = result.weeks[0]
        self.assertEqual((week.activity_count, week.active_days), (2, 1))
        self.assertEqual((week.week_end.astimezone(timezone.utc) -
                          week.week_start.astimezone(timezone.utc)).total_seconds(), 169 * 3600)

    def test_invalid_timezone_is_private(self):
        for zone in ('private/not-a-zone', '/private/path', '../private', '', ' UTC ', None, 4):
            with self.subTest(zone=zone), self.assertRaises(ActivityAnalyticsError) as caught:
                self.summarize(zone=zone)
            self.assertNotIn('private', str(caught.exception))
            self.assertIsNone(caught.exception.__cause__)

    def test_utc_without_iana_database_and_nonutc_refusal(self):
        with patch('src.analytics.service.ZoneInfo', side_effect=ZoneInfoNotFoundError('private')):
            self.assertEqual(self.summarize(activity()).activity_count, 1)
            with self.assertRaisesRegex(ActivityAnalyticsError, '^analytics_timezone_unavailable$'):
                self.summarize(zone='Europe/Bucharest')

    def test_reversal_shuffle_and_repeated_call_determinism(self):
        source = history(*(activity(str(i), start=START + timedelta(days=i * 2),
            distance_m=value) for i, value in enumerate([1e16, 1, 1, .1, .2, 8])))
        expected = summarize_activity_history(source)
        self.assertEqual(summarize_activity_history(source), expected)
        self.assertEqual(summarize_activity_history(replace(source, activities=source.activities[::-1])), expected)
        shuffled = list(source.activities)
        random.Random(42).shuffle(shuffled)
        self.assertEqual(summarize_activity_history(replace(source, activities=tuple(shuffled))), expected)
        self.assertEqual(expected.totals.distance_m.value, math.fsum([1e16, 1, 1, .1, .2, 8]))

    def test_filtered_history_retains_global_diagnostics(self):
        full = history(activity(pair=True, distance_m=50), activity('b', distance_m=100))
        filtered = replace(history(full.activities[0]), diagnostics=full.diagnostics,
                           unfiltered_activity_count=full.unfiltered_activity_count)
        result = summarize_activity_history(filtered)
        self.assertEqual(result.activity_count, 1)
        self.assertEqual(result.totals.distance_m.value, 50)
        self.assertEqual(result.unfiltered_activity_count, 2)
        self.assertEqual(result.global_diagnostics.source_observation_count, 3)
        self.assertEqual(result.history_inventory, filtered.inventory)

        empty = replace(history(), diagnostics=full.diagnostics,
                        unfiltered_activity_count=full.unfiltered_activity_count)
        result = summarize_activity_history(empty)
        self.assertEqual(result.activity_count, 0)
        self.assertEqual(result.weeks, ())
        self.assertEqual(result.global_diagnostics, full.diagnostics)
        self.assertIsNone(result.totals.distance_m.value)

    def test_ambiguities_and_conflicts_are_not_activities(self):
        source = history(activity())
        refs = activity('unresolved').contributors
        conflicts = (ProjectionConflict(activity('conflict').contributors, 'synthetic'),)
        source = replace(source, ambiguous_groups=(refs,), conflicts=conflicts,
            diagnostics=replace(source.diagnostics, source_observation_count=3,
                                ambiguous_group_count=1, projection_conflict_count=1))
        result = summarize_activity_history(source)
        self.assertEqual(result.activity_count, 1)
        self.assertEqual(result.global_diagnostics.ambiguous_group_count, 1)
        self.assertEqual(result.global_diagnostics.projection_conflict_count, 1)

    def test_unresolved_reference_cannot_also_be_counted(self):
        source = history(activity())
        source = replace(source, ambiguous_groups=(source.activities[0].contributors,))
        with self.assertRaisesRegex(ActivityAnalyticsError, '^invalid_analytics_contributors$'):
            summarize_activity_history(source)

    def test_input_unchanged_and_models_immutable(self):
        source = history(activity(distance_m=123))
        before = deepcopy(source)
        result = summarize_activity_history(source, aggregation_timezone='Europe/Bucharest')
        self.assertEqual(source, before)
        self.assertEqual(source.activities[0].selected('start_time').value, START)
        for obj, name, value in [(result, 'activity_count', 99),
            (result.totals, 'distance_m', None), (result.totals.distance_m, 'value', 99),
            (result.coverage[0], 'available_count', 99), (result.weeks[0], 'active_days', 99)]:
            with self.assertRaises(FrozenInstanceError):
                setattr(obj, name, value)

    def test_versions(self):
        source = history(activity())
        result = summarize_activity_history(source)
        self.assertEqual(result.analytics_version, ANALYTICS_VERSION)
        self.assertEqual(ANALYTICS_VERSION, 'activity-analytics-v1')
        self.assertEqual(result.linkage_version, source.linkage_version)
        self.assertEqual(result.projection_version, source.projection_version)

    def test_sport_subtype_counts(self):
        result = self.summarize(activity(), activity('b', sport='strength_training', subtype=None))
        self.assertEqual(result.counts_by_sport, (('cycling', 1), ('strength_training', 1)))
        self.assertEqual(result.counts_by_subtype, ((None, 1), ('road_biking', 1)))
        self.assertEqual(result.weeks[0].counts_by_subtype, result.counts_by_subtype)

    def test_negative_nonfinite_and_invalid_numeric_values(self):
        for name in NUMERIC:
            for value in (-1, float('nan'), float('inf'), -float('inf'), True, 'private'):
                with self.subTest(name=name, value=value):
                    with self.assertRaisesRegex(ActivityAnalyticsError, '^invalid_analytics_metric$'):
                        self.summarize(activity(**{name: value}))

    def test_sum_overflow(self):
        with self.assertRaisesRegex(ActivityAnalyticsError, '^invalid_analytics_total$'):
            self.summarize(activity(distance_m=1e308), activity('b', distance_m=1e308))

    def test_naive_start_and_missing_duplicate_fields(self):
        source = history(activity())
        original = source.activities[0]
        variants = [replace(original, fields=tuple(replace(f, value=START.replace(tzinfo=None))
            if f.name == 'start_time' else f for f in original.fields)),
            replace(original, fields=original.fields[1:]),
            replace(original, fields=original.fields + (original.fields[0],))]
        for variant in variants:
            with self.assertRaises(ActivityAnalyticsError):
                summarize_activity_history(replace(source, activities=(variant,)))

    def test_invalid_identity_inventory_and_diagnostics(self):
        source = history(activity())
        variants = [replace(source, activities=source.activities * 2),
            replace(source, inventory=replace(source.inventory, total_activities=99)),
            replace(source, diagnostics=replace(source.diagnostics, source_observation_count=-1)),
            replace(source, unfiltered_activity_count=0),
            replace(source, activities=(replace(source.activities[0], linkage_status=LinkStatus.AMBIGUOUS),)),
            replace(source, projection_version='mismatched'), None]
        for variant in variants:
            with self.assertRaises(ActivityAnalyticsError):
                summarize_activity_history(variant)

    def test_malformed_metadata_and_unexpected_failures_are_private(self):
        for values in ({'sport': 'private\x00'}, {'subtype': 123}, {'device_model': []}):
            with self.assertRaises(ActivityAnalyticsError) as caught:
                self.summarize(activity(**values))
            self.assertNotIn('private', str(caught.exception))
        with patch('src.analytics.service._summarize', side_effect=RuntimeError('private payload')):
            with self.assertRaisesRegex(ActivityAnalyticsError, '^invalid_analytics_input$') as caught:
                self.summarize()
            self.assertIsNone(caught.exception.__cause__)
            self.assertTrue(caught.exception.__suppress_context__)

    def test_weekly_reconciliation_and_coverage_invariants(self):
        result = self.summarize(*(activity(str(i), start=START + timedelta(days=i * 3),
            **{name: i / 3 if i % 2 else None for name in ADDITIVE}) for i in range(20)))
        self.assertEqual(sum(w.activity_count for w in result.weeks), result.activity_count)
        self.assertEqual(sum(w.active_days for w in result.weeks), result.active_days)
        for name in ADDITIVE:
            values = [getattr(w.totals, name) for w in result.weeks]
            self.assertAlmostEqual(math.fsum(v.value for v in values if v.value is not None),
                                   getattr(result.totals, name).value)
        for summary in (result,) + result.weeks:
            self.assertLessEqual(summary.active_days, summary.activity_count)
            for item in summary.coverage:
                self.assertLessEqual(item.available_count, summary.activity_count)
                self.assertTrue(0 <= item.fraction <= 1)


if __name__ == '__main__':
    unittest.main()
