# Services and destinations plan

_3 November 2026. A plan, not started. The names are decided (below); the rest is proposed and waits on the open questions at the end. It comes after the desktop app's D4 (tray and notifications) and before a second service or the first automatic integration._

## Why

The far end of a feed has no object of its own today. A recipe names the company in `vendor:`, which is display text, and each feed carries its own `delivery:` block: transport, server, port, user name, folder, pinned server key, secret reference and key file. That works for one feed to one vendor, but:

- Two feeds to one SFTP account repeat the server, user name and pin, and must log in the same way, because Credential Manager keeps one secret per account (`user@host`). That shared account is already a destination, but only `setupops._account_clash` knows it.
- Nothing ties a feed to the service its recipe was written for. A feed file copied from another keeps its delivery, so a feed for one vendor can end up sending to another vendor's server, and the server key check doesn't catch it, because the key is right for that server.
- When vendors offer automatic setup (API keys, OAuth, provisioning links), the settings and tokens need a home that isn't a feed, since one token serves every feed to that service.
- Nothing in the app shows everything the library sends to collectionHQ in one place.

The aim is that "data from FOLIO, through feeds, to services" stays clear as more services and automatic setup arrive.

## Decided (3 Nov 2026)

| Term | Means | Not |
| --- | --- | --- |
| **Site** | The local folder for one FOLIO tenant: its connection, the shared cache (`data/cache.sqlite`), run history and alerts. A site holds feeds to many services. | A service or a tenant. |
| **Service** | A third-party platform the library sends data to: collectionHQ, OCLC, a discovery layer. | A FOLIO service account or service point, a Windows service, or a keyring target. |
| **Destination** | Where a service receives the data: today an SFTP server, folder and account with a pinned server key, or a local folder; later, API endpoints. | The service itself. collectionHQ is a service; the library's account on its US SFTP server is a destination. |
| **Tenant** | Only the FOLIO tenant itself, as in "FOLIO tenant fs00001234". | The site folder. |

Rejected: renaming site to service, because a site shares one FOLIO login and cache across services, and one folder per service would mean a full catalog sync per vendor; and renaming site to tenant, because "Set up tenant", "Rename tenant" and "Delete tenant" would read like FOLIO platform operations.

## The model

```
site            one FOLIO tenant: connection, shared cache, run history, alerts
 └─ service     collectionHQ, OCLC, a discovery layer
     └─ destination   an SFTP server, folder and account with a pinned server key, or a local folder
         └─ feed      a recipe plus this library's settings, sending here on a schedule
```

- A recipe names the service it's for. A feed picks one destination, which must belong to that service; a feed pointed at another service's destination doesn't load.
- A feed with no destination is a dry run, as `transport: none` is today. "None" isn't a destination.
- Several feeds can share a destination, for example two branch libraries' feeds to one collectionHQ account.
- A login belongs to an account (`user@host`), so destinations on one account share it, as feeds on one account do today.
- The cache, sync, history and alerts stay per site, so one catalog sync still serves every service.

## Today

- **Recipe:** `vendor: collectionHQ` (required display text, shown in the Recipes view and `recipes list`) and `delivery: {transport, upload_mode}`, the only delivery keys a shared recipe may set (`recipe.RECIPE_DELIVERY_KEYS`, from the 26 Oct decision that hosts, folders and server keys are local).
- **Feed:** `delivery:` is merged over the recipe's in `recipe.load_feed_data`: `transport` (`sftp`, `local` or `none`), `host`, `port`, `username`, `remote_dir`, `host_key_fingerprint`, `host_key_type`, `host_key_confirmed`, `secret`, `private_key`, `known_hosts`, `upload_mode` and `directory`. Relative paths are taken from the site folder.
- **Secrets:** the SFTP password or key passphrase is in Credential Manager under `folio-exchange/sftp` and the account `<username>@<host>`; the feed's `secret:` says `keyring` or `env:NAME`. A private key saved from the page or the app is `keys/<feed>.key`, readable only by this Windows user. Two feeds on one account must both use a password or both a key (`setupops._account_clash`).
- **Where a feed sends, as one string:** `delivery.destination_key(d)` is `sftp://<user>@<host>:<port>/<folder>` (the folder is `.` when empty, with a trailing slash dropped; the host is kept as typed), a `file:` URI of the resolved local folder, or `none`. The server key pin isn't part of it. Every run records it (`runs.destination`), and the first-delivery hold, the layout hold, the item-count baseline, the release check and file collisions all compare it.
- **How a destination counts as new:** `runner.hold_reason` asks `History.last_delivered(feed.name, destination_key)`. With no delivered run of that feed to that string, the reason is "first delivery to …". So "new" is per feed and per destination: a new feed on a destination other feeds already use is held, a feed moved from a test folder to SFTP is held, and the same settings saved again are not.

## Proposed file format

**Recommended: `services/<id>.yaml`, one file per service, holding its destinations.**

```yaml
# services/collectionhq.yaml
service: collectionhq            # id: lowercase letters, digits and hyphens; matches the file name
title: collectionHQ
destinations:
  us-sftp:                       # id, unique within the service, same characters
    title: collectionHQ US       # optional
    transport: sftp
    host: sftp.us.collectionhq.com
    port: 22
    username: example_library
    remote_dir: "."
    host_key_fingerprint: "SHA256:..."
    host_key_type: ssh-rsa
    secret: keyring              # keyring | env:NAME; never the password
    private_key: null            # a key file's path, never the key
    upload_mode: direct          # optional; else the recipe's
  test-folder:
    title: Test folder on this PC
    transport: local
    directory: test-deliveries/chq   # relative paths are taken from the site folder
```

A feed names it in place of `delivery:`:

```yaml
uses: chq-academic@^1.0
destination: collectionhq/us-sftp
schedule: Sun 03:30
```

A recipe gains an optional `service: collectionhq`. Without it, the service id comes from `vendor`, lowercased, with each run of other characters made a hyphen (`collectionHQ` becomes `collectionhq`). So `chq-academic` 1.0.0 needs no change, and its bundled copy stays identical to the one in cHQ FOLIO Exchange Recipe.

**Loading.** `recipe.load_feed` resolves the reference and merges the defaults, the recipe's `delivery` and then the destination into `feed.delivery`, the same dict a feed has today. `delivery.deliver`, `check_delivery`, `destination_key`, the holds, collisions and release see no difference. `Feed` gains `service: str` and `destination: str | None`. A destination is checked with today's rules for a feed's delivery: `sftp` needs a host, a user name and a pinned fingerprint or `known_hosts`; `local` needs a folder; plain FTP is refused; key text is refused (`recipe.key_text`); `host_key_type` is one of `delivery.KEY_TYPES`; a secret key is rejected outright (`config.reject_secrets`). A feed with both `destination:` and `delivery:` doesn't load, and neither does one whose reference names no destination: a missing destination never falls back to a dry run that looks successful. Run reports also record the destination's name, for display only; no decision reads it.

**Alternatives.**

| Option | For | Against |
| --- | --- | --- |
| A. `services/<id>.yaml` with its destinations (recommended) | Matches the model. One file per service is where a vendor's own definition can attach later. Few files. | Destinations of one service share a file version, so two forms open on one file conflict. That's rare, since one action runs at a time. |
| B. One file per destination, `services/<service>/<destination>.yaml` | Each is saved and versioned alone. | Many small files for one account, and the service itself has nowhere to live. |
| C. A `services:` section in `site.yaml` | One file. | A typo there stops every feed and the FOLIO connection, and each destination edit conflicts with connection and alert edits. |
| D. Keep inline `delivery:` and add an `account:` name for shared logins | Smallest change. | No service object for automatic setup, and the pin still repeats in every feed. |

**Local folders and dry runs.**

- A local folder is a destination of the service, with `transport: local`, as `test-folder` above. Its first delivery is held like any other, and its key differs from the SFTP one, so a test delivery to a folder never disarms the review of the first real upload (the 26 Oct decision). The other way is to keep folders on the feed as a testing setting (open question 1).
- A dry run is a feed with no `destination:` and no `delivery:` (or `transport: none`), as today. The app's "Set up delivery" picks or makes a destination.

**Destinations on one account.** collectionHQ's login folder holds `holds/` and `transactions/`, which look like other collectionHQ feeds, so one account can serve several folders. Following the definition above, each folder is its own destination, and destinations on one account share its secret. `_account_clash` checks one login method across destinations and inline feeds alike, and `check` warns when destinations on one host and port pin different server keys. Whether to split the account from the folder is open question 2.

## Migration

- **Inline deliveries keep working.** Nothing is rewritten when FOLIO-Exchange is updated, and a feed with its own `delivery:` loads and runs exactly as now.
- **The offer.** `check` adds a note (code `delivery-inline`): "Feed chq has its own delivery settings. `folio-exchange services move chq` names them, so other feeds can share them." The app's Delivery tab has "Move to a named destination…".
- **The move** reads the feed's `delivery:` as written, before merging; finds an identical destination in the recipe's service (the same `destination_key`, pin, login method and upload mode) or makes one with a name the person chooses; writes `services/<id>.yaml` and replaces the feed's `delivery:` with `destination:`; checks that every feed still loads and no file collision appears, and undoes both writes if not; and audits "delivery moved to collectionhq/us-sftp" with where it was done.
- **Values are copied as written.** The host isn't lowercased, the folder isn't tidied, and relative paths stay relative, because any difference would change `destination_key` and hold the next delivery. With the key unchanged, the move holds nothing, the item-count baseline continues, and a run waiting for review can still be released. A test compares the key before and after.
- **Secrets don't move.** Keyring entries stay under `user@host`, an `env:NAME` reference is copied, and `keys/<feed>.key` stays where it is, named by the destination. A key saved later for a destination is `keys/<service>.<destination>.key`; feed names have no dots, so the two can't clash.
- **Feeds with identical inline settings** are offered one destination together.
- **A `transport: local` override** used while testing becomes a folder destination.
- **Going back.** An older FOLIO-Exchange doesn't know `destination:`, so such a feed stops loading there rather than sending somewhere else. `services inline FEED` writes the settings back into the feed.

## Safety rules, kept

1. **The first delivery to a new destination is held.** Unchanged: the hold is keyed by the feed's name and the `destination_key` of the resolved settings, never by a destination's name. So:
   - renaming a destination, or moving inline settings into one, holds nothing;
   - changing a destination's server, port, user name or folder holds the next delivery of every feed that uses it, and runs already waiting for review refuse release ("built for …"), as they do today when a feed's delivery changes;
   - a new feed on a destination other feeds use is still held, because its files are new to the service;
   - changing only the pin holds nothing, as today (open question 3).

   The destination dialog names the feeds that will hold before it saves.
2. **The layout-change and recipe-major holds** compare with the feed's last delivery to that destination. Unchanged.
3. **The FOLIO-change hold** (`runner.folio_source`) is per site. Unchanged.
4. **The pinned server key.** Only the pinned SHA-256 value (or `known_hosts`) is accepted, a typed pin limits the negotiation, and while `host_key_confirmed` is false nothing logs in. The code is the same, now per destination. Saving a destination reads every key the server offers and pins the one that matches the vendor's fingerprint, as `setupops.save_delivery` does.
5. **No secrets in files.** `services/*.yaml` goes through `config.reject_secrets` and holds only secret references. It joins the settings audit (`runner.audit_settings`), the folder-access check (`permissions.problems`), because a services file another Windows user can change could redirect deliveries, and the rename's absolute-path check (`siteops.absolute_paths`). Keyring targets don't change (see Name clashes).
6. **A shared recipe can't choose where files go.** Recipes still set only `transport` and `upload_mode`. `service:` only limits which destinations a feed may pick; it can't add one. Destinations live in the site. In a minor version from a vendor's recipe source, a changed `service` will count as substance (so it needs a new major), except adding it with the id already taken from `vendor`, which will count as wording (`sources._WORDING`).
7. **Recipes stay declarative.** Service files and service definitions are data too: no code, scripts or templates.
8. **Patron data.** A service or destination can't widen what is read. Reads come only from recipes, and `patron_data` stays `none` unless a recipe declares it and an admin approves it. A service that wants patron data gets it only through such a recipe.
9. **File collisions** compare destination keys. Unchanged, and more useful once feeds share destinations.
10. **One login per account.** `_account_clash` covers destinations and inline feeds.

## Automatic integrations (later)

**What a service definition could carry.** A shared, versioned YAML file, like a recipe:

- the service's id and title, the vendor's home page and a setup contact;
- the recipes it takes, by id;
- destination templates: transport, hosts per region (collectionHQ US and UK, for example), port, the folder for each recipe, upload mode, whether accounts are upload-only, and the server keys the vendor publishes, with their types;
- for APIs later: the base address, how it signs in (an API key, OAuth client credentials or a device code), the scopes it asks for, and a provisioning link the person opens in their browser to create an account or get a code.

It carries no reads, fields or patron data, and nothing that runs.

**Where it could come from.**

- Bundled with FOLIO-Exchange (`src/folio_exchange/services/<id>/<version>.yaml`), reviewed by pull request as bundled recipes are.
- The curated `recipe_sources.yaml`: a source may list `services:` beside `recipes:` and provide only those ids, downloaded through `sources.check` and `sources.install` with the same rules (one reviewed commit, SHA-256 recorded, only `api.github.com` and `raw.githubusercontent.com`, the size limits, installed only into the site).
- A site's own `recipe_sources:` in `site.yaml`, with the same rules.

**How it's used.** A definition only fills in the destination form. The destination is written to the site's `services/<id>.yaml` once the person confirms it. A newer definition never changes a saved destination: `check` and the app say what changed (a new server key, a new host), and the person applies it through the same checks. Secrets from automatic setup (API keys, tokens) go to Credential Manager under new targets keyed by the service and the account, never by a destination's name, since a rename would lose them, as it loses the webhook address stored under the site folder's name.

**A vendor-published fingerprint doesn't skip the library's confirmation.**

- Today the pin rests on two channels: the fingerprint the vendor gave the library (by email, phone or in a setup guide) and the key the server shows. Someone on the network path can fake the second but not the first.
- A fingerprint read from a vendor's repository is also separate from the SSH connection. But the same definition supplies the host, so whoever controls that repository (a stolen token, a bad commit, a source a site added) would control both where the data goes and which key is trusted, with no position on the network needed. Review by pull request covers the curated list once, not every later commit in a vendor's repository.
- So a definition may fill in the fingerprint and show where it came from (repository and commit), and the app asks the person to confirm it against what the vendor told them ("This matches what collectionHQ gave us") instead of typing it. The server's key is still compared before anything logs in. That removes the typing, not the check.
- Skipping the confirmation could be argued only for a fingerprint whose trust doesn't come from the same place as the host, such as a statement signed with the vendor's own key and checked against a key in FOLIO-Exchange's reviewed code. That's out of scope until a vendor offers it.

## Name clashes

| Word | Where it's used | What to do |
| --- | --- | --- |
| FOLIO service account | App text: "A read-only service account, not someone's personal login." (`desktop/setup.py`, and the page's `web/app.js`); `init` asks for the "Service account user name". | App text says "FOLIO user": "A read-only FOLIO user, not someone's personal login." Docs write "FOLIO service account" in full. |
| FOLIO service point | Circulation desks; `/service-points-users` is on the patron denylist (`reads.py`). | Any future field or text says "FOLIO service point", never "service" alone. |
| Windows service | Nothing today: the hourly run is a Task Scheduler task, and the tray (D4) is a Startup entry. | Neither becomes a Windows service, and text never calls them one. The spec's "a Linux service" becomes "a systemd timer". |
| Keyring service | `keyring` calls the target name a service. `credentials.FOLIO_SERVICE`, `SFTP_SERVICE`, `SMTP_SERVICE`, `WEBHOOK_SERVICE` and `HEARTBEAT_SERVICE` are used across nine modules and two test files; "secret stored" audit entries carry the key `service`. | Rename the constants to `FOLIO_TARGET`, `SFTP_TARGET` and so on, with the strings unchanged: `folio-exchange/folio`, `folio-exchange/sftp`, `folio-exchange/smtp`, `folio-exchange/webhook` and `folio-exchange/heartbeat`. A changed string loses every saved password, and nothing says so until a run fails to log in. A test pins the five strings. New audit entries write `target`, and the Activity views read both keys, so old entries still show. |
| Secret Service | The Linux keyring backend's name, in docs. | Keep it as a proper name. |
| Vendor | App text ("ask the vendor", "the vendor's fingerprint", "Add a vendor feed") and the recipe's `vendor:` field. | Keep "vendor" for the company and "service" for the platform as an object in the app (open question 7). |

## What changes

### Command line

- **`init`:** the delivery step lists the service's destinations ("Send to collectionHQ US?") or makes a new one with today's questions (server, port, user name, folder, the server key check, the password). A second feed on that service reuses the destination and its login. It writes `destination:`, not `delivery:`.
- **`check`:** validates `services/`; notes inline deliveries (the offer) and destinations no feed uses; warns when one host and port has different pins; and reports a feed whose destination belongs to another service as a problem.
- **`check --delivery [FEED | SERVICE/DESTINATION]`:** tests each destination once and names the feeds that use it. A feed's name tests its destination.
- **`secret`:** `secret set <feed>` keeps working and stores for its destination's account. `secret set <service>/<destination>` is new; the slash means it can't clash with a feed's name. `secret status` lists one line per account, with the destinations and feeds that use it.
- **`services`** (new): lists each service, its destinations, where each sends, its server key state, where its login comes from, and the feeds that use it. `services move FEED [SERVICE/DESTINATION]` and `services inline FEED` as under Migration.
- **`feeds`, `history` and `release`** show the destination's name beside where it sends.
- **`rename-site`** looks for absolute paths in the service files too.

### Desktop app

- **`desktop/delivery.py`:** `DeliveryDialog` becomes the feed's choice of a destination of its service, a new one, or none (a dry run). A new `DestinationDialog` holds today's form (server, port, user name, folder, the vendor's fingerprint or "I don't have it yet"; or a folder), lists the feeds that use it, and before a change of place says that their next delivery will be held and that runs waiting for review will need to run again. `LoginBox` belongs to the destination.
- **`desktop/wizard.py`:** no new step. A feed is still added as a paused dry run and opens on its Delivery tab; the last look names the service.
- **The feed page (`desktop/feed.py`):** Overview and Delivery show the destination's name and where it sends. "Change delivery…" picks a destination, "Edit destination…" opens the destination, and "Move to a named destination…" appears for inline deliveries. Test delivery tests the destination.
- **A Services view** in the sidebar, after Activity: each service, its destinations with their server key and login state, Test, Edit, and the feeds that use each.
- **Behind them:** `model.py` words, `actions.py` jobs, and new `setupops` operations (`save_destination`, `choose_destination`, `move_delivery`, and the password and key saves for a destination), each audited with where it was done.
- **Text** follows `docs/wording.md`: control names, label in name, mnemonics, and the safety facts. "Nothing logs in to an SFTP server until its key is confirmed" moves to the destination dialog.

### Browser page

New features go into the app only, and D8 may retire the page first. While it's here, it shows where a feed sends as now. Its Delivery tab, password save and key save refuse a feed that uses a named destination ("This feed sends to collectionHQ US. Change it in the desktop app.") and change nothing. Inline feeds keep working there.

### Documentation

`docs/design.md` once each phase is built, `examples/`, the JSON Schemas (`feed.schema.json`, `recipe.schema.json` and a new `service.schema.json`), the commands in cHQ FOLIO Exchange Recipe's `docs/setup.md`, and the live spec (see the last section).

## Phases

| # | Change | Done when | Size |
| --- | --- | --- | --- |
| S1 | **Groundwork.** `service` in the recipe schema (optional, taken from `vendor` when absent) and `Feed.service`; the `*_TARGET` constants with their strings pinned by a test; `target` in new audit entries; "FOLIO user" in the app's text. No change in behavior. | Every test passes with unchanged output, and byte parity holds. | 1 day |
| S2 | **Service files and references.** A `services.py` module (load, validate, resolve), the feed's `destination:`, the service match, the settings audit, folder-access and rename coverage, the account rule across destinations, collisions, `check`, `check --delivery`, `secret` and `services`. | A feed sends through a named destination, and every hold and check behaves as with the same settings inline. | 3 days |
| S3 | **Migration and setup.** `services move` and `services inline`, the `check` note, `init` writing destinations, identical inline deliveries offered one destination. | A site with inline feeds moves with nothing held, no password lost and the same item-count baseline. | 2 days |
| S4 | **The desktop app.** The Services view, `DestinationDialog`, the Delivery tab's choice, `LoginBox` per destination, the move; the page's refusal. | A librarian does all of it in the app alone. | 4 days |
| S5 | **Service definitions.** Bundled and from sources; forms filled in from them, with the fingerprint still confirmed; notices when a definition changes. | A destination made from a definition still asks the person to confirm the fingerprint, and a newer definition changes no saved destination. | 3 days |

About two and a half weeks to S4. S1 to S4 come before a second service, and S5 before the first automatic integration. API destinations and signing in to a vendor get a plan of their own.

## Tests

- An inline delivery and the same settings in a named destination give the same `destination_key`. After `services move`, the next run isn't held, the item-count baseline is the last delivery's, and a run held before the move can still be released.
- Renaming a destination holds nothing. Changing its server, port, user name or folder holds the next delivery of each feed that uses it, and a waiting run refuses release. A new feed on a destination already used is held.
- A feed whose recipe's service differs from its destination's doesn't load, and neither does an unknown reference (never a dry run) or a feed with both `destination:` and `delivery:`.
- A services file with a secret key, or key text in a path, is refused; relative paths are taken from the site folder; hand edits are audited; the folder-access check covers `services/`; `rename-site` finds absolute paths there.
- The five keyring target strings are pinned. `secret set chq` and `secret set collectionhq/us-sftp` store under the same account, and a password saved before a move is found after it.
- One login method per account, across destinations and inline feeds. File collisions between feeds that share a destination.
- `host_key_confirmed: false` on a destination stops every feed that uses it from logging in.
- The app, as in D3's tests: each secret looked for in the log, the jobs, the widgets and the files; label in name and mnemonics; problems announced; the dialog naming the feeds that will hold.
- The page refuses a delivery change for a feed with a named destination and changes nothing.
- Service definitions: a fingerprint from a definition is never pinned without the person's confirmation, a newer definition changes no saved destination, and a source provides only the service ids it lists.
- Unchanged: byte parity, and the test that the bundled `chq-academic` matches its source.

## Risks

- **Holds across feeds.** Editing a shared destination holds every feed that uses it. That's the right outcome, and the dialog says so before saving.
- **One file, many feeds.** A broken services file stops every feed of that service, as a broken recipe file does. `check` names it, and saves from the app and `init` are checked and undone when anything would fail to load.
- **Older versions** can't read a feed with `destination:` and stop. `services inline` reverses the move.
- **Two interfaces** while the page lives, kept in step by refusing on the page what only the app can do.
- **The recipe repository.** `service:` in `chq-academic` must be added in cHQ FOLIO Exchange Recipe first (the copy rule). Taking the id from `vendor` means there's no hurry.
- **Signing in with a browser.** An OAuth sign-in that returns to this computer would need a local web server, which the desktop plan removes. Device codes or a pasted code fit better (S5's follow-up plan).

## Open questions

1. **Folder destinations:** a local folder as a destination of the service (recommended), or a testing setting kept on the feed?
2. **Account and folder:** one destination per folder, as decided, with destinations on one account sharing its login; or a destination as server and account, with each feed naming its folder?
3. **A changed pin:** should changing only the server key pin hold the next delivery? Today it doesn't, and the change already needs the vendor's fingerprint.
4. **Inline settings:** supported for good in v1 (recommended), or deprecated once the app can move them, with a warning and later a refusal?
5. **Vendor fingerprints:** keep the person's confirmation for every published fingerprint, even from the curated list (recommended)?
6. **The sidebar:** a Services view beside a flat list of feeds (recommended), or feeds grouped under their service?
7. **"Vendor" in app text:** keep it for the company ("ask the vendor", "Add a vendor feed"), or move to "service" ("Add a feed")?

## Suggested text for the live spec

For the maintainer to put into the live spec, which this plan doesn't edit.

**Terms** (a new short section after "At a glance"):

> - **Site:** the folder on the library's computer for one FOLIO tenant: its connection, the shared local cache, run history and alerts. A site holds feeds to many services.
> - **Service:** a platform outside FOLIO that the library sends data to, such as collectionHQ, OCLC WorldCat or a discovery layer.
> - **Destination:** where a service receives the data: an SFTP server, folder and account with a pinned server key, or a local folder; later, an API endpoint.
> - **Recipe:** the shared, versioned description of what one service expects. It names that service.
> - **Feed:** a recipe plus this library's settings, sending to one destination on a schedule.
> - **Tenant:** the FOLIO tenant itself, never the site folder.

**Roadmap**, under phase 1 (Generalize):

> Before the second service, services and destinations become objects of their own: feeds to one service share its login and server key, a feed can only send to a destination of the service its recipe names, and vendors get a place to offer automatic setup later. The library still confirms every server key.

**Elsewhere:** in "Local settings", replace the `delivery:` line with `destination: collectionhq/us-sftp` and a short `services/collectionhq.yaml` example; in the architecture picture's caption, call the end of the data path the service rather than the vendor platform; and in "Deployment", say "a systemd timer" for "a Linux service".
