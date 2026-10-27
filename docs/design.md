# Design: how v1 is built

_Started 26 Oct 2026; revised 26 Oct 2026 after a three-lens design review (spec coverage, safety and correctness, operator UX and FOLIOHQ parity), and on 27 Oct 2026 for the fixes from the second review round (`docs/review-2026-10-27-round-2.md`). This is the build plan behind `docs/spec.md`: files, modules and their contracts. When code and this file disagree, fix one of them in the same change._

## Shape

FOLIO-Exchange v1 is a Python package and a guided command line, `folio-exchange`, that a systems librarian installs on one PC or server inside the library's network. It generalizes FOLIOHQ (`folio-chq-extract`): the same pipeline – sync, build, check, deliver – but every vendor-specific choice moves out of code into a **recipe**.

```
FOLIO (Eureka, Kong) ──> client ──> sync ──> cache (SQLite) ──> build ──> checks ──> hold? ──> delivery ──> vendor
                          ▲ read-only, no         shared by        ▲ recipe +    ▲ counts,    ▲ first to a   ▲ SFTP (pinned key),
                          │ patron paths, no      all feeds        │ local       │ required   │ destination  │ local folder
                          │ redirects                              │ settings    │ fields     │ or major     │
                       secrets (env, keyring)                      │                          └─ release     └─> history, audit, alerts
                                                      recipe library (versioned YAML)
```

A run is a batch job. The OS scheduler (Task Scheduler, cron or a systemd timer) starts `folio-exchange run --due` every hour; feeds whose slot has passed run, and missed slots catch up once at the next start. Nothing stays resident.

**Done means byte parity.** With the `chq-academic` recipe and FOLIOHQ's `config.example.yaml` layout (now `legacy/config.example.yaml`), FOLIO-Exchange writes byte-identical `.mrc` and `.psv` files to FOLIOHQ from the same data (checked on 26 Oct 2026 against the shared mock FOLIO). Since 27 Oct 2026 FOLIOHQ's CI checks it on every change (`tests/test_parity.py`, against its `legacy/` tool).

## What a library keeps: the site folder

Everything for one FOLIO tenant lives in one folder, the **site**. Commands find it from `-s/--site`, else `FOLIO_EXCHANGE_SITE`, else the current directory. It must contain `site.yaml`, except for the commands that set a site up: `ui`, `launcher`, `schedule --register` and `run --due` accept a folder named with `--site` or `FOLIO_EXCHANGE_SITE` that has no `site.yaml` yet (never the current directory, so a mistyped command cannot start a site anywhere). A folder that still holds `feeds/` or `data/history.sqlite` is an existing site whose `site.yaml` has gone missing, and stays an error; a new site folder is only created inside a folder that exists. The wizard warns against cloud-synced folders (OneDrive, Dropbox), where SQLite and the lock break, and suggests a plain local folder that only that Windows user can change, such as `%LOCALAPPDATA%\FOLIO-Exchange\<tenant>`.

```
site.yaml                    FOLIO connection, folders, sync, alerts. No secrets.
feeds/<name>.yaml            one feed each: local settings over a shared recipe
recipes/<id>/<version>.yaml  optional local or community recipes (searched before the bundled library)
recipes/sources.json         where downloaded recipe versions came from (source, repository, commit, SHA-256)
keys/<feed>.key              an SFTP private key saved from the page; only this Windows user can read it
data/cache.sqlite            local cache, shared by every feed
data/history.sqlite          run history, audit log, and small bookkeeping (meta)
data/run.lock                held while a run is in progress
data/last-pass.json          when `run --due` last started (the file's time) and how it ended ({error, alerted})
out/<feed>/<YYYYMMDD-HHMMSS>/   output files plus run-report.json, one folder per run
out/<feed>/preview-<...>/       PREVIEW_ files from `preview`; never delivered
logs/folio-exchange.log
```

`site.yaml` with every default (paths are relative to the site folder; `examples/site.yaml` is the same, with comments):

```yaml
folio:
  base_url: https://folio-kong.example.edu   # the Kong gateway URL on Eureka (or Okapi URL). https only,
                                             # except http://localhost or 127.0.0.1 for testing
  tenant: example
  username: fx_reader                        # a read-only service account
  secret: keyring                            # keyring | env:NAME. FOLIO_PASSWORD always wins.
  page_size: 500
  srs_batch_size: 200
  timeout_seconds: 120
  max_retries: 5
  verify_tls: true                           # false is warned about everywhere; use ca_bundle instead
  ca_bundle: null                            # a PEM file (relative to the site folder) or windows (the
                                             # Windows certificate store), for campus networks that inspect TLS
  srs_include_shared: false                  # true for ECS member tenants (SYN-2)
sync:
  mode: auto                                 # auto | full | delta
  full_refresh_days: 30
paths: {data: data, out: out, logs: logs, feeds: feeds, recipes: [recipes]}
runs:
  keep_local_runs: 5                         # run folders kept per feed; held and blocked runs and the last
                                             # delivered run are never pruned
  history_months: 12                         # audit and history retention (each feed's last delivery is kept)
  max_attempts_per_slot: 3                   # scheduled retries of a failed run within one slot
  timezone: null                             # schedules use this zone; null = this computer's local time
alerts:
  on: failure                                # failure | always | never
  webhook: null                              # keyring | env:NAME – the webhook URL is itself a secret
  webhook_format: slack                      # slack ({"text"}) | teams (Adaptive Card, for a Teams workflow)
  email: null                                # or {to: [..], from: .., smtp_host: .., smtp_port: 587,
                                             #     starttls: true, username: null, secret: keyring}
  heartbeat: null                            # keyring | env:NAME – a monitor's ping URL, requested after
                                             # every run --due pass (the URL is a secret)
```

`${NAME}` and `${NAME:-default}` in any string are replaced from the environment (FOLIOHQ behaviour), and `FOLIO_URL`, `FOLIO_TENANT` and `FOLIO_USERNAME` override the `folio` block, so containers need no file edits.

**YAML.** Every file is read with only `true` and `false` as booleans (YAML 1.2 style), so a setting named `on` and codes such as `NO`, `ON` or `Y` stay text. Numbers that look like codes (`0123`) must be quoted; validation says so.

## Secrets

Secrets never appear in `site.yaml`, feed files, recipes, logs, reports or alerts. A file that contains a key named `password`, `passphrase`, `private_key_passphrase`, `token`, `api_key`, `webhook_url` or `heartbeat_url` is rejected outright, whatever its value. Wherever a secret is needed, the file holds a **secret reference**:

| Reference | Meaning |
| --- | --- |
| `keyring` | the OS credential store (Windows Credential Manager, macOS Keychain, Secret Service) |
| `env:NAME` | the environment variable `NAME` |

| Secret | Keyring service | Keyring account | Environment override |
| --- | --- | --- | --- |
| FOLIO password | `folio-exchange/folio` | `<username>@<tenant>` | `FOLIO_PASSWORD` (always checked first) |
| Vendor SFTP password or key passphrase | `folio-exchange/sftp` | `<username>@<host>` | the feed's `secret: env:NAME` |
| SMTP password | `folio-exchange/smtp` | `<username>@<smtp_host>` | `secret: env:NAME` |
| Alert webhook URL | `folio-exchange/webhook` | `<site folder name>` | `webhook: env:NAME` |
| Heartbeat URL | `folio-exchange/heartbeat` | `<site folder name>` | `heartbeat: env:NAME` |

**From the page.** The page takes the FOLIO password (stored only after FOLIO accepts it), vendor SFTP passwords and SFTP private keys, in write-only fields. Passwords and passphrases go straight to the keyring, exactly as `secret set` stores them, and are never written, logged or sent back; the audit log records that one was stored.

**Private keys are the one secret kept in a file.** An RSA-4096 private key is about 3.2 KB, and Windows Credential Manager holds at most 2,560 bytes, so the page saves the key as `keys/<feed>.key` in the site folder, readable only by this Windows user (the `keys` folder first gets `icacls /inheritance:r /grant:r <user>:(OI)(CI)F`, so nothing written there is ever readable by others, and the key is written to a new temporary file of its own; mode 700/600 elsewhere), sets `private_key: keys/<feed>.key` in the feed file and keeps any passphrase in the keyring. The key is read before it is kept (OpenSSH or PEM; Ed25519, ECDSA or RSA; a PuTTY `.ppk` file is explained, not read). Switching the feed back to a password, or its delivery away from SFTP, removes that copy. The feed file is saved (and checked) before the keyring is touched or the key put in place, so a refused save leaves the stored secrets as they were. Credential Manager keeps one secret per SFTP account (`user@host`): the password, or a key's passphrase. Two feeds on one account must therefore log in the same way, and the page refuses the other method.

`folio-exchange secret set folio|smtp|webhook|heartbeat|<feed>` prompts with `getpass` and stores the value in the keyring; nothing is echoed or logged, and the audit log records that a secret was stored (never its value). When a webhook or heartbeat URL is stored but `site.yaml` does not name it yet, `secret set` says to add `<target>: keyring` under `alerts`. These two URLs are stored under the site folder's name, so renaming or copying the folder loses them: `check` and the page then warn (`alert-secret-missing`, for each of `alerts.webhook` and `alerts.heartbeat` whose URL `credentials.resolve` cannot find) until they are stored again. `secret status` says where each comes from. Feed names `folio`, `smtp`, `webhook`, `heartbeat` and `all` are reserved so `secret set` is never ambiguous. Tests block the real keyring (autouse fixture in `tests/conftest.py`).

**Keyrings and schedulers.** Keyring entries belong to the operating-system user who stored them. On Windows the hourly task must run as that user, "whether logged on or not", with the user's password given to Task Scheduler (`schedule --register` registers it so, and schtasks asks for the password; see `schedule.py`). After a change of Windows password the stored task stops starting until it is registered again (`schedule --register`); `check` warns when it has stopped (see Schedules). Under cron or systemd on Linux there is often no Secret Service session: use `env:NAME` references and an environment file there. `secret status` names the current user to make this visible.

**TLS.** The FOLIO base URL must be `https://` (plain `http://` only for localhost testing). FOLIO's certificate is checked against the usual public certificates or, with `folio.ca_bundle`, against a PEM file instead (for a campus CA or a TLS-inspecting proxy) or the Windows certificate store (`windows`, through the `truststore` package). An untrusted certificate stops the run at once with a message naming `ca_bundle` and saying not to switch off `verify_tls`. `verify_tls: false` still works, but `check` and the page warn (`tls-off`), `init` prints a warning before it connects, and `run-report.json` records `tls_unchecked: true`. SMTP logins need STARTTLS or port 465 (SMTP over TLS); the password is never sent in clear. The client never follows redirects (they could carry the token or a re-sent login to another host): a 3xx is an error naming the address to use instead. Webhook and heartbeat URLs must be `https://` (plain `http://` only to localhost, 127.0.0.1 or ::1), and their redirects are never followed: a 3xx counts as a failure.

## Recipes

A recipe is two files (spec, "What an integration recipe holds"): a shared one, versioned and reviewed in Git, and the library's local feed file. Upgrading the shared recipe never touches local settings.

### The recipe library

Shared recipes live at `<library dir>/<recipe id>/<version>.yaml`, one file per published version, so a feed can stay on 1.x while 2.0 waits for review. The bundled library is `src/folio_exchange/recipes/` and never removes a published version. A site searches `paths.recipes` first, then the bundled library; on an identical id and version, the earlier folder wins. When the copy used differs from the one it hides (line endings and a byte-order mark aside), `check`, the page and `recipes list` warn (`recipe-shadow`): feeds then send what the local copy says, and a changed layout is held anyway (see the hold below). A recipe folder may also hold `README.md` (notes for recipe authors) and `samples/<output name>.<ext>` (the vendor's sample files, used by the conformance check). A bundled recipe may have its source in another repository: `chq-academic` is a copy of FOLIOHQ's `recipes/chq-academic/`, which is changed first and copied here unchanged.

`uses: <id>@<range>` picks the highest version that matches the range:

| Range | Matches |
| --- | --- |
| `1.2.0` | exactly 1.2.0 |
| `^1.2` or `^1.2.0` | ≥ 1.2.0 and < 2.0.0 (minor updates automatically; majors held for review) |
| `^0.3` | ≥ 0.3.0 and < 0.4.0 |
| `~1.2` | ≥ 1.2.0 and < 1.3.0 |

A bare id with no range is an error: the operator must choose. When a newer version exists than the feed's range allows, `check` and `feeds` say so; the feed keeps running on the version it matches until `folio-exchange upgrade FEED` moves it.

**Vendor sources.** Recipes can also be downloaded from vendors' GitHub repositories, from a curated list that ships with FOLIO-Exchange (`src/folio_exchange/recipe_sources.yaml`; vendors are added by pull request) plus any a site adds under `recipe_sources:` in `site.yaml` (same keys: `id`, `vendor`, `title`, `description`, `repo` as owner/name, `branch` (main), `path` (recipes), `recipes` – the ids it may provide – and `homepage`). Nothing else is ever downloaded. Today the list holds FOLIOHQ (`chq-academic`). `sources.check` reads the branch's current commit and every `<path>/<id>/<x.y.z>.yaml` file there through the GitHub API, loads each as a recipe (so a recipe that reads patron data is refused like any other), and compares it with the library: *new*, *installed* (the same bytes) or refused (*conflict*: different bytes under a version already installed, because a published version never changes; *invalid*; or an id the source may not provide). A new minor or patch version that existing feeds would take from their next run is also refused when anything but its wording (`version`, `title`, `description`, `changelog`, `spec_source`) differs from the version they use now: the versioning rule below, enforced, because such a change would otherwise reach the vendor without the review and held delivery a new major gets. `default_schedule` is substance, not wording: a feed file without a `schedule:` of its own follows it. It installs nothing. `sources.install` then takes exactly the commit that was reviewed and writes only the new versions into the site's first `paths.recipes` folder, records the source, repository, commit and SHA-256 of each file in `recipes/sources.json`, and audits it. A new minor version is used by every feed whose range allows it from that feed's next run (the review lists those feeds); a new major waits for `upgrade`, as with any recipe. Only `api.github.com` and `raw.githubusercontent.com` are contacted, over https; redirects are followed by hand and only within those hosts. Downloads go only into the site's own recipes folder, never the program's bundled one, with 30-second timeouts, 256 KB per file and 100 files per source.

**Hand-placed versions.** A recipe version put in the site's recipes folder by hand (not recorded in `recipes/sources.json`, or changed since it was downloaded) is not refused, but when a feed uses it and it changes more than wording against the version that feed would use otherwise (the highest lower version its range allows), `check`, `feeds` and the page warn (`recipe-local-substance`, `sources.local_minor_warning`): as a minor version it is used from the next run, without the review and held first delivery a new major gets. The warning says to make it a new major and `upgrade`, or delete it. The settings audit records the file as added (see Settings audit).

**Versioning rule for recipe authors.** A minor or patch version must not change the bytes of any existing output: fixes to descriptions, validation, defaults that only apply to new feeds (not `default_schedule`, which feeds without their own schedule follow). A minor version from a vendor source is held to the stricter rule above: only its wording may change. Anything a vendor would notice – a column added, removed, renamed or moved, a delimiter, empty value or date format, the link – is a new **major** version, because the vendor must accept it again and the first delivery on it is held. A change that breaks this rule, or a local edit to a feed's columns or formats, is held too, because the hold compares the delivered layout itself.

### Shared recipe format

```yaml
recipe: chq-academic             # id: lowercase letters, digits and hyphens; matches its folder name
version: 1.0.0                   # MAJOR.MINOR.PATCH; matches the file name
vendor: collectionHQ
title: collectionHQ Academic – bibs and items
description: >                   # optional, plain language
spec_source: cHQ implementation team, Oct 2026 (Library of Michigan layout)   # optional
changelog:                       # REC-1: one entry per version, newest first; must include this version
  - {version: 1.0.0, date: 2026-10-26, changes: First recipe, ported from folio-chq-extract.}
reads: [instances, holdings, items, loans, marc]   # names from the reads registry; nothing else is fetched
patron_data: none                # v1 accepts only `none`; anything else is refused
link: {bib_field: "999 ff $i", item_column: "Bib record ID", value: instance_id}   # MAP-4
file_prefix: chq                 # default for {prefix} in file names
selection:                       # which records go out; a feed's `filters` override any of these
  include_suppressed_instances: false
  include_suppressed_items: false
  include_suppressed_holdings: true   # items under a suppressed holdings record (true: as FOLIOHQ)
  include_items_without_barcode: true
  bibs_without_items: false
  exclude_item_statuses: []
  include_location_codes: []     # empty = every location
  exclude_location_codes: []
options:                         # a feed's `options` override any of these
  last_circ_date_from: checkout  # checkout | any
  loan_window_months: 12
  timezone: UTC                  # zone for dates written to files, and for reading legacy note dates
outputs:                         # one or more; names are unique
  - name: bibs
    format: marc21               # ISO 2709, UTF-8 (MAP-3)
    file_name: "{prefix}_bibs_{date}"   # .mrc is added; split files get _001, _002 ...
    split_every: 0               # 0 = one file
    embed_items: false
    date_format: "%Y-%m-%d"      # for date fields in embedded item subfields
    item_field:                  # stripped from source records when strip_existing, embedded or not
      tag: "949"
      strip_existing: true
      max_per_record: 500
      subfields: {a: barcode, b: item_hrid, c: call_number, l: location_code, t: material_type, s: status}
  - name: items
    format: delimited
    file_name: "{prefix}_items_{date}"
    extension: psv               # letters and digits; never mrc
    delimiter: "|"
    header: true
    empty_value: "None"
    date_format: "%Y-%m-%d %H:%M:%S"
    columns:
      - {header: Bib record ID, field: instance_id, required: true}   # required = a feed cannot remove it
      - {header: Barcode, field: barcode}
checks:
  min_item_ratio: 0.9            # VAL-3: block if items fall below this share of the last delivery; 0 = off
  must_be_filled: [instance_id, status, material_type]   # block if any delivered row has one of these empty
delivery:                        # a shared recipe may set only these two; where files go is local
  transport: sftp                # sftp | local | none. Plain FTP is refused.
  upload_mode: direct            # staged (.part, then rename) | direct
default_schedule: weekly         # used when the feed sets no schedule; else manual
```

`{date}` is the run's UTC date as `YYYYMMDD`; `{prefix}` is the feed's `file_prefix`, else the recipe's, else the recipe id. Only those two placeholders exist.

**Output bytes** (pinned so the FOLIOHQ parity holds):
- Delimited files are UTF-8 without a BOM, `\r\n` line ends, Python `csv` minimal quoting with `"` doubled: a value is quoted only when it holds the delimiter, a quote or a line break. The header row comes first. Only empty values (`None` or `""` after date formatting) become `empty_value`, so `0` stays `0`. Rows follow `ORDER BY instance, holdings, item` and are written when their bib is, including when the MARC record is dropped as oversize.
- MARC records are ISO 2709 with leader position 9 `a`. Every record gets 001 = instance HRID and 999 ff $i = instance UUID (FOLIO's own convention, so both link options always hold). Fields over 9,999 bytes are shortened; embedded items are shed until a record fits 99,999 bytes; a record that still does not fit is skipped and its items stay in the delimited file. An empty `.mrc` is written when there are no records.

**Validation** (plain-language messages that name the file and the path, e.g. `feeds/chq.yaml: outputs.items.columns[3].field: unknown field 'barcod' (did you mean 'barcode'?)`), checked on every load:

- Unknown keys are errors, at every level, in recipes and feed files alike.
- Every `field` (columns, item subfields, `link.value`, `must_be_filled`) exists in the field registry, and its `needs` are within `reads`.
- `reads` names exist in the reads registry. `patron_data` is `none`.
- `link`: `bib_field` is `"999 ff $i"` (value `instance_id`) or `"001"` (value `instance_hrid`); `item_column` is the header of a column in a delimited output whose field equals `value`. Required when a recipe has both a `marc21` and a `delimited` output.
- At most one `marc21` output in v1. Delimiter is one character. Extension is alphanumeric and not `mrc`. Headers are unique within an output, and every output writes its own file name. Item field tag is a 3-digit data field tag (010–999). `date_format` is a valid `strftime` pattern that renders a sample date. File names use only letters, digits, `.`, `-`, `_` and the two placeholders.
- `delivery.transport` is `sftp`, `local` or `none` (`ftp` gets its own message: plain FTP is never offered). `upload_mode` is `staged` or `direct`.
- `must_be_filled: [barcode]` with `include_items_without_barcode: true` is refused (every run would be blocked).
- Lists of codes and statuses must be text (a hint says to quote them).
- `version` matches the file name, `recipe` matches the folder, and `changelog` has an entry for `version`.
- The JSON Schemas in `src/folio_exchange/schemas/` describe the same rules for editors; `tests/test_schemas.py` keeps their keys in step with the code and validates the bundled recipes and `examples/` against them.

### The feed file (local settings)

`feeds/<name>.yaml`; the feed's name is the file name without `.yaml` (lowercase letters, digits, hyphens; not `folio`, `smtp`, `webhook`, `heartbeat` or `all`). `examples/feeds/chq.yaml` is a commented example.

```yaml
uses: chq-academic@^1.0          # required
title: collectionHQ (Main Library)   # optional; defaults to the recipe title
enabled: true                    # `feeds pause|resume FEED` sets it
file_prefix: chq
filters: {exclude_location_codes: ["ONLINE"], exclude_item_statuses: ["Withdrawn"]}   # keys of `selection`
options: {timezone: America/Detroit}                                                 # keys of `options`
legacy:                          # item note type *names* from Settings > Inventory, for migrated values
  created_date_note_type: null
  circ_count_note_type: null     # added to loans_total
  last_circ_date_note_type: null
  date_format: null              # optional strftime pattern for date notes that are not ISO 8601, e.g. "%d/%m/%Y"
outputs:                         # MAP-2: local column settings, by output name
  items: {empty_value: "", date_format: "%Y-%m-%d", columns: [...]}   # any output key except name and format
checks: {min_item_ratio: 0.8, must_be_filled: [item_hrid]}   # must_be_filled is added to the recipe's
delivery:
  transport: sftp                # optional override, e.g. `local` while testing
  host: sftp.us.collectionhq.com
  port: 22
  username: example_library
  remote_dir: "."
  host_key_fingerprint: "SHA256:..."   # pinned; or known_hosts: ~/.ssh/known_hosts. Unknown keys are never accepted.
  host_key_type: ssh-rsa         # the pinned key's type (ssh-ed25519, ecdsa-sha2-nistp256/384/521, ssh-rsa)
  secret: keyring                # password (or key passphrase): keyring | env:NAME
  private_key: null              # path to a key file (never the key text), instead of a password
  upload_mode: direct            # optional override
  directory: null                # the folder, for transport: local
schedule: Sun 03:30              # optional; else the recipe's default_schedule (the page always writes one)
```

**Overlay rules.** `filters` and `options` replace individual keys. `outputs.<name>` deep-merges into the recipe's output of that name; a `columns` list replaces the recipe's list wholesale, but every recipe column marked `required: true` must still be present (matched by `field`). `checks.min_item_ratio` replaces; `checks.must_be_filled` adds. `delivery` merges over the recipe's. The merged result is validated with the same rules as a recipe, and errors name the feed file. Transport `sftp` needs `host`, `username` and a pinned `host_key_fingerprint` (or `known_hosts`); `local` needs `directory`. `host_key_type`, when set, is one of `delivery.KEY_TYPES`. Relative paths in `delivery` (`directory`, `private_key`, `known_hosts`) are taken from the site folder, never from wherever the scheduler started the process. `private_key` and `known_hosts` must be file paths: key text (a line break, or `PRIVATE KEY`; `recipe.key_text`) is refused with "give the path of the key file, or store the key from the page", and masked if it ever reaches history (`runner.redact`).

**Legacy notes** are read strictly (`dates.legacy`, `fields.read_count`). A date is accepted as ISO 8601, else in `legacy.date_format` when it is set, else as free text that holds a day, a month and a year (a part missing from the text is never filled in from today). A leading label (`Created: `) is ignored, two-digit years fall in the past, and only dates from 1800 to tomorrow are kept. A `date_format` that starts with the label (`Created: %d/%m/%Y`) is tried on the note as written, then on the note and the pattern without their labels, so it reads labelled and bare notes alike. `date_format` must name a day, a month and a year and read a sample date back (`dates.legacy_format_problem`). A count is one whole number, thousands separators allowed, optionally after a label (`Checkouts: 1,234`). A note that is present but cannot be read counts as missing (the item keeps FOLIO's own value) and is counted per setting with up to five item HRIDs (`BuildReport.legacy_unparsed`); `build.legacy_warnings` turns that into one warning per setting, in `run-report.json` (`warnings`), in the alert when a warning first appears or changes, in `preview`'s output, and under *Warnings* in the page's run summary (runs and previews, through `pageWords`). FOLIOHQ's legacy tool still reads these notes leniently; byte parity runs without legacy settings, so it still holds (see `docs/notes.md`).

**File collisions** (SPEC-4). Loading refuses two enabled, delivering feeds that send the same file name to the same destination, naming both (`recipe.file_collisions`; a clash there stops both feeds loading). Paused feeds are left out of that check, so an enabled feed keeps running; instead a paused feed run or released by hand is refused when any other feed, paused or not, sends one of the same file names to the same destination (`recipe.collision_with`, `runner.delivery_clash`), `feeds resume` refuses the clash, and the page refuses a save that creates one.

### Schedules

| Text | Meaning |
| --- | --- |
| `daily`, `weekly`, `monthly` | daily at 02:00; Sundays at 02:00; the 1st at 02:00 |
| `daily 03:30` | every day at 03:30 |
| `Sun 03:30`, `Mon,Thu 06:00` | those weekdays at that time (Mon … Sun, case-insensitive) |
| `monthly 15 01:00` | that day of the month (1–28) at that time |
| `manual` | never due; runs only when started by hand |

Times are wall-clock times in `runs.timezone` (or local time). On the spring change a missing time moves forward by the gap (02:15 becomes 03:15); on the autumn change a repeated time runs once, at its first occurrence. Tests pin both.

A feed is **due** when its latest slot at or before now has passed and nothing since has settled it:
- A run started by the scheduler (`trigger: schedule`) settles the slot when it ends `success`, `built`, `blocked` or `held`. A failed one is retried at the next hourly start, up to `runs.max_attempts_per_slot` scheduled attempts.
- A run started by hand settles the slot only if it delivered (`success`). Building, previews and failures by hand never cancel the scheduled delivery.
- Slots before the site first saw the feed (recorded in history the first time a run, `check` or the wizard sees the file) are never due, so a new feed waits for its first slot even after a manual run or preview. Editing the feed file later does not move that.
- Weekday names may be abbreviated to three or more letters (`Mon`, `Thurs`, `Monday`); other words are errors, so `monthly 03:30` is never read as Mondays.

`feeds` and `check` show, and every `run --due` logs, a scheduled feed as **overdue** when its last success is older than the slot before the latest one: the hourly task may have stopped, or the PC is off at the scheduled time.

**Is the hourly task running?** Every `run --due` on a set-up site records its start before anything else, even when nothing is due: first in `data/last-pass.json` beside the run lock (its modified time is the pass's start, set with `os.utime`, which needs no free space; its content is how the previous pass ended, `{error, alerted}`), then in history meta (`last_due_start`, UTC). For a site from before this, the newest scheduled run stands in. `task_status` uses the newer of the two. `check` and `feeds` print "The scheduled task last ran N ago.", and after more than two hours `check`, `feeds` and the page warn (`task-stale`): the PC may be asleep or on battery (a task registered by an older version never starts on battery power), the task disabled, or the Windows password changed; `schedule --register` fixes the last two. With no record at all they note that scheduled runs have not started (`task-never`). Neither appears while no enabled feed has a schedule other than `manual`. While a pass holds the site's run lock, Task Scheduler does not start the task again (its default), so a long pass (a first full sync can take hours) would look stale: instead `task_status` reports `running` ("A run has been going since …"), and `check`, `feeds` and the page say so without a warning. Once the holder has had the lock for more than four hours (`feedops.LOCK_STUCK_AFTER`, shared with the stuck-lock alert) and some enabled feed has a schedule, it reports `stuck` instead, a warning (`task-stuck`) worded as the stuck-lock alert (`feedops.stuck_text`): a long first download or a stuck run, with what to do. It looks at the lock without taking it (`run_started`: on Windows the locked byte cannot be read through another handle, on Linux `/proc/locks` lists it; elsewhere it cannot tell), and the lock file's modified time is when the run took it. A run that took the lock more than two hours after the task last started (a manual run while the task is stopped) hides nothing. For a warning that reaches someone even when the PC is off, `alerts.heartbeat` names a monitor's ping URL (any dead-man's-switch service), requested with a GET at the start and at the end of every `run --due` pass whatever its result (10-second timeout, no redirects). A long pass still sends nothing between the two, so a monitor's period (or grace time) must allow for the longest pass, such as a first full sync. Manual runs never send it, a failed heartbeat never fails the pass, and the URL is never logged. The heartbeat's wording and timing are the same whether the pass succeeded or failed.

**A task that runs but fails.** A pass can start every hour and still do nothing, for example when the disk is full and history cannot be written. So `task_status` reports `stale` with `failing: true` (warning code `task-failing`) when the task runs but the last start in `last-pass.json` is more than two hours newer than the last one history recorded (history cannot be written), or when the last pass ended with an error; the text names the reason and points to free space, programs holding the files open, and the log. `check` and the page also warn when less than 1 GB is free on the drive of the data folder (`low-disk`). A pass that fails outright sends "The scheduled pass failed: <type>: <message>" without needing history, and does not send the same error again on later passes once it was alerted (`last-pass.json` remembers). `main` turns an `sqlite3.Error` into a plain message about free space and files held open, with exit 1.

**A lock held for hours.** When `run --due` finds the lock taken, it skips the hour (exit 0). When one run has held the lock for four hours or more (a manual run or release that stalled, a stuck process) and some feed is due (`due_feeds`, read without the lock), it alerts once for that run (history meta `lock_stuck_since`, the lock holder's start); with nothing due, nothing was skipped and nothing is sent, so a long first sync between a weekly feed's slots stays quiet. A scheduled pass that stalls never meets this, since Task Scheduler starts no second instance: `check` and the page show `task-stuck`, the heartbeat stops, and once the task's 12-hour limit ends the pass the next one marks its run failed and alerts.

**Given-up slots and lost alerts.** When the last scheduled attempt a slot allows fails (or FOLIO refused the login), nothing more is tried until the next slot; that is alerted once per slot (`runner.report_given_up`, history meta `slot_given_up:<feed>`), naming the slot, the attempts, the last error and the next slot. An alert that no channel took while a webhook or email is set up (the network was down, say) is kept in history meta (`unsent_alerts`, the newest 20) and sent again at the start of the next pass, oldest first, prefixed "First raised <time>, when it could not be sent."; resending stops at the first that still fails. Each is taken off the list in history before it is sent and put back if the send fails, so a history that cannot be written sends none of them, rather than the same ones every hour. Test alerts (status `test`, from `check --alerts` and `init`) are never kept: they are for the person waiting on them.

## Packages and modules

Flat, small modules under `src/folio_exchange/`, as in FOLIOHQ. Copied FOLIOHQ code keeps its tested behaviour unless this section says otherwise.

| Module | Owns |
| --- | --- |
| `errors.py` | `FxError` and subclasses: `ConfigError`, `RecipeError`, `PatronDataRefused`, `SafetyCheckFailed`, `HeldForRelease`, `DeliveryError` (carries `sent`), `LockBusy`. |
| `dates.py` | FOLIOHQ's date helpers, plus strict reading of legacy date notes (`legacy`, `legacy_format_problem`); a time-zone conversion out of range gives no date instead of an error. |
| `credentials.py` | Secret references, keyring access, `secret set`. |
| `config.py` | `site.yaml`: load, defaults, `${ENV}`, validation, secret-key rejection, YAML loader, write. |
| `recipe.py` | Recipe library, versions and ranges, validation, feed overlay. |
| `reads.py` | The reads registry, reference data, permission names, path allowlist, patron-path denylist. |
| `client.py` | FOLIOHQ's `FolioClient` plus the guard (patron, allowlist, read-only), no redirects, `iter_where_in`, `tls_verify`. |
| `cache.py` | FOLIOHQ's SQLite cache, plus `sync_state` and `marc_source_without_marc`. |
| `sync.py` | Full and delta sync of the reads given; `sample()` for previews; staff data dropped on arrival. |
| `fields.py` | The field registry (MAP-1) and `RowBuilder`. |
| `marc.py` | MARC 21 building (from FOLIOHQ), link enforcement. |
| `delimited.py` | Delimited text output. |
| `build.py` | One streaming pass from the cache to every output of a feed. |
| `checks.py` | Required fields, item-count ratio, MARC intake check, conformance with vendor samples. |
| `delivery.py` | SFTP (from FOLIOHQ), local folder, destination keys, host key lookup (every key type the server offers) and typed pins, run pruning. |
| `notify.py` | Alerts by webhook and email; the heartbeat. |
| `history.py` | Run history, audit log and meta in SQLite; file digests. |
| `lock.py` | One run at a time per site. |
| `schedule.py` | Schedule text, due runs, OS scheduler commands and the Windows task definition. |
| `runner.py` | The pipeline for one feed, `--due` runs, release, preview, settings audit, alerts. |
| `cli.py` | `folio-exchange` commands. |
| `feedops.py` | Feed operations shared by `cli.py` and `ui.py`: `set_enabled` (pause and resume by rewriting the `enabled:` line, even when the feed file does not load; audited with `from: cli` or `ui`) `site_warnings` (check's problems, warnings and notes as `{level, code, text, feed}`), and the hourly task's health (`record_due_start`, `last_due_start`, `mark_pass_start(lock_path, now) -> dict` (how the previous pass ended), `mark_pass_end(lock_path, started, error, alerted=False)`, `last_pass(lock_path) -> (datetime | None, dict)`, `free_bytes(folder) -> int | None`, `run_started(lock_path)`, `task_status(hist, feeds, now, *, lock_path=None)` with the states `running`, `stuck` (the lock held past `LOCK_STUCK_AFTER`; text from `stuck_text(since, now)`, as the alert), `ok`, `stale` (with `failing: true` when the task runs but its passes fail), `never` and `unneeded`, `ago`, `alert_secret_warnings(site)`). Warning codes include `task-stale`, `task-failing`, `task-stuck`, `task-never`, `low-disk`, `alert-secret-missing` and `recipe-local-substance`. `ui.py` imports nothing from `cli.py`. |
| `permissions.py` | Who else can change the site folder, `site.yaml`, `feeds/`, `keys/`, local recipe folders, the installed program (`install_paths`: the virtual environment's folder `sys.prefix`, which holds `Scripts\folio-exchange.exe`, its scripts and site-packages folders and the package) and the Python it is built on (`sys.base_prefix` and its `Lib`): `problems(site) -> list[str]`, one plain warning per folder with the fix, never raising and quick (well under half a second). On Windows each access list is read through the Windows API (`GetNamedSecurityInfoW` and its entries, by ctypes) and matched by SID: SYSTEM, Administrators, CREATOR OWNER, OWNER RIGHTS, TrustedInstaller and this user's own SID (looked up once from the process token) are trusted, and app package SIDs (`S-1-15-*`) are ignored. Only when the API cannot be used does it fall back to `icacls` and match by name, where a `?` that icacls prints for a letter outside the console's code page stands for any one letter; an icacls that does not answer within 10 seconds gets its own message. Group and world write bits elsewhere. `check`'s `folder-access` warnings. |
| `wizard.py` | `folio-exchange init`, the guided setup. |
| `ui.py`, `web/` | `folio-exchange ui`: a small local page for setting up, looking at and running feeds (see below). |
| `setupops.py` | The operations behind the page's setup forms: `connect` (log in, then store the password and write `site.yaml`; when the FOLIO address or tenant changes and a feed has delivered, the result carries a `notice` that those feeds hold their next run), `tenant_choices`, `recipe_permissions`, `add_feed` (paused, no delivery, always with its own `schedule`), `feed_settings` and `save_feed` (everyday settings only, with a file version against lost updates), `save_delivery` (reads every host key the server offers and pins the one matching the vendor's fingerprint, with its `host_key_type`), `store_sftp_password`, `store_private_key`. Every save is undone if any feed would then fail to load, or if it creates a file collision with a paused feed. Also the checks `init` shares (`url_problem`, `feed_name_problem`, `schedule_problem`, `own_comments`, `write_settings`). |
| `sources.py`, `recipe_sources.yaml` | Vendor recipe sources: the curated list plus `site.yaml`'s, `check` (review, installs nothing) and `install` (one reviewed commit); `local_minor_warning(site, library, feed) -> str \| None` for hand-placed versions. |
| `launcher.py` | `folio-exchange launcher`: writes a double-click script (`.cmd` on Windows, `.command` on macOS, `.sh` elsewhere) that runs `ui` for one site, naming this install's program and the site in full. |

### Contracts

Signatures are the contract between modules; keep them, or change every caller and this section together. `Site` and `Feed` are dataclasses; sections inside them stay plain dicts, as in FOLIOHQ.

**credentials.py**
```python
FOLIO_SERVICE = "folio-exchange/folio"; SFTP_SERVICE = "folio-exchange/sftp"
SMTP_SERVICE = "folio-exchange/smtp"; WEBHOOK_SERVICE = "folio-exchange/webhook"
HEARTBEAT_SERVICE = "folio-exchange/heartbeat"
SECRET_KEYS = frozenset({"password", "passphrase", "private_key_passphrase", "token", "api_key", "webhook_url",
                         "heartbeat_url"})
def is_secret_ref(value: object) -> bool                      # "keyring" or "env:NAME"
def resolve(ref: str | None, service: str, account: str | None, *, env_first: str | None = None) -> str | None
    # env_first if set and non-empty; then env:NAME; then keyring (ref "keyring" or None). Backend errors -> None.
def source(ref, service, account, *, env_first=None) -> str   # "environment (FOLIO_PASSWORD)", "system keyring
                                                               # (service, account)" or "NOT FOUND (why)"
def keyring_available() -> bool
def store(service: str, account: str, value: str) -> None     # FxError when no usable keyring
def folio_account(username, tenant) -> str; def sftp_account(username, host) -> str   # "user@tenant", "user@host"
```

**config.py**
```python
@dataclass
class Site:
    root: Path; folio: dict; sync: dict; paths: dict; runs: dict; alerts: dict
    cache_path: Path; history_path: Path; lock_path: Path; out_dir: Path; logs_dir: Path
    feeds_dir: Path; recipe_dirs: list[Path]     # site recipe folders, then the bundled library
    name: str (property); timezone: ZoneInfo | None (property)
    def folio_password(self) -> str | None; def folio_password_source(self) -> str
def find_site(explicit) -> Path               # ConfigError naming where it looked, and suggesting `init`
def load_site(root) -> Site                   # defaults deep-merged, ${ENV}, env overrides, validation
def read_yaml(path, where=None) -> dict; def load_yaml_text(text) -> object   # YAML 1.2 booleans
def reject_secrets(data, where) -> None       # ConfigError at the first SECRET_KEYS key, with its path
def write_yaml(path, data, header="") -> None # atomic, UTF-8, keeps key order, refuses secret keys
def merge(base, override) -> dict             # deep merge; lists and scalars replace
BUNDLED_RECIPES: Path
```

**recipe.py**
```python
@dataclass(frozen=True)
class Recipe: id: str; version: tuple[int, int, int]; data: dict; path: Path
    version_str, title, ref ("id@1.0.0") (properties)
def parse_version(text) -> tuple[int, int, int]
def version_matches(version, spec: str) -> bool
def parse_uses(uses) -> tuple[str, str]               # ("chq-academic", "^1.0"); RecipeError without a range
class RecipeLibrary:
    def __init__(self, dirs: list[Path]) -> None
    def all() / ids() / versions(id) / latest(id) / problems()
    def resolve(self, uses: str) -> Recipe            # RecipeError listing the versions that exist
    def newer_major(self, uses: str) -> Recipe | None # highest version newer than anything the range allows
    def shadow_warnings(self) -> list[str]            # a file used in place of a different copy of the same
                                                      # id@version (line endings, BOM ignored); warns only
def recipe_digest(recipe: Recipe) -> str | None       # SHA-256 of the file, line endings and BOM ignored
def validate_recipe(data: dict, where: str, *, shared: bool = True) -> list[str]
def load_recipe_file(path: Path) -> Recipe            # RecipeError with every problem
@dataclass
class Feed:
    name; path; recipe; uses; title; enabled; file_prefix; reads: tuple[str, ...]; selection; options; legacy
    link: dict | None; outputs: list[dict]; checks; delivery; schedule: str
    def output(self, name) -> dict; delivers: bool (property: transport is not none)
def load_feed(path, library, root=None) -> Feed       # root: the site folder, for relative delivery paths
def load_feed_data(raw, path, library, root=None) -> Feed   # used by `upgrade` to try a new range
def load_all(site, library=None) -> tuple[list[Feed], dict[str, str]]   # good feeds, and name -> problem
def load_feeds(site, library=None) -> list[Feed]      # ConfigError listing every bad feed
def feed_errors(site, library) -> dict[str, str]
def layout_signature(feed: Feed) -> dict              # what decides the delivered layout, as JSON data
def layout_changes(before: dict, after: dict) -> list[str]   # in plain words; keys one side lacks are skipped
def settings_changes(before: Feed, after: Feed) -> list[str] # checks, selection, options, reads, schedule,
                                                      # delivery transport and upload mode, link; in plain words
def file_collisions(feeds: list[Feed]) -> dict[str, str]     # enabled, delivering feeds only: name -> problem
def collision_with(feed: Feed, feeds: list[Feed]) -> str | None   # any other delivering feed, paused or not
def key_text(value) -> bool                           # a delivery path setting that holds key text instead
```
`layout_signature` records `file_prefix`, `link` and, per output, the format and file name pattern; for a delimited output also the extension, delimiter, header, empty value, date format and the columns as (header, field) pairs (not `required`, which is a check); for a MARC output `split_every`, `embed_items` and the item field's tag and `strip_existing`, plus its subfields, `max_per_record` and the date format only when items are embedded, since only then do they change the bytes. `layout_changes` describes columns as renamed, "now holds X (was Y)", removed, added or "the order changed", a tab as "tab", an empty value as "nothing (empty)" and true/false as on/off.

**reads.py**
```python
@dataclass(frozen=True)
class Read: name: str; path: str; key: str; permission: str; description: str
READS: dict[str, Read]     # instances, holdings, items, loans (/loan-storage/loans), marc (POST /source-storage/...)
REFDATA: dict[str, tuple[str, str, str]]   # kind -> (path, response key, permission); FOLIOHQ's ten tables
ENTITY_ORDER = ("instances", "holdings", "items", "loans")
LOGIN_PATHS; POST_PATHS; ALLOWED_PATHS
PATRON_PATH_PREFIXES: tuple[str, ...]      # /users, /bl-users, /patron, /circulation (loans there embed the
    # borrower), /loan-storage/loan-history, /request-storage, /accounts, /notes, /perms/users, /authn/credentials,
    # /data-export-spring, /consortia ... Matched on whole leading path segments, case-insensitive.
def is_patron_path(path) -> bool; def is_allowed_path(path) -> bool
def permissions_for(reads) -> list[str]    # sorted; includes reference data permissions
```

**client.py** – FOLIOHQ's `FolioClient` and `FolioError` (now a subclass of `FxError`), plus:
- `guard(method, path)` runs before anything is sent: a patron path raises `PatronDataRefused`; a path outside the reads registry raises `FolioError`; anything but `GET`, or `POST` to the login paths and `/source-storage/source-records` (a read by POST), raises `FolioError("read-only: …")`.
- Redirects are never followed (`allow_redirects=False`, `max_redirects = 0`); a 3xx is a `FolioError` naming the `Location`.
- `iter_where_in(path, key, field, values, batch=50) -> Iterator[dict]`: CQL `field==("v1" or "v2" …)` in batches, for previews.
- `Verify = bool | str | ssl.SSLContext`; `tls_verify(folio: dict, root=None) -> Verify` turns the site's `folio` settings into what `FolioClient(verify_tls=...)` takes: `False` when `verify_tls` is false (it wins over `ca_bundle`); the absolute path of a `ca_bundle` PEM file (relative to the site folder `root`; a missing file is a `ConfigError` naming `folio.ca_bundle`); a `truststore` SSL context for `windows` (a `ConfigError` off Windows or when the package is missing); else `True`. `runner.client_for`, `setupops.connect` and the wizard all use it. An SSL context is mounted on the session through an `HTTPAdapter`. An untrusted certificate raises `FolioUnreachable` with the `UNTRUSTED` text at once, at login or later, and is never retried.
- **Fail fast, log in once.** When FOLIO has never answered (connection refused, unknown name, untrusted certificate), the client raises `FolioUnreachable` at once with a plain message instead of retrying for minutes. A refused login raises `LoginFailed` only when FOLIO judged the user name and password (HTTP 400, 401, 403 or 422, `JUDGED`); a 404 from both `/authn/login-with-expiry` and the legacy `/authn/login`, or any other unexpected status, means the login module is missing or restarting (a FOLIO upgrade, say) and raises `FolioUnreachable`, so the next hourly start tries again. After `LoginFailed` the client never posts the password again in that run; a scheduled run that ended this way is not retried within its slot (`no_retry` in the report), so a wrong password cannot lock the service account. Reference-data loading never swallows either error.
- **One way in for check, init and the page.** `connect()` logs in with the same retries as `request()`, so a busy gateway (502, 503, 504, a login timeout) is retried and the last failure is a plain `FolioError`, never a traceback. `FolioError.status` carries the HTTP status. `probe(client, reads)` reads one page of every endpoint the reads need plus all reference data and says for each `ok` (with a count), `denied` (401 or 403: a missing permission) or `failed` (anything else, named as it is); only `denied` rows are listed for the FOLIO administrator.
- **FOLIO's clock.** Every answer's HTTP `Date` header records how far this computer's clock is from FOLIO's; `server_now()` gives FOLIO's time, `check` warns when they differ by more than five minutes.
- Messages never include the password. The empty-password message names `FOLIO_PASSWORD` and `folio-exchange secret set folio`. Default user agent `folio-exchange/<version>`.

**cache.py** – FOLIOHQ's, unchanged, plus `sync_state(entity) -> str | None` ("never synced" or "the last full sync did not finish") and `marc_source_without_marc() -> list[str]`. Loans never store a borrower: the table has no user columns, and sync drops `userId`, `proxyUserId` and related keys on arrival (tested). Instances, holdings and items lose staff data on arrival too (`strip_staff_data`, below).

**sync.py**
```python
@dataclass class SyncReport: mode, fetched, removed: dict; marc_fetched, marc_missing: int; seconds: float
class Syncer:
    def __init__(self, client: FolioClient, cache: Cache, site: Site) -> None
    def run(self, reads: Iterable[str], force_full: bool = False) -> SyncReport
    def sample(self, reads: Iterable[str], instances: int) -> SyncReport   # previews: about N instances found
        # from the first items by id (most instances on many tenants have none), with all their holdings,
        # items, loans and MARC, into this (temporary) cache; a tenant with no items gets its first instances
def reads_to_sync(feeds: list[Feed]) -> set[str]   # the union over enabled feeds
def strip_staff_data(record: dict) -> dict          # applied to instances, holdings and items as they are stored
```
`strip_staff_data` drops `circulationNotes`, cuts `lastCheckIn` to `{dateTime}` (or drops it when there is no date) and removes `createdByUserId`, `updatedByUserId`, `createdByUsername` and `updatedByUsername` from `metadata`. Item `notes` stay, because feeds choose note types from them, and so do `administrativeNotes`. No field reads what is dropped. Records cached before are cleaned as they are fetched again: at the next full refresh, or at once with `sync --full`.
Reference data always syncs; when a table cannot be loaded, the copy from the last sync is used, and with no copy the sync fails (names and codes go into the files). Entities sync in `ENTITY_ORDER`, limited to `reads`; MARC is fetched for MARC-source instances when `marc` is in `reads`, and each sync retries MARC-source instances that still have none. FOLIOHQ's full/delta/auto rules, `incomplete` flags and 10-minute overlap are kept, and:
- A full sync counts FOLIO's records before and after the download and compares what it fetched with the smaller count before removing anything; a shortfall (beyond 0.1 % or 5 records) fails the sync and leaves the entity marked incomplete, so a truncated download never empties the cache, while records added during the download (which may sit behind the paging cursor) are left for the next delta.
- The watermark for the next delta is FOLIO's time at the start of the sync (`client.server_now()`), not this computer's, so a PC clock that runs fast cannot skip updates.
- A delta falls back to a full sync when the cache holds more records than FOLIO (deletions) or noticeably fewer.
- When `marc` joins the reads after a sync without it, the next instance sync is full, so older bibs get their MARC.

**Shared cache.** All feeds share one cache, and sync state is per entity. So a run always syncs the union of the reads of every enabled feed and of the feeds being run: otherwise one feed could advance `last_sync` and leave another feed's data stale. A feed's own `reads` still limit what it may output, and `build` refuses to run when any of the feed's reads has never fully synced.

**dates.py** – FOLIOHQ's helpers (`parse`, `norm`, `fmt`, `latest`, `to_folio`, `now_utc`), plus:
```python
def legacy(value: str, naive_tz, date_format: str | None = None, now: datetime | None = None) -> str | None
    # a legacy date note as a UTC string (FMT), or None (see "Legacy notes"); now: for tests
def legacy_format_problem(pattern) -> str | None    # why legacy.date_format cannot read dates, or None
```
`parse` and `norm` return None, and `fmt` returns `""`, when the conversion to or from a time zone is out of range (0001-01-01 east of UTC, 9999-12-31 late in the day west of it), so one such value never fails a build.

**fields.py**
```python
@dataclass(frozen=True)
class FieldDef: name: str; description: str; needs: frozenset[str]; date: bool = False
FIELDS: dict[str, FieldDef]       # FOLIOHQ's 47 item fields; loan-derived fields need "loans"
ITEM_FIELDS: dict[str, str]; DATE_FIELDS: set[str]
def suggest(name: str) -> str | None             # closest field name, for "did you mean"
def read_count(text: str) -> int | None          # a legacy loan count (replaces FOLIOHQ's _int), or None
class Ref: (FOLIOHQ's, unchanged)
class RowBuilder:
    def __init__(self, ref: Ref, options: dict, legacy: dict | None) -> None   # options.timezone also reads legacy dates
    def build(self, item, holdings, instance, loans) -> dict                  # FOLIOHQ's, with strict legacy notes
    unparsed: dict[str, dict]     # legacy setting -> {"count", "examples": up to 5 item HRIDs}: notes not read
```

**marc.py** – FOLIOHQ's functions; `MarcBuilder(output: dict, date_fmt)` reads `embed_items` and `item_field.{tag, subfields, strip_existing, max_per_record}`; `date_fmt(name, value)` formats date fields with the output's `date_format` and `options.timezone`. `finalize` always sets 001 and 999 ff $i (see "Output bytes") and keeps the counters, size limits and item shedding.

**delimited.py**
```python
class DelimitedWriter:
    def __init__(self, output: dict, path: Path, tz: ZoneInfo) -> None   # writes the header row if output["header"]
    def write(self, row: dict) -> None      # dates via dates.fmt and output["date_format"]; empty -> empty_value
    def close(self) -> None
    rows: int
```

**build.py**
```python
@dataclass class BuildReport: FOLIOHQ's fields (files, bibs_written, items_written, items_skipped, ...), plus
    bibs_marc_missing (MARC-source instances with no SRS record), bibs_marc_missing_consortium, marc_unreadable
    (SRS records that could not be read or written), marc_problem_hrids (up to ten examples),
    outputs: dict[str, list[str]] (output name -> file names), unfilled: Counter (must_be_filled field -> rows)
    and legacy_unparsed: dict[str, dict] (legacy setting -> {"count", "examples"}, from RowBuilder.unparsed)
def build(cache: Cache, feed: Feed, run_dir: Path, *, limit_bibs: int | None = None,
          stamp: str | None = None, name_prefix: str = "") -> BuildReport
def legacy_warnings(rep: BuildReport, legacy: dict) -> list[str]   # one plain warning per legacy setting with
    # unreadable notes: how many, what the items carry instead, examples, and the accepted formats
```
One SQL pass, as in FOLIOHQ. Selection filters apply once, before any output sees a row. `limit_bibs` stops after that many bibs and `name_prefix` (`PREVIEW_`) goes before every file name (previews). `stamp` overrides `{date}` (tests). Loan statistics are left empty for a feed without the `loans` read, even if another feed synced loans.

**checks.py**
```python
def required_fields(feed: Feed, report: BuildReport) -> None           # SafetyCheckFailed listing field: count
def item_ratio(feed: Feed, items_written: int, last_delivered: int | None) -> None   # SafetyCheckFailed
def check_marc_file(path: Path) -> list[str]   # binary MARC parses; spots text-converted MARC (no 0x1D/0x1E)
def conformance(feed, run_dir, samples: dict[str, Path], files: dict[str, list[str]] | None = None) -> list[str]
    # VAL-2: per output name (files: BuildReport.outputs, so each output is compared with its own file),
    # delimited = same header row, column count and delimiter; marc21 = both files parse as binary MARC
def sample_files(feed: Feed) -> dict[str, Path]   # <recipe folder>/samples/<output name>.<ext>
```
Blocked messages give the last delivered and current counts, the run folder, the command to release the run, and what to do if the drop is not expected (fix FOLIO and run again; the blocked run is then superseded and never sent). Feed problems name settings as the feed file writes them (`filters.…`, `checks.must_be_filled`), and a misspelled required column is reported as a misspelling.

**delivery.py**
```python
def sha256_fingerprint(key) -> str
KEY_TYPES: dict[str, tuple[str, ...]]        # pinnable key type -> host key algorithms that ask for it
                                             # ("ssh-rsa" -> rsa-sha2-512, rsa-sha2-256, ssh-rsa)
def host_key_fingerprints(host: str, port: int = 22, timeout: float = 30) -> dict
    # no login: {"sha256", "md5", "type"} of the key the server prefers, and "keys": every key it offers
    # (that one first, then one per other type it has: Ed25519, RSA, then the ECDSA kinds)
def host_key_fingerprint(host: str, port: int = 22, timeout: float = 30) -> str   # the preferred key's SHA-256
def disabled_host_key_algorithms(key_type: str) -> list[str]   # paramiko's disabled_algorithms["keys"]
def destination_key(d: dict) -> str          # "sftp://user@host:port/dir", a file: URI, or "none"
def describe_destination(d: dict) -> str
def deliver(files: list[Path], delivery: dict, feed_name: str) -> list[str]     # destinations. FxError when it
    # fails before anything reached the destination (a login or host-key failure); DeliveryError (with .sent)
    # once the destination's copy may have changed (a direct upload started, a rename or local move done)
def check_delivery(delivery: dict) -> list[str] | None   # sftp: login + list (None when listing is refused);
                                                          # local: folder exists and is writable; uploads nothing
def prune_runs(feed_out_dir: Path, keep: int, protect: set[Path] | None = None) -> None
```
SFTP keeps FOLIOHQ's pinned key or `known_hosts` (never auto-accept), staged and direct modes. Only the pinned SHA-256 value is ever accepted. A pin with `host_key_type` limits the negotiation to that type (`ssh-rsa` allows `rsa-sha2-512`, `rsa-sha2-256` and `ssh-rsa`), so a vendor adding a newer kind of key does not stop deliveries; a server that no longer offers the pinned type fails as a changed key (`Host key for <host>: the server no longer offers a <type> key (pinned SHA256:…); it now offers <type> SHA256:…`), which `plain_error` words as one to ask the vendor about. A pin without a type (from before types were kept) is tried in paramiko's own order, then once per other type the server has not yet shown (Ed25519, RSA, then the ECDSA kinds), and any other key is refused before logging in. These probing handshakes, and `host_key_fingerprints`' per-type reads, run with `paramiko.transport` logging raised to CRITICAL, so a missing type leaves no error traceback in the log. While `host_key_confirmed` is false (the wizard's *not confirmed yet*), nothing logs in to the server at all: run, release, the page and `check --delivery` stop with a message, and `check --delivery` reads every key the server offers without logging in and says *the same* when one of them (preferably of `host_key_type`) is the stored one, otherwise *DIFFERENT* with each offered type and fingerprint. Sessions use 30-second keepalives and a 300-second channel timeout, so a stalled transfer becomes an ordinary failed delivery. When a staged upload fails before any rename, its `.part` files are removed, and any that could not be are named in the error. The password comes from `credentials.resolve(delivery["secret"], SFTP_SERVICE, sftp_account(...))`. `local` copies the files into `delivery["directory"]`, each as `.part` then renamed.

**notify.py**
```python
def send(alerts: dict, subject: str, text: str, *, account: str = "default") -> list[str]
    # channels used ("webhook", "email"); never raises. account = the keyring account of the webhook URL
    # (runner passes the site folder name)
def should_alert(alerts: dict, status: str) -> bool   # on: failure -> any status but success, built and preview
def has_channel(alerts: dict) -> bool                 # a webhook or an email is set up
def heartbeat(alerts: dict, account: str) -> bool     # GET alerts.heartbeat's URL; never raises, never logs the URL
def url_problem(url: str) -> str | None               # why a webhook or heartbeat URL is refused (https, or http
                                                      # only to this computer); never includes the URL
```
Webhook: `POST {"text": ...}` (Slack-style), or with `webhook_format: teams` a message with an Adaptive Card, as Teams workflows expect. Email: `smtplib` with STARTTLS (or SMTP over TLS on port 465) and optional login. Neither ever includes a secret, and the HTTP library's request logging stays at WARNING even with `-v`, so webhook URLs never reach the log. The webhook is posted with `allow_redirects=False`; a refused URL or a 3xx is logged as a failure without the URL. The heartbeat logs only "Heartbeat sent." or why it failed.

**history.py**
```python
class History:                               # also a context manager
    def __init__(self, path: Path) -> None
    def start_run(self, feed, recipe, recipe_version, trigger, run_dir, destination=None, *,
                  folio_source=None) -> int
    def finish_run(self, run_id, status, *, items, bibs, files: list[dict], delivered: list[str], error, report)
    def mark_delivered(self, run_id, status, delivered, files, report=None) -> None   # release of a run
    def fail_stale(self) -> int              # runs left "running" by a crash -> failed (call under the lock)
    def last_delivered(self, feed, destination=None) -> dict | None   # baseline and first-delivery hold
    def last_run(feed) / runs(feed=None, limit=20) / runs_since(feed, since) / get(id)
    def run_by_dir(self, path) -> dict | None   # by the stored path, else by its last two parts <feed>/<stamp>
                                                # (a run recorded before the site folder moved)
    def delivered_feeds(self) -> list[str]      # feeds that have delivered at least once
    def audit(self, actor, feed, action, detail) -> None; def audit_log(self, limit=50, feed=None) -> list[dict]
    def get_meta(key, default=None) / set_meta(key, value)   # settings:<file> snapshots, settings_audited,
        # feed_seen:<feed>, last_due_start, upgrade:<feed>, unsent_alerts, slot_given_up:<feed>, lock_stuck_since
    def prune(self, months: int) -> int      # keeps each feed's last delivered run
def file_digest(path: Path) -> dict          # {"name", "bytes", "sha256"}
def current_user() -> str
```
Statuses: `running`, `success` (delivered, or transport `none`), `built` (`--no-deliver` or `build`), `blocked`, `held`, `failed`, `preview`. Triggers: `manual`, `schedule`, `preview`. Every run records its files with sizes and SHA-256 hashes, its destination key, its recipe version and the FOLIO it read from (`folio_source`); every delivery records where each file went (spec, Audit). Columns added after the first release (`ADDED_COLUMNS`, today `runs.folio_source`) are added to an older history file when it is opened, under the write lock (`BEGIN IMMEDIATE`, checking again under it), so the page and the hourly task opening the same old file at once add each column once. `run_dir` is stored as an absolute path; `runner.run_path` finds it again after the site folder moves.

**lock.py** – `class RunLock: __init__(path: Path); __enter__/__exit__` with an OS file lock (`msvcrt` on Windows, `fcntl` elsewhere); a second holder gets `LockBusy` at once. `run_many`, `release` and `sync` take it; `run_feed` expects its caller to hold it.

**schedule.py**
```python
@dataclass(frozen=True) class Schedule: kind: str; days: tuple[int, ...]; hour: int; minute: int; text: str
def parse(text: str) -> Schedule                    # ConfigError with the accepted forms
def last_slot(s, now, tz) -> datetime | None; def next_slot(s, now, tz) -> datetime | None   # aware UTC
def describe(s: Schedule) -> str                    # "Sundays at 03:30"
def is_due(s, now, tz, runs: list[dict], max_attempts: int, created: datetime | None) -> bool
    # runs: the feed's runs since the slot; created: when the site first saw the feed (earlier slots never due)
def register_command(site_root, executable, shell="powershell") -> str
    # `folio-exchange --site ... schedule --register`, as `& '...'` for PowerShell or quoted for Command Prompt
def os_command(site_root, executable, system, user=None) -> str
    # shown, never run. Windows: PowerShell lines (New-ScheduledTaskAction, -Trigger, -Settings with
    # -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable, Get-Credential for the password,
    # Register-ScheduledTask) for the same task as windows_task_xml; elsewhere a crontab line at minute 7
def windows_task_action(site_root, executable) -> tuple[str, str]   # program, and "--site ... run --due"
def windows_task_xml(site_root, executable, user=None) -> str       # Task Scheduler XML (see below)
def windows_task_args(site_root, xml_file, user=None) -> list[str]  # schtasks /Create /TN ... /XML file
                                                                    # [/RU user /RP *] /F, run with no shell
def os_helpers(site_root, system) -> dict[str, str]   # run now, last result, remove (Windows)
def ps_quote(text) -> str                             # a single-quoted PowerShell literal
```
The Windows task runs every hour at minute 7 from 2020-01-01, as the user whether or not anyone is signed in (`LogonType` Password). Unlike schtasks' defaults, it starts and keeps running on battery power, starts as soon as it can after a missed start (`StartWhenAvailable`: the PC was off or asleep), never starts twice at once (`IgnoreNew`) and stops a pass after 12 hours (`TASK_TIME_LIMIT`). `schedule --register` writes that XML to a temporary file (it holds no secret) and registers it with `schtasks /XML`, which asks for the password. A task registered by an older version (`schtasks /TR`) kept the defaults and never starts on battery: the installer notices and offers to set it up again, and the stale-task warning says so.

**runner.py**
```python
@dataclass class RunOptions: full=False; no_deliver=False; skip_sync=False; force=False; trigger="manual"
@dataclass class RunResult: feed: str; status: str; code: int; run_dir: Path | None; report: dict
def run_feed(site, feed, opts, *, hist: History, sync_report: dict | None = None) -> RunResult  # lock held by caller
def run_many(site, feeds, opts, *, all_feeds=None, bad_feeds=None) -> list[RunResult]   # one lock, one sync
def due_feeds(site, feeds, now, hist) -> list[Feed]
def release(site, feed, run_dir: Path | None, *, force=False, actor=None, confirm=None) -> RunResult
def preview(site, feed, instances=10) -> RunResult  # temporary cache; PREVIEW_ files in out/<feed>/preview-…
def hold_reason(hist, feed, source: str | None = None) -> str | None   # source: folio_source(site)
def upgrade_review(hist, feed) -> list[str]         # what `upgrade` found the new major changes (meta upgrade:<feed>)
def overdue(site, feed, hist, now) -> str | None
def layout_note(last: dict | None, layout) -> str | None   # what changed since the delivery `last`, or None
def site_recipe_paths(site) -> list[Path]           # recipe files in the site's own recipe folders
def audit_settings(site, hist, written: set[str] | None = None) -> list[str]; def alert(site, feed, status, text, *, force=False) -> list[str]
    # alert keeps what no channel took (meta unsent_alerts) when a channel is set up, except a 'test'
def start_pass(site, hist) -> None                  # resend_alerts, then fail_stale (with an alert); lock held
def resend_alerts(site, hist) -> int                # kept alerts, oldest first, "First raised ..."; each taken off
                                                    # before it is sent, put back on a failure, where it stops
def given_up(site, feed, hist, now) -> tuple[datetime, list[dict]] | None   # slot, its failed scheduled runs
def report_given_up(site, feeds, hist, now) -> None # one alert per given-up slot (meta slot_given_up:<feed>)
def delivery_clash(site, feed) -> str | None        # a paused feed's file collision with any other feed
def run_path(site, stored) -> Path                  # a run folder now: <out>/<feed>/<stamp> under the site's
                                                    # current out folder (the stored place only while it alone exists)
def folio_source(site) -> str; def source_of(folio: dict) -> str   # "<base url, lower case> tenant <tenant>"
def source_change_note(before: dict | None, after: dict) -> str | None   # pointed at another FOLIO: what holds
def cache_damaged(exc: sqlite3.DatabaseError) -> bool    # SQLITE_CORRUPT or SQLITE_NOTADB only
def cache_problem(site, exc) -> str                 # plain words for a full disk, a lock, a file that will not open
def quarantine_cache(site, kind="broken") -> Path   # cache.<kind>-<time>[-02, -03 ...].sqlite, never reused
def worst_code(results) -> int                      # 1 if any failed, else 2 if any blocked or held, else 0
```

`run_many`: take the lock → `start_pass` (resend kept alerts, then mark stale `running` rows failed and alert) → audit hand edits → prune history → report problem feed files that are not paused as failed (alerting once per distinct problem) while good feeds still run; one unreadable file never stops the others → one sync of the union of reads → `run_feed` for each feed. A failed sync fails every feed once, with one alert; the advice depends on the failure (`check` for FOLIO problems, the log otherwise). A damaged cache (an `sqlite3.DatabaseError` that SQLite reports as `SQLITE_CORRUPT` or `SQLITE_NOTADB`; on Python 3.10, "malformed" or "not a database" in the message) is moved aside as `data/cache.broken-<time>.sqlite` (with `-02`, `-03` … when that name is taken, so one quarantine never replaces another) and rebuilt with a full download in the same run; history is never touched. Any other database error (a full disk, a lock held by a backup or virus scan, a file that cannot be opened) fails the run with a plain message and leaves the cache as it is, so the next run carries on from it.

`run --due` does more before choosing feeds, under the lock and even when nothing is due: `start_pass` (so a pass killed on a slot's last attempt is reported now), the settings audit, and `report_given_up`; after running the due feeds it calls `report_given_up` again. The cache records which FOLIO filled it (`folio_source`: the API address and tenant). After either changes (in `init`, on the page or by hand), the next sync moves the old cache aside as `data/cache.other-folio-<time>.sqlite` and downloads everything, because a delta on top of it would mix two catalogues; a build that skips the sync refuses until then. Each run records the source too (`runs.folio_source`, `run-report.json`), and the first delivery from another FOLIO is held (see Hold), whether the change came from `site.yaml` or from `FOLIO_URL`/`FOLIO_TENANT`. `init` says so after logging in to a changed address or tenant, and the page's Connect form shows it when a feed has delivered before (`source_change_note`).

**MARC warnings.** When bibs whose source is MARC had no SRS record, or their SRS record could not be read or written, they are sent as brief records generated from the instance, and the run report and alert carry a warning with the count and examples; when some are shared consortium records and `folio.srs_include_shared` is false, the warning says to set it. The alert goes once, when the warning first appears or changes against the feed's last run that was not a preview (a preview's warnings come from its sample). One unreadable record never fails the feed. Legacy notes that could not be read are warned about the same way (`legacy_warnings`, see "Legacy notes").

`run_feed`: history start → sync (unless `skip_sync` or a `sync_report` was passed) → refuse if the cache is not ready for the feed's reads → build → required fields → item ratio against `last_delivered(feed, destination)` (unless `force`, which is audited) → `--no-deliver` ends `built`; transport `none` ends `success` → a paused feed whose files collide with another feed's fails ("Nothing was sent: …") → **hold** → deliver → history finish → prune → alert. Checks run before every status, so a `built` run has passed them too.

**Hold.** A delivery waits for a person (status `held`) when the feed has never delivered to this destination (transport, host, port and folder, or local directory), when it reads from another FOLIO than the last delivery to that destination did ("first delivery from FOLIO X; the last came from Y", comparing `folio_source`; a delivery that recorded none counts as matching), when its recipe's major version differs from the last delivery's (the reason then adds what `upgrade` found the new version changes in the feed's settings), or when its layout (`layout_signature`) differs from the one recorded with the last delivery to that destination. The reason names every change, for example "the files' layout changed since the last delivery on 2026-10-27 14:02: items file: columns changed: 'Item record ID' renamed to 'Item ID'". The layout includes the time zone dates are written in (`options.timezone`, "time zone changed from UTC to America/Detroit"), for every delimited output and for a MARC output that embeds items (the only MARC dates). Without a header row the headers are never written, so columns are compared by field and order alone, and switching the header row on or off is named as only that. A setting that the recorded layout does not hold (one from an older version) is not compared. A delivery recorded before layouts were kept has none; that alone never holds, and the run records its layout, so the next change does. Deliberately not fixed: because such a delivery has no layout to compare with, a layout change made at the same time as the upgrade to this version is not held. A local test delivery therefore never disarms the hold for the real SFTP destination, and the item-ratio baseline always compares like with like. `run_feed` works out the hold reason before the safety checks, so a `built` run and a `blocked` run keep it as `would_hold` in the report, and a blocked run's error (and so its alert) adds "It would also have been held for review: …": a smaller test tenant that trips the item-ratio check is named as another FOLIO, not only as a drop.

**Release.** `release FEED [RUN_DIR]` (also `deliver`) takes the lock and sends the latest held, blocked or built run that is newer than the feed's last delivery (previews and failed runs never). A run older than the last delivery is superseded: sending it would replace the vendor's newer files, so it needs its `RUN_DIR` and `--force`. It first shows the run, its counts against the last delivery, the reason it waited (the block error, the hold reason, what a delivery would have held for (`would_hold`, for a built or blocked run: another FOLIO, a new major version with what `upgrade` found it changes) and any layout change since the last delivery, each said once), the files and the destination, and asks for confirmation (`--yes` for scripts). The question is asked without the lock held, so a person who leaves it open never holds up scheduled runs; under the lock the run is checked again before sending, and if anything changed meanwhile (another run, or a delivery, was recorded) nothing is sent and the person is asked to run the release again. It re-checks every file's SHA-256 against the run record (a file opened and saved in Excel is refused), refuses if the feed's destination changed since the build or (for a paused feed) another feed sends the same file names there, and runs the item-ratio check for a `built` run unless `--force`. The run folder is found through `run_path`, so a run held before the site folder moved can still be released, and pruning still protects held, blocked and last delivered runs after a move. Without a `RUN_DIR`, `release` refuses when a newer run of the feed failed: sending the older run would quietly replace what the person expects to send, so they run the feed again or name the older run's folder. Releasing a held or blocked run is itself the person's approval. The release is recorded in history and the audit log with who did it.

**Partial deliveries.** When a delivery stops after the vendor's copy may have changed (a direct upload started, a staged rename, or a local move done), the run fails with a message listing what the vendor already has, `partially_delivered` in the report, and an audit entry. Every delivery failure advises running the feed again (`folio-exchange run FEED`; a scheduled run also retries), never `release`. A partial delivery never becomes the ratio baseline.

**Alerts.** The text names the site, feed, recipe version, status, counts, the error, the run folder and the next command, and never a secret. With `on: failure`, a successful run after a failed or blocked one sends a "recovered" message. `run --due` exits 0 when another run holds the lock (the next hourly start catches up), alerting only once one run has held it for four hours while a feed is due; a manual run reports the lock and exits 1. Alerts that could not be sent, given-up slots and failed passes are covered under Schedules.

**Settings audit.** Every run and `check` compares `site.yaml`, each feed file and the site's own recipe files (`site_recipe_paths`; never the bundled ones) with the snapshot kept in history; a change is written to the audit log as a key-level diff (`checks.min_item_ratio: 0.9 -> 0.5`), with list entries numbered (`outputs[1].columns[8].header`), so hand edits are audited, not only wizard changes. Once the site has snapshots (or history meta `settings_audited`, set by the first audit), a feed or site recipe file that appears is audited as "settings file added" with its redacted settings, and one that disappears as "settings file removed" with its last settings, and its snapshot is dropped; a renamed file shows as both. A new site's first audit only records snapshots, and so does a file the program just wrote and audited itself (`init` and the page pass it as `written`), so a feed added by `init` or the page is not logged a second time as "settings file added". Snapshots are redacted first: a secret-looking key, a `secret`, `webhook` or `heartbeat` value that is not a reference (someone typed the secret itself), or key text in `private_key` or `known_hosts`, is masked, so a mistake in a file never copies a secret into history.

`run-report.json` is written in every run folder: FOLIOHQ's keys (`version`, `started`, `finished`, `run_dir`, `sync`, `build`, `delivered`, `status`, `error`) plus `feed`, `recipe`, `recipe_version`, `recipe_sha256` (`recipe_digest`), `layout` (`layout_signature`), `folio_source`, `trigger`, `destination`, `files` (digests), `last_delivered_items`, and when relevant `held`, `warnings` (MARC and legacy notes), `next`, `partially_delivered`, `released_by` and `tls_unchecked` (true when `folio.verify_tls` is false). The history row keeps the report, so a delivery's layout needs no column of its own. It never holds a secret.

**cli.py** – global options `-s/--site`, `-v/--verbose`, `--version` (with the git commit when pip recorded one in `direct_url.json`, as for a `git+https://` install; an installer zip records none, so the installer prints the commit instead). Logging to stderr and `logs/folio-exchange.log`; stdout is UTF-8. `run`, `build`, `sync` and `release` also print a short JSON summary, as FOLIOHQ did. `fields` and `recipes` work without a site.

| Command | Does |
| --- | --- |
| `init [FOLDER]` | Guided setup (`wizard.py`): connect, pick a recipe, filters, preview, delivery, schedule, alerts. |
| `check [FEED]` | Password source (and a note when it is only this window's environment, which a scheduled task will not have), login, every endpoint the enabled feeds need with record counts, recipe updates held for review, feed and recipe file problems, overdue feeds, a 0 item ratio on a delivering feed, alerts switched off, an alert webhook or heartbeat URL that cannot be found (`alert-secret-missing`), when the hourly task last ran (or `task-stale`, `task-failing`, `task-stuck`, `task-never`), less than 1 GB free for the data folder (`low-disk`), `verify_tls` off (`tls-off`), a local recipe copy that differs (`recipe-shadow`), a hand-placed recipe version that changes more than wording (`recipe-local-substance`), and folders other users can change (`folder-access`). Only DENIED endpoints produce the list of permissions for the FOLIO administrator; a file problem ends with how to fix the file. |
| `check --delivery` (alias `--sftp`) | Tests each feed's vendor login and lists the folder, or checks the local folder; uploads nothing. Messages as FOLIOHQ's `check --sftp`. |
| `check --alerts` | Sends a test alert on every configured channel. |
| `preview FEED [-n 25] [--rows 3] [--columns ...]` | Sample from live data in a temporary cache; prints item rows one per block (header: value); writes `PREVIEW_` files; shows sample conformance. |
| `run FEED… \| --all \| --due` | Sync, build, check, hold or deliver. `--full`, `--no-deliver`, `--skip-sync`, `--force`. Exit code: the worst status. `--due` records its start (`data/last-pass.json`, then `last_due_start`), sends the heartbeat at the start and at the end of the pass, runs `start_pass`, the settings audit and `report_given_up` under the lock before choosing due feeds, and records how the pass ended in `last-pass.json` (a pass that fails outright is alerted, once per distinct error). `--due` on a named folder with no `site.yaml` yet (installed, not set up) says so and exits 0: nothing is due. |
| `release FEED [RUN_DIR] [--yes] [--force]` | Deliver a held, blocked or built run after showing it (`deliver` is the same). |
| `sync [--full]` | Update the cache only. |
| `build FEED [--force]` | Write files from the cache without contacting FOLIO, and run the checks (status `built` or `blocked`). |
| `feeds [list \| pause FEED \| resume FEED]` | First, when the hourly task last ran (labelled `WARNING` or `NOTE` when it applies). Then each feed: recipe and version, schedule and next slot, destination, last run and the next step, held updates, overdue, a hand-placed recipe version that changes more than wording, problems. `pause`/`resume` set `enabled` (keeping comments) and audit it. `resume` refuses a feed whose SFTP server key is not confirmed yet, and one that would stop another feed from loading (the same file names to the same place). |
| `recipes [list \| show ID[@RANGE] \| validate PATH \| summary ID[@RANGE]\|FEED \| sources \| get SOURCE [--yes]]` | Browse and check recipes (`list` also prints `recipe-shadow` warnings); `summary` prints the one-page layout for the vendor, from the recipe or from a feed's merged settings. `sources` lists the vendor sources and the commit last downloaded from each; `get` shows what a source offers now (new, installed, refused, what changed, which feeds take it) and installs the new versions after asking. |
| `upgrade FEED [--to RANGE] [--yes]` | Show the changelog, column changes and every other change to the feed's effective settings (checks, selection, options, reads, schedule, delivery method and the link; `recipe.settings_changes`), validate the feed against the new version, rewrite `uses:` (keeping comments) and audit it, with those changes in the "recipe upgraded" entry (and in history meta `upgrade:<feed>`). The next delivery on a new major is held, and its reason names those changes. |
| `fields` | The field registry with descriptions and what each needs. |
| `history [FEED] [-n 20] [--audit] [--show RUN]` | Run history, one run in plain language, or the audit log. |
| `secret set {folio\|smtp\|webhook\|heartbeat\|FEED}` and `secret status` | Store a secret in the keyring; show where each comes from. |
| `schedule [--register]` | Next slot per feed, the exact command to register the hourly `run --due` with the OS, and commands to run it now, see its last result or remove it. On Windows that command is `schedule --register` itself, in a PowerShell and a Command Prompt form, followed by PowerShell lines that register the same task by hand (`os_command`), so either way the task keeps the battery settings. Registers nothing, unless `--register`: then it registers the task from `windows_task_xml` with schtasks (as an argument list, no shell), and schtasks asks for the Windows password; the installer uses it. Elsewhere `--register` prints the crontab line. |
| `ui [--port N] [--no-browser]` | Serve the local page on 127.0.0.1 and open the browser at a one-time link; Ctrl+C stops it. With `--no-browser` (or `FOLIO_EXCHANGE_NO_BROWSER`, or when no browser opens) the link is printed in the window instead, to be opened within 10 minutes. Without `--port` it uses 8765, or a free port (with a warning) when something other than this site's page has 8765. When this site's page already listens on the port (proved through `/api/whoami` with the site's page secret), it asks that server to open the browser at a fresh link, or to print one in its own window, instead of starting a second one. On a named folder with no `site.yaml` yet, the page starts with the FOLIO connection. |
| `launcher [--folder DIR]` | Works on a site still to be set up (it creates the folder). Write `FOLIO-Exchange (<site>).cmd` (or `.command`, `.sh`) to the desktop, or `DIR`, for opening the page with a double-click. It replaces an earlier launcher but never another file; it holds no secrets. Paths are quoted for the shell (`%` doubled in a `.cmd`, `chcp 65001` when a path is not ASCII), and the file name and window title keep only letters, digits, spaces and `.()-`. |

Exit codes: 0 success, built or nothing due; 1 failed (or a usage or settings problem, including a mistyped command, or a database file in `data/` that cannot be read or written, reported in plain words); 2 blocked, held, or a release that was not confirmed. `release` refuses a run folder that belongs to another feed, and a released blocked run keeps its block reason as `released_after`, not as an error.

**wizard.py**
```python
class Prompter:                                  # console implementation (ConsolePrompter)
    def say(self, text) -> None; def ask(self, prompt, default=None, validate=None) -> str
    def choose(self, prompt, options: list[tuple[str, str]], default=None) -> str
    def choose_many(self, prompt, options, default=None) -> list[str]
    def confirm(self, prompt, default=True) -> bool; def secret(self, prompt) -> str   # getpass
class ScriptedPrompter(Prompter): __init__(answers: list); transcript: list[str]
def run_wizard(root: Path, prompter: Prompter, *, library=None, read_host_key=None) -> int
    # read_host_key(host, port) -> str | dict; default delivery.host_key_fingerprints
```
Steps, saving as it goes: site folder (warn on synced folders; `FOLIO_EXCHANGE_SITE` when no folder is given) → FOLIO URL, tenant, user (an existing `site.yaml` is read through `load_site`, so `${ENV}` values and overrides work) → password (`FOLIO_PASSWORD`, the stored one, or typed) → a warning when `verify_tls` is false (`TLS_OFF_WARNING`) → test login with `connect()` (its client built with `tls_verify`), and only then **store a typed password** and **write `site.yaml`** (with a `NOTE` when the address or tenant changed: feeds that have delivered hold their next run) → choose a recipe and feed name (a second feed on the same recipe gets `file_prefix` = its name), then **write a new feed, paused** → `probe()` the recipe's reads: missing permissions are listed for the FOLIO administrator, other failures are named, and setup stops (progress kept) → filters from the tenant's own location codes and common item statuses → legacy note types from the tenant's note types → preview of 10 bibs → delivery: SFTP host, port, user, folder; read every host key the server offers (one per type) and show them, then *matches the vendor's* (with several keys, which one does; pin it with its `host_key_type`), *not confirmed yet* (pin the preferred key with `host_key_confirmed: false`, keep the feed paused, never log in) or *differs* (stop, delivery off) → save the delivery → SFTP password into the keyring (blank: later, feed paused; no keyring: an `env:NAME` reference) → test the destination unless the key is unconfirmed (uploads nothing) → schedule (a feed the person paused with `feeds pause` stays paused unless they choose to resume it) → alerts by email or webhook, with a test message → print the next steps (`run`, `release`, `schedule`) and a `setx FOLIO_EXCHANGE_SITE` tip on Windows. Every step can be re-run; existing answers are offered as defaults; every write is audited. A site or feed file that cannot be read stops setup without being changed, and a hand-edited file (one with comments init did not write) is kept as `<name>.yaml.bak` before it is rewritten. Blank secrets mean *later* everywhere (SFTP, SMTP, webhook), and end of input stops the wizard.

## The local page (`folio-exchange ui`)

Started early (26 Oct 2026), ahead of the phase 2 web UI, as a simple companion to the command line. The layout follows Postman's: feeds in a sidebar with a status label each (OK, Held, Blocked, Fail, Built, New, Off), a request-style bar per feed (recipe, destination, **Preview**, and **Run** with a *Build only* choice), tabs for Overview, Layout, Settings and History, and a result panel below with the run's status, time, counts, item rows, summary (with the report's `warnings`, for runs and previews) and live log. *Connection* runs the same probes as `check`; *Activity* shows the audit log. A held, blocked or built run gets a **Review and send…** button, which shows the same summary as `release` and sends only after the person confirms.

- **No new dependencies.** Python's own `http.server` serves `web/index.html`, `app.css` and `app.js` from the package; nothing is loaded from the internet, and there is no build step. The page reuses the runner, so it adds no new way to reach FOLIO or a vendor.
- **Who can use it.** It listens on 127.0.0.1, and on Windows it holds its port exclusively (`SO_EXCLUSIVEADDRUSE`): with http.server's default `SO_REUSEADDR`, a second copy could bind the same port and requests would reach either one. Any program or Windows account on the PC can reach loopback, so being on this computer is not enough:
  - Each run of the server has a host name of its own, `fx-<16 random hex>.localhost`. Chrome, Edge and Firefox send any `*.localhost` name to this computer themselves (checked with Edge, Firefox and the Chromium in-app browser; Windows' own resolver does not, which is why tools use 127.0.0.1). The browser is opened at a one-time link on that name, `http://fx-<random>.localhost:<port>/?t=<code>`. Each code works once, within 10 minutes (`CODE_SECONDS`). A good code is swapped for the cookie `fx_<port>` (HttpOnly, SameSite=Strict, Path=/, no Domain, kept until the browser closes) and a 303 to `/` on the same name; a wrong, used or expired code also redirects, but sets nothing. With no Domain the cookie belongs to that one host name, so the browser never sends it to 127.0.0.1, localhost or any other server on this computer; the name is never told to anyone but the browser and the server's own window (browsers send a cookie to every port under its host name).
  - When the browser is opened for the person, the window also prints a second one-time link, for another browser on this computer; the plain address it prints lands signed out. If a browser cannot find the address (an old browser, or a proxy that does not let `*.localhost` through), the window says to use Chrome, Edge or Firefox, or to ask IT to let `*.localhost` bypass the proxy.
  - `/`, `/index.html`, every `/api/` GET and every POST need that cookie. Without it, `/` is a short page (`web/signed-out.html`, with no token) saying to use the launcher again, and `/api/*` answers 403 with `"signed_out": true`; the page then shows one notice and stops refreshing and polling. A restarted server has a new cookie, so it signs every open tab out.
  - Only `/static/` files and `GET /api/whoami` (the app name, version and a SHA-256 of the site's resolved path) are open. A second start of the page proves who is on the port before trusting it: it sends `whoami?nonce=<random>`, and the page answers with `proof`, an HMAC-SHA256 of `folio-exchange whoami <port> <nonce>` under the site's page secret, and a one-time `challenge` (60 seconds). The secret is 32 random bytes in `<data>/page.secret` (the site's data folder), made on first use with `setupops._owner_only` before anything is written into it, so only this Windows account can read it; naming the port stops a program on another port passing the nonce on to the real page. `POST /api/launch` needs no cookie but needs `{challenge, proof}` with the proof made the same way for `launch` (else 403); it makes the running server show a fresh link itself (in the browser, or printed in its own window), never to the asker, at most once every 5 seconds. So another program or Windows account can neither pass for the page nor make the operator's browser open.
  - When 8765 is held by something that does not prove it is this site's page, the page moves to a free port and the window says so, with the risk in one sentence: whatever holds 8765 could pretend to be this page and see what is typed into it, so open the page only from the window's links.
  - Every action is a POST that also carries the per-session token written into the page (compared in constant time), and a request from another origin is refused. Every request must name the server's own `fx-….localhost:<port>`, `127.0.0.1:<port>` or `localhost:<port>` in its Host header (against DNS rebinding); the last two are for tools, which send the cookie themselves.
  - `Content-Length` must be plain ASCII digits (else 400, before anything is read) and at most 64 KiB (else 413, never cut short). A request that stops half way times out after 30 seconds.
  - Responses carry a strict Content-Security-Policy (`script-src 'self'`, no inline scripts or styles), `X-Frame-Options: DENY` and `no-store`.
- **Secrets go in, never out.** The page is told where each secret comes from, never its value. Password, passphrase and key fields are write-only (`autocomplete="new-password"`, which asks browsers not to fill them in; a browser's password manager may still offer to save what is typed), cleared after sending, and cleared when the person leaves the form; they are never kept in the page's state; the server stores them (see Secrets) and no response, log line or audit entry carries one. It shows item rows only from files recorded for a run, and every value from FOLIO is set as text, never as HTML.
- **Setting up without the command line.** On a folder with no `site.yaml` the page shows only *Connect to FOLIO*. After that: *Add a vendor feed* (choose an installed recipe, name it, check the account's permissions, choose locations and statuses from the tenant's own lists, a schedule) saves a paused feed with no delivery, a dry run that can send nothing; the *Settings* tab edits the everyday settings with a before-and-after review; the *Delivery* tab sets the destination (for SFTP, looking for the vendor's fingerprint among every key the server offers, pinning the matching one with its type and listing them all when there are several), stores the password or private key, tests the login and resumes the feed; the *Recipes* view lists installed recipes and checks the vendor sources for new versions. When *Change connection* points the site at another FOLIO address or tenant and a feed has delivered before, the *Connection* view says that those feeds hold their next run. A settings save that would make a feed send the same file names to the same place as another feed is refused. Layout changes stay with the recipe (`upgrade`), and alert settings stay in `site.yaml` and `init`.
- **Setup API** (JSON): `GET /api/setup`, `/api/choices`, `/api/recipes`, `/api/permissions?uses=`, `/api/feeds/<name>/settings`; `POST /api/setup/connection`, `/api/feeds` (add), `/api/feeds/<name>/{settings,delivery,password,private-key,test-delivery}`, `/api/recipes/{check,install}`. Saves carry the file's version and are refused when the file changed underneath the form.
- **One action at a time.** Check, preview, run, build and release run in a background thread; a second action while one runs is refused (HTTP 409) and shown as *busy*, not as a failure. The page polls `/api/jobs/<id>` for log lines and the result, updating only the result panel in place (scroll, selection and focus stay). `/api/site` names the running job, so a reload or a second tab reattaches to it. Results are kept per feed, and a late answer never draws over a newer view. Ctrl+Enter previews (it never sends) and is ignored while a dialog is open. Ctrl+C in the server window waits for a running job to finish (a second Ctrl+C stops anyway).
- **Status means the last real run.** Previews show in History but never set a feed's label or "Last run".
- **API** (JSON): `GET /api/site`, `/api/feeds/<name>`, `/api/feeds/<name>/runs`, `/api/runs/<id>`, `/api/runs/<id>/rows`, `/api/audit`, `/api/jobs/<id>`, `/api/whoami`; `POST /api/check`, `/api/feeds/<name>/{preview,run,build,release-preview,release,pause,resume}`, `/api/runs/<id>/open-folder` (opens a run folder inside `out/` in the file browser; never a shell), `/api/launch`. `limit` is kept within 1..500 for runs and the audit log and 1..1000 for rows, and a job's `since` within 0 and its number of log lines. `release` refuses any body that carries `force`, even false: sending past the safety checks stays on the command line, where it is audited. `/api/site` carries `warnings` from `feedops.site_warnings`; the page words them by `code` and drops a command line only where the page has its own button for that step. It also carries `task` from `feedops.task_status`, whose text the *Connection* view shows quietly when no `task-*` warning already says it.
- **Keyboard and screen readers.** Tabs are ARIA tablists (arrow keys, Home, End), the Run menu is a menu button (arrow keys, Escape returns focus), and History rows are buttons. Text and badges meet WCAG 2.2 AA contrast in both schemes (a test checks the colour pairs).
- **Fresh without a reload.** The sidebar and the open feed refresh every 45 seconds and when the tab becomes visible, skipping while hidden, while an action runs, or while a dialog, menu or drop-down is open; scroll and focus stay.
- **Narrow windows** (under 760 px) scroll as one page instead of three nested panes.
- **Not yet:** alert settings, recipe upgrades to a new major version, removing a feed, and layout changes. Those stay on the command line or in the files.

## From FOLIOHQ

FOLIOHQ's original tool, `folio-chq-extract`, is now in its `legacy/` folder. FOLIO-Exchange reads none of its files; a library moving over runs `init` and maps its settings by hand (FOLIOHQ's `legacy/README.md` lists the steps). The item-ratio baseline does not carry over, and the first delivery is held, so the first run deserves a careful look.

| FOLIOHQ (`config.yaml`) | FOLIO-Exchange |
| --- | --- |
| `folio.*` (minus `password`) | `site.yaml` `folio.*`; the password goes to the keyring (`secret set folio`) or `FOLIO_PASSWORD` |
| `extract.mode`, `full_refresh_days` | `site.yaml` `sync.*` |
| `extract.cache_path`, `output.directory` | `site.yaml` `paths.data`, `paths.out` |
| `extract.include_*`, `exclude_*`, `bibs_without_items` | feed `filters` |
| `extract.loan_window_months`, `last_circ_date_from`; `items_csv.timezone` | feed `options` |
| `legacy.*` | feed `legacy.*`, read strictly here (see "Legacy notes"); `legacy.date_format` is new |
| `output.file_prefix` | feed `file_prefix` (`date_stamp_format` is fixed: `{date}` = UTC `YYYYMMDD`) |
| `output.min_item_count_ratio` | `checks.min_item_ratio` |
| `output.marc.item_field_tag`, `item_subfields`, `strip_existing_item_field`, `max_items_per_record` | `outputs.bibs.item_field.{tag, subfields, strip_existing, max_per_record}` |
| `output.items_csv.null_value`, `file_extension` | `outputs.items.empty_value`, `extension` |
| `delivery.method` | `delivery.transport` |
| `delivery.sftp.*` (minus secrets); `known_hosts_path`, `private_key_path` | feed `delivery.*`; `known_hosts`, `private_key` |
| `SFTP_PASSWORD`, `SFTP_KEY_PASSPHRASE` | `secret set <feed>`, or `secret: env:SFTP_PASSWORD` in the feed |
| `delivery.keep_local_runs` | `site.yaml` `runs.keep_local_runs` |
| `notify.webhook_url` (`NOTIFY_WEBHOOK_URL`) | `alerts.webhook: env:NOTIFY_WEBHOOK_URL` (or `keyring`) |
| keyring services `folio-chq-folio`, `folio-chq-sftp` (account = user name) | `folio-exchange/folio` (`user@tenant`), `folio-exchange/sftp` (`user@host`); store them again with `secret set` |
| `folio-chq check --sftp` | `folio-exchange check --delivery` (`--sftp` still works) |

## Testing

- Everything runs offline against `tests/mock_folio.py` (login, keyset paging, `updatedDate` deltas, `field==("a" or "b")` lookups, SRS by instance id; its loans carry a `userId` so tests can prove it is dropped). SFTP is mocked; the keyring is blocked by an autouse fixture.
- `tests/sitekit.py` writes site and feed files; `conftest.py` gives `folio` (a running mock) and `site` (a site folder with a `chq` feed delivering to a local folder).
- FOLIOHQ's tests are ported (`test_end_to_end.py`, `test_units.py`), including its regression tests: fields over 9,999 bytes, refusing to deliver a partial or failed run, login retry, the legacy date off-by-one, stale MARC after a source change, the atomic SFTP pair, direct mode, upload-only listing, credentials.
- Regression tests for the build review's findings (`test_regressions.py`) and the operator walkthrough (`test_walkthrough.py`).
- Improvement-plan batch 1: the layout hold and recipe copies (`test_layout_hold.py`), the hourly task's health and the heartbeat (`test_heartbeat.py`), TLS settings, staff data and folder access (`test_hardening.py`), the page's sign-in and request limits (`test_ui.py`).
- The second review round: legacy notes and date overflow (`test_review2_values.py`), unattended runs (`test_review2_unattended.py`), host key types, the FOLIO-source hold, the cache on a full disk, moved sites, alert URLs and the Windows task (`test_review2_keys.py`), settings audit, collisions, upgrades and recipe sources (`test_review2_settings.py`).
- CI installs with `pip install -c constraints.txt -e ".[dev]"`, the dependency versions the tests pass with, and pins every GitHub Action to a full commit SHA (the tag in a comment).
- New: recipes and overlay (`test_recipe.py`), patron and read-only guards, redirects, TLS and secrets (`test_safety.py`), schedules across daylight saving and the due rules (`test_schedule.py`), holds, release, scheduled runs, alerts, audit and crash recovery (`test_runner.py`), the wizard (`test_wizard.py`), schemas (`test_schemas.py`).
- Every bundled recipe has a fixture test against the mock (`test_chq_academic_psv_layout`).
- Byte parity with FOLIOHQ's legacy tool is checked in FOLIOHQ's CI, which runs both tools on the same mock data with FOLIO-Exchange's main branch. Run it there when the builders change.
- Every bug fix gets a regression test.

## Deliberately not in this build

Reviewed and left for later, each with a reason:

- **The full phase 2 web UI** (setup, editing settings and storing secrets in the page, more than one user), **inbound loads (v2), patron-data approvals** – roadmap; the local page above only looks and runs. Patron data is refused outright until the open question is answered.
- **Transports beyond SFTP and a local folder (DEL-3), MARCXML/JSON/KBART builders (MAP-5)** – roadmap.
- **A 9XX barcode link (MAP-4's third option)** – needs embedded items and a per-row check; add with the first vendor that links that way.
- **Changed-records and deletes outputs** (`scope: changed`, a deletes file) – discovery layers and OCLC usually want them. They need per-feed delivered state; this is the main known limit for gate 2 ("a second recipe with no core code changes") and should be decided when the partner library picks the second vendor.
- **Eureka capability names in `check`** – `check` and the wizard list Okapi-style permission names, which Eureka maps to capabilities; printing the capability sets themselves waits until they are verified on the reference system.
- **`recipes import/export`, `init --from-foliohq`, `init --step`** – recipes are shared through Git for now; the FOLIOHQ mapping table covers one-off migration; re-running `init` keeps earlier answers.
- **Quoting options for delimited files** – v1 uses minimal quoting (FOLIOHQ's), which every current vendor accepts.
- **A Docker image and a `serve` loop** – containers have neither a scheduler nor a keyring; a later image will run `run --due` hourly with `env:` secrets.
- **Translated messages** – strings are plain English; routing them through `gettext` waits for phase 2.
- **Signed releases, a release tag for installs, and CI against the public reference systems** – after gate 1. Until then the guide and the installer install `main`, with `constraints.txt` fixing the dependency versions.
