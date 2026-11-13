# FOLIO-Exchange documentation

## For library staff

- [Windows installer](../installer/README.md) – install FOLIO-Exchange on a library PC, update it or remove it.
- [Dry-run guide](dry-run.md) – try FOLIO-Exchange against your own FOLIO tenant without sending anything to a vendor, then go on to the first real delivery and to updates.
- [collectionHQ setup guide](https://github.com/ByYudkowskysTentacle/cHQ-FOLIO-Exchange-Recipe/blob/main/docs/setup.md) and [file layout](https://github.com/ByYudkowskysTentacle/cHQ-FOLIO-Exchange-Recipe/blob/main/docs/layout.md) – every step to a live collectionHQ Academic feed, and what its files hold. They live in cHQ FOLIO Exchange Recipe, the home of that recipe.

## For vendors and recipe authors

- [What a recipe holds](spec.md#what-an-integration-recipe-holds) – the shared recipe and a library's local settings, in brief.
- [Recipes in the build design](design.md#recipes) – the full recipe and feed file format, and the versioning rules.
- [`examples/`](../examples/) – a commented site and feed file. The JSON Schemas in [`src/folio_exchange/schemas/`](../src/folio_exchange/schemas/) let an editor check them.

## For developers and contributors

- [Contributing](../CONTRIBUTING.md) – setup, tests, ground rules and how to send a change.
- [Build design](design.md) – the v1 build contract: files, modules, the command line and tests.
- [Wording guide](wording.md) – how text in the desktop app should read.
- [Security policy](../SECURITY.md) – how to report a security problem privately.

## Direction and status

- [Spec sheet](spec.md) – goals, who it's for, the feeds it aims to cover, requirements, safety rules and roadmap. It's a snapshot of a working copy; the working notes have the current status.
- [Working notes](notes.md) – where things stand, the decisions so far and why, and what's been learned about FOLIO and collectionHQ.
- [Desktop app plan](desktop-plan.md) – why and how a Windows app replaces the browser page, in phases D1 to D8.
- [In-app updates plan](updates-plan.md) – how the app will tell a library that a new version is out and install it after a yes, in phases U0 to U3 (D7 of the desktop plan).
- [Services and destinations plan](services-plan.md) – a proposal, not started, to make the services a library sends data to, and the places they receive it, objects of their own, in phases S1 to S5.
- [Improvement plan](improvement-plan.md) – the work planned after the October 2026 review, in batches, with what's done.

## History

- [Design and function review, 27 October 2026](review-2026-10-27.md) – the first full review's 71 verified findings, kept as a record.
- [Second review round, 27 October 2026](review-2026-10-27-round-2.md) – 19 findings in areas the first round covered thinly, all fixed.

## Terms

- **Site** – a folder that holds one FOLIO tenant's connection settings, its feeds, a local cache, run history and alerts.
- **Feed** – one scheduled export to one vendor, set up from a recipe plus the library's own choices.
- **Recipe** – a vendor's file layout, shared and versioned, such as `chq-academic@^1.0`. A recipe is declarative: it holds no code.
- **Service** – a third-party platform the library sends data to, such as collectionHQ.
- **Destination** – where a service receives the data: today an SFTP server, folder and account, or a local folder.
- **Dry run** – a run that builds and checks files but sends nothing: a feed with no delivery set up yet, or `run --no-deliver`.
- **Held delivery, release** – the first delivery to a destination, or the first after a change to the files' layout, waits until a person reviews and releases it.
- **The page, the app** – the local browser page (`folio-exchange ui`) and the desktop app (`folio-exchange-app`) that is to replace it.
- **collectionHQ (cHQ)** – a collection-analysis service for libraries, and the first vendor FOLIO-Exchange sends files to.
- **cHQ FOLIO Exchange Recipe** – the repository that holds the collectionHQ Academic recipe and its setup guide. It was called FOLIOHQ until 28 October 2026.
- **The legacy tool** – folio-chq-extract, the original collectionHQ-only tool (phase 0), now in the `legacy/` folder of cHQ FOLIO Exchange Recipe as a fallback.
- **Byte parity** – the recipe and the legacy tool write identical files from the same data, which that repository's CI checks.
- **Eureka, Okapi** – FOLIO's newer and older platforms. FOLIO-Exchange targets Eureka; Okapi works only where the same code covers both.
- **Kong** – the API gateway in front of Eureka-based FOLIO. Its address is the FOLIO API address FOLIO-Exchange connects to.
- **SRS** – Source Record Storage, where FOLIO keeps MARC records.
- **HRID** – the short, human-readable ID FOLIO gives each instance, holdings record and item.
- **999 ff $i** – the MARC field where FOLIO stores an instance's UUID. collectionHQ links items to bibs on it.
- **ECS** – Enhanced Consortial Support, FOLIO's setup for consortium member tenants that share records from a central tenant.
- **Flower release** – FOLIO's named releases (Sunflower, Trillium and so on), about two a year.
- **Phases and gates** – the roadmap in the [spec sheet](spec.md#roadmap). Gate 1 is collectionHQ accepting a library's first delivery; gate 2 is a second vendor's recipe that needs no core code change.
- **The pilot library** – the first library setting up FOLIO-Exchange. It isn't named in these docs.
- **Batches 1 to 5, D1 to D8, U0 to U3** – work packages in the [improvement plan](improvement-plan.md), the desktop app's phases in its [plan](desktop-plan.md), and the in-app updates' phases in [theirs](updates-plan.md).
- **The tray** – the FOLIO-Exchange icon in the Windows taskbar's notification area. It shows each watched site's feeds and tells you when a scheduled run is held, blocked or fails, with no window open.
