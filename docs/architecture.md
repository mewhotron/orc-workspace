# Architecture

Orc Workspace is a set of local instructions and optional deterministic tools, used by an AI application the owner already has. No server, database of agents, always-on scheduler or provider SDK is required by the core.

The root AGENTS.md loads core coordination/delegation/model-routing rules. Pack manifests identify roles and prerequisites; role Markdown is authoritative. Host adapters point to these files. The setup script chooses packs and maps the selected host overlay into its conventional local paths.

Orc routes coaching to Lead Coach, which consults relevant specialists. Knowledge Curator handles ingestion/provenance, Search Crawler handles research, and Media Lab handles local media work. Keep private data with its owning project. Ordinary software work uses scoped implementation workers, not coaching specialists.

Tools under packs run only when invoked. They do not create AI sessions or authenticate accounts. The host supplies model inference, filesystem permissions, search and delegation. The owner maps available models to task tiers. Actual tool access may differ from role instructions.

Records under local/ are plain inspectable files. Assignment/attempt correlation reduces confusion but does not provide transactional exactly-once execution. One writer and direct artifact checks remain necessary. Book retrieval returns candidate evidence; successful lookup alone is not proof that an answer is supported.

No original author's personal profile, standing schedule, remote project IDs or chat history is part of this architecture. The new owner decides the scope.
