"""Protect the release boundary between blank templates and private owner state."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("build_release", Path(__file__).resolve().parents[1] / "scripts/build_release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseBoundaryTests(unittest.TestCase):
    def test_real_package_has_all_blank_owner_templates(self):
        names = {name for name, path in release.files()}
        for name in ("activity", "decisions", "preferences", "projects"):
            self.assertIn(f"templates/local/{name}.md", names)

    def test_private_state_excluded_and_unknown_template_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for filename in release.ROOT_FILES:
                (root / filename).write_text("synthetic release input")
            (root / "local").mkdir()
            (root / "local/private.md").write_text("fictional private state")
            templates = root / "templates/local"
            templates.mkdir(parents=True)
            (templates / "preferences.md").write_text("blank template")
            self.assertEqual({name for name, path in release.files(root)}, release.ROOT_FILES | {"templates/local/preferences.md"})
            (templates / "unknown.md").write_text("unreviewed")
            with self.assertRaises(ValueError):
                release.files(root)


if __name__ == "__main__":
    unittest.main()
