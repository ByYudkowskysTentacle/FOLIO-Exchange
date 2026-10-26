# Working notes

_Last updated: 26 October 2026, after the v1 build. Read with `docs/spec.md` and `docs/design.md`._

## Where things stand

- **FOLIO-Exchange** v1 is built and tested offline (see below); not yet released or run against a real tenant. The spec sheet is `docs/spec.md`.
- **Phase 0 is [FOLIOHQ](https://github.com/ByYudkowskysTentacle/FOLIOHQ)** (`folio-chq-extract`), a working one-vendor tool for collectionHQ Academic. It is the reference implementation this platform generalizes. Its `docs/HANDOFF.md` has its own status and open questions.
- **FOLIOHQ status:**
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
- A final review found 14 bugs (each reproduced) and an operator walkthrough found 15 usability problems; all are fixed, with regression tests (`test_regressions.py`, `test_walkthrough.py`).
- `chq-academic` 1.0.0 writes byte-identical `.mrc` and `.psv` files to FOLIOHQ from the same data.
- 161 offline tests (`pytest`), `ruff` clean, JSON Schemas and commented examples, CI with a secret scan (not yet run on GitHub).

**Not done:** nothing has run against a real tenant or vendor server; the Windows scheduled task, keyring use under it, and the Teams card are untested outside mocks. FOLIOHQ remains the path to gate 1.

**Next**
1. Run the tests on GitHub (Linux and Windows) and the secret scan once the branch is pushed.
2. Try `init`, `run` and `release` against the public Eureka reference system (`diku`), with a local-folder delivery.
3. Decide the license before the first release.

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
| 2026-10-26 | A delivery to a new destination, or on a new recipe major version, waits for a person to release it | A test delivery to a local folder must not disarm the review of the first real upload |
| 2026-10-26 | Recipe minor and patch versions never change an output's bytes; any layout change is a new major | `^1.0` updates automatically, so a vendor must never see an unreviewed change |
| 2026-10-26 | A shared recipe sets only how files travel (transport, upload mode); hosts, folders and server keys are local | A recipe from another site must not choose where files go or which server to trust |

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
