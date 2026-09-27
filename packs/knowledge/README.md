# Knowledge pack

The technical **Knowledge Curator** role is in `roles/knowledge-curator.md`. The optional local tool is `knowledge/orc_knowledge.py`, using Python 3 and the standard library. It creates a private SQLite collection from owner-selected UTF-8 `.txt` or `.md` files. It does not call an AI provider or send data anywhere. The included `knowledge/starter.sqlite3` is rebuilt from the two newly authored fictional files in `knowledge/examples/` by `knowledge/build_starter.py`; it contains no inherited book excerpts or personal profile.

Use these commands from the installed workspace, substituting your own paths:

```powershell
python knowledge/orc_knowledge.py inspect --db knowledge/starter.sqlite3
python knowledge/orc_knowledge.py search --db knowledge/starter.sqlite3 --query recovery
python knowledge/orc_knowledge.py init --db local/my-knowledge.sqlite3
python knowledge/orc_knowledge.py import-text --db local/my-knowledge.sqlite3 --source C:\path\to\my-notes.txt --source-id my-notes --title "My notes" --chapter "Chapter one" --page 1
```

The owner chooses locally held source files and is responsible for rights and disclosure. A source ID cannot silently replace different content: a repeat of the same source is unchanged; a changed file or metadata requires deliberate new ID or a new collection. Imports store text, source SHA-256, chapter/page values supplied by the owner, basename and chunk hashes. Search returns candidate passages with source/chunk locators, not a verified answer. Chapter and page values are annotations supplied by the importer, not extracted or independently checked. The tool cannot extract arbitrary PDF/EPUB, scan images, retain table layout, establish source completeness or provide semantic retrieval. Convert a lawfully held document to reviewed plain text with appropriate tools, inspect omissions, then import that text privately.

`inspect` and `search` also read the older The Plan `catalog_meta`/`sources`/`chunks` schema 1.1/1.2 without modifying it. Legacy cycling EPUB locators and fitness PDF provenance are exposed when present. The original cycling, fitness and nutrition databases include substantive book-derived text and are **not included** here. The original system's title-specific extractors, book/recipe structures, OpenAI embedding and claim verification pipeline are not recreated. A citation identifies a candidate source passage; it does not establish that a coaching or nutrition conclusion is safe or supported. Keep imported content under the installed ignored `local/` directory, which is not encryption.
