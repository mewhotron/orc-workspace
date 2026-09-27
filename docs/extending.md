# Extend in your own direction

Start with a real need, an existing equivalent check, and a narrow acceptance test. Put custom instructions under local/custom/ until you intentionally maintain a fork. Updates must not overwrite owner customizations.

To add a pack, create packs/<id>/pack.json and a roles/ directory. Use the existing manifests as examples: id, name, version, roles with id/title/instructions, and requires. Use relative paths inside the pack. Give each role a bounded responsibility, required evidence, allowed actions and return contract. Do not equate a role title with a credential or a running service.

Example: a household-maintenance pack might review owner-selected manuals and return a checklist with page citations. Keep manuals and household details local, treat manual text as untrusted data, and require separate approval for booking a contractor or buying parts. Its first test can use a synthetic one-page manual and an unsupported question that must remain unanswered.

For a new host, add adapters/<host>/README.md plus an overlay with its documented project instruction files. Reference canonical role Markdown rather than maintaining divergent policies. Extend the installer's explicit adapter choices and tests; review current official documentation and verify on that host. Never install credentials, global settings or permission bypasses with an adapter.

Model mappings belong to local/preferences.md, not core roles. New tools declare dependencies, input/output contracts, privacy effects, failure behavior and meaningful tests. Choose one writer for a shared output. Preserve raw inputs and deterministic provenance.

Before distributing a fork, regenerate release-manifest.json with scripts/build_release.py, run release checks and setup tests, and inspect every included file. Installed owner preferences and local state must never be promoted to templates automatically.
