"""Deterministic, synthetic observation linkage tests; no files or live APIs."""

import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from itertools import permutations
import json
from pathlib import Path
import unittest

from src.linkage.activity import LINKAGE_VERSION, LinkageError, LinkStatus, link_activity_observations
from src.models.activity import ActivityMetrics, ActivityRecord
from src.models.provenance import SourceFile


START = datetime(2024, 1, 1, tzinfo=timezone.utc)


def observation(kind, tag='a', **changes):
    is_fit = kind == 'fit'
    source = SourceFile('garmin', Path('synthetic.zip' if is_fit else 'synthetic.json'),
                        START, ('b' if is_fit else 'a') * 64,
                        'member.fit' if is_fit else None, 'c' * 64 if is_fit else None)
    record = ActivityRecord(
        record_id=f'synthetic-{kind}-{tag}', source_file=source,
        source_activity_id=None if is_fit else '123', source_record_index=0,
        start_time=START, end_time=None, sport='cycling', subtype='road_biking',
        source_activity_type=json.dumps({'sport': 'cycling', 'sub_sport': 'road'}) if is_fit else 'road_biking',
        metrics=ActivityMetrics(distance_m=1000.0, elapsed_time_s=100.0),
    )
    return replace(record, **changes)


class ActivityLinkageTests(unittest.TestCase):
    def setUp(self):
        self.summary, self.fit = observation('summary'), observation('fit')

    def result(self, *records):
        return link_activity_observations(records or (self.summary, self.fit))

    def assert_unlinked(self, *records):
        result = self.result(*records)
        self.assertEqual(len(result.links), len(records))
        self.assertTrue(all(link.status == LinkStatus.UNLINKED for link in result.links))
        self.assertEqual(result.ambiguous_groups, ())

    def test_exact_strong_candidate_never_authoritative(self):
        result = self.result()
        self.assertEqual(len(result.links), 1)
        link = result.links[0]
        self.assertEqual(link.status, LinkStatus.STRONG_CANDIDATE)
        self.assertEqual(link.strength, 'required_fields')
        self.assertEqual(link.observation_a.record_id, self.summary.record_id)
        self.assertEqual(link.observation_b.record_id, self.fit.record_id)
        self.assertEqual(link.rule_version, LINKAGE_VERSION)
        self.assertEqual(result.rule_version, 'garmin-summary-fit-v1')
        # Equal upstream IDs are not evidence of an authoritative shared ID.
        link = self.result(self.summary, replace(self.fit, source_activity_id='123')).links[0]
        self.assertEqual(link.status, LinkStatus.STRONG_CANDIDATE)

    def test_timestamp_mismatch_even_one_microsecond(self):
        self.assert_unlinked(self.summary, replace(self.fit, start_time=START + timedelta(microseconds=1)))

    def test_timezone_normalization(self):
        local = START.astimezone(timezone(timedelta(hours=2)))
        self.assertEqual(self.result(self.summary, replace(self.fit, start_time=local)), self.result())

    def test_sport_mismatch(self):
        self.assert_unlinked(self.summary, replace(self.fit, sport='running'))

    def test_distance_and_elapsed_thresholds_inclusive(self):
        for field, inside, outside in [('distance_m', 1000.02, 1000.020001),
                                       ('elapsed_time_s', 100.002, 100.002001)]:
            with self.subTest(field=field):
                within = replace(self.fit, metrics=replace(self.fit.metrics, **{field: inside}))
                self.assertEqual(self.result(self.summary, within).links[0].status, LinkStatus.STRONG_CANDIDATE)
                beyond = replace(self.fit, metrics=replace(self.fit.metrics, **{field: outside}))
                self.assert_unlinked(self.summary, beyond)

    def test_optional_metrics_missing(self):
        self.assertEqual(len(self.result().links[0].evidence), 4)
        fit = replace(self.fit, metrics=replace(self.fit.metrics, average_power_w=123))
        self.assertEqual(self.result(self.summary, fit).links[0].strength, 'required_fields')

    def test_optional_metrics_agree_or_differ_without_changing_required_rule(self):
        summary = replace(self.summary, metrics=replace(self.summary.metrics, average_power_w=123))
        for power, comparison, strength in [(123, 'optional_equal', 'corroborated'),
                                            (124, 'optional_difference', 'required_fields')]:
            fit = replace(self.fit, metrics=replace(self.fit.metrics, average_power_w=power))
            link = self.result(summary, fit).links[0]
            self.assertEqual(link.status, LinkStatus.STRONG_CANDIDATE)
            self.assertEqual(link.strength, strength)
            evidence = next(e for e in link.evidence if e.field == 'average_power_w')
            self.assertEqual(evidence.comparison, comparison)

    def test_one_fit_two_summaries_ambiguous(self):
        result = self.result(self.summary, observation('summary', 'second'), self.fit)
        self.assertEqual(len(result.links), 2)
        self.assertTrue(all(link.status == LinkStatus.AMBIGUOUS for link in result.links))
        self.assertEqual(len(result.ambiguous_groups), 1)
        self.assertEqual(len(result.ambiguous_groups[0]), 3)

    def test_one_summary_two_fits_ambiguous(self):
        result = self.result(self.summary, self.fit, observation('fit', 'second'))
        self.assertEqual(len(result.links), 2)
        self.assertTrue(all(link.status == LinkStatus.AMBIGUOUS for link in result.links))
        self.assertEqual(len(result.ambiguous_groups), 1)

    def test_connected_ambiguity_is_not_resolved_by_closest_match(self):
        summary2 = replace(observation('summary', 'second'),
                           metrics=replace(self.summary.metrics, distance_m=1000.03))
        fit2 = replace(observation('fit', 'second'), metrics=replace(self.fit.metrics, distance_m=1000.015))
        result = self.result(self.summary, summary2, self.fit, fit2)
        self.assertEqual(len(result.links), 3)
        self.assertTrue(all(link.status == LinkStatus.AMBIGUOUS for link in result.links))
        self.assertEqual(len(result.ambiguous_groups[0]), 4)

    def test_unmatched_summary_and_fit(self):
        self.assert_unlinked(self.summary)
        self.assert_unlinked(self.fit)
        self.assertEqual(link_activity_observations([]).links, ())

    def test_order_and_evidence_order_are_deterministic(self):
        records = [self.summary, self.fit, observation('fit', 'second'),
                   observation('summary', 'unmatched', start_time=START + timedelta(days=1))]
        expected = self.result(*records)
        for order in permutations(records):
            self.assertEqual(link_activity_observations(iter(order)), expected)
        for link in expected.links:
            fields = [e.field for e in link.evidence]
            self.assertEqual(fields, sorted(fields))

    def test_no_mutation_or_discovery_time_dependency(self):
        records = [self.summary, self.fit]
        before = copy.deepcopy(records)
        expected = link_activity_observations(records)
        self.assertEqual(records, before)
        rediscovered = [replace(r, source_file=replace(r.source_file,
                       discovered_at=START + timedelta(days=3))) for r in records]
        self.assertEqual(link_activity_observations(rediscovered), expected)

    def test_elapsed_required_not_moving_time_or_end_time(self):
        fit = replace(self.fit, end_time=START + timedelta(seconds=100),
                      metrics=replace(self.fit.metrics, elapsed_time_s=None, moving_time_s=100))
        self.assert_unlinked(self.summary, fit)
        summary = replace(self.summary, metrics=replace(self.summary.metrics, elapsed_time_s=None))
        self.assert_unlinked(summary, self.fit)
        fit = replace(self.fit, metrics=replace(self.fit.metrics, moving_time_s=1))
        self.assertEqual(self.result(self.summary, fit).links[0].status, LinkStatus.STRONG_CANDIDATE)
        self.assertNotIn('moving_time_s', [e.field for e in self.result(self.summary, fit).links[0].evidence])

    def test_missing_distance_remains_unlinked(self):
        self.assert_unlinked(self.summary, replace(self.fit, metrics=replace(self.fit.metrics, distance_m=None)))

    def test_explicit_sport_compatibility(self):
        for summary_type, sport, summary_sub, fit_sport, fit_sub, normalized_sub in [
            ('road_biking', 'cycling', 'road_biking', 'cycling', 'road', 'road_biking'),
            ('mountain_biking', 'cycling', 'mountain_biking', 'cycling', 'mountain', 'mountain_biking'),
            ('e_bike_fitness', 'cycling', 'e_bike_fitness', 'cycling', 'e_bike_fitness', 'e_bike_fitness'),
            ('cycling', 'cycling', None, 'cycling', 'generic', None),
            ('cycling', 'cycling', None, 'cycling', 'commuting', 'commuting'),
            ('strength_training', 'strength_training', None, 'fitness_equipment', 'strength_training', None),
            ('strength_training', 'strength_training', None, 'training', 'strength_training', None),
        ]:
            summary = replace(self.summary, sport=sport, subtype=summary_sub, source_activity_type=summary_type)
            fit = replace(self.fit, sport=sport, subtype=normalized_sub,
                          source_activity_type=json.dumps({'sport': fit_sport, 'sub_sport': fit_sub}))
            self.assertEqual(self.result(summary, fit).links[0].status, LinkStatus.STRONG_CANDIDATE)

    def test_unknown_and_incompatible_cycling_subtypes_fail_closed(self):
        for subtype, raw_sub in [(None, 'future_type'), ('mountain_biking', 'mountain'),
                                 ('e_bike_fitness', 'e_bike_fitness'), (None, 'generic')]:
            fit = replace(self.fit, subtype=subtype,
                          source_activity_type=json.dumps({'sport': 'cycling', 'sub_sport': raw_sub}))
            self.assert_unlinked(self.summary, fit)
        summary = replace(self.summary, sport='unknown', subtype=None, source_activity_type='future_type')
        self.assert_unlinked(summary, self.fit)

    def test_unsupported_source_pair_and_format_not_inferred_from_null_ids(self):
        for source in [replace(self.fit.source_file, source='strava'),
                       replace(self.fit.source_file, archive_member='member.bin'),
                       replace(self.fit.source_file, original_path=Path('source.json'),
                               archive_member=None, archive_checksum_sha256=None)]:
            self.assert_unlinked(self.summary, replace(self.fit, source_file=source))
        standalone = replace(self.fit, source_file=replace(self.fit.source_file,
                            original_path=Path('source.fit'), archive_member=None, archive_checksum_sha256=None))
        self.assertEqual(self.result(self.summary, standalone).links[0].status, LinkStatus.STRONG_CANDIDATE)

    def test_repeated_record_ids_and_identical_inputs_are_not_collapsed(self):
        duplicate = replace(self.fit, source_file=replace(self.fit.source_file, archive_member='copy.fit'))
        for duplicate in (duplicate, self.fit):
            records = (self.summary, self.fit, duplicate)
            result = self.result(*records)
            self.assertEqual(len(result.links), 2)
            self.assertEqual(len(result.ambiguous_groups[0]), 3)
            self.assertNotEqual(result.links[0].observation_b, result.links[1].observation_b)
            self.assertTrue(all(link.status == LinkStatus.AMBIGUOUS for link in result.links))
            self.assertEqual(self.result(*reversed(records)), result)

    def test_result_and_errors_are_private(self):
        fit = replace(self.fit, name='private-athlete-name', source_file=replace(self.fit.source_file,
                      original_path=Path('private-path.zip'), archive_member='private-member.fit'))
        self.assertNotIn('private', repr(self.result(self.summary, fit)))
        invalid = replace(fit, start_time=START.replace(tzinfo=None))
        with self.assertRaisesRegex(LinkageError, '^invalid_linkage_observation$') as caught:
            self.result(self.summary, invalid)
        self.assertIsNone(caught.exception.__cause__)


if __name__ == '__main__':
    unittest.main()
