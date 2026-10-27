# Improvement plan

_27 October 2026. Follows the design and function review in `docs/review-2026-10-27.md`; IDs refer to its findings. The "Fix now" group (27 findings) is done. This plans everything else._

Each batch is one or a few pull requests, each with regression tests. Whenever the builders change, byte parity with FOLIOHQ is re-checked.

## Batch 1: safety and unattended running

_Done on 27 October 2026 (branch `claude/batch-1`)._

The changes most likely to prevent a bad delivery, or one that silently never happens. About two days.

| # | Change | Findings | Done when |
| --- | --- | --- | --- |
| 1.1 | **Hold on any change to what the vendor receives.** Store a layout signature per output (format, file name, extension, delimiter, header, empty value, date format, columns as header and field, link, item field, split) and the recipe file's SHA-256 in the run report and the runs table. `hold_reason` also holds when the signature differs from the last delivery to that destination, and names what changed. | SPEC-1 | Changing a column, the date format or the delimiter in a feed file holds the next delivery with "columns changed: …". The same layout delivers as before. |
| 1.2 | **Notice when the hourly task stops.** Record each `run --due` start in history meta and show its age in `check`, `feeds` and the page, with a warning after two hours. Add an optional `alerts.heartbeat` URL (keyring or env) that each `run --due` calls, for any dead-man's-switch monitor. The guide notes that a Windows password change breaks a stored task. | SPEC-6 | A site whose task has not run for three hours shows a warning in all three places. The heartbeat is called once per pass and never logged. |
| 1.3 | **Install where only the operator can write.** The guide and `init` default to `%LOCALAPPDATA%\FOLIO-Exchange`. `check` warns when the site or install folder is writable by other Windows users. | SEC-3 | A fresh install following the guide has no other-user write access; `check` flags a folder that does. |
| 1.4 | **Small hardening.** Pin the guide's install to a release tag, with a constraints file. Add `folio.ca_bundle` (and the Windows certificate store) and warn when `verify_tls` is false. Remove `force` from the page's release endpoint. Validate `Content-Length` strictly and clamp every `limit`. Strip circulation notes and staff user ids from items as they are cached. | SEC-5, SEC-4, SEC-2, SEC-7, SEC-6 | Each has a test; the guide installs a tag. |

Also done in this batch: the page opens only through a one-time link that it swaps for a cookie (SEC-2's "next" step), webhook and heartbeat URLs must be https and are never redirected (SEC-4), and `--version` and the installer show the git commit (SEC-5).

Left on purpose, or still to do:
- **No release tag yet.** The guide and the installer still install `main`; `constraints.txt` pins the dependency versions, and CI pins its actions to commit SHAs. Tag a version, and point both at it, before a second library installs.
- **The page shows the hourly task's age only as a warning** (after two hours, or when it never ran); `check` and `feeds` also print the age when all is well.
- **`init`** still names `C:\FOLIO-Exchange\<tenant>` in its warning about synced folders; the guide now uses `%LOCALAPPDATA%\FOLIO-Exchange`.
- **`administrativeNotes`** on instances and holdings stay in the cache (optional in SEC-6).
- **Run reports** record the program version and the recipe's SHA-256, not the program's git commit.

## Batch 2: command line, page and the shared core under them

_Done on 27 October 2026 (branch `claude/batch-2`)._

Make daily use comfortable, and give the page the same operations as the command line without copying code. About three days.

| # | Change | Findings | Done when |
| --- | --- | --- | --- |
| 2.1 | **A small core for feed operations,** used by both the command line and the page: pause and resume that work on the file even when it does not load; `check`'s site warnings (alerts off, ratio 0, recipe update waiting, overdue) as data; the password note and the in-place key rewrite moved out of `cli.py`. | ARCH-5 | `ui.py` imports nothing private from `cli.py`; the page can pause a feed whose file is broken; the Connection view shows the same warnings as `check`. |
| 2.2 | **Command line.** Show paused feeds in `check` and `check --delivery`, marked as paused. Give a dry-run feed (transport none) its own next steps, and make `release` refuse it plainly. Print the scheduled-task command in a form that works in PowerShell as well as Command Prompt. Add a "from dry run to first delivery" and an "updating FOLIO-Exchange" section to the guide. | OPS-5, OPS-6, OPS-9, OPS-10 | Walking the guide from dry run to a held first delivery needs no other document. |
| 2.3 | **Page usability and accessibility.** Make History rows, both tab sets and the Run menu work by keyboard, with correct ARIA roles. Fix contrast on the Run button, recipe label and small badges (WCAG 2.2 AA). Refresh the sidebar and the open feed every 30 to 60 seconds, and when the tab becomes visible. Use the page's own wording instead of command lines, readable settings, and an Open folder button. Let narrow windows scroll as one page. | UI-6, UI-7, UI-8, UI-9, UI-10 | Keyboard-only review of a past run and release works; contrast checks pass; a scheduled run's result appears without a reload. |

## Batch 3: large catalogues

Measure first, then remove the full re-downloads that make a weekly run take hours. About three days.

| # | Change | Findings | Done when |
| --- | --- | --- | --- |
| 3.0 | **A benchmark.** A slow, opt-in test builds a synthetic cache of 300,000 and 2,000,000 instances and reports sync request counts, build time, memory and disk. | PERF (all) | Numbers recorded in `docs/notes.md` before and after this batch. |
| 3.1 | **Reconcile deletions by id range** instead of re-downloading a whole record type: compare FOLIO's count with the cache's over UUID ranges, narrow down, and re-fetch only the ranges that differ. The run report says why any full sync happened. Correct the guide's "later runs fetch only what changed". | PERF-1, PIPE-9 | Deleting one record re-fetches a few pages, not the catalogue and its MARC. |
| 3.2 | **Resume an interrupted full sync** from its last committed page (resume point, generation and start time in meta). | PERF-2 | A full sync cut off halfway resumes on the next run, not from the start. |
| 3.3 | **Smaller wins:**<br>- per-record-type refresh intervals (loans and MARC can wait longer);<br>- a retry window for missing MARC instead of retrying every sync;<br>- build progress in the log;<br>- `keep_local_runs: 2` by default, with a free-space check;<br>- a faster `from_srs`, a lighter loan-statistics table, and reading only the rows the page shows. | PERF-3, PERF-9, PERF-6, PERF-7, PERF-4, PERF-8, PERF-5 | Benchmark improves; byte parity holds. |

## Batch 4: tests, packaging and error types

Keeps the rest cheap to change. About one day.

| # | Change | Findings |
| --- | --- | --- |
| 4.1 | Test alert payloads (Slack text, Teams card) and SMTP (STARTTLS before login, port 465, no login without TLS). | TEST-5 |
| 4.2 | Faster test servers (short poll interval; currently half the suite's time). Share `no_sleep` and site helpers in `conftest.py` and `sitekit.py`. | TEST-2, TEST-7 |
| 4.3 | Make the mock FOLIO refuse queries it does not understand, and give it real-tenant record shapes. | TEST-6 |
| 4.4 | A CI job that builds the wheel, installs it in a clean environment and runs the tests. Declare the version in one place. | TEST-8 |
| 4.5 | Daylight-saving schedule tests in a real time zone, not the machine's. | TEST-9 |
| 4.6 | Exit codes carried by the error types, with a `JobBusy` error for the page. One definition of a run awaiting release, shared by pruning and `release`. | ARCH-7, ARCH-8 |

## Batch 5: before a second recipe (gate 2)

Decide these with the partner library when its vendor is chosen. Several change the recipe format, so they belong before more recipes and libraries depend on it.

| # | Change | Findings |
| --- | --- | --- |
| 5.1 | **Bib-level feeds.** Validate the effective feed (a bibs-only feed must not skip every bib). Build from instances alone when items are not read. Count what is delivered (bibs or items) for the ratio check, with `min_bib_ratio`. Block a delivery whose every file is empty when the last was not. Add an option to send only SRS MARC (no generated brief records), which an OCLC recipe would need. | ARCH-1, SPEC-2, SPEC-3 |
| 5.2 | **Neutral MARC defaults.** Strip an existing 949 only when a recipe asks; chq-academic already says so explicitly, so its bytes do not change. | SPEC-5 |
| 5.3 | **Recipe format.** Add a `requires` key naming the FOLIO-Exchange versions a recipe needs, and a `{local_date}` placeholder. Decide the output row grain (instance, holdings, item, aggregate) and the bib-level fields. Decide changed-records and deletes outputs. | SPEC-9, SPEC-8, SPEC-7 |
| 5.4 | **A fixture kit for recipes:** golden files per recipe, run by one parametrized test, starting with the second recipe. | SPEC-10 |

## Also: a second review round

_Done on 27 October 2026: `docs/review-2026-10-27-round-2.md` (19 verified findings in four groups, none high)._

The review's completeness check named four areas that the first round covered thinly. A focused round on these should run before or alongside batch 3:
- field values in the output files (`fields.py`, `dates.py`, the delimited writer, MARC built from instances);
- unattended scheduled running on a Windows PC, end to end;
- living with the tool over time: upgrades, stored state, recipe versions, renamed feeds;
- feed settings as a policy boundary (`recipe.py`, the schemas).

## Order and dependencies

- **Batch 1 first:** it guards deliveries and the unattended schedule.
- **Batch 2:** 2.1 comes before the page work in 2.3, which builds on it.
- **Batch 3** starts with the benchmark (3.0), so every change there is measured.
- **Batch 4** can run alongside any other batch.
- **Batch 5** waits for the partner library's choice of a second vendor, except 5.2 and the `requires` key in 5.3, which are cheap and safe now.
- **FOLIOHQ** stays the path to gate 1 throughout.
