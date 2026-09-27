"""Validate declared handoff correlation and scope; not proof of real execution."""
import argparse
import json
from pathlib import Path, PurePosixPath


def safe_path(value):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and all(p not in ("", ".", "..") for p in value.split("/"))


def validate(assignment, result):
    errors = []
    if not isinstance(assignment, dict) or not isinstance(result, dict):
        return ["records must be objects"]
    for key in ("assignment_id", "attempt_id", "worker"):
        if not isinstance(assignment.get(key), str) or not assignment[key] or result.get(key) != assignment[key]:
            errors.append("mismatched or missing " + key)
    if assignment.get("state") not in ("delegated", "in progress", "unresolved"):
        errors.append("assignment is not accepting results")
    if result.get("status") not in ("completed", "blocked", "cancelled", "unresolved"):
        errors.append("invalid result status")
    for key in ("allowed_artifacts", "allowed_effects", "required_checks"):
        values = assignment.get(key)
        if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
            errors.append("invalid assignment " + key)
    for key in ("findings", "artifacts", "checks", "side_effects", "limitations"):
        if not isinstance(result.get(key), list):
            errors.append("missing result list " + key)
    if errors:
        return errors
    for path in assignment["allowed_artifacts"] + result["artifacts"]:
        if not safe_path(path):
            errors.append("unsafe artifact path")
    if any(p not in assignment["allowed_artifacts"] for p in result["artifacts"]):
        errors.append("artifact outside declared scope")
    if any(not isinstance(e, str) or e not in assignment["allowed_effects"] for e in result["side_effects"]):
        errors.append("side effect outside declared scope")
    checks = {}
    for check in result["checks"]:
        if not isinstance(check, dict) or not isinstance(check.get("name"), str) or check["name"] in checks:
            errors.append("invalid or duplicate check")
            continue
        checks[check["name"]] = check
        if type(check.get("passed")) is not bool or not isinstance(check.get("evidence"), str) or not check["evidence"].strip():
            errors.append("check needs boolean outcome and evidence pointer")
    if result["status"] == "completed":
        for name in assignment["required_checks"]:
            if checks.get(name, {}).get("passed") is not True:
                errors.append("required check missing or failed: " + name)
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("assignment", type=Path)
    parser.add_argument("result", type=Path)
    args = parser.parse_args()
    try:
        errors = validate(json.loads(args.assignment.read_text(encoding="utf-8-sig")), json.loads(args.result.read_text(encoding="utf-8-sig")))
    except (OSError, ValueError) as exc:
        parser.exit(2, "Cannot read handoff records: " + str(exc) + "\n")
    print(json.dumps({"accepted_as_candidate": not errors, "errors": errors,
                      "limit": "Declared fields checked only; inspect real artifacts and effects independently."}, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
