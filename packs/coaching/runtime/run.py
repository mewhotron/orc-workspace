"""Launch the offline activity utilities with paths relative to workspace root."""

import os
from pathlib import Path
import sys


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in ("fit-import", "report"):
        print("Usage: python packs/coaching/runtime/run.py {fit-import|report} [options]", file=sys.stderr)
        return 2
    workspace = Path(__file__).resolve().parents[3]
    os.chdir(workspace)
    command = args.pop(0)
    if command == "fit-import":
        try:
            from scripts.import_garmin_fit_activities import main as action
        except ModuleNotFoundError as exc:
            if exc.name == "garmin_fit_sdk":
                print("fit_sdk_unavailable: install optional garmin-fit-sdk in this Python environment", file=sys.stderr)
                return 2
            raise
    else:
        from scripts.personal_training_report import main as action
    return action(args)


if __name__ == "__main__":
    raise SystemExit(main())
