"""Export a private, deterministic training report from the local activity store."""

import argparse
from datetime import datetime
from pathlib import Path
import sys

from src.history import HistoryFilter
from src.ingestion.activity_storage import DEFAULT_ACTIVITY_DATABASE
from src.reports import (
    TrainingReportError, read_personal_training_report,
    render_training_report_json, render_training_report_markdown,
)
from src.training_load import ThresholdConfiguration, TrainingLoadError, load_threshold_configuration


def _timestamp(text):
    try:
        value = datetime.fromisoformat(text)
        if value.utcoffset() is None or 'T' not in text or text.endswith('-00:00'):
            raise ValueError
        return value
    except ValueError:
        raise argparse.ArgumentTypeError('Use an ISO timestamp with T and an explicit UTC offset.') from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=DEFAULT_ACTIVITY_DATABASE)
    parser.add_argument('--start', type=_timestamp, help='Inclusive activity start timestamp')
    parser.add_argument('--end', type=_timestamp, help='Exclusive activity start timestamp')
    parser.add_argument('--sport', help='Exact canonical sport, e.g. cycling')
    parser.add_argument('--subtype', help='Exact canonical subtype')
    parser.add_argument('--timezone', default='UTC', help='IANA calendar zone; default UTC')
    parser.add_argument('--thresholds', type=Path, help='Explicit private threshold configuration')
    parser.add_argument('--format', choices=('markdown', 'json'), default='markdown')
    parser.add_argument('--output', type=Path, help='New private export file; existing files are never replaced')
    args = parser.parse_args(argv)
    try:
        thresholds = (load_threshold_configuration(args.thresholds) if args.thresholds
                      else ThresholdConfiguration())
        report = read_personal_training_report(args.database,
            HistoryFilter(args.start, args.end, args.sport, args.subtype),
            aggregation_timezone=args.timezone, thresholds=thresholds)
        render = render_training_report_json if args.format == 'json' else render_training_report_markdown
        output = render(report)
    except (TrainingReportError, TrainingLoadError) as error:
        print(str(error), file=sys.stderr)
        return 1
    try:
        if args.output:
            # Exclusive creation protects sources, existing exports and the DB.
            # Parent directories must already exist; no implicit filesystem setup.
            with args.output.open('x', encoding='utf-8', newline='\n') as destination:
                destination.write(output)
        else:
            sys.stdout.write(output)
    except (OSError, UnicodeError):
        print('report_output_failed', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
