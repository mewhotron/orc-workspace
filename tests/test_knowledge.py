"""Meaningful offline checks for the provider-neutral knowledge tool."""

import importlib.util
from contextlib import closing
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("orc_knowledge", ROOT / "knowledge/orc_knowledge.py")
knowledge = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = knowledge
SPEC.loader.exec_module(knowledge)


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "private.sqlite3"
        knowledge.create(self.db)
        self.source = self.root / "notes.txt"
        self.source.write_text("A synthetic chapter discusses careful recovery.\n\nMissing data is unknown.", encoding="utf-8")

    def test_import_citation_and_negative_lookup(self):
        result = knowledge.import_text(self.db, self.source, "synthetic", "Synthetic handbook", "Example author", "Recovery", 12)
        self.assertEqual(result["status"], "imported")
        rows = knowledge.search(self.db, "recovery")
        self.assertTrue(rows)
        self.assertEqual(rows[0]["source_sha256"], knowledge.digest(self.source.read_bytes()))
        self.assertEqual(rows[0]["first_page"], 12)
        self.assertEqual(rows[0]["document_path"], "notes.txt")
        self.assertEqual(knowledge.search(self.db, "nonexistentzebra"), [])

    def test_conflict_is_atomic_and_repeat_is_idempotent(self):
        knowledge.import_text(self.db, self.source, "synthetic", "Synthetic handbook")
        before = knowledge.inspect(self.db)
        again = knowledge.import_text(self.db, self.source, "synthetic", "Synthetic handbook")
        self.assertEqual(again["status"], "unchanged")
        self.source.write_text("Changed synthetic content.", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "source_id_conflict"):
            knowledge.import_text(self.db, self.source, "synthetic", "Synthetic handbook")
        self.assertEqual(knowledge.inspect(self.db), before)

    def test_locator_change_requires_new_source_id(self):
        knowledge.import_text(self.db, self.source, "synthetic", "Synthetic handbook",
                              "Example author", "First chapter", 12)
        before = knowledge.inspect(self.db)
        for chapter, page in (("Corrected chapter", 12), ("First chapter", 13)):
            with self.assertRaisesRegex(ValueError, "source_id_conflict"):
                knowledge.import_text(self.db, self.source, "synthetic", "Synthetic handbook",
                                      "Example author", chapter, page)
            self.assertEqual(knowledge.inspect(self.db), before)
        renamed = self.root / "renamed.txt"
        renamed.write_bytes(self.source.read_bytes())
        with self.assertRaisesRegex(ValueError, "source_id_conflict"):
            knowledge.import_text(self.db, renamed, "synthetic", "Synthetic handbook",
                                  "Example author", "First chapter", 12)
        corrected = knowledge.import_text(self.db, self.source, "synthetic-corrected",
                                          "Synthetic handbook", "Example author",
                                          "Corrected chapter", 13)
        self.assertEqual(corrected["status"], "imported")
        result = [row for row in knowledge.search(self.db, "recovery")
                  if row["source_id"] == "synthetic-corrected"][0]
        self.assertEqual((result["chapter"], result["first_page"]),
                         ("Corrected chapter", 13))

    def test_missing_database_is_not_created_by_import(self):
        missing = self.root / "missing.sqlite3"
        with self.assertRaises(FileNotFoundError):
            knowledge.import_text(missing, self.source, "synthetic", "Synthetic handbook")
        self.assertFalse(missing.exists())

    def test_unicode_punctuation_and_no_match_queries(self):
        self.source.write_text("A fictional café serves résumé notes about recovery.", encoding="utf-8")
        knowledge.import_text(self.db, self.source, "synthetic", "Synthetic handbook")
        self.assertTrue(knowledge.search(self.db, "café"))
        self.assertTrue(knowledge.search(self.db, "résumé"))
        for query in ("__", "???", "%", "nonexistentzebra"):
            self.assertEqual(knowledge.search(self.db, query), [])

    def test_source_path_and_format_safety(self):
        with self.assertRaisesRegex(ValueError, "invalid_source_id"):
            knowledge.import_text(self.db, self.source, "../outside", "Title")
        pdf = self.root / "fake.pdf"
        pdf.write_bytes(b"not a PDF")
        with self.assertRaisesRegex(ValueError, "only_utf8"):
            knowledge.import_text(self.db, pdf, "fake", "Title")
        if hasattr(Path, "symlink_to"):
            link = self.root / "link.txt"
            try:
                link.symlink_to(self.source)
            except OSError:
                return
            with self.assertRaisesRegex(ValueError, "source_must_be_regular_file"):
                knowledge.import_text(self.db, link, "link", "Title")

    def test_legacy_read_only_adapter(self):
        legacy = self.root / "legacy.sqlite3"
        with closing(sqlite3.connect(legacy)) as db, db:
            db.executescript("""
                CREATE TABLE catalog_meta(key TEXT PRIMARY KEY,value TEXT);
                INSERT INTO catalog_meta VALUES('catalog_schema_version','1.1.0');
                CREATE TABLE sources(source_id TEXT PRIMARY KEY,book_title TEXT,author TEXT);
                CREATE TABLE chunks(chunk_id TEXT PRIMARY KEY,source_id TEXT,text TEXT,breadcrumb TEXT,
                    epub_path TEXT,retrieval_eligible INTEGER);
                INSERT INTO sources VALUES('s','Synthetic legacy','Example');
                INSERT INTO chunks VALUES('c','s','A fictional hill ride.','Synthetic legacy / Hill','chapter.xhtml',1);
            """)
        self.assertEqual(knowledge.inspect(legacy)["schema"], "legacy-1.1.0")
        self.assertEqual(knowledge.search(legacy, "hill")[0]["document_path"], "chapter.xhtml")
        with self.assertRaisesRegex(ValueError, "imports_require_orc_schema"):
            knowledge.import_text(legacy, self.source, "new", "New")


if __name__ == "__main__":
    unittest.main()
