# CLAUDE.md

Guidance for Claude Code (and humans) working in this repository.

## What this is

FOLIO-Exchange is an open-source platform, with v1 in development, for scheduled data exchanges between the FOLIO library services platform and vendor platforms. collectionHQ comes first, followed by OCLC, other analytics tools and discovery layers. Each feed is a declarative **recipe**: a shared, versioned layout plus a library's local settings. Staff set feeds up and run them without code.

Start with:
- `docs/spec.md`: the spec sheet (goals, roles, catalogue, requirements, recipe format, safety, roadmap, open questions).
- `docs/design.md`: the v1 build contract – site and feed files, recipe format, every module's signatures, the CLI, testing, and what is deliberately left out. Keep code and this file in step in the same change.
- `docs/notes.md`: decisions so far, lessons from folio-chq-extract, collectionHQ and FOLIO facts, and next steps.

Code lives in `src/folio_exchange/` (bundled recipes in `recipes/<id>/<version>.yaml`, JSON Schemas in `schemas/`). Run the tests with `.venv/Scripts/python -m pytest -q` and lint with `.venv/Scripts/ruff check .` (dev setup: `py -3.12 -m venv .venv`, then `.venv/Scripts/python -m pip install -e ".[dev]"`).

## The live spec

The editable spec is a Claude Docs document owned by the maintainer: https://claude.ai/artifact/JiPszXhwh6zTMTRP8xzf5Q. Read and edit it through the Claude Docs connector, never by web fetch. `docs/spec.md` is a snapshot of it. After changing the live copy, refresh the snapshot and its two diagrams in `docs/images/`.

## Relation to cHQ FOLIO Exchange Recipe

[cHQ FOLIO Exchange Recipe](https://github.com/ByYudkowskysTentacle/cHQ-FOLIO-Exchange-Recipe) (called FOLIOHQ until 28 Oct 2026) is the home of the collectionHQ Academic recipe and its setup guide. Its `recipes/chq-academic/` is the source of the bundled `src/folio_exchange/recipes/chq-academic/`: change the recipe there first, then copy the folder here unchanged (its tests fail when the two differ). Its `docs/setup.md` and `docs/layout.md` are the collectionHQ documentation; keep commands and behaviour they describe in step with this code.

Its original tool, `folio-chq-extract` (phase 0, the reference implementation), now lives in its `legacy/` folder as a fallback until the first real delivery from FOLIO-Exchange. Its CI checks that the recipe here writes byte-identical files to it. Reuse its tested pieces rather than rewriting them:
- the client (login, retries, keyset paging)
- sync and cache (deltas, deletion detection)
- the field registry
- the MARC and delimited builders
- delivery (pinned-key SFTP, staged or direct upload)
- credentials (environment or OS keyring)

cHQ FOLIO Exchange Recipe's `CLAUDE.md` lists the FOLIO API facts that code depends on.

## Ground rules

- **Eureka first.** Okapi-based FOLIO keeps working only while the same code serves both; no Okapi-only fixes, features or tests.
- **Never read or export patron data** unless a recipe declares it and an admin approves it. Do not add user or patron endpoints casually.
- **Secrets** come only from environment variables or the OS keyring. The one exception is an SFTP private key saved from the page: too large for Credential Manager, it is a file in the site's `keys` folder that only the Windows user can read. Never put secrets in recipes, config, logs, commits or chat. Tests never touch the real keyring.
- **Signing in:** do not sign in to a production service (FOLIO tenant, vendor SFTP) with a password yourself. Give the person a command to run, and read only its output.
- **This repo is public.** Never commit credentials, `config.yaml`, `.env`, cache data or output files, or personal names and emails. Commits use the GitHub noreply address.
- **Recipes stay declarative:** no executable code in recipes.

## Conventions (carried over from folio-chq-extract)

- Python 3.10+, type hints, small modules, no heavy dependencies without reason.
- Tests run offline against a mock FOLIO; every bug fix gets a regression test.
- Docs style: use en dashes ( – ) for asides, never em dashes.
