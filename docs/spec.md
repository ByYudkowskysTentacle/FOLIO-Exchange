# FOLIO Integration Platform – Spec Sheet

_Snapshot of the working spec as of 26 Oct 2026. The live, editable copy is a Claude Docs document (see `CLAUDE.md`); when the two differ, the live copy wins until this file is refreshed._

An open-source platform that lets library staff set up, test and run scheduled data exchanges between FOLIO and outside platforms – collectionHQ first – from tested recipes, without writing code. It generalizes what folio-chq-extract already does for one vendor, and keeps its safeguards.

## Problem and goals

v1 succeeds when a library can stand up a vendor feed from a shared recipe in under a day, with no code, and leave it running unattended.

**Why this is hard today**

- Each vendor feed is a one-off script, written by whoever was on staff and fragile across FOLIO upgrades (the Okapi to Eureka login change broke many).
- Vendor specs arrive as sample files and emails, not schemas. Mismatches surface late: cHQ links items to bibs on the instance UUID in 999 ff $i, not on 001, which its sample showed but no spec said.
- FOLIO's own tools export on demand, but none builds a vendor's exact layout on a schedule, checks it and delivers it.
- Most libraries have no developer. The people who know the data cannot maintain Python.

**v1 goals**

1. **Recipes:** one shared, versioned definition per vendor feed (fields, format, schedule, delivery) that any library reuses and adjusts.
2. **Guided setup:** connect to FOLIO, pick a recipe, preview real output, test the vendor login and schedule it, from a guided command line in v1 and a browser from phase 2.
3. **Unattended runs:** incremental sync, safety checks, run history and failure alerts.
4. **Safe by default:** read-only FOLIO account, no patron data unless a recipe needs it and an admin approves, secrets never in files.
5. **Community upkeep:** libraries on the same vendor share and validate one recipe.

**Measures:** a new library on an existing recipe is live in 1 day or less. A second vendor's recipe ships with no core code changes. A failed run alerts within 15 minutes.

**FOLIO platform:** Eureka first. Okapi-based FOLIO keeps working while the same code serves both, but gets no Okapi-only fixes, features or tests.

**Not in v1:** writing into FOLIO (inbound loads), replacing Data Import, Data Export or the reporting database, real-time event streams, a hosted service, and general-purpose ETL.

## At a glance

![Every feed runs one pipeline; only the recipe changes. The data path runs FOLIO on Eureka, connector and sync, local cache, recipe engine, checks, delivery, vendor platform. Beside it, the scheduler starts runs, the web UI (phase 2) edits the recipe library, the recipe library shapes the engine, secrets serve sync and delivery, and delivery logs to run history and audit.](images/architecture.png)

Data moves down the left column on every run. The right column starts, shapes and records each run, and only the recipe differs from one vendor feed to the next.

## Who uses it

Four roles inside the library and one outside it; at a small library one person holds most of them.

| Role | Typical person | Does | Needs from the platform |
| --- | --- | --- | --- |
| Integration admin | Systems librarian or campus IT | Installs it, connects FOLIO, stores secrets, approves recipes that touch patron data | Install wizard, approval gates, audit log |
| Operator | The systems librarian in v1; collections or circulation staff once the web UI ships | Picks a recipe, adjusts filters and columns, previews output, sets the schedule | A guided command line and instant preview in v1; screens and a field picker in phase 2 |
| Recipe author | A library in the community, sometimes with the vendor | Turns a vendor's spec into a tested recipe | Recipe format, validator that checks output against vendor samples, mock FOLIO test kit |
| Watcher | Anyone on the alert list | Gets success and failure notices, reads run history | Email, Teams or Slack alerts; a run history page |
| Vendor contact (outside) | The vendor's implementation team | Receives files, confirms the layout | A sample file and a one-page summary of the layout, generated from the recipe |

## Integration catalogue

Of nine candidate feeds, the platform should own the five outbound file feeds FOLIO does not schedule or deliver. Two already run natively in FOLIO and stay there, and two inbound loads wait for v2. After cHQ, the outbound feeds are unranked candidates.

| Platform | What flows | Direction | Usual format and route | What FOLIO already does | Status |
| --- | --- | --- | --- | --- | --- |
| collectionHQ Academic | Bibs, items, loan and renewal counts | Out | MARC plus pipe-delimited items, SFTP | Nothing built in | First – recipe exists (folio-chq-extract) |
| Other collection-analysis tools (e.g. GreenGlass) | Bibs, items, circulation | Out | MARC plus CSV, SFTP | Data Export builds MARC; no schedule or delivery documented | Candidate – same shape as cHQ |
| OCLC WorldCat holdings (Data Sync) | Bibs with holdings | Out | MARC, SFTP | Data Export builds MARC; no schedule or delivery documented | Candidate |
| Discovery layers that need pushed files (EDS, Primo, Summon) | Full and changed MARC, plus deletes | Out | MARC, SFTP | OAI-PMH feed with `marc21_withholdings` for systems that harvest | Candidate – only where the vendor cannot harvest |
| Usage dashboards | Circulation counts by location and period | Out | CSV, SFTP or API | Lists app exports on demand | Candidate |
| Book vendors (GOBI and others) | Orders | Out | EDI over FTP or SFTP | Yes: Organizations integrations, scheduled hourly to monthly | Out of scope |
| Bursar or campus ERP | Fees and fines | Out | Text file in a layout you define | Yes: Settings > Users > Transfer criteria, scheduled | Out of scope |
| Student information system | Patron records | In | CSV or JSON into user import | Partly: a user-import API, no screens | v2 – writes to FOLIO and carries patron data |
| E-book vendor record loads | Bibs | In | MARC from a vendor portal or FTP | Data Import, started by hand | v2 – fetch the file, then hand it to Data Import |

FOLIO features checked in its documentation: [Organizations integrations](https://docs.folio.org/docs/acquisitions/organizations/), [Transfer criteria](https://docs.folio.org/docs/settings/settings_users/settings_users/), [OAI-PMH](https://docs.folio.org/docs/settings/settings_oaipmh/settings_oaipmh/), [Data export](https://docs.folio.org/docs/settings/settings_data_export/settings_data_export/). The user-import API and the vendor examples are from general knowledge, not checked.

## Functional requirements

Nineteen requirements across seven stages; 15 are Must for v1. Each Why cites what folio-chq-extract taught us.

| ID | Requirement | Priority | Why |
| --- | --- | --- | --- |
| CON-1 | Connect to Eureka FOLIO through its Kong gateway: token login with expiry, refresh, retries with backoff. Okapi works only while the same code covers it | Must | Eureka is where FOLIO is headed; today's login code already passed on both public reference systems |
| CON-2 | Test connection checks every endpoint and Eureka capability a recipe needs, before the first run | Must | `check` catches missing access before a long sync |
| SYN-1 | Incremental sync into a local cache: keyset paging, deletion detection, scheduled full refresh | Must | Offset paging degrades on large tenants; deltas keep weekly runs short |
| SYN-2 | ECS consortium support, including shared records | Should | Member tenants need shared MARC from the central tenant |
| MAP-1 | One field registry: every output field defined once, with a plain description | Must | cHQ needed five new fields; each was one entry |
| MAP-2 | Column settings (headers, order, date format, empty-value text, delimiter) edited in local settings with instant preview; a visual editor arrives with the web UI | Must | cHQ's layout differed from ours on all five |
| MAP-3 | Output builders for MARC 21 (ISO 2709, size limits handled) and delimited text | Must | Records over 99,999 bytes and fields over 9,999 bytes occur |
| MAP-4 | Link identifiers set per vendor (999 ff $i, 001, a 9XX barcode) and enforced on every record | Must | cHQ links on 999 ff $i; we had assumed 001 |
| MAP-5 | More builders: MARCXML, JSON, KBART | Could | Asked for by discovery and e-resource vendors |
| VAL-1 | Preview: a sample of the first N bibs and items from live data in under a minute | Must | Staff and vendors approve a layout from real records, not a spec |
| VAL-2 | Conformance check against the vendor's sample files: headers, column count, delimiter, binary MARC | Should | cHQ's sample bib had been converted to text and would not parse |
| VAL-3 | Safety check blocks delivery when item counts drop sharply against the last delivery | Must | Stops a half-empty file from replacing good data at the vendor |
| DEL-1 | SFTP with a pinned host key and a password or key | Must | Most vendors, cHQ included, take SFTP |
| DEL-2 | Upload modes for restricted accounts: staged (.part then rename) or direct | Must | cHQ's account is upload-only |
| DEL-3 | More transports: FTPS, S3, HTTPS upload, email for small files | Should | Vendors differ; plain FTP is never offered |
| RUN-1 | Scheduler with a cadence per recipe, retries and run history (counts, files, duration, errors) | Must | Unattended runs need a record staff can read |
| RUN-2 | Alerts by email, Teams or Slack, on failure or on every run | Must | A silent failure is found weeks later by the vendor |
| REC-1 | Recipe library: versioned, shared recipes with a changelog; local overrides kept apart so upgrades never overwrite them | Must | Every cHQ library would otherwise copy and drift |
| REC-2 | A recipe is one YAML file that can be imported, exported and reviewed in Git | Must | Reviewable, diffable, works without the UI |

## What an integration recipe holds

A recipe is two files: a shared one that describes the vendor's layout, and a local one that holds this library's settings. Upgrading the shared recipe never touches local settings, and the local file never leaves the library.

**Shared recipe** (in the community library, versioned):

```yaml
recipe: chq-academic
version: 1.2.0            # minor = new optional column; major = layout the vendor must re-accept
vendor: collectionHQ
title: collectionHQ Academic – bibs and items
spec_source: cHQ implementation team, Oct 2026 (Library of Michigan layout)
reads: [instances, holdings, items, loans, marc]   # the platform refuses user/patron endpoints here
patron_data: none
link: {bib_field: "999 ff $i", item_column: "Bib record ID", value: instance_id}
outputs:
  - name: bibs
    format: marc21
    embed_items: false
  - name: items
    format: delimited
    delimiter: "|"
    extension: psv
    empty_value: "None"
    date_format: "%Y-%m-%d %H:%M:%S"
    columns:
      - {header: Bib record ID, field: instance_id, required: true}
      - {header: Barcode, field: barcode}
      - {header: Location, field: location_name_code}
      # … the other 17 columns
checks:
  min_item_ratio: 0.9
  must_be_filled: [instance_id, status, material_type]
delivery: {transport: sftp, upload_mode: direct}
default_schedule: weekly
```

**Local settings** (this library only, edited in the UI):

```yaml
uses: chq-academic@^1.2   # take minor updates automatically, hold major ones for review
filters: {exclude_location_codes: [ONLINE]}
legacy: {circ_count_note_type: Legacy circ count}
delivery: {host: sftp.us.collectionhq.com, remote_dir: "(ask cHQ)", secret: keyring}
schedule: Sun 02:15
```

## Safety, privacy and security

No patron data leaves by default: a recipe must declare it, an admin must approve it, and every run that sends it is logged. Recipes are declarative and contain no code, so a shared recipe cannot run anything on a library's server.

| Area | Rule |
| --- | --- |
| Patron data | A recipe declares `patron_data` and lists the exact fields. Anything but `none` needs admin approval and is limited to that list. Loans are read only for item, dates and counts. |
| FOLIO account | A read-only service account, given access through a Eureka authorization role. The connection test lists the exact capabilities the enabled recipes need, and no more. Writes (v2) need a separate account and a second approver. |
| Secrets | Never in recipes, settings files or logs. The OS keyring on a desktop; a secrets manager or environment variables on servers and in containers. Screens show where a secret comes from, never its value. |
| Vendor servers | Host keys pinned and confirmed with the vendor; plain FTP refused; unknown keys never auto-accepted. |
| Bad data at the vendor | Count and required-field checks; a preview approved before the first delivery; the first run held until a person releases it. |
| Audit | Who changed which recipe setting, and what each run sent: where, file names, hashes and counts. Kept 12 months by default. |
| Releases and recipes | Signed releases with pinned dependencies. Recipes reviewed before they join the shared library. Secret scanning in CI, and credential-looking files ignored by default. |

## Non-functional requirements

It runs on one modest machine inside the library's network and handles a collection of 1 million items overnight.

| Area | Target |
| --- | --- |
| Scale | First full sync of 1 million items in 6 hours or less on a typical hosted tenant; a weekly incremental run in 15 minutes or less. The build step already does about 100,000 bibs and 200,000 items in 30 seconds. |
| FOLIO versions | Eureka: the current flower release and the one before (Trillium and Sunflower as of Oct 2026). Okapi-based FOLIO keeps working while the same code serves both, with no Okapi-only fixes or tests. |
| Deployment | One Docker image; a Windows installer for single-PC libraries; a Linux service. It runs where FOLIO accepts its API traffic, usually inside the library network. |
| Footprint | 2 GB RAM and 10 GB disk for 1 million items (target, to be measured). |
| Resilience | Batch, not always-on: a missed run catches up at the next start, and an interrupted sync forces a clean full sync, never a partial file. |
| Accessibility | The phase 2 web UI meets WCAG 2.2 AA and works by keyboard alone. |
| Languages | English first; every UI string translatable. |
| Testing | Every recipe ships a fixture test against a mock FOLIO. CI runs the recipes weekly against FOLIO's public Eureka reference systems; an Okapi run, while one exists, only warns. |

## Roadmap

![cHQ goes live first; a second vendor proves the design before the UI. Phases: 0 Ship cHQ (now), 1 Generalize, 2 Guided setup, 3 Community, 4 Inbound (v2). Gates between them: first cHQ delivery accepted by cHQ; second recipe with no core code change; a non-developer sets up a feed unaided; three libraries in production.](images/roadmap.png)

No phase starts until the gate before it is met. Dates wait on the first four open questions below.

## Open questions

The four scope questions were settled on 26 Oct 2026; the rest can wait for the first partner library.

- [x] **Operator:** a systems librarian. v1 ships as a guided command line with recipes; the web UI follows in phase 2.
- [x] **Where it runs:** on each library's own PC or server, inside its network.
- [x] **Next feeds:** every outbound candidate stays open, unranked. Phase 1's second recipe is whichever a partner library needs first.
- [x] **Writing into FOLIO:** in v2, behind a separate account and a second approver.
- [x] **Your tenant:** on Eureka, so phase 0 ships and is tested on Eureka.
- [ ] **Patron data:** does any feed you need carry it, and under what agreement?
- [x] **Home:** a new repo, FOLIO-Exchange; FOLIOHQ stays as phase 0. Proposing it to the FOLIO community (Reporting SIG) is still open.
- [ ] **License:** keep MIT, or move to Apache-2.0, which FOLIO's own modules use?
- [ ] **Name:** FOLIO-Exchange for now. Before announcing it, check FOLIO's trademark guidance so the name does not imply the FOLIO project endorses it.
- [ ] **Upkeep:** whose staff time or funding maintains it, and which partner libraries test recipes?
