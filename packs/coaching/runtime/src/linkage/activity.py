"""Deterministic Garmin summary/FIT candidates above immutable observations.

Only this source pair has validated rules. Provenance format selects the rule;
filenames and archive times are never match evidence. No shared authoritative
ID is available, so this version never emits an authoritative link.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import timezone
from decimal import Decimal
from enum import StrEnum
import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Iterable

from src.ingestion.activity import validate_activity_record
from src.models.activity import ActivityRecord


LINKAGE_VERSION = 'garmin-summary-fit-v1'
DISTANCE_TOLERANCE_M = Decimal('0.02')
ELAPSED_TOLERANCE_S = Decimal('0.002')


class LinkageError(ValueError):
    """Privacy-safe linkage input failure."""


class LinkStatus(StrEnum):
    AUTHORITATIVE = 'authoritative'  # Reserved for a future validated ID rule.
    STRONG_CANDIDATE = 'strong_candidate'
    AMBIGUOUS = 'ambiguous'
    UNLINKED = 'unlinked'


@dataclass(frozen=True, order=True)
class ObservationReference:
    """Opaque reference, not workout identity or a globally unique record ID.

    The digest distinguishes stable record content and physical provenance;
    discovery time is excluded. Occurrence preserves even identical inputs.
    Neither provenance values nor athlete fields appear in the result.
    """
    observation_key: str
    occurrence: int
    record_id: str
    representation: str


@dataclass(frozen=True)
class ObservationLinkEvidence:
    field: str
    comparison: str
    delta: float | None = None
    tolerance: float | None = None


@dataclass(frozen=True)
class ObservationLink:
    observation_a: ObservationReference
    observation_b: ObservationReference | None
    status: LinkStatus
    strength: str
    evidence: tuple[ObservationLinkEvidence, ...]
    rule_version: str = LINKAGE_VERSION


@dataclass(frozen=True)
class LinkageResult:
    links: tuple[ObservationLink, ...]
    ambiguous_groups: tuple[tuple[ObservationReference, ...], ...]
    rule_version: str = LINKAGE_VERSION


_SUMMARY_TYPES = {
    'road_biking': ('cycling', 'road_biking'),
    'mountain_biking': ('cycling', 'mountain_biking'),
    'e_bike_fitness': ('cycling', 'e_bike_fitness'),
    'cycling': ('cycling', None),
    'strength_training': ('strength_training', None),
}
_FIT_CYCLING = {
    'road': 'road_biking', 'mountain': 'mountain_biking',
    'e_bike_fitness': 'e_bike_fitness', 'generic': None, 'commuting': 'commuting',
}
_OPTIONAL_METRICS = (
    'average_cadence_rpm', 'average_heart_rate_bpm', 'average_power_w',
    'elevation_gain_m', 'elevation_loss_m', 'max_heart_rate_bpm',
    'max_power_w', 'normalized_power_w',
)


def observation_representation(record: ActivityRecord) -> str:
    """Recognize current provenance formats, never infer from nullable IDs.

    SourceFile has no explicit parser-format tag. Limit this rule to standalone
    Garmin JSON and FIT files/members. Classification contracts are checked
    separately; a future provenance format tag can replace suffix inspection.
    """
    source = record.source_file
    if source.source != 'garmin':
        return 'unsupported'
    if source.archive_member is not None:
        if (source.archive_checksum_sha256 is not None
                and source.original_path.suffix.lower() == '.zip'
                and PurePosixPath(source.archive_member).suffix.lower() == '.fit'):
            return 'garmin_fit'
        return 'unsupported'
    if source.archive_checksum_sha256 is not None:
        return 'unsupported'
    return {'.json': 'garmin_summary', '.fit': 'garmin_fit'}.get(
        source.original_path.suffix.lower(), 'unsupported')


def garmin_sport_compatibility(summary: ActivityRecord, fit: ActivityRecord) -> str | None:
    """Explicit known mappings; unknown or inconsistent raw/normalized types fail closed."""
    summary_kind = _SUMMARY_TYPES.get(summary.source_activity_type)
    if summary_kind is None or summary_kind != (summary.sport, summary.subtype):
        return None
    try:
        raw = json.loads(fit.source_activity_type)
        sport, sub = raw['sport'], raw['sub_sport']
        if sport == 'cycling' and sub in _FIT_CYCLING:
            fit_kind = ('cycling', _FIT_CYCLING[sub])
        elif sport in ('fitness_equipment', 'training') and sub == 'strength_training':
            fit_kind = ('strength_training', None)
        else:
            return None
    except (ValueError, TypeError, KeyError):
        return None
    if fit_kind != (fit.sport, fit.subtype):
        return None
    if summary_kind == fit_kind:
        return 'known_classification_equal'
    if summary_kind == ('cycling', None) and fit_kind == ('cycling', 'commuting'):
        return 'generic_cycling_to_commuting'
    return None


def _evidence(summary: ActivityRecord, fit: ActivityRecord) -> tuple[ObservationLinkEvidence, ...]:
    classification = garmin_sport_compatibility(summary, fit)
    if classification is None:
        return ()
    evidence = [ObservationLinkEvidence('start_time', 'exact_utc', 0.0),
                ObservationLinkEvidence('sport_subtype', classification)]
    for field, tolerance in (('distance_m', DISTANCE_TOLERANCE_M),
                             ('elapsed_time_s', ELAPSED_TOLERANCE_S)):
        a, b = getattr(summary.metrics, field), getattr(fit.metrics, field)
        if a is None or b is None:
            return ()
        # Decimal representations avoid cancellation error at decimal thresholds;
        # no relative tolerance or magnitude-dependent widening is used.
        delta = abs(Decimal(str(a)) - Decimal(str(b)))
        if delta > tolerance:
            return ()
        evidence.append(ObservationLinkEvidence(field, 'within_tolerance', float(delta), float(tolerance)))
    for field in _OPTIONAL_METRICS:
        a, b = getattr(summary.metrics, field), getattr(fit.metrics, field)
        if a is not None and b is not None:
            delta = abs(Decimal(str(a)) - Decimal(str(b)))
            evidence.append(ObservationLinkEvidence(field,
                'optional_equal' if delta == 0 else 'optional_difference', float(delta)))
    return tuple(sorted(evidence, key=lambda item: item.field))


def _reference_key(record: ActivityRecord) -> str:
    value = asdict(record)
    # Preserve pre-timer observation references for unchanged legacy records.
    if value['metrics']['timer_time_s'] is None:
        del value['metrics']['timer_time_s']
    value['source_file'].pop('discovered_at')
    value['source_file']['original_path'] = str(record.source_file.original_path)
    for field in ('start_time', 'end_time'):
        timestamp = getattr(record, field)
        value[field] = timestamp.astimezone(timezone.utc).isoformat() if timestamp else None
    payload = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def link_activity_observations(observations: Iterable[ActivityRecord]) -> LinkageResult:
    """Index by UTC start/broad sport, then evaluate the validated source pair.

    Every connected candidate component larger than a pair is ambiguous. No
    closest-match selection or transitive merging occurs. Zero-degree inputs
    receive explicit unlinked results. Input multiplicity is always retained.
    """
    try:
        entries = []
        for record in observations:
            validate_activity_record(record)
            if not isinstance(record.source_file.original_path, Path):
                raise LinkageError('invalid_linkage_observation')
            entries.append((_reference_key(record), record))
        entries.sort(key=lambda item: item[0])
        counts = Counter()
        refs = []
        for key, record in entries:
            refs.append(ObservationReference(key, counts[key], record.record_id,
                                            observation_representation(record)))
            counts[key] += 1
        index = defaultdict(list)
        for i, (_, record) in enumerate(entries):
            if refs[i].representation == 'garmin_summary':
                index[(record.start_time.astimezone(timezone.utc), record.sport)].append(i)
        edges, neighbors = {}, defaultdict(set)
        for b, (_, fit) in enumerate(entries):
            if refs[b].representation != 'garmin_fit':
                continue
            for a in index.get((fit.start_time.astimezone(timezone.utc), fit.sport), ()):
                evidence = _evidence(entries[a][1], fit)
                if evidence:
                    edges[(a, b)] = evidence
                    neighbors[a].add(b)
                    neighbors[b].add(a)
    except (ValueError, TypeError, AttributeError, OverflowError, RecursionError):
        raise LinkageError('invalid_linkage_observation') from None

    groups, ambiguous, seen = [], set(), set()
    for node in sorted(neighbors):
        if node in seen:
            continue
        pending, component = [node], set()
        while pending:
            current = pending.pop()
            if current in component:
                continue
            component.add(current)
            pending.extend(neighbors[current] - component)
        seen.update(component)
        if len(component) > 2:
            ambiguous.update(component)
            groups.append(tuple(sorted(refs[i] for i in component)))
    links = []
    for (a, b), evidence in sorted(edges.items()):
        status = LinkStatus.AMBIGUOUS if a in ambiguous else LinkStatus.STRONG_CANDIDATE
        strength = ('corroborated' if any(e.comparison == 'optional_equal' for e in evidence)
                    else 'required_fields')
        links.append(ObservationLink(refs[a], refs[b], status, strength, evidence))
    for i, ref in enumerate(refs):
        if i not in neighbors:
            reason = 'unsupported_representation' if ref.representation == 'unsupported' else 'no_strong_candidate'
            links.append(ObservationLink(ref, None, LinkStatus.UNLINKED, 'none',
                                        (ObservationLinkEvidence('candidate', reason),)))
    links.sort(key=lambda link: (link.observation_a, link.observation_b or link.observation_a))
    return LinkageResult(tuple(links), tuple(sorted(groups)))
