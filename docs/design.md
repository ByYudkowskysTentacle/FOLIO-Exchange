# Design: how v1 is built

_Started 26 Oct 2026. This is the build plan behind `docs/spec.md`: files, modules and their contracts. When code and this file disagree, fix one of them in the same change._

## Shape

FOLIO-Exchange v1 is a Python package and a guided command line, `folio-exchange`, that a systems librarian installs on one PC or server inside the library's network. It generalizes FOLIOHQ (`folio-chq-extract`): the same pipeline – sync, build, check, deliver – but every vendor-specific choice moves out of code into a **recipe**.

```
FOLIO (Eureka, Kong) ──> client ──> sync ──> cache (SQLite) ──> build ──> checks ──> delivery ──> vendor
                          ▲ read-only,           shared by        ▲ recipe +    ▲ counts,     ▲ SFTP (pinned key),
                          │ no patron paths      all feeds        │ local       │ required    │ local folder
                       secrets (env, keyring)                     │ settings    │ fields      │
                                                                  │             │             └─> history + audit, alerts
                                                     recipe library (versioned YAML)
```

A run is a batch job. The OS scheduler (Task Scheduler, cron or a systemd timer) starts `folio-exchange run --due` every hour; feeds whose slot has passed run, and missed slots catch up at the next start. Nothing stays resident.

## What a library keeps: the site folder

Everything for one FOLIO tenant lives in one folder, the **site**. Commands find it from `-s/--site`, else `FOLIO_EXCHANGE_SITE`, else the current directory. It must contain `site.yaml`.

```
site.yaml                    FOLIO connection, folders, sync, alerts. No secrets.
feeds/<name>.yaml            one feed each: local settings over a shared recipe
recipes/<id>/<version>.yaml  optional local or community recipes (searched before the bundled library)
data/cache.sqlite            local cache, shared by every feed
data/history.sqlite          run history and audit log
data/run.lock                held while a run is in progress
out/<feed>/<YYYYMMDD-HHMMSS>/   output files plus run-report.json, one folder per run
logs/folio-exchange.log
```

`site.yaml` with every default (paths are relative to the site folder):

```yaml
folio:
  base_url: https://folio-kong.example.edu   # the Kong gateway URL on Eureka (or Okapi URL)
  tenant: example
  username: fx_reader                        # a read-only service account
  secret: keyring                            # where the password lives: keyring | env:NAME. FOLIO_PASSWORD always wins.
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
  keep_local_runs: 5                         # run folders kept per feed
  history_months: 12                         # audit and history retention
  max_attempts_per_slot: 3                   # retries of a failed scheduled run within one slot
  timezone: null                             # schedules use this zone; null = this computer's local time
alerts:
  on: failure                                # failure | always | never
  webhook: null                              # keyring | env:NAME – a Slack or Teams incoming webhook URL
  email: null                                # or {to: [..], from: .., smtp_host: .., smtp_port: 587,
                                             #     starttls: true, username: null, secret: keyring}
```

`${NAME}` and `${NAME:-default}` in any string are replaced from the environment (FOLIOHQ behaviour), and `FOLIO_URL`, `FOLIO_TENANT` and `FOLIO_USERNAME` override the `folio` block, so containers need no file edits.

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

`folio-exchange secret set folio|smtp|webhook|<feed>` prompts with `getpass` and stores the value in the keyring; nothing is echoed or logged. Screens and `check` say where a secret comes from, never its value. Tests block the real keyring (autouse fixture in `tests/conftest.py`).

## Recipes

A recipe is two files (spec, "What an integration recipe holds"): a shared one, versioned and reviewed in Git, and the library's local feed file. Upgrading the shared recipe never touches local settings.

### The recipe library

Shared recipes live at `<library dir>/<recipe id>/<version>.yaml`, one file per published version, so a feed can stay on 1.x while 2.0 waits for review. The bundled library is `src/folio_exchange/recipes/`. A site searches `paths.recipes` first, then the bundled library; on an identical id and version, the earlier folder wins. A recipe folder may also hold `README.md` (notes for recipe authors) and a `fixtures/` folder.

`uses: <id>@<range>` picks the highest version that matches the range:

| Range | Matches |
| --- | --- |
| `1.2.0` | exactly 1.2.0 |
| `^1.2` or `^1.2.0` | ≥ 1.2.0 and < 2.0.0 (minor updates automatically; majors held for review) |
| `^0.3` | ≥ 0.3.0 and < 0.4.0 |
| `~1.2` | ≥ 1.2.0 and < 1.3.0 |

A bare id with no range is an error: the operator must choose. When a newer major version exists than the feed's range allows, `check` and `feeds` say so; the feed keeps running on the version it matches.

### Shared recipe format

```yaml
recipe: chq-academic             # id: lowercase letters, digits and hyphens; matches its folder name
version: 1.0.0                   # MAJOR.MINOR.PATCH; matches the file name. Minor = new optional column;
                                 # major = a layout the vendor must re-accept
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
  timezone: UTC                  # zone for dates written to files
outputs:                         # one or more; names are unique
  - name: bibs
    format: marc21               # ISO 2709, UTF-8 (MAP-3)
    file_name: "{prefix}_bibs_{date}"   # .mrc is added; split files get _001, _002 ...
    split_every: 0               # 0 = one file
    embed_items: false
    item_field:                  # used when embed_items is true
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
delivery:
  transport: sftp                # sftp | local | none. Plain FTP is refused.
  upload_mode: direct            # staged (.part, then rename) | direct
default_schedule: weekly         # used when the feed sets no schedule
```

`{date}` is the run's UTC date as `YYYYMMDD`; `{prefix}` is the feed's `file_prefix`, else the recipe's. Only those two placeholders exist.

**Validation** (plain-language messages that name the file and the path, e.g. `feeds/chq.yaml: outputs.items.columns[3].field: unknown field 'barcod' (did you mean 'barcode'?)`), checked on every load:

- Unknown keys are errors, at every level, in recipes and feed files alike.
- Every `field` (columns, item subfields, `link.value`, `must_be_filled`) exists in the field registry, and its `needs` are within `reads`.
- `reads` names exist in the reads registry. `patron_data` is `none`.
- `link`: `bib_field` is `"999 ff $i"` (value `instance_id`) or `"001"` (value `instance_hrid`); `item_column` is the header of a column in a delimited output whose field equals `value`. Required when a recipe has both a `marc21` and a `delimited` output.
- At most one `marc21` output in v1. Delimiter is one character. Extension is alphanumeric and not `mrc`. Item field tag is a 3-digit data field tag (010–999). `date_format` is a valid `strftime` pattern that renders a sample date.
- `delivery.transport` is `sftp`, `local` or `none` (`ftp` gets its own message: plain FTP is never offered). `upload_mode` is `staged` or `direct`.
- `version` matches the file name, `recipe` matches the folder, and `changelog` has an entry for `version`.
- The JSON Schemas in `src/folio_exchange/schemas/` describe the same rules for editors; a test keeps them in step with the bundled recipes and example files.

### The feed file (local settings)

`feeds/<name>.yaml`; the feed's name is the file name without `.yaml` (lowercase letters, digits, hyphens).

```yaml
uses: chq-academic@^1.0          # required
title: collectionHQ (Main Library)   # optional; defaults to the recipe title
enabled: true
file_prefix: chq
filters: {exclude_location_codes: [ONLINE], exclude_item_statuses: [Withdrawn]}   # keys of `selection`
options: {timezone: America/Detroit}                                             # keys of `options`
legacy:                          # item note type *names* from Settings > Inventory, for migrated values
  created_date_note_type: null
  circ_count_note_type: null     # added to loans_total
  last_circ_date_note_type: null
outputs:                         # MAP-2: local column settings, by output name
  items: {empty_value: "", date_format: "%Y-%m-%d", columns: [...]}   # any output key except name and format
checks: {min_item_ratio: 0.8, must_be_filled: [barcode]}   # must_be_filled is added to the recipe's, never replaces it
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
schedule: Sun 02:15              # optional; else the recipe's default_schedule
```

**Overlay rules.** `filters` and `options` replace individual keys. `outputs.<name>` deep-merges into the recipe's output of that name; a `columns` list replaces the recipe's list wholesale, but every recipe column marked `required: true` must still be present (matched by `field`). `checks.min_item_ratio` replaces; `checks.must_be_filled` adds. `delivery` merges over the recipe's. The merged result is validated with the same rules as a recipe, and errors name the feed file.

### Schedules

| Text | Meaning |
| --- | --- |
| `daily`, `weekly`, `monthly` | daily at 02:00; Sundays at 02:00; the 1st at 02:00 |
| `daily 03:30` | every day at 03:30 |
| `Sun 02:15`, `Mon,Thu 06:00` | those weekdays at that time (Mon … Sun, case-insensitive) |
| `monthly 15 01:00` | that day of the month (1–28) at that time |
| `manual` | never due; runs only when started by hand |

A feed is **due** when its latest slot at or before now is later than the start of its last run that ended `success`, `built`, `blocked` or `held`, and fewer than `runs.max_attempts_per_slot` runs have started since that slot. A feed that has never run is due at its first slot after the feed file was created (use the file's modification time).

## Packages and modules

Flat, small modules under `src/folio_exchange/`, as in FOLIOHQ. Copied FOLIOHQ code keeps its tested behaviour unless this section says otherwise; port its tests along with it.

| Module | Owns |
| --- | --- |
| `errors.py` | `FxError` and subclasses (done). |
| `dates.py` | FOLIOHQ's date helpers, unchanged. |
| `credentials.py` | Secret references, keyring access, `secret set`. |
| `config.py` | `site.yaml`: load, defaults, `${ENV}`, validation, secret-key rejection, write. |
| `recipe.py` | Recipe library, versions and ranges, validation, feed overlay. |
| `reads.py` | The reads registry, reference data, permission names, the patron-path denylist. |
| `client.py` | FOLIOHQ's `FolioClient` plus read-only and patron guards and `iter_where_in`. |
| `cache.py` | FOLIOHQ's SQLite cache. |
| `sync.py` | Full and delta sync of the reads given; `sample()` for previews. |
| `fields.py` | The field registry (MAP-1) and `RowBuilder`. |
| `marc.py` | MARC 21 building (from FOLIOHQ), link enforcement. |
| `delimited.py` | Delimited text output. |
| `build.py` | One streaming pass from the cache to every output of a feed. |
| `checks.py` | Required fields, item-count ratio, conformance with vendor samples. |
| `delivery.py` | SFTP (from FOLIOHQ), local folder, host key lookup, run pruning. |
| `notify.py` | Alerts by webhook and email. |
| `history.py` | Run history and audit log in SQLite; file digests. |
| `lock.py` | One run at a time per site. |
| `schedule.py` | Schedule text, due runs, OS scheduler commands. |
| `runner.py` | The pipeline for one feed, `--due` runs, release, preview. |
| `cli.py` | `folio-exchange` commands. |
| `wizard.py` | `folio-exchange init`, the guided setup. |

### Contracts

Signatures are the contract between modules; keep them, or change every caller and this table together. Types: `Site` and `Feed` are dataclasses; sections inside them stay plain dicts, as in FOLIOHQ.

**credentials.py**
```python
FOLIO_SERVICE = "folio-exchange/folio"; SFTP_SERVICE = "folio-exchange/sftp"
SMTP_SERVICE = "folio-exchange/smtp"; WEBHOOK_SERVICE = "folio-exchange/webhook"
SECRET_KEYS = frozenset({"password", "passphrase", "private_key_passphrase", "token", "api_key", "webhook_url"})
def is_secret_ref(value: object) -> bool                      # "keyring" or "env:NAME"
def resolve(ref: str | None, service: str, account: str | None, *, env_first: str | None = None) -> str | None
    # env_first (e.g. "FOLIO_PASSWORD") if set and non-empty; then env:NAME; then keyring (ref "keyring" or None).
    # Missing keyring package or a backend error -> None (log a warning without the value).
def source(ref, service, account, *, env_first=None) -> str   # "environment (FOLIO_PASSWORD)", "system keyring
                                                               # (folio-exchange/folio)" or "NOT FOUND"
def keyring_available() -> bool
def store(service: str, account: str, value: str) -> None     # raises FxError when no usable keyring
def folio_account(username: str, tenant: str) -> str          # f"{username}@{tenant}"
def sftp_account(username: str, host: str) -> str             # f"{username}@{host}"
```

**config.py**
```python
@dataclass
class Site:
    root: Path; folio: dict; sync: dict; paths: dict; runs: dict; alerts: dict
    cache_path: Path; history_path: Path; lock_path: Path; out_dir: Path; logs_dir: Path
    feeds_dir: Path; recipe_dirs: list[Path]     # site recipe folders, then the bundled library
    def folio_password(self) -> str | None
def find_site(explicit: str | Path | None) -> Path   # ConfigError naming where it looked, and suggesting `init`
def load_site(root: Path) -> Site                     # defaults deep-merged, ${ENV}, env overrides, validation
def reject_secrets(data: object, where: str) -> None  # ConfigError at the first SECRET_KEYS key, with its path
def write_yaml(path: Path, data: dict, header: str = "") -> None   # used by the wizard; UTF-8, keeps key order
BUNDLED_RECIPES: Path                                 # src/folio_exchange/recipes
```

**recipe.py**
```python
@dataclass(frozen=True)
class Recipe: id: str; version: tuple[int, int, int]; data: dict; path: Path
    @property version_str -> str ; @property title -> str
def parse_version(text: str) -> tuple[int, int, int]
def version_matches(version: tuple[int, int, int], spec: str) -> bool
def parse_uses(uses: str) -> tuple[str, str]           # ("chq-academic", "^1.0"); RecipeError without a range
class RecipeLibrary:
    def __init__(self, dirs: list[Path]) -> None
    def all(self) -> list[Recipe]                       # every valid version; invalid files are reported by problems()
    def problems(self) -> list[str]
    def versions(self, recipe_id: str) -> list[Recipe]  # ascending
    def resolve(self, uses: str) -> Recipe              # RecipeError listing the versions that exist
    def newer_major(self, uses: str) -> Recipe | None   # highest version above the range, if any
def validate_recipe(data: dict, where: str) -> list[str]
def load_recipe_file(path: Path) -> Recipe              # RecipeError with every problem
@dataclass
class Feed:
    name: str; path: Path; recipe: Recipe; uses: str; title: str; enabled: bool; file_prefix: str
    reads: tuple[str, ...]; selection: dict; options: dict; legacy: dict; link: dict | None
    outputs: list[dict]; checks: dict; delivery: dict; schedule: str
    def output(self, name: str) -> dict
def load_feed(path: Path, library: RecipeLibrary) -> Feed
def load_feeds(site: Site, library: RecipeLibrary | None = None) -> list[Feed]   # sorted by name; ConfigError
                                                                                   # lists every bad feed
def feed_errors(site: Site, library: RecipeLibrary) -> dict[str, str]           # name -> message, for `feeds`
```

**reads.py**
```python
@dataclass(frozen=True)
class Read: name: str; path: str; key: str; permission: str; description: str
READS: dict[str, Read]     # instances, holdings, items, loans, marc (marc = POST /source-storage/source-records)
REFDATA: dict[str, tuple[str, str, str]]   # kind -> (path, response key, permission); FOLIOHQ's ten tables
ENTITY_ORDER = ("instances", "holdings", "items", "loans")
PATRON_PATH_PREFIXES: tuple[str, ...]      # /users, /bl-users, /patron, /patron-pin, /perms/users, /proxiesfor,
    # /request-storage, /circulation/requests, /circulation/loans (embeds the borrower), /accounts,
    # /feefineactions, /manualblocks, /automated-patron-blocks, /service-points-users, /users-keycloak,
    # /authn/credentials, /notes ... Matched on path segments, case-insensitive.
def permissions_for(reads: Iterable[str]) -> list[str]   # sorted; includes reference data permissions
def is_patron_path(path: str) -> bool
```

**client.py** – FOLIOHQ's `FolioClient` and `FolioError` (now a subclass of `FxError`), plus:
- Every request path goes through `reads.is_patron_path`; a match raises `PatronDataRefused` before anything is sent.
- Only `GET` is allowed, except `POST` to `/authn/login-with-expiry`, `/authn/login` and `/source-storage/source-records` (a read by POST). Anything else raises `FolioError("read-only: …")`.
- `iter_where_in(path, key, field, values, batch=50) -> Iterator[dict]`: CQL `field==("v1" or "v2" …)` in batches, for previews.
- The empty-password message names `FOLIO_PASSWORD` and `folio-exchange secret set folio`. Default user agent `folio-exchange/<version>`.

**cache.py** – FOLIOHQ's, unchanged, except that loans never store `userId` or `proxyUserId` (they never did; keep a test).

**sync.py**
```python
@dataclass class SyncReport: mode, fetched, removed: dict; marc_fetched, marc_missing: int; seconds: float
class Syncer:
    def __init__(self, client: FolioClient, cache: Cache, site: Site) -> None
    def run(self, reads: Iterable[str], force_full: bool = False) -> SyncReport
    def sample(self, reads: Iterable[str], instances: int) -> SyncReport   # first N instances by id and their
        # holdings, items, loans and MARC, into this (temporary) cache, for previews
def reads_to_sync(feeds: list[Feed]) -> set[str]   # the union over enabled feeds (see "Shared cache" below)
```
Reference data always syncs. Entities sync in `ENTITY_ORDER`, limited to `reads`; MARC is fetched for MARC-source instances when `marc` is in `reads`. Loans drop `userId` and `proxyUserId` the moment they arrive. FOLIOHQ's full/delta/auto rules, `incomplete` flags, deletion fallback and 10-minute overlap are kept.

**Shared cache.** All feeds share one cache, and sync state is per entity. So a run always syncs the union of the reads of every enabled feed: otherwise a feed without `marc` could advance the instances' `last_sync` and a later feed would miss MARC for those instances. A feed's own `reads` still limit what it may output.

**fields.py**
```python
@dataclass(frozen=True)
class FieldDef: name: str; description: str; needs: frozenset[str]; date: bool = False
FIELDS: dict[str, FieldDef]       # FOLIOHQ's ITEM_FIELDS, each with needs ⊆ READS; loan-derived fields need "loans"
ITEM_FIELDS: dict[str, str]       # name -> description (compatibility)
DATE_FIELDS: set[str]
def suggest(name: str) -> str | None             # closest field name, for "did you mean"
class Ref: (FOLIOHQ's, unchanged)
class RowBuilder:
    def __init__(self, ref: Ref, options: dict, legacy: dict) -> None   # was (ref, cfg)
    def build(self, item, holdings, instance, loans) -> dict           # unchanged
```

**marc.py** – FOLIOHQ's functions; `MarcBuilder(output: dict, link: dict | None, date_fmt)` reads `embed_items`, `item_field.{tag, subfields, strip_existing, max_per_record}`. `finalize` always sets 001 = instance HRID and 999 ff $i = instance UUID (FOLIO's convention, and the two link options), keeps the counters, the size limits and the 949 shedding.

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
@dataclass class BuildReport: FOLIOHQ's fields, plus outputs: dict[str, list[str]] (output name -> file names)
    and unfilled: Counter (must_be_filled field -> rows where it was empty)
def build(cache: Cache, feed: Feed, run_dir: Path, *, limit_bibs: int | None = None,
          stamp: str | None = None) -> BuildReport
```
One SQL pass, as in FOLIOHQ. Selection filters apply once, before any output sees a row. `limit_bibs` stops after that many bibs are written (previews). `stamp` overrides `{date}` (tests).

**checks.py**
```python
def required_fields(feed: Feed, report: BuildReport) -> None           # SafetyCheckFailed listing field: count
def item_ratio(feed: Feed, items_written: int, last_delivered: int | None) -> None   # SafetyCheckFailed
def check_marc_file(path: Path) -> list[str]   # binary MARC parses; spots text-converted MARC (no 0x1D/0x1E)
def conformance(feed: Feed, run_dir: Path, samples: dict[str, Path]) -> list[str]   # VAL-2: per output name,
    # delimited = same header row, column count and delimiter; marc21 = both files parse as binary MARC
```

**delivery.py**
```python
def sha256_fingerprint(key) -> str
def host_key_fingerprint(host: str, port: int = 22, timeout: float = 30) -> str   # reads the server key; no login
def deliver(files: list[Path], delivery: dict, feed_name: str) -> list[str]     # by transport; returns destinations
def check_delivery(delivery: dict) -> list[str] | None   # sftp: login + list (None when listing is refused);
                                                          # local: folder exists and is writable; uploads nothing
def prune_runs(feed_out_dir: Path, keep: int) -> None
```
SFTP keeps FOLIOHQ's pinned key or `known_hosts` (never auto-accept), staged and direct modes. The password comes from `credentials.resolve(delivery["secret"], SFTP_SERVICE, sftp_account(...))`. `local` copies the files into `delivery["directory"]`, each as `.part` then renamed.

**notify.py**
```python
def send(alerts: dict, subject: str, text: str) -> list[str]   # channels used ("webhook", "email"); never raises
def should_alert(alerts: dict, status: str) -> bool             # on: failure -> any status but success and built
```
Webhook: `POST {"text": text}` (Slack and Teams both accept it). Email: `smtplib` with optional STARTTLS and login. Neither ever includes a secret.

**history.py**
```python
class History:
    def __init__(self, path: Path) -> None
    def start_run(self, feed: str, recipe: str, recipe_version: str, trigger: str, run_dir: Path) -> int
    def finish_run(self, run_id: int, status: str, *, items: int | None, bibs: int | None,
                   files: list[dict], delivered: list[str], error: str | None, report: dict) -> None
    def last_delivered(self, feed: str) -> dict | None     # the latest run with delivered files
    def runs(self, feed: str | None = None, limit: int = 20) -> list[dict]
    def runs_since(self, feed: str, since: datetime) -> list[dict]
    def audit(self, actor: str, feed: str | None, action: str, detail: dict) -> None
    def audit_log(self, limit: int = 50) -> list[dict]
    def prune(self, months: int) -> int
    def close(self) -> None
def file_digest(path: Path) -> dict      # {"name", "bytes", "sha256"}
def current_user() -> str
```
Statuses: `success` (delivered, or transport `none`), `built` (`--no-deliver`), `blocked`, `held`, `failed`, `preview`. Every delivery records destinations, file names, sizes and SHA-256 hashes (spec, Audit).

**lock.py** – `class RunLock: __init__(path: Path); __enter__/__exit__` with an OS file lock (`msvcrt` on Windows, `fcntl` elsewhere); a second holder gets `LockBusy` at once.

**schedule.py**
```python
@dataclass(frozen=True) class Schedule: kind: str; days: tuple[int, ...]; hour: int; minute: int; text: str
def parse(text: str) -> Schedule                    # ConfigError with the accepted forms
def last_slot(s: Schedule, now: datetime, tz: ZoneInfo | None) -> datetime | None   # aware; None for manual
def next_slot(s: Schedule, now: datetime, tz: ZoneInfo | None) -> datetime | None
def describe(s: Schedule) -> str                    # "Sundays at 02:15"
def is_due(s: Schedule, now: datetime, tz, runs: list[dict], max_attempts: int, created: datetime) -> bool
def os_command(site_root: Path, executable: str, system: str) -> str   # schtasks (Windows), crontab line (Linux,
                                                                         # macOS); hourly `run --due`
```

**runner.py**
```python
@dataclass class RunOptions: full: bool = False; no_deliver: bool = False; skip_sync: bool = False
    force: bool = False; trigger: str = "manual"
@dataclass class RunResult: feed: str; status: str; code: int; run_dir: Path | None; report: dict
def run_feed(site: Site, feed: Feed, opts: RunOptions, *, sync_report: dict | None = None) -> RunResult
def run_many(site: Site, feeds: list[Feed], opts: RunOptions) -> list[RunResult]   # one lock, one sync, then each
def due_feeds(site: Site, feeds: list[Feed], now: datetime) -> list[Feed]
def release(site: Site, feed: Feed, run_dir: Path | None) -> RunResult
def preview(site: Site, feed: Feed, instances: int) -> RunResult     # temporary cache; writes out/<feed>/preview-…
```
`run_feed`: lock → history start → sync (unless `skip_sync` or a `sync_report` was passed) → build → required fields → item ratio against `history.last_delivered` (unless `force`) → first-delivery hold (transport is not `none` and the feed has never delivered: status `held`) → deliver → history finish → prune → alert. Exit codes: 0 success or built, 1 failed, 2 blocked or held. `run-report.json` is written in every run folder that exists, as in FOLIOHQ, and never holds a secret.

**cli.py** – global options `-s/--site`, `-v/--verbose`, `--version`. Logging to stderr and `logs/folio-exchange.log`.

| Command | Does |
| --- | --- |
| `init` | Guided setup (`wizard.py`): connect, pick a recipe, filters, preview, delivery, schedule. |
| `check [FEED] [--delivery]` | Password source, login, every permission and endpoint the enabled feeds need, with record counts; recipe updates held for review. `--delivery` tests the vendor login and lists the folder; uploads nothing. |
| `preview FEED [-n 25]` | Sample from live data in a temporary cache; prints the first rows as a table; writes files to `out/<feed>/preview-…`. |
| `run FEED… \| --all \| --due` | Sync, build, check, deliver. `--full`, `--no-deliver`, `--skip-sync`, `--force`. |
| `release FEED [RUN_DIR]` | Deliver a held run (the latest by default) and record who released it. |
| `deliver FEED [RUN_DIR] [--force]` | Re-send a completed build; refuses partial or failed runs (FOLIOHQ rule). |
| `sync [--full]` | Update the cache only. |
| `build FEED` | Write files from the cache without contacting FOLIO. |
| `feeds` | Each feed: recipe and version, schedule and next slot, last run and its status, held updates, errors. |
| `recipes [list \| show ID[@RANGE] \| validate PATH \| summary ID[@RANGE]]` | Browse and check recipes; `summary` prints the one-page layout for the vendor. |
| `fields` | The field registry with descriptions and what each needs. |
| `history [FEED] [-n 20] [--audit]` | Run history, or the audit log. |
| `secret set {folio\|smtp\|webhook\|FEED}` and `secret status` | Store a secret in the keyring; show where each comes from. |
| `schedule` | Next slot per feed and the exact command to register the hourly `run --due` with the OS. Registers nothing. |

**wizard.py**
```python
class Prompter:                                  # console implementation; tests use ScriptedPrompter
    def say(self, text: str) -> None
    def ask(self, prompt: str, default: str | None = None, validate=None) -> str
    def choose(self, prompt: str, options: list[tuple[str, str]], default: str | None = None) -> str
    def choose_many(self, prompt: str, options: list[tuple[str, str]], default: list[str] | None = None) -> list[str]
    def confirm(self, prompt: str, default: bool = True) -> bool
    def secret(self, prompt: str) -> str          # getpass
class ScriptedPrompter(Prompter): __init__(answers: list); transcript: list[str]
def run_wizard(root: Path, prompter: Prompter, *, library: RecipeLibrary | None = None) -> int
```
Steps: site folder → FOLIO URL, tenant, username → password into the keyring (or tell them to set `FOLIO_PASSWORD`) → test login → choose a recipe → test the recipe's permissions → filters, chosen from the tenant's own location codes and item statuses → legacy note types from the tenant's note types → preview of 10 bibs → delivery host, user, folder; read the server's host key and show it, and pin it only after the person confirms it matches what the vendor gave them → SFTP password into the keyring → test the vendor login (uploads nothing) → schedule → write `site.yaml` and `feeds/<name>.yaml`, audit the change, and print the OS scheduler command. Every step can be re-run; existing files are shown and kept unless the person confirms a change.

## Testing

- Everything runs offline against `tests/mock_folio.py` (extend it with each API call: it now needs `field==("a" or "b")` queries). SFTP is mocked; the keyring is blocked by an autouse fixture.
- `tests/sitekit.py` writes site and feed files; `conftest.py` gives `folio` (a running mock) and `site` (a site folder with a `chq` feed delivering to a local folder).
- Port FOLIOHQ's 26 tests to the new modules, including the eight regression tests from its code review: fields over 9,999 bytes, delivering a partial run, login retry, legacy date off-by-one, stale MARC, CSV rows for oversize bibs, the atomic SFTP pair, sorted reference-data paging.
- Every bundled recipe has a fixture test: build it against the mock and check the layout (spec, Testing).
- Every bug fix gets a regression test.

## Deliberately not in this build

The phase 2 web UI, inbound loads (v2), patron-data approvals (refused outright until the open question is answered), transports beyond SFTP and a local folder (DEL-3), MARCXML/JSON/KBART builders (MAP-5), a Windows installer, signed releases and CI against the public reference systems.
