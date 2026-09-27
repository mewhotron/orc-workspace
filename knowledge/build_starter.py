"""Rebuild the distributable database from original synthetic examples."""

from pathlib import Path

from orc_knowledge import create, import_text, inspect


HERE = Path(__file__).resolve().parent
OUT = HERE / "starter.sqlite3"


def main() -> None:
    if OUT.exists():
        raise SystemExit("starter.sqlite3 exists; remove only this generated file to rebuild")
    create(OUT)
    for filename, source_id, title, chapter in (
        ("commute-recovery.txt", "synthetic-commute", "Fictional commute and recovery", "Context"),
        ("strength-schedule.txt", "synthetic-schedule", "Fictional schedule conflict", "Priorities"),
    ):
        import_text(OUT, HERE / "examples" / filename, source_id, title,
                    author="Orc Workspace synthetic example", chapter=chapter)
    print(inspect(OUT))


if __name__ == "__main__":
    main()
