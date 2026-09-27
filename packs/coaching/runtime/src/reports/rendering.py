"""Deterministic private Markdown/JSON exports with explicit coverage and scope."""

from dataclasses import asdict
from datetime import datetime
import html
import json

from .service import PersonalTrainingReport


def _cell(value):
    # Source classifications/identifiers are data, never Markdown or HTML.
    special = {'\\', '`', '*', '_', '[', ']', '|', '#', '~'}
    text = ''.join(f'&#{ord(char)};' if char in special else html.escape(char, quote=True)
                   for char in str(value))
    return text.replace('\r', ' ').replace('\n', ' ')


def _number(value, divisor=1):
    return 'unavailable' if value is None else f'{value / divisor:,.2f}'


def _total(metric, divisor=1):
    c = metric.coverage
    return f'{_number(metric.value, divisor)} ({c.available_count}/{c.activity_count})'


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError('unsupported_report_value')


def render_training_report_json(report: PersonalTrainingReport) -> str:
    """Full precision, canonical IDs, selected-field references and load evidence.

    References resolve against the original observation store; this is not a
    self-contained archive of source files. Treat the entire export as private.
    """
    return json.dumps(asdict(report), default=_json_default, allow_nan=False,
                      ensure_ascii=False, sort_keys=True, indent=2) + '\n'


def render_training_report_markdown(report: PersonalTrainingReport) -> str:
    """Human-readable factual inventory; display rounding never affects inputs."""
    a, coverage = report.analytics, report.load_coverage
    inventory, diagnostics = a.history_inventory, a.global_diagnostics
    lines = ['# Personal training report', '',
        'Private activity summary. Descriptive only; no readiness assessment or training prescription.', '',
        '## Scope', '', f'Calendar timezone: {_cell(a.aggregation_timezone)}.', '']
    selection = report.selection
    if selection is None:
        lines += ['Scope: supplied canonical history; original filter bounds are not known.']
    else:
        start = selection.start.isoformat() if selection.start else 'unbounded'
        end = selection.end.isoformat() if selection.end else 'unbounded'
        lines += [f'Requested start interval: {_cell(start)} (inclusive) to {_cell(end)} (exclusive).',
            f'Sport: {_cell(selection.sport or "all")}; subtype: {_cell(selection.subtype or "all")}.']
    if inventory.earliest_start is None:
        lines += ['', 'No canonical activities in this selection. This does not establish that no training occurred.']
    else:
        lines += ['', f'Observed activity starts (UTC): {inventory.earliest_start.isoformat()} to '
                  f'{inventory.latest_start.isoformat()}.']
    lines += ['', f'**{a.activity_count} activities · {a.active_days} active calendar days · '
              f'{a.weeks_represented} weeks with recorded activities**', '',
        'The imported history may be incomplete. Active days and weeks use activity starts; '
        'durations are not split across midnight. Weeks without records are omitted, not classified as rest.', '',
        '## Recorded volume', '',
        'Values sum available measurements only. Coverage is activities with a value / selected activities. '
        'Partial sums are not complete training totals; unavailable is distinct from zero.', '',
        '| Metric | Available-value sum | Coverage |', '| --- | ---: | ---: |']
    for label, name, divisor in (
        ('Elapsed time (hours, includes pauses)', 'elapsed_time_s', 3600),
        ('Moving time (hours)', 'moving_time_s', 3600),
        ('Distance (km)', 'distance_m', 1000),
        ('Elevation gain (m)', 'elevation_gain_m', 1),
        ('Elevation loss (m)', 'elevation_loss_m', 1),
        ('Recorded work (kJ)', 'work_kj', 1),
        ('Recorded calories (kcal)', 'calories_kcal', 1),
    ):
        metric = getattr(a.totals, name)
        c = metric.coverage
        lines.append(f'| {label} | {_number(metric.value, divisor)} | {c.available_count}/{c.activity_count} |')
    lines += ['', '## Activity types', '', '| Sport | Count |', '| --- | ---: |']
    lines += [f'| {_cell(sport)} | {count} |' for sport, count in a.counts_by_sport]
    lines += ['', '| Subtype | Count |', '| --- | ---: |']
    lines += [f'| {_cell(subtype if subtype is not None else "unspecified")} | {count} |'
              for subtype, count in a.counts_by_subtype]
    lines += ['', '## Weekly volume', '',
        'Monday-to-Monday local weeks, end exclusive. Each measurement includes coverage in parentheses. '
        'Rows describe selected records only; boundary weeks may be partial.', '',
        '| Week start | Week end | Activities | Active days | Elapsed h | Moving h | Distance km | Gain m |',
        '| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for week in a.weeks:
        t = week.totals
        lines.append(f'| {week.week_start.date()} | {week.week_end.date()} | {week.activity_count} | '
            f'{week.active_days} | {_total(t.elapsed_time_s, 3600)} | {_total(t.moving_time_s, 3600)} | '
            f'{_total(t.distance_m, 1000)} | {_total(t.elevation_gain_m)} |')
    lines += ['', '## Measurement coverage', '', '| Measurement | Activities |', '| --- | ---: |']
    by_name = {c.metric: c for c in a.coverage}
    for label, name in (('Heart rate (average or maximum)', 'heart_rate'),
            ('Cadence (average or maximum)', 'cadence'),
            ('Power (average or maximum)', 'power'), ('Normalized power', 'normalized_power_w'),
            ('Device metadata', 'device_metadata')):
        c = by_name[name]
        lines.append(f'| {label} | {c.available_count}/{c.activity_count} |')
    lines += ['', '## Training load availability', '',
        f'Power TSS calculated for **{dict(coverage.scored_by_method)["power"]}/{a.activity_count} activities**.', '',
        f'Cycling activities with the required power and duration fields: {coverage.power_activity_inputs_available}. '
        f'Activities with all scoring prerequisites: {coverage.power_prerequisites_available}.', '',
        'A score requires normalized power, a unique FTP valid at activity start, and an explicit '
        'assertion that the selected duration matches the normalized-power interval. '
        'Average power is never substituted. Missing scores are not zero.', '',
        'HR-based load is not implemented. Duration alone is not a load score. '
        'No overall load, fitness, fatigue or form estimate is inferred.', '',
        'Reasons are counted per method and may overlap within an activity.', '',
        '| Method | Reason | Activity count |', '| --- | --- | ---: |']
    lines += [f'| {_cell(method)} | {_cell(reason.replace("_", " "))} | {count} |'
              for method, reason, count in coverage.abstention_reasons]
    lines += ['', '## Source quality — entire imported snapshot', '',
        'These diagnostics are global, before date or sport filtering. '
        'Unresolved groups and projection conflicts are excluded from canonical activity totals.', '',
        f'- Source observations: {diagnostics.source_observation_count}.',
        f'- Canonical activities before filtering: {a.unfiltered_activity_count}.',
        f'- Linked pairs: {diagnostics.linked_pair_count} '
        f'({diagnostics.strong_link_count} strong candidates; '
        f'{diagnostics.authoritative_link_count} authoritative).',
        f'- Single-source activities: {diagnostics.singleton_count}.',
        f'- Unresolved ambiguous groups: {diagnostics.ambiguous_group_count}.',
        f'- Projection conflicts: {diagnostics.projection_conflict_count}.', '',
        '## Activity ledger', '',
        'Canonical IDs connect each row to its original observation references in the JSON export. '
        'Linked summary/FIT observations contribute one activity. Ledger timestamps include their stored UTC offset.', '',
        '| Canonical ID | Start | Sport | Distance km | Elapsed h | Power TSS | Load status / reasons |',
        '| --- | --- | --- | ---: | ---: | ---: | --- |']
    for activity, load in zip(report.history.activities, report.power_loads, strict=True):
        value = lambda name: activity.selected(name).value
        detail = ', '.join(load.reasons) if load.reasons else load.status.value
        lines.append(f'| {_cell(activity.canonical_id)} | {_cell(value("start_time").isoformat())} | '
            f'{_cell(value("sport"))} | {_number(value("distance_m"), 1000)} | '
            f'{_number(value("elapsed_time_s"), 3600)} | {_number(load.value)} | '
            f'{_cell(detail.replace("_", " "))} |')
    lines += ['', '## Reproducibility', '',
        f'Report: {_cell(report.report_version)}; analytics: {_cell(a.analytics_version)}; '
        f'linkage: {_cell(a.linkage_version)}; projection: {_cell(a.projection_version)}.', '',
        'JSON retains unrounded values, per-field selection rules and observation references, '
        'load formula versions, selected thresholds and explicit duration assertions. '
        'Observation references require the original local store to resolve source provenance. '
        'Source files and athlete data are not modified by report generation.', '']
    return '\n'.join(lines)
