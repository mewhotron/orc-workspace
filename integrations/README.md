# Optional integration recipes

No accounts, tokens, connectors, plugins or scheduled tasks are installed. The recipes below define an owner-driven setup and verification procedure; they do not claim a complete implementation of every original integration.

## Garmin and FIT

Use the coaching pack's offline tools where included to process explicitly selected owner files. A Garmin account sync is a separate capability: choose a maintained connector or implement an adapter against the local import contract, inspect credential storage and verify a single bounded import before scheduling anything. Do not copy an existing owner's token cache. Preserve raw files, provenance and missing values. Establish timezone explicitly. No standing daily schedule or missed-run behavior is inherited.

## Calendar

Connect the new owner's chosen calendar through their AI host's supported authorization flow. First verify read-only timezone/calendar selection. Prepare a concrete event proposal and create/update it only when authorized; read it back and reconcile duplicates before retrying. A coaching recommendation alone does not authorize calendar writes. This release does not implement a calendar API client.

## Notion or another knowledge/document service

Use the owner's supported connector and select destination pages explicitly. Keep personal coaching data and book text local unless separately approved for that recipient/purpose. Prepare exact updates, preserve unrelated blocks and verify readback. Marketplace plugins and account permissions are external prerequisites, not bundled capabilities.

## AI providers and additional tools

Use the host's supported provider configuration; model names and endpoint credentials do not belong in this repository. Map task tiers in local/preferences.md. Document tool capabilities and their actual test evidence. A new connector does not gain blanket cross-project data access.

## Deferred source-project features

Original Discord transport, the Streamlit dashboard and provider-specific multi-stage RAG services are not started or connected by setup. Navigator is unimplemented in the source scope. Their future adaptation requires explicit scoped development and fresh privacy/dependency verification. A saved design is not a working integration.
