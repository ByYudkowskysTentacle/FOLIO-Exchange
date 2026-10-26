# Design: how v1 is built

_Started 26 Oct 2026; revised 26 Oct 2026 after a three-lens design review (spec coverage, safety and correctness, operator UX and FOLIOHQ parity). This is the build plan behind `docs/spec.md`: files, modules and their contracts. When code and this file disagree, fix one of them in the same change._

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

**Done means byte parity.** With the `chq-academic` recipe and FOLIOHQ's `config.example.yaml` layout, FOLIO-Exchange writes byte-identical `.mrc` and `.psv` files to FOLIOHQ from the same data (checked on 26 Oct 2026 against the shared mock FOLIO).

## What a library keeps: the site folder

Everything for one FOLIO tenant lives in one folder, the **site**. Commands find it from `-s/--site`, else `FOLIO_EXCHANGE_SITE`, else the current directory. It must contain `site.yaml`. The wizard warns against cloud-synced folders (OneDrive, Dropbox), where SQLite and the lock break, and suggests a plain local folder such as `C:\FOLIO-Exchange\<tenant>`.

```
site.yaml                    FOLIO connection, folders, sync, alerts. No secrets.
feeds/<name>.yaml            one feed each: local settings over a shared recipe
recipes/<id>/<version>.yaml  optional local or community recipes (searched before the bundled library)
data/cache.sqlite            local cache, shared by every feed
data/history.sqlite          run history, audit log, and small bookkeeping (meta)
data/run.lock                held while a run is in progress
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
  verify_tls: true
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
```

`${NAME}` and `${NAME:-default}` in any string are replaced from the environment (FOLIOHQ behaviour), and `FOLIO_URL`, `FOLIO_TENANT` and `FOLIO_USERNAME` override the `folio` block, so containers need no file edits.

**YAML.** Every file is read with only `true` and `false` as booleans (YAML 1.2 style), so a setting named `on` and codes such as `NO`, `ON` or `Y` stay text. Numbers that look like codes (`0123`) must be quoted; validation says so.

## Secrets

Secrets never appear in `site.yaml`, feed files, recipes, logs, reports or alerts. A file that contains a key named `password`, `passphrase`, `private_key_passphrase`, `token`, `api_key` or `webhook_url` is rejected outright, whatever its value. Wherever a secret is needed, the file holds a **secret reference**:

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

`folio-exchange secret set folio|smtp|webhook|<feed>` prompts with `getpass` and stores the value in the keyring; nothing is echoed or logged, and the audit log records that a secret was stored (never its value). `secret status` says where each comes from. Feed names `folio`, `smtp`, `webhook` and `all` are reserved so `secret set` is never ambiguous. Tests block the real keyring (autouse fixture in `tests/conftest.py`).

**Keyrings and schedulers.** Keyring entries belong to the operating-system user who stored them. On Windows the hourly task must run as that user, "whether logged on or not", with the user's password given to Task Scheduler (`schedule` prints a `schtasks /RU ... /RP` command that does this). Under cron or systemd on Linux there is often no Secret Service session: use `env:NAME` references and an environment file there. `secret status` names the current user to make this visible.

**TLS.** The FOLIO base URL must be `https://` (plain `http://` only for localhost testing). SMTP logins need STARTTLS or port 465 (SMTP over TLS); the password is never sent in clear. The client never follows redirects (they could carry the token or a re-sent login to another host): a 3xx is an error naming the address to use instead.

## Recipes

A recipe is two files (spec, "What an integration recipe holds"): a shared one, versioned and reviewed in Git, and the library's local feed file. Upgrading the shared recipe never touches local settings.

### The recipe library

Shared recipes live at `<library dir>/<recipe id>/<version>.yaml`, one file per published version, so a feed can stay on 1.x while 2.0 waits for review. The bundled library is `src/folio_exchange/recipes/` and never removes a published version. A site searches `paths.recipes` first, then the bundled library; on an identical id and version, the earlier folder wins. A recipe folder may also hold `README.md` (notes for recipe authors) and `samples/<output name>.<ext>` (the vendor's sample files, used by the conformance check).

`uses: <id>@<range>` picks the highest version that matches the range:

| Range | Matches |
| --- | --- |
| `1.2.0` | exactly 1.2.0 |
| `^1.2` or `^1.2.0` | ≥ 1.2.0 and < 2.0.0 (minor updates automatically; majors held for review) |
| `^0.3` | ≥ 0.3.0 and < 0.4.0 |
| `~1.2` | ≥ 1.2.0 and < 1.3.0 |

A bare id with no range is an error: the operator must choose. When a newer version exists than the feed's range allows, `check` and `feeds` say so; the feed keeps running on the version it matches until `folio-exchange upgrade FEED` moves it.

**Versioning rule for recipe authors.** A minor or patch version must not change the bytes of any existing output: fixes to descriptions, validation, defaults that only apply to new feeds. Anything a vendor would notice – a column added, removed, renamed or moved, a delimiter, empty value or date format, the link – is a new **major** version, because the vendor must accept it again and the first delivery on it is held.

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

`feeds/<name>.yaml`; the feed's name is the file name without `.yaml` (lowercase letters, digits, hyphens; not `folio`, `smtp`, `webhook` or `all`). `examples/feeds/chq.yaml` is a commented example.

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
  secret: keyring                # password (or key passphrase): keyring | env:NAME
  private_key: null              # path to a key file, instead of a password
  upload_mode: direct            # optional override
  directory: null                # the folder, for transport: local
schedule: Sun 03:30              # optional; else the recipe's default_schedule
```

**Overlay rules.** `filters` and `options` replace individual keys. `outputs.<name>` deep-merges into the recipe's output of that name; a `columns` list replaces the recipe's list wholesale, but every recipe column marked `required: true` must still be present (matched by `field`). `checks.min_item_ratio` replaces; `checks.must_be_filled` adds. `delivery` merges over the recipe's. The merged result is validated with the same rules as a recipe, and errors name the feed file. Transport `sftp` needs `host`, `username` and a pinned `host_key_fingerprint` (or `known_hosts`); `local` needs `directory`. Relative paths in `delivery` (`directory`, `private_key`, `known_hosts`) are taken from the site folder, never from wherever the scheduler started the process.

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

## Packages and modules

Flat, small modules under `src/folio_exchange/`, as in FOLIOHQ. Copied FOLIOHQ code keeps its tested behaviour unless this section says otherwise.

| Module | Owns |
| --- | --- |
| `errors.py` | `FxError` and subclasses: `ConfigError`, `RecipeError`, `PatronDataRefused`, `SafetyCheckFailed`, `HeldForRelease`, `DeliveryError` (carries `sent`), `LockBusy`. |
| `dates.py` | FOLIOHQ's date helpers, unchanged. |
| `credentials.py` | Secret references, keyring access, `secret set`. |
| `config.py` | `site.yaml`: load, defaults, `${ENV}`, validation, secret-key rejection, YAML loader, write. |
| `recipe.py` | Recipe library, versions and ranges, validation, feed overlay. |
| `reads.py` | The reads registry, reference data, permission names, path allowlist, patron-path denylist. |
| `client.py` | FOLIOHQ's `FolioClient` plus the guard (patron, allowlist, read-only), no redirects, `iter_where_in`. |
| `cache.py` | FOLIOHQ's SQLite cache, plus `sync_state` and `marc_source_without_marc`. |
| `sync.py` | Full and delta sync of the reads given; `sample()` for previews. |
| `fields.py` | The field registry (MAP-1) and `RowBuilder`. |
| `marc.py` | MARC 21 building (from FOLIOHQ), link enforcement. |
| `delimited.py` | Delimited text output. |
| `build.py` | One streaming pass from the cache to every output of a feed. |
| `checks.py` | Required fields, item-count ratio, MARC intake check, conformance with vendor samples. |
| `delivery.py` | SFTP (from FOLIOHQ), local folder, destination keys, host key lookup, run pruning. |
| `notify.py` | Alerts by webhook and email. |
| `history.py` | Run history, audit log and meta in SQLite; file digests. |
| `lock.py` | One run at a time per site. |
| `schedule.py` | Schedule text, due runs, OS scheduler commands. |
| `runner.py` | The pipeline for one feed, `--due` runs, release, preview, settings audit, alerts. |
| `cli.py` | `folio-exchange` commands. |
| `wizard.py` | `folio-exchange init`, the guided setup. |

### Contracts

Signatures are the contract between modules; keep them, or change every caller and this section together. `Site` and `Feed` are dataclasses; sections inside them stay plain dicts, as in FOLIOHQ.

**credentials.py**
```python
FOLIO_SERVICE = "folio-exchange/folio"; SFTP_SERVICE = "folio-exchange/sftp"
SMTP_SERVICE = "folio-exchange/smtp"; WEBHOOK_SERVICE = "folio-exchange/webhook"
SECRET_KEYS = frozenset({"password", "passphrase", "private_key_passphrase", "token", "api_key", "webhook_url"})
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
```

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
- Messages never include the password. The empty-password message names `FOLIO_PASSWORD` and `folio-exchange secret set folio`. Default user agent `folio-exchange/<version>`.

**cache.py** – FOLIOHQ's, unchanged, plus `sync_state(entity) -> str | None` ("never synced" or "the last full sync did not finish") and `marc_source_without_marc() -> list[str]`. Loans never store a borrower: the table has no user columns, and sync drops `userId`, `proxyUserId` and related keys on arrival (tested).

**sync.py**
```python
@dataclass class SyncReport: mode, fetched, removed: dict; marc_fetched, marc_missing: int; seconds: float
class Syncer:
    def __init__(self, client: FolioClient, cache: Cache, site: Site) -> None
    def run(self, reads: Iterable[str], force_full: bool = False) -> SyncReport
    def sample(self, reads: Iterable[str], instances: int) -> SyncReport   # previews: the first instances by id
        # that have holdings, with their holdings, items, loans and MARC, into this (temporary) cache
def reads_to_sync(feeds: list[Feed]) -> set[str]   # the union over enabled feeds
```
Reference data always syncs; when a table cannot be loaded, the copy from the last sync is used, and with no copy the sync fails (names and codes go into the files). Entities sync in `ENTITY_ORDER`, limited to `reads`; MARC is fetched for MARC-source instances when `marc` is in `reads`, and each sync retries MARC-source instances that still have none. FOLIOHQ's full/delta/auto rules, `incomplete` flags and 10-minute overlap are kept, and:
- A full sync compares what it fetched with FOLIO's count before removing anything; a shortfall (beyond 0.1 % or 5 records) fails the sync and leaves the entity marked incomplete, so a truncated download never empties the cache.
- A delta falls back to a full sync when the cache holds more records than FOLIO (deletions) or noticeably fewer.
- When `marc` joins the reads after a sync without it, the next instance sync is full, so older bibs get their MARC.

**Shared cache.** All feeds share one cache, and sync state is per entity. So a run always syncs the union of the reads of every enabled feed and of the feeds being run: otherwise one feed could advance `last_sync` and leave another feed's data stale. A feed's own `reads` still limit what it may output, and `build` refuses to run when any of the feed's reads has never fully synced.

**fields.py**
```python
@dataclass(frozen=True)
class FieldDef: name: str; description: str; needs: frozenset[str]; date: bool = False
FIELDS: dict[str, FieldDef]       # FOLIOHQ's 47 item fields; loan-derived fields need "loans"
ITEM_FIELDS: dict[str, str]; DATE_FIELDS: set[str]
def suggest(name: str) -> str | None             # closest field name, for "did you mean"
class Ref: (FOLIOHQ's, unchanged)
class RowBuilder:
    def __init__(self, ref: Ref, options: dict, legacy: dict | None) -> None   # options.timezone also reads legacy dates
    def build(self, item, holdings, instance, loans) -> dict                  # unchanged
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
    outputs: dict[str, list[str]] (output name -> file names) and unfilled: Counter (must_be_filled field -> rows)
def build(cache: Cache, feed: Feed, run_dir: Path, *, limit_bibs: int | None = None,
          stamp: str | None = None, name_prefix: str = "") -> BuildReport
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
Blocked messages give the last delivered and current counts and the exact command to release the run.

**delivery.py**
```python
def sha256_fingerprint(key) -> str
def host_key_fingerprint(host: str, port: int = 22, timeout: float = 30) -> str   # reads the server key; no login
def destination_key(d: dict) -> str          # "sftp://user@host:port/dir", a file: URI, or "none"
def describe_destination(d: dict) -> str
def deliver(files: list[Path], delivery: dict, feed_name: str) -> list[str]     # destinations. FxError when it
    # fails before anything reached the destination (a login or host-key failure); DeliveryError (with .sent)
    # once the destination's copy may have changed (a direct upload started, a rename or local move done)
def check_delivery(delivery: dict) -> list[str] | None   # sftp: login + list (None when listing is refused);
                                                          # local: folder exists and is writable; uploads nothing
def prune_runs(feed_out_dir: Path, keep: int, protect: set[Path] | None = None) -> None
```
SFTP keeps FOLIOHQ's pinned key or `known_hosts` (never auto-accept), staged and direct modes. The password comes from `credentials.resolve(delivery["secret"], SFTP_SERVICE, sftp_account(...))`. `local` copies the files into `delivery["directory"]`, each as `.part` then renamed.

**notify.py**
```python
def send(alerts: dict, subject: str, text: str, *, account: str = "default") -> list[str]
    # channels used ("webhook", "email"); never raises. account = the keyring account of the webhook URL
    # (runner passes the site folder name)
def should_alert(alerts: dict, status: str) -> bool   # on: failure -> any status but success, built and preview
```
Webhook: `POST {"text": ...}` (Slack-style), or with `webhook_format: teams` a message with an Adaptive Card, as Teams workflows expect. Email: `smtplib` with STARTTLS (or SMTP over TLS on port 465) and optional login. Neither ever includes a secret, and the HTTP library's request logging stays at WARNING even with `-v`, so webhook URLs never reach the log.

**history.py**
```python
class History:                               # also a context manager
    def __init__(self, path: Path) -> None
    def start_run(self, feed, recipe, recipe_version, trigger, run_dir, destination=None) -> int
    def finish_run(self, run_id, status, *, items, bibs, files: list[dict], delivered: list[str], error, report)
    def mark_delivered(self, run_id, status, delivered, files, report=None) -> None   # release of a run
    def fail_stale(self) -> int              # runs left "running" by a crash -> failed (call under the lock)
    def last_delivered(self, feed, destination=None) -> dict | None   # baseline and first-delivery hold
    def last_run(feed) / runs(feed=None, limit=20) / runs_since(feed, since) / run_by_dir(path) / get(id)
    def audit(self, actor, feed, action, detail) -> None; def audit_log(self, limit=50, feed=None) -> list[dict]
    def get_meta(key, default=None) / set_meta(key, value)   # settings snapshots, when a feed was first seen
    def prune(self, months: int) -> int      # keeps each feed's last delivered run
def file_digest(path: Path) -> dict          # {"name", "bytes", "sha256"}
def current_user() -> str
```
Statuses: `running`, `success` (delivered, or transport `none`), `built` (`--no-deliver` or `build`), `blocked`, `held`, `failed`, `preview`. Triggers: `manual`, `schedule`, `preview`. Every run records its files with sizes and SHA-256 hashes, its destination key and its recipe version; every delivery records where each file went (spec, Audit).

**lock.py** – `class RunLock: __init__(path: Path); __enter__/__exit__` with an OS file lock (`msvcrt` on Windows, `fcntl` elsewhere); a second holder gets `LockBusy` at once. `run_many`, `release` and `sync` take it; `run_feed` expects its caller to hold it.

**schedule.py**
```python
@dataclass(frozen=True) class Schedule: kind: str; days: tuple[int, ...]; hour: int; minute: int; text: str
def parse(text: str) -> Schedule                    # ConfigError with the accepted forms
def last_slot(s, now, tz) -> datetime | None; def next_slot(s, now, tz) -> datetime | None   # aware UTC
def describe(s: Schedule) -> str                    # "Sundays at 03:30"
def is_due(s, now, tz, runs: list[dict], max_attempts: int, created: datetime | None) -> bool
    # runs: the feed's runs since the slot; created: when the site first saw the feed (earlier slots never due)
def os_command(site_root, executable, system, user=None) -> str   # schtasks /SC HOURLY ... /RU user /RP (Windows),
                                                                    # crontab line at minute 7 (Linux, macOS)
def os_helpers(site_root, system) -> dict[str, str]   # run now, last result, remove (Windows)
```

**runner.py**
```python
@dataclass class RunOptions: full=False; no_deliver=False; skip_sync=False; force=False; trigger="manual"
@dataclass class RunResult: feed: str; status: str; code: int; run_dir: Path | None; report: dict
def run_feed(site, feed, opts, *, hist: History, sync_report: dict | None = None) -> RunResult  # lock held by caller
def run_many(site, feeds, opts, *, all_feeds=None, bad_feeds=None) -> list[RunResult]   # one lock, one sync
def due_feeds(site, feeds, now, hist) -> list[Feed]
def release(site, feed, run_dir: Path | None, *, force=False, actor=None, confirm=None) -> RunResult
def preview(site, feed, instances=10) -> RunResult  # temporary cache; PREVIEW_ files in out/<feed>/preview-…
def hold_reason(hist, feed) -> str | None; def overdue(site, feed, hist, now) -> str | None
def audit_settings(site, hist) -> list[str]; def alert(site, feed, status, text, *, force=False) -> list[str]
def worst_code(results) -> int                      # 1 if any failed, else 2 if any blocked or held, else 0
```

`run_many`: take the lock → mark stale `running` rows failed (and alert) → audit hand edits → prune history → report problem feed files that are not paused as failed (alerting once per distinct problem) while good feeds still run; one unreadable file never stops the others → one sync of the union of reads → `run_feed` for each feed. A failed sync fails every feed once, with one alert.

`run_feed`: history start → sync (unless `skip_sync` or a `sync_report` was passed) → refuse if the cache is not ready for the feed's reads → build → required fields → item ratio against `last_delivered(feed, destination)` (unless `force`, which is audited) → `--no-deliver` ends `built`; transport `none` ends `success` → **hold** → deliver → history finish → prune → alert. Checks run before every status, so a `built` run has passed them too.

**First-delivery hold.** A delivery waits for a person (status `held`) when the feed has never delivered to this destination (transport, host, port and folder, or local directory), or when its recipe's major version differs from the last delivery's. A local test delivery therefore never disarms the hold for the real SFTP destination, and the item-ratio baseline always compares like with like.

**Release.** `release FEED [RUN_DIR]` (also `deliver`) takes the lock and sends the latest held, blocked or built run (previews and failed runs never). It first shows the run, its counts against the last delivery, the reason it waited, the files and the destination, and asks for confirmation (`--yes` for scripts). It re-checks every file's SHA-256 against the run record (a file opened and saved in Excel is refused), refuses if the feed's destination changed since the build, and runs the item-ratio check for a `built` run unless `--force`. Releasing a held or blocked run is itself the person's approval. The release is recorded in history and the audit log with who did it.

**Partial deliveries.** When a delivery stops after the vendor's copy may have changed (a direct upload started, a staged rename, or a local move done), the run fails with a message listing what the vendor already has, `partially_delivered` in the report, and an audit entry. A partial delivery never becomes the ratio baseline.

**Alerts.** The text names the site, feed, recipe version, status, counts, the error, the run folder and the next command, and never a secret. With `on: failure`, a successful run after a failed or blocked one sends a "recovered" message. `run --due` exits 0 without alerting when another run holds the lock (the next hourly start catches up); a manual run reports the lock and exits 1.

**Settings audit.** Every run and `check` compares `site.yaml` and each feed file with the snapshot kept in history; a change is written to the audit log as a key-level diff (`checks.min_item_ratio: 0.9 -> 0.5`), so hand edits are audited, not only wizard changes. Snapshots are redacted first: a secret-looking key, or a `secret` or `webhook` value that is not a reference (someone typed the secret itself), is masked, so a mistake in a file never copies a secret into history.

`run-report.json` is written in every run folder: FOLIOHQ's keys (`version`, `started`, `finished`, `run_dir`, `sync`, `build`, `delivered`, `status`, `error`) plus `feed`, `recipe`, `recipe_version`, `trigger`, `destination`, `files` (digests), `last_delivered_items`, and when relevant `held`, `next`, `partially_delivered`, `released_by`. It never holds a secret.

**cli.py** – global options `-s/--site`, `-v/--verbose`, `--version`. Logging to stderr and `logs/folio-exchange.log`; stdout is UTF-8. `run`, `build`, `sync` and `release` also print a short JSON summary, as FOLIOHQ did. `fields` and `recipes` work without a site.

| Command | Does |
| --- | --- |
| `init [FOLDER]` | Guided setup (`wizard.py`): connect, pick a recipe, filters, preview, delivery, schedule, alerts. |
| `check [FEED]` | Password source, login, every endpoint the enabled feeds need with record counts (DENIED names the missing permission), recipe updates held for review, feed and recipe file problems, overdue feeds, a 0 item ratio on a delivering feed, alerts switched off. |
| `check --delivery` (alias `--sftp`) | Tests each feed's vendor login and lists the folder, or checks the local folder; uploads nothing. Messages as FOLIOHQ's `check --sftp`. |
| `check --alerts` | Sends a test alert on every configured channel. |
| `preview FEED [-n 25] [--rows 3] [--columns ...]` | Sample from live data in a temporary cache; prints item rows one per block (header: value); writes `PREVIEW_` files; shows sample conformance. |
| `run FEED… \| --all \| --due` | Sync, build, check, hold or deliver. `--full`, `--no-deliver`, `--skip-sync`, `--force`. Exit code: the worst status. |
| `release FEED [RUN_DIR] [--yes] [--force]` | Deliver a held, blocked or built run after showing it (`deliver` is the same). |
| `sync [--full]` | Update the cache only. |
| `build FEED [--force]` | Write files from the cache without contacting FOLIO, and run the checks (status `built` or `blocked`). |
| `feeds [list \| pause FEED \| resume FEED]` | Each feed: recipe and version, schedule and next slot, destination, last run and the next step, held updates, overdue, problems. `pause`/`resume` set `enabled` (keeping comments) and audit it. |
| `recipes [list \| show ID[@RANGE] \| validate PATH \| summary ID[@RANGE]\|FEED]` | Browse and check recipes; `summary` prints the one-page layout for the vendor, from the recipe or from a feed's merged settings. |
| `upgrade FEED [--to RANGE] [--yes]` | Show the changelog and column changes, validate the feed against the new version, rewrite `uses:` (keeping comments) and audit it. The next delivery on a new major is held. |
| `fields` | The field registry with descriptions and what each needs. |
| `history [FEED] [-n 20] [--audit] [--show RUN]` | Run history, one run in plain language, or the audit log. |
| `secret set {folio\|smtp\|webhook\|FEED}` and `secret status` | Store a secret in the keyring; show where each comes from. |
| `schedule` | Next slot per feed, the exact command to register the hourly `run --due` with the OS, and commands to run it now, see its last result or remove it. Registers nothing. |

Exit codes: 0 success, built or nothing due; 1 failed (or a usage or settings problem, including a mistyped command); 2 blocked, held, or a release that was not confirmed. `release` refuses a run folder that belongs to another feed, and a released blocked run keeps its block reason as `released_after`, not as an error.

**wizard.py**
```python
class Prompter:                                  # console implementation (ConsolePrompter)
    def say(self, text) -> None; def ask(self, prompt, default=None, validate=None) -> str
    def choose(self, prompt, options: list[tuple[str, str]], default=None) -> str
    def choose_many(self, prompt, options, default=None) -> list[str]
    def confirm(self, prompt, default=True) -> bool; def secret(self, prompt) -> str   # getpass
class ScriptedPrompter(Prompter): __init__(answers: list); transcript: list[str]
def run_wizard(root: Path, prompter: Prompter, *, library=None, read_host_key=None) -> int
```
Steps, saving as it goes: site folder (warn on synced folders) → FOLIO URL, tenant, user → password into the keyring (or `FOLIO_PASSWORD`) → test login, then **write `site.yaml`** → choose a recipe and feed name, then **write the feed, paused** → test the recipe's permissions, listing the missing ones for the FOLIO administrator and stopping (progress kept) → filters from the tenant's own location codes and common item statuses → legacy note types from the tenant's note types → preview of 10 bibs → delivery: SFTP host, port, user, folder; read the server's host key and show it, then *matches the vendor's* (pin), *not confirmed yet* (pin, keep the feed paused) or *differs* (stop, delivery off) → SFTP password into the keyring → test the destination (uploads nothing) → schedule → alerts by email or webhook, with a test message → print the next steps (`run`, `release`, `schedule`) and a `setx FOLIO_EXCHANGE_SITE` tip on Windows. Every step can be re-run; existing answers are offered as defaults; every write is audited.

## From FOLIOHQ

FOLIO-Exchange reads none of FOLIOHQ's files; a library moving over runs `init` and maps its settings by hand. The item-ratio baseline does not carry over, and the first delivery is held, so the first run deserves a careful look.

| FOLIOHQ (`config.yaml`) | FOLIO-Exchange |
| --- | --- |
| `folio.*` (minus `password`) | `site.yaml` `folio.*`; the password goes to the keyring (`secret set folio`) or `FOLIO_PASSWORD` |
| `extract.mode`, `full_refresh_days` | `site.yaml` `sync.*` |
| `extract.cache_path`, `output.directory` | `site.yaml` `paths.data`, `paths.out` |
| `extract.include_*`, `exclude_*`, `bibs_without_items` | feed `filters` |
| `extract.loan_window_months`, `last_circ_date_from`; `items_csv.timezone` | feed `options` |
| `legacy.*` | feed `legacy.*` |
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
- Regression tests for the build review's findings (`test_regressions.py`).
- New: recipes and overlay (`test_recipe.py`), patron and read-only guards, redirects, TLS and secrets (`test_safety.py`), schedules across daylight saving and the due rules (`test_schedule.py`), holds, release, scheduled runs, alerts, audit and crash recovery (`test_runner.py`), the wizard (`test_wizard.py`), schemas (`test_schemas.py`).
- Every bundled recipe has a fixture test against the mock (`test_chq_academic_psv_layout`).
- Byte parity with FOLIOHQ is checked by running both tools on the mock data; repeat it when the builders change.
- Every bug fix gets a regression test.

## Deliberately not in this build

Reviewed and left for later, each with a reason:

- **Phase 2 web UI, inbound loads (v2), patron-data approvals** – roadmap; patron data is refused outright until the open question is answered.
- **Transports beyond SFTP and a local folder (DEL-3), MARCXML/JSON/KBART builders (MAP-5)** – roadmap.
- **A 9XX barcode link (MAP-4's third option)** – needs embedded items and a per-row check; add with the first vendor that links that way.
- **Changed-records and deletes outputs** (`scope: changed`, a deletes file) – discovery layers and OCLC usually want them. They need per-feed delivered state; this is the main known limit for gate 2 ("a second recipe with no core code changes") and should be decided when the partner library picks the second vendor.
- **Eureka capability names in `check`** – `check` and the wizard list Okapi-style permission names, which Eureka maps to capabilities; printing the capability sets themselves waits until they are verified on the reference system.
- **`recipes import/export`, `init --from-foliohq`, `init --step`** – recipes are shared through Git for now; the FOLIOHQ mapping table covers one-off migration; re-running `init` keeps earlier answers.
- **Quoting options for delimited files** – v1 uses minimal quoting (FOLIOHQ's), which every current vendor accepts.
- **A Docker image and a `serve` loop** – containers have neither a scheduler nor a keyring; a later image will run `run --due` hourly with `env:` secrets.
- **Translated messages** – strings are plain English; routing them through `gettext` waits for phase 2.
- **A Windows installer, signed releases, and CI against the public reference systems** – after gate 1.
