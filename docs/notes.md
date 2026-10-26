# Working notes

_Last updated: 26 October 2026, when this repo was started. Read with `docs/spec.md`._

## Where things stand

- **FOLIO-Exchange** is at the spec stage: no code yet. The spec sheet is `docs/spec.md`.
- **Phase 0 is [FOLIOHQ](https://github.com/ByYudkowskysTentacle/FOLIOHQ)** (`folio-chq-extract`), a working one-vendor tool for collectionHQ Academic. It is the reference implementation this platform generalizes. Its `docs/HANDOFF.md` has its own status and open questions.
- **FOLIOHQ status:**
  - Tested end to end against FOLIO's public reference systems (Okapi and Eureka).
  - The cHQ Academic file layout is implemented.
  - The cHQ SFTP login is verified.
  - It has **not** yet run against the library's own tenant, and nothing has been uploaded to cHQ.
- **The first library's tenant is on Eureka.**
- **The v1 build has started** (26 Oct 2026, paused at a checkpoint): see "Build checkpoint" below and `docs/design.md`.

## Build checkpoint (26 Oct 2026)

The plan is `docs/design.md`: the site folder, recipe and feed formats, every module's contract and the CLI. This is the sanctioned "in parallel" work from Next steps (recipe schema, splitting FOLIOHQ's core); FOLIOHQ stays the path to gate 1.

**Done**
- Scaffold: `pyproject.toml` (package and command `folio-exchange`, Python 3.10+, `keyring` as a core dependency), `src/` layout, `NOTICE.md` (MIT notice for code adapted from FOLIOHQ), `.gitignore` for site files, `tests/conftest.py` (real keyring and env secrets blocked; `folio` and `site` fixtures) and `tests/sitekit.py`.
- Written and smoke-tested by hand, **no unit tests yet:** `errors`, `credentials` (secret references, keyring), `config` (`site.yaml`, rejects secrets), `reads` (read registry, path allowlist, patron denylist), `fields` (registry with what each field needs), `lock`, `history` (runs and audit), `schedule` (slots, due runs, OS scheduler command), `notify` (webhook, email), `delivery` (pinned-key SFTP, local folder).
- Recipe `chq-academic` 1.0.0: same 20 columns, headers, delimiter, empty value, date format and 949 settings as FOLIOHQ's `config.example.yaml` (checked by script).

**Copied from FOLIOHQ, not yet adapted** (the package does not import cleanly until they are): `client.py` (needs the allowlist guard and `iter_where_in`), `sync.py` (driven by reads and `Site`; `sample()` for previews), `marc.py`, `build.py` (still on FOLIOHQ's config dict), `tests/mock_folio.py` (needs `field==("a" or "b")` queries).

**Not started:** `recipe.py`, `delimited.py`, `checks.py`, `runner.py`, `cli.py`, `wizard.py`, JSON Schemas, example site and feed files, all tests, CI, and README and CLAUDE.md updates.

**To resume**
1. Re-run the design critique (three reviewers: spec coverage, safety and correctness, operator UX and FOLIOHQ parity) and fold its findings into `docs/design.md`.
2. Build in parallel against the contract: recipe loader and schemas; client and sync; build and checks; tests for the modules already written. Then runner and CLI, the setup wizard, and docs and CI.
3. Review the whole build adversarially, fix, and run `pytest` and `ruff check .` clean.

Dev setup: `py -3.12 -m venv .venv`, then `.venv/Scripts/python -m pip install -e ".[dev]"`.

## Decisions

| Date | Decision | Why |
| --- | --- | --- |
| 2026-10-26 | Eureka first; Okapi kept only while the same code covers it, with no Okapi-only work | FOLIO is moving to Eureka, and the first library is already on it |
| 2026-10-26 | New repo FOLIO-Exchange; FOLIOHQ stays as phase 0 | Keep the working cHQ tool stable while the platform is designed |
| 2026-10-26 | The operator is a systems librarian: v1 is a guided command line plus recipes; the web UI is phase 2 | Matches who will actually run it |
| 2026-10-26 | It runs on each library's own PC or server, inside its network | FOLIO hosts usually accept API traffic only from known networks |
| 2026-10-26 | Outbound feeds after cHQ are unranked candidates | The second recipe is whichever a partner library needs first |
| 2026-10-26 | Writing into FOLIO (inbound loads) waits for v2, behind a separate account and a second approver | Writes carry far more risk than reads |
| 2026-10-26 | Working name FOLIO-Exchange | Check FOLIO's trademark guidance before announcing it |

Still open: patron data, license (MIT vs Apache-2.0), upkeep and funding, a proposal to the FOLIO community (Reporting SIG), and the name check.

## Lessons carried over from FOLIOHQ

**Vendor specs**
- cHQ Academic links items to bibs on **Bib record ID = FOLIO instance UUID = MARC 999 ff $i**, which SRS writes. It does not use 001 or a barcode. FOLIOHQ enforces 999 ff $i on every record (`MarcBuilder.finalize`).
- cHQ's PSV layout has 20 columns, pipe-delimited, with `None` for empty values and its own header names. FOLIOHQ's `config.example.yaml` reproduces it exactly.
- Vendor sample files get mangled in transit. cHQ's sample `.marc` had its field and subfield separators (0x1E, 0x1F) turned into newlines and ` $`, so no MARC reader could open it. Validate binary MARC on intake, and ask vendors for the original file, zipped.

**collectionHQ SFTP (US)**
- **Host:** `sftp.us.collectionhq.com:22`, an AWS Transfer Family server.
- **Host key:** RSA `SHA256:qt5HxgZ6ktlwhFUUAwdjMZlL2KkAbKXhy2TsRxWoITI`. This was read from the server on 26 Oct 2026, not received from cHQ, so confirm it with them.
- **FTP host:** `ftp.collectionhq.com` is plain FTP only, so it is unusable.
- **Account:** the library's service account was described as upload-only, so FOLIOHQ uses `upload_mode: direct`, with no stat and no rename. Listing the login folder works.
- **Folders:** the login folder contains `holds/` and `transactions/`, which look like other cHQ feeds. Which folder bibs and items go into is still to confirm with cHQ.

**FOLIO**
- `POST /authn/login-with-expiry` works on both Okapi and Eureka (through the Kong gateway).
- **Public reference systems:** current URLs and demo logins are listed at https://folio-org.atlassian.net/wiki/x/9oCeHg. As of Oct 2026, the Eureka API is `https://folio-etesting-snapshot-kong.ci.folio.org`, tenant `diku`, and the environment is rebuilt daily, so record counts change. These are public demo accounts, not secrets.
- **What FOLIO already does natively** (checked in docs.folio.org, Oct 2026):
  - Data Export builds MARC (including holdings and item fields), but its docs describe no schedule or delivery.
  - EDI order exports to vendors are scheduled, over FTP or SFTP.
  - Bursar transfers are scheduled (Settings > Users > Transfer criteria).
  - OAI-PMH serves `marc21_withholdings` to systems that harvest.
- Keyset paging (`id>"last" sortBy id`) is required for large collections; offset paging degrades badly.
- Never read or export patron data. Loans keep `itemId` and dates after anonymization, which is all circulation counts need.

**Secrets and accounts**
- Passwords come from environment variables or the OS keyring (`keyring` package; Windows Credential Manager on Windows). FOLIOHQ uses the services `folio-chq-folio` and `folio-chq-sftp`, keyed by username.
- Tests must never touch a developer's real credential store. FOLIOHQ blocks the real keyring with an autouse fixture.
- AI assistants on this project do not sign in to production services with a password, even when allowed to. They hand the person a command to run, and never read, print or write the password.
- A credentials text file nearly got committed to a public repo. Ignore credential-looking files by default (see `.gitignore`).

**GitHub**
- On this account, GitHub Actions in private repos is blocked by billing; public repos run free.
- Current Node 24 action versions (Oct 2026): `actions/checkout@v7`, `actions/setup-python@v7`, `actions/cache@v6`, `actions/upload-artifact@v7`.
- Commits use the GitHub noreply address, so no personal email is published.

## Next steps

1. **Finish phase 0 in FOLIOHQ.**
   - **Connect:** get the library's FOLIO API (Kong) URL, the tenant ID and a read-only service account, then run `folio-chq check` and `run --no-deliver`.
   - **Analyze:** review locations, call numbers and counts in that output.
   - **Agree with cHQ:** the remote folder, plus a sample for their approval.
   - **First delivery:** passing it is gate 1.
2. **Here, in parallel:**
   - Decide the license.
   - Turn the recipe example in the spec into a draft recipe schema.
   - Plan how FOLIOHQ's core splits into reusable parts: the client, sync and cache, the field registry, the builders and delivery.
3. **Keep the spec current:** edit the live copy, then refresh `docs/spec.md` from it.
