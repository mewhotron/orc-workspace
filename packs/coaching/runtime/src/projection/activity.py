"""Canonical activity v2 adds explicit timer duration without a storage merge.

Only Garmin summary/FIT pairs have multi-source selection rules. Singletons
retain their own fields without a quality downgrade. Ambiguities and conflicts
remain unresolved. No files, database connections, or persistence are used.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from typing import Iterable

from src.ingestion.activity import validate_activity_record
from src.linkage.activity import (
    DISTANCE_TOLERANCE_M, ELAPSED_TOLERANCE_S, LINKAGE_VERSION,
    LinkageResult, LinkStatus, ObservationReference, _reference_key,
    garmin_sport_compatibility, observation_representation,
)
from src.models.activity import ActivityRecord


PROJECTION_VERSION = 'canonical-activity-v2'
# Identity remains based on contributor record IDs in the established namespace.
# Adding a projected field must not rename every existing canonical activity.
_IDENTITY_VERSION = 'canonical-activity-v1'
FieldValue = str | float | int | bool | datetime | None


class ProjectionError(ValueError):
    """Invalid source/input structure; never includes private source values."""


@dataclass(frozen=True)
class CanonicalField:
    name: str
    value: FieldValue = field(repr=False)
    selected_from: ObservationReference | None
    selection_rule: str
    corroborated_by: tuple[ObservationReference, ...] = ()


@dataclass(frozen=True)
class ExternalIdentifier:
    namespace: str
    identifier_type: str
    value: str | None = field(repr=False)
    observation: ObservationReference


@dataclass(frozen=True)
class FieldDifference:
    """Nonfatal differences are reported without raw values."""
    field_name: str
    observations: tuple[ObservationReference, ...]
    delta: float | None
    reason: str = 'source_representation_difference'


@dataclass(frozen=True)
class CanonicalActivity:
    """Derived view. Contributor references resolve to the original SourceFiles.

    ID uses the version and sorted record IDs only, not path-sensitive linkage
    reference digests. Field provenance uses full references so repeated record
    IDs cannot silently conflate contributors. Values are excluded from repr.
    """
    canonical_id: str
    contributors: tuple[ObservationReference, ...]
    fields: tuple[CanonicalField, ...]
    external_ids: tuple[ExternalIdentifier, ...]
    differences: tuple[FieldDifference, ...]
    linkage_status: LinkStatus
    projection_version: str = PROJECTION_VERSION

    @property
    def contributor_record_ids(self) -> tuple[str, ...]:
        return tuple(sorted(ref.record_id for ref in self.contributors))

    def selected(self, name: str) -> CanonicalField:
        return next(item for item in self.fields if item.name == name)


@dataclass(frozen=True)
class ProjectionConflict:
    observations: tuple[ObservationReference, ...]
    code: str
    fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProjectionResult:
    activities: tuple[CanonicalActivity, ...]
    ambiguous_groups: tuple[tuple[ObservationReference, ...], ...]
    conflicts: tuple[ProjectionConflict, ...]
    projection_version: str = PROJECTION_VERSION


# Each metric uses only its equivalently named normalized field. No moving /
# timer / elapsed substitutions, averaging, or magnitude-based preferences.
_FIT_METRICS = (
    'elapsed_time_s', 'moving_time_s', 'timer_time_s', 'distance_m',
    'elevation_gain_m', 'elevation_loss_m', 'average_speed_mps', 'max_speed_mps',
    'average_heart_rate_bpm', 'max_heart_rate_bpm', 'average_cadence_rpm',
    'max_cadence_rpm', 'average_power_w', 'max_power_w', 'normalized_power_w',
    'work_kj', 'calories_kcal',
)
_METADATA = ('start_time', 'end_time', 'sport', 'subtype', 'name',
             'origin_platform', 'device_manufacturer', 'device_model', 'is_indoor', 'is_manual')


def _ordered(refs) -> tuple[ObservationReference, ...]:
    return tuple(sorted(refs, key=lambda ref: (ref.record_id, ref)))


def _value(record, name):
    value = getattr(record.metrics if name in _FIT_METRICS else record, name)
    return value.astimezone(timezone.utc) if isinstance(value, datetime) else value


def _delta(a, b):
    return abs(Decimal(str(a)) - Decimal(str(b)))


def _pair_conflicts(summary, fit) -> tuple[str, ...]:
    """Recheck values, not the supplied evidence text or confidence label.

    Start/classification and required metrics must still satisfy linkage v1.
    Conflicting known origin/flags and endpoints (>2ms) are also fatal.
    Other optional differences are informational, with FIT/summary precedence
    explicit below; no new optional-metric tolerances are claimed as validated.
    """
    bad = []
    if _value(summary, 'start_time') != _value(fit, 'start_time'):
        bad.append('start_time')
    if garmin_sport_compatibility(summary, fit) is None:
        bad.append('sport_subtype')
    for name, tolerance in (('distance_m', DISTANCE_TOLERANCE_M),
                            ('elapsed_time_s', ELAPSED_TOLERANCE_S)):
        a, b = _value(summary, name), _value(fit, name)
        if a is None or b is None or _delta(a, b) > tolerance:
            bad.append(name)
    for name in ('origin_platform', 'is_indoor', 'is_manual'):
        a, b = _value(summary, name), _value(fit, name)
        if a is not None and b is not None and a != b:
            bad.append(name)
    if summary.end_time is not None and fit.end_time is not None:
        delta = abs((_value(summary, 'end_time') - _value(fit, 'end_time')).total_seconds())
        if Decimal(str(delta)) > ELAPSED_TOLERANCE_S:
            bad.append('end_time')
    return tuple(sorted(bad))


def _project_group(refs, records, status):
    contributors = _ordered(refs)
    identifier = json.dumps([_IDENTITY_VERSION, sorted(ref.record_id for ref in contributors)],
                            separators=(',', ':'), ensure_ascii=True)
    canonical_id = 'canonical-activity-' + hashlib.sha256(identifier.encode()).hexdigest()
    selected, differences = [], []
    if len(contributors) == 1:
        ref = contributors[0]
        record = records[ref]
        for name in (*_METADATA, *_FIT_METRICS):
            value = _value(record, name)
            selected.append(CanonicalField(name, value, ref, 'singleton_observation'))
    else:
        summary_ref = next(ref for ref in contributors if ref.representation == 'garmin_summary')
        fit_ref = next(ref for ref in contributors if ref.representation == 'garmin_fit')
        summary, fit = records[summary_ref], records[fit_ref]
        for name in (*_METADATA, *_FIT_METRICS):
            a, b = _value(summary, name), _value(fit, name)
            # The summary establishes Connect names. FIT does not establish an
            # activity title, so linked FIT names are not used as fallback.
            if name == 'name':
                value, provider, rule = a, summary_ref, 'summary_activity_name'
            elif name in ('start_time', 'sport', 'subtype'):
                value, provider, rule = b, fit_ref, 'fit_session_classification_or_start'
            elif b is not None:
                value, provider = b, fit_ref
                rule = 'fit_session_metric' if name in _FIT_METRICS else 'fit_metadata'
            else:
                value, provider, rule = a, summary_ref, 'summary_equivalent_field_fallback'
            if value is None and name != 'subtype':
                provider, rule = None, 'unavailable'
            corroboration = ()
            if provider is not None:
                other = summary_ref if provider == fit_ref else fit_ref
                if (a is not None and a == b) or name in ('start_time', 'sport', 'subtype'):
                    corroboration = (other,)
            selected.append(CanonicalField(name, value, provider, rule, corroboration))
            if a is not None and b is not None and a != b:
                delta = float(_delta(a, b)) if name in _FIT_METRICS else None
                differences.append(FieldDifference(name, contributors, delta))
    external_ids = tuple(ExternalIdentifier(
        ref.representation if ref.representation != 'unsupported' else records[ref].source_file.source,
        'activity_id', records[ref].source_activity_id, ref) for ref in contributors)
    return CanonicalActivity(canonical_id, contributors,
        tuple(sorted(selected, key=lambda item: item.name)), external_ids,
        tuple(sorted(differences, key=lambda item: item.field_name)), status)


def _bind_observations(observations):
    """Use linkage v1's exact reference digest, never bind by record_id alone."""
    entries = []
    for record in observations:
        validate_activity_record(record)
        entries.append((_reference_key(record), record))
    counts, records = Counter(), {}
    for key, record in sorted(entries, key=lambda item: item[0]):
        ref = ObservationReference(key, counts[key], record.record_id, observation_representation(record))
        records[ref] = record
        counts[key] += 1
    return records


def _project(records, linkage):
    all_refs = _ordered(records)
    def fail(code):
        return ProjectionResult((), (), (ProjectionConflict(all_refs, code),))
    if linkage.rule_version != LINKAGE_VERSION or any(
        link.rule_version != LINKAGE_VERSION for link in linkage.links
    ):
        return fail('unsupported_linkage_version')
    mentioned = set()
    neighbors = {ref: set() for ref in records}
    per_ref = defaultdict(list)
    edges = set()
    for link in linkage.links:
        a, b = link.observation_a, link.observation_b
        if a not in records or (b is not None and b not in records):
            return fail('linkage_observation_mismatch')
        if link.status not in tuple(LinkStatus):
            return fail('invalid_linkage_status')
        if (link.status == LinkStatus.UNLINKED) != (b is None) or a == b:
            return fail('invalid_linkage_shape')
        mentioned.add(a)
        per_ref[a].append(link)
        if b is not None:
            edge = frozenset((a, b))
            if edge in edges:
                return fail('duplicate_linkage_edge')
            edges.add(edge)
            mentioned.add(b)
            per_ref[b].append(link)
            neighbors[a].add(b)
            neighbors[b].add(a)
    declared_ambiguous = set()
    for group in linkage.ambiguous_groups:
        if len(group) < 2 or len(set(group)) != len(group) or any(ref not in records for ref in group):
            return fail('invalid_ambiguous_group')
        declared_ambiguous.update(group)
        mentioned.update(group)
        for ref in group[1:]:
            neighbors[group[0]].add(ref)
            neighbors[ref].add(group[0])
    if mentioned != set(records):
        return fail('incomplete_linkage_coverage')
    activities, ambiguities, conflicts, seen = [], [], [], set()
    for ref in all_refs:
        if ref in seen:
            continue
        pending, component = [ref], set()
        while pending:
            current = pending.pop()
            if current in component:
                continue
            component.add(current)
            pending.extend(neighbors[current] - component)
        seen.update(component)
        group = _ordered(component)
        links = [link for member in group for link in per_ref[member]]
        if (len(group) > 2 or component & declared_ambiguous
                or any(link.status == LinkStatus.AMBIGUOUS for link in links)):
            ambiguities.append(group)
            continue
        if len(group) == 1:
            if len(links) != 1 or links[0].status != LinkStatus.UNLINKED:
                conflicts.append(ProjectionConflict(group, 'inconsistent_linkage_classification'))
                continue
            activities.append(_project_group(group, records, LinkStatus.UNLINKED))
            continue
        if len(links) != 2 or any(link.status != LinkStatus.STRONG_CANDIDATE for link in links):
            conflicts.append(ProjectionConflict(group, 'unsupported_or_inconsistent_link_classification'))
            continue
        if {member.representation for member in group} != {'garmin_summary', 'garmin_fit'}:
            conflicts.append(ProjectionConflict(group, 'unsupported_source_combination'))
            continue
        summary = records[next(member for member in group if member.representation == 'garmin_summary')]
        fit = records[next(member for member in group if member.representation == 'garmin_fit')]
        bad = _pair_conflicts(summary, fit)
        if bad:
            conflicts.append(ProjectionConflict(group, 'contradictory_linked_fields', bad))
            continue
        activities.append(_project_group(group, records, LinkStatus.STRONG_CANDIDATE))
    # IDs based on record IDs can collide when duplicate observations are
    # supplied as separate singletons. Surface this instead of dropping one or
    # introducing filesystem-dependent IDs.
    by_id = defaultdict(list)
    for activity in activities:
        by_id[activity.canonical_id].append(activity)
    resolved = []
    for same_id in by_id.values():
        if len(same_id) == 1:
            resolved.extend(same_id)
        else:
            refs = _ordered(ref for activity in same_id for ref in activity.contributors)
            conflicts.append(ProjectionConflict(refs, 'canonical_id_collision', ('record_id',)))
    return ProjectionResult(tuple(sorted(resolved, key=lambda activity: activity.canonical_id)),
        tuple(sorted(ambiguities)), tuple(sorted(conflicts, key=lambda conflict:
            (conflict.observations, conflict.code, conflict.fields))))


def project_canonical_activities(
    observations: Iterable[ActivityRecord], linkage: LinkageResult,
) -> ProjectionResult:
    """Project complete validated linkage in memory, preserving unresolved state.

    Invalid normalized records fail the call with a privacy-safe error. Stale,
    incomplete, unsupported or contradictory linkage returns explicit conflicts;
    no field selection relies on unverified evidence strings.
    """
    try:
        return _project(_bind_observations(observations), linkage)
    except (ValueError, TypeError, AttributeError, KeyError, OverflowError, RecursionError):
        raise ProjectionError('invalid_projection_input') from None
