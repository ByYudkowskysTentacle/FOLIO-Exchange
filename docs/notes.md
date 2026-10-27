# Working notes

_Last updated: 27 October 2026, after batch 1 of the improvement plan. Read with `docs/spec.md` and `docs/design.md`._

## Where things stand

- **FOLIO-Exchange** v1 is built and tested offline (see below); not yet released or run against a real tenant. The spec sheet is `docs/spec.md`.
- **[FOLIOHQ](https://github.com/ByYudkowskysTentacle/FOLIOHQ)** is, since 27 Oct 2026, the home of the `chq-academic` recipe and the collectionHQ Academic setup guide (`docs/setup.md`) and file layout (`docs/layout.md`). Its `docs/HANDOFF.md` has the collectionHQ status and open questions.
- **Phase 0, `folio-chq-extract`**, the one-vendor tool this platform generalizes, moved to FOLIOHQ's `legacy/` folder. It stays installable and tested as a fallback until FOLIO-Exchange makes its first real delivery. Its status:
  - Tested end to end against FOLIO's public reference systems (Okapi and Eureka).
  - The cHQ Academic file layout is implemented.
  - The cHQ SFTP login is verified.
  - It has **not** yet run against the library's own tenant, and nothing has been uploaded to cHQ.
- **The first library's tenant is on Eureka.**

## v1 build (26 Oct 2026)

The contract is `docs/design.md`; it was revised after a three-lens design review (spec coverage, safety and correctness, operator UX and FOLIOHQ parity), and its last section lists what was reviewed and left for later.

**Done**
- The whole pipeline: recipe library and feed overlay, read-only FOLIO client with patron and redirect guards, sync, MARC and delimited builders, checks, SFTP and local delivery, run history and audit, schedules, alerts, the runner, the command line and the guided `init`.
- Safety from the review: first delivery held per destination and per recipe major version; `release` shows the run, checks file hashes and asks first; partial SFTP deliveries recorded; manual runs never cancel a scheduled delivery; full syncs refuse a truncated download; https and TLS for SMTP required; shared recipes cannot set where files go or which server key to trust; relative paths taken from the site folder; hand edits to settings audited.
- A design and function review on 27 Oct (`docs/review-2026-10-27.md`) verified 71 findings; the 27 in "Fix now" are fixed, and the rest are planned in `docs/improvement-plan.md`.
- Batch 1 of the plan is done: any change to a feed's delivered layout holds the next delivery and says what changed; `check`, `feeds` and the page warn when the hourly task has not run for two hours, and an optional heartbeat URL tells an outside monitor; `check` warns about folders other Windows users can change, and the guide installs under `%LOCALAPPDATA%`; `folio.ca_bundle` (a PEM file or the Windows certificate store) replaces turning TLS checks off; the page opens only through a one-time link; staff data is dropped from cached records; dependencies are pinned in `constraints.txt`. No release tag yet: the guide and installer still install `main`.
- Batch 2 of the plan is done: a shared feed-operations core (`feedops.py`), paused and dry-run feeds handled plainly on the command line, a schedule command that works in PowerShell, guide sections for going live and updating, and a keyboard-usable, self-refreshing page.
- A final review found 14 bugs (each reproduced) and an operator walkthrough found 15 usability problems; all are fixed, with regression tests (`test_regressions.py`, `test_walkthrough.py`).
- `chq-academic` 1.0.0 writes byte-identical `.mrc` and `.psv` files to FOLIOHQ from the same data. Since 27 Oct 2026, FOLIOHQ's CI checks it on every change.
- 202 offline tests (`pytest`), `ruff` clean, JSON Schemas and commented examples. CI passes on Linux (Python 3.10 to 3.13) and Windows, with a gitleaks secret scan. Merged to `main` in PR #1.

**Eureka trial (26 Oct 2026).** Run against the public Eureka Snapshot reference system (`https://folio-etesting-snapshot-kong.ci.folio.org`, tenant `diku`, the published demo login), delivering to a local folder:
- `check`: login and every endpoint readable (10,042 instances, 27 holdings, 33 items, 15 loans, ten reference tables).
- `run chq`: a full sync in 30 seconds (10,000 MARC records fetched), held as a first delivery; `release` sent it; the next run was a delta of under 4 seconds and delivered straight away.
- A feed with `bibs_without_items: true` built 10,041 bibs (10,000 from SRS). Every record parsed; 001 = instance HRID and 999 ff $i = instance UUID on all of them; leader 9 = `a`; no duplicates; every PSV row had 20 columns and linked to a bib.
- `init` ran end to end with typed answers: login, permissions, the tenant's own locations, a live preview, a local delivery test.
- Fixed from what it showed: previews sampled the first instances by id, and on this tenant (like a library with many e-resources) none had items, so previews were empty. They now start from items. Also: the password note in `check` and `schedule` now matches where `site.yaml` says the password is; the wizard writes `secret: env:FOLIO_PASSWORD` when the password stays in the environment, and no empty `filters`; `check --delivery` names the local folder.
- In this sample data every instance with items is FOLIO-source, so the default chq feed sends generated bibs only; a real tenant will mostly send SRS MARC, as the all-bibs feed showed.

**Not done:** nothing has run against the library's own tenant or a vendor server; the Windows scheduled task, keyring use under it, and the Teams card are untested outside mocks. Since 27 Oct 2026, gate 1 goes through FOLIO-Exchange, with FOLIOHQ's legacy tool as the fallback.

**Next**
1. A dry run against the library's own tenant: `docs/dry-run.md` is the guide. The operator runs it; the password goes from their keyboard into Windows Credential Manager and is never shared.
2. Gate 1: the first delivery to collectionHQ, following FOLIOHQ's `docs/setup.md` (see Next steps below).
3. Try the hourly Windows task (`folio-exchange schedule`) on the library PC, with the password in Credential Manager.

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
| 2026-10-26 | MIT license, the same as FOLIOHQ | One license across both tools; FOLIOHQ's code is reused here |
| 2026-10-26 | Start a simple UI early, before gate 1: a local page served by `folio-exchange ui`, laid out like Postman, with no new dependencies | The dry run waits on the library's credentials. The page reuses the runner, so it adds no new path to FOLIO or a vendor |
| 2026-10-26 | A delivery to a new destination, or on a new recipe major version, waits for a person to release it | A test delivery to a local folder must not disarm the review of the first real upload |
| 2026-10-26 | Recipe minor and patch versions never change an output's bytes; any layout change is a new major | `^1.0` updates automatically, so a vendor must never see an unreviewed change |
| 2026-10-26 | A shared recipe sets only how files travel (transport, upload mode); hosts, folders and server keys are local | A recipe from another site must not choose where files go or which server to trust |
| 2026-10-27 | FOLIOHQ becomes the home of the `chq-academic` recipe and its documentation; its tool moves to `legacy/` as a fallback, and gate 1 runs on FOLIO-Exchange | One engine to maintain. The recipe and the collectionHQ docs live with what was learned about collectionHQ, and byte parity, checked in FOLIOHQ's CI, lets a library switch either way |

Still open: patron data, upkeep and funding, a proposal to the FOLIO community (Reporting SIG), and the name check.

## Lessons carried over from FOLIOHQ

**Vendor specs**
- cHQ Academic links items to bibs on **Bib record ID = FOLIO instance UUID = MARC 999 ff $i**, which SRS writes. It does not use 001 or a barcode. FOLIOHQ enforces 999 ff $i on every record (`MarcBuilder.finalize`).
- cHQ's PSV layout has 20 columns, pipe-delimited, with `None` for empty values and its own header names. The `chq-academic` recipe and FOLIOHQ's `legacy/config.example.yaml` reproduce it exactly.
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
- Current Node 24 action versions (Oct 2026): `actions/checkout@v7`, `actions/setup-python@v7`, `actions/cache@v6`, `actions/upload-artifact@v7`. CI pins each action to the full commit SHA of its tag, with the tag in a comment; look the commit up read-only with `gh api repos/<owner>/<repo>/commits/<tag> --jq .sha` when moving to a new version.
- Commits use the GitHub noreply address, so no personal email is published.

**Refreshing `constraints.txt`** (on purpose, in a change of its own; CI and the installer both install with it):
1. In a fresh venv on Windows with Python 3.12, install FOLIO-Exchange alone, so pip picks the newest versions: `py -3.12 -m venv ../venv-constraints` (outside the repository), then `../venv-constraints/Scripts/python -m pip install .`
2. `../venv-constraints/Scripts/python -m pip freeze --exclude folio-exchange` lists the runtime packages. Put those lines in `constraints.txt` under its header. Keep the environment markers: `sys_platform == "win32"` on `pywin32-ctypes`, and the `python_version` pairs for packages whose newest version needs Python 3.11 (check on PyPI which version is the newest for 3.10). Leave out the Linux-only and older-Python packages the header names.
3. Install the dev tools with the new file (`../venv-constraints/Scripts/python -m pip install -c constraints.txt -e ".[dev]"`), run the tests and ruff, and let CI check Linux (Python 3.10 to 3.13) before merging.

## Next steps

1. **Gate 1 with FOLIO-Exchange**, following FOLIOHQ's `docs/setup.md`:
   - **Connect:** get the library's FOLIO API (Kong) URL, the tenant ID and a read-only service account, then run `folio-exchange init`, `check` and `run chq --no-deliver`.
   - **Analyze:** review locations, call numbers and counts in that output.
   - **Agree with cHQ:** the remote folder and host key, plus a sample for their approval.
   - **First delivery:** `run chq`, then `release chq`. cHQ accepting it is gate 1. If FOLIO-Exchange is blocked, FOLIOHQ's legacy tool sends the same files.
2. **Here, in parallel:**
   - ~~Decide the license~~: MIT, 26 Oct 2026.
   - ~~Turn the recipe example in the spec into a draft recipe schema~~: `schemas/recipe.schema.json`, 26 Oct 2026.
   - ~~Plan how FOLIOHQ's core splits into reusable parts~~: done in the v1 build, 26 Oct 2026.
3. **Keep the spec current:** edit the live copy, then refresh `docs/spec.md` from it.
