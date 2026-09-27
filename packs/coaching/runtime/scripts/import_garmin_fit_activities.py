"""Import recognized Garmin activity FIT members from a ZIP without extraction."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path

from src.ingestion.activity_storage import DEFAULT_ACTIVITY_DATABASE, ActivityStorageError
from src.ingestion.garmin_fit_import import GarminFitImportError, import_garmin_fit_activities


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--database', type=Path, default=DEFAULT_ACTIVITY_DATABASE)
    args = parser.parse_args(argv)
    try:
        report = import_garmin_fit_activities(args.archive, args.database)
    except (GarminFitImportError, ActivityStorageError):
        print(json.dumps({'error': 'fit_activity_import_failed'}))
        return 1
    # Keep CLI output aggregate-only; detailed safe diagnostics live in receipts.
    output = asdict(report)
    output.pop('rejection_diagnostics')
    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
