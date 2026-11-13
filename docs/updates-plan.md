# In-app updates plan

_The plan for D7 of [desktop-plan.md](desktop-plan.md): how FOLIO-Exchange tells a library that a new version is out, and how it installs one. Written on 12 November 2026 and revised on 13 November after a review. Only the groundwork is built: format numbers on the site's databases and a folder for this PC's own files (see Today). Phase U0 can start now. U1 needs the installer (D5), and U2 needs signed releases (D6). The open questions at the end wait on the maintainer._

## Why

A library PC keeps whatever version was installed until someone runs the installer again. Fixes don't reach it, and nobody is told they exist. Once FOLIO-Exchange runs unattended, an old copy is a risk: a fix for a delivery bug or for a change in FOLIO's API only helps the libraries that install it. Staff shouldn't have to watch GitHub or run PowerShell to stay current.

## Today

- **Versions.** `__version__` in `src/folio_exchange/__init__.py` and `version` in `pyproject.toml` (0.2.0). `--version`, the app's sidebar and the page show `version_text()`, with the commit when pip recorded one.
- **Releases.** `v0.2.0` is a GitHub pre-release (tag on a30c2c1, 12 Nov 2026). Installs follow `main` by default until gate 1. `install.cmd -Ref v0.2.0` or `@v0.2.0` installs a tag.
- **Installing and updating.** `installer/install.ps1` builds a Python environment in `<install folder>\program` and downloads FOLIO-Exchange from GitHub as a zip. Run again, it updates in place, so the scheduled task's program path doesn't change. It already warns when a FOLIO-Exchange process is running from that folder. IT staff can use pip from the git URL.
- **Not yet built.** The one-file installer (D5, PyInstaller and Inno Setup) and signing (D6, SignPath Foundation).
- **Recipes update on their own path.** Vendor recipe sources (`sources.py`) are checked and installed from the Recipes view. A program update can bring new bundled recipe versions, which the layout hold and the major-version rule already guard (see [design.md](design.md#recipes)). This plan doesn't change that.
- **The tray** (D4) runs while the person is signed in. It's the natural place to say a new version is out.
- **Format numbers** (built 12 and 13 Nov 2026). `history.sqlite` and `cache.sqlite` carry a format number (SQLite's `user_version`, `history.FORMAT` and `cache.FORMAT`, both 1), read before anything else touches the file. A version never uses a history or a cache a newer one wrote: it stops with `NewerData` or `NewerCache` and says to reinstall the newer version, or to put back the history from before the update, or to rename the cache so it's downloaded again. These guards protect only the versions released after them: 0.2.0 has none.
- **Settings files** have no number. An older version refuses a setting it doesn't know ("unknown setting"), but not a setting whose meaning changed. #31 is an example: `host_key_confirmed: false` used to mean "don't log in" and now means "pinned on first use". Going back past it, feeds saved without a vendor fingerprint are refused at login. That fails safe, but it stops those feeds.
- **The app's own folders.** `places.config_dir()` (`%APPDATA%`, which roams) holds preferences, and `places.local_dir()` (`%LOCALAPPDATA%\FOLIO-Exchange\.app`, which doesn't) holds what belongs to this PC: the tray's list of sites and its log. No site can be called `.app` (`siteops.name_problem` refuses names that start with a period).

## Goals

1. The person hears within a day that a new version is out, and what changed.
2. Installing takes one confirmation and no PowerShell, Python or Git.
3. Nothing installs without the person's yes, and never while a run or an action is going.
4. Only a release this project made can install: one signed with the project's own key. A download that fails any check is deleted and never run.
5. The scheduled task, Credential Manager entries, site folders and the tray keep working across an update.
6. Nothing about the library is sent anywhere. The check is one anonymous request a day, and IT can turn it off.

## Design

### Checking

- **Where:** the tray, and the window when it opens. Never the scheduled task: `run --due` stays quick, offline apart from FOLIO and the vendor, and has nobody to ask.
- **How often:** at most once a day per Windows user after a check that worked. The time of the last check is kept in `places.config_dir()` (`updates.json`, which also holds the person's choices below).
- **What:** one `GET https://api.github.com/repos/ByYudkowskysTentacle/FOLIO-Exchange/releases` over https, with redirects followed by hand only within `api.github.com`, as `sources.py` does for recipes. The repository is fixed in the code, not read from a setting, so a changed file can't point updates elsewhere.
- **Which releases:** the newest release whose tag is `vX.Y.Z` and higher than the running version. Drafts are never used. Pre-releases are used only when the person chose them (see open question 1). Never a lower version, so a downgrade can't be pushed.
- **Failures:** a check that fails (offline, a proxy, GitHub's rate limit) is logged and tried again two hours later, at most four times a day, honoring GitHub's `Retry-After` and `X-RateLimit-Reset`. It never shows an error dialog. GitHub allows 60 unauthenticated requests an hour per IP address, shared by every program behind a campus's address (FOLIO-Exchange's recipe sources included), and checks cluster when people sign in, so a failed morning check must not wait a whole day.
- **Privacy:** the request carries a `User-Agent` of `FOLIO-Exchange/<version>` and nothing else: no site name, tenant, user or counts. No telemetry is added.

### Telling

- The tray shows one notification per new version: "FOLIO-Exchange 0.3.0 is available." Clicking it opens Options.
- Options gets an **Updates** section:
  - the running version, and when updates were last checked;
  - "Check for updates";
  - for a newer version, its release notes (the release's text, shown as plain text) and "Install 0.3.0…";
  - "Get pre-releases" and "Check for updates automatically", both per Windows user.
- An install made with pip (no installer) gets the same notice, with the command to run instead of an Install button:
  `pip install "folio-exchange[desktop] @ git+https://github.com/ByYudkowskysTentacle/FOLIO-Exchange.git@v0.3.0"`. FOLIO-Exchange is never installed by its bare name, which isn't on PyPI.

### Downloading and checking the file

1. Download the release's installer asset (`FOLIO-Exchange-Setup-X.Y.Z.exe`), `SHA256SUMS` and `SHA256SUMS.sig`. GitHub redirects release downloads to a download host of its own, and has changed it before (to `release-assets.githubusercontent.com` in 2025, from `objects.githubusercontent.com`), so redirects are followed by hand, only to `github.com` and the download hosts listed in the code. A redirect anywhere else stops the download, and Options offers the release page instead, so a moved host never strands an installed copy. Size limit: 300 MB. Timeout: 60 seconds without progress.
2. Save to `updates\X.Y.Z\` in `places.local_dir()`, a folder only this Windows user can change (`setupops._owner_only`, as for `keys/`), under a temporary name until every check passes.
3. **The project's signature.** Check `SHA256SUMS.sig`, an Ed25519 signature over `SHA256SUMS`, against the project's public key written into the code (`updates.RELEASE_KEY`), with the `cryptography` package FOLIO-Exchange already installs for SFTP. The maintainer signs at release time with a private key kept off GitHub (an offline step in the release checklist), so taking over the GitHub account or its CI secrets isn't enough to publish an update. This is the check that ties the file to this project, and it works before D6.
4. Check the installer's SHA-256 against the signed `SHA256SUMS`.
5. **Windows' signature** (from D6). Check the Authenticode signature with `WinVerifyTrust` (through ctypes, as `permissions.py` reads access lists), its chain to a trusted root, and that the file's version resource names this product and the release's version. SignPath Foundation signs every project it serves under its own name, so its publisher name alone doesn't tie a file to this project: step 3 does. Authenticode is what Windows, SmartScreen and antivirus programs look at.
6. Mark the file as downloaded from the internet (a `Zone.Identifier` stream with zone 3), as a browser would, so Windows SmartScreen checks it when it runs. A file a program writes itself has no such mark, so without it SmartScreen would never ask.
7. Any failure deletes the file and says why in Options ("The download didn't pass its checks, so it was deleted. Nothing was installed."). A failed check is tried again with the next day's check.

Before D6, step 5 can't hold, so U1 stops after step 4, marks the file (step 6) and opens it only when the person clicks Install. The project's signature (step 3) is what makes that click safe; SmartScreen then asks as it does for a browser download.

### Installing

- **Only with a yes:** "Install 0.3.0…" shows the version, the release notes and what will happen ("FOLIO-Exchange closes, updates and opens again. Your sites, feeds and passwords stay as they are."). Cancel is the default button, as in the release dialog.
- **Never during work:** the install waits while any FOLIO-Exchange process other than the asking window and the tray runs from the program folder (the process list, as `install.ps1` reads it), which covers a scheduled run or the command line for any site, known to the app or not. It also waits while a window has an action running, and doesn't start between minute 5 and minute 15 of the hour, when the scheduled task starts.
- **A copy of each site first:** before the installer starts, the updater copies each site in the tray's list and the recent list into `updates\from-<version>\<site id>\` in `places.local_dir()`, with a note of the site's folder: `site.yaml`, `feeds\`, the site's own `recipes\` with `sources.json`, and `data\history.sqlite` through SQLite's backup API, which gives a consistent copy while the scheduled task or a window has the file open. The cache and run folders aren't copied (they can be built again), nor `keys\` (a secret file, which an update never changes) or Credential Manager. A site that can't be copied stops the update and says why.
- **The scheduled task:** before the installer starts, the updater writes `updating.json` (the version and a time) in the program folder. `run --due` skips its pass and exits 0 while that file is less than an hour old, and the next hourly start catches up. The installer removes it at the end. A file older than an hour is ignored, and `check` warns about it, so a failed update can't stop runs for good.
- **Closing and opening again:** the updater asks each open window and the tray to quit, through the local socket each already listens on for "show" (`single.py`), with a new "quit for update" message. A window with an action running refuses, and the update waits. Once they have quit and no FOLIO-Exchange process is left in the program folder, the updater (a small helper copied outside the program folder) runs the D5 Inno Setup installer per user with `/SILENT`, without `/FORCECLOSEAPPLICATIONS`, which could cut a delivery short. Afterwards it starts again the windows (with `--site`) and the tray it closed. Windows' Restart Manager isn't relied on: it closes only programs that answer an end-session message, which a tray with no window may not, and reopens only programs that registered with `RegisterApplicationRestart`.
- **The same program folder:** the installer installs into the same folder, so the scheduled task's program path, the Startup entry and desktop shortcuts keep working.
- **A changed scheduled task:** Task Scheduler keeps the definition a task was registered with, so a version that changes it (a new setting, as when tasks registered by an early version never started on battery) or moves the program (a pip install replaced by the installer) leaves existing tasks as they were. The task's description gets a definition number (`schedule.TASK_FORMAT`). `check`, Home and Options compare the registered task's number and program path (`schtasks /Query /XML`) with this install's, and warn `task-outdated` with "Register the scheduled task…". Registering needs the Windows password in a console of its own, so an update never does it quietly: when it finds the task outdated, it says so in the "Updated to …" line. Until then, the task runs the older program, whose format checks refuse a history or cache the newer one stamped higher.
- **Afterwards:** the app compares its version with the one it installed and says "Updated to 0.3.0." in Options, or that the update didn't finish and how to run the installer again. Each site's audit log records "program updated" with both versions.

### Data and going back

- Site folders, Credential Manager entries, the app's own folders and the Startup entry are never touched by an update, apart from the copies above.
- Stored formats change only by adding when they can. History already adds columns it lacks when it opens (`History`'s migrations), which older versions ignore, so a patch or minor release should leave every site readable by the release before it. A change an older version can't read raises `history.FORMAT` or `cache.FORMAT`, takes a new minor version and is said in the release notes, with the update asking first. A setting whose meaning changes, as in #31, is said in the release notes too, with what going back does to it.
- The last installer and each site's copy are kept in `updates\` until the next update passes its checks. Older ones are deleted.
- **Going back** is offered only to a version that has the installer and the format checks (0.3.0 or later); an install can't go back to 0.2.0 this way. It:
  1. uninstalls the newer program and installs the older one into a clean program folder, so no newer file (the program, Qt or Python libraries) is left behind;
  2. puts back each site's history only when the newer version raised its format, since the older one would refuse it otherwise. Runs and audit entries recorded since the update, deliveries among them, are then lost, and Options says so before it starts;
  3. renames a cache the newer version stamped higher, so the older one downloads everything again;
  4. never puts settings back wholesale, which would undo every change since the update and could start delivering a feed someone paused after it. It lists the feed files and `site.yaml` settings the older version can't load, and offers each from the copy with a before-and-after. A feed file put back this way comes back paused;
  5. lists the feeds a changed setting affects (feeds saved without a vendor fingerprint, for #31) and what to do for each.

### For IT staff

- `FOLIO_EXCHANGE_UPDATES=off` in the environment, or `"check": false` in `updates.json`, turns checking off. Options then says updates are managed by IT.
- `FOLIO_EXCHANGE_UPDATES=notify` checks and tells but never offers to install.
- The installer's own command line keeps working for software deployment tools.

## Phases

| # | Change | Done when | Size |
| --- | --- | --- | --- |
| U0 | **Notice only** (can start now). `updates.py` (no Qt): the check with its retries, version comparison, the pre-release choice and `updates.json`. The tray's notification and the Options section with release notes and, for every install today, the pip command or a link to the release page. The environment switch. The release key made, its public half in the code, and signing `SHA256SUMS` added to the release checklist. | An install of the first version with U0 says "FOLIO-Exchange 0.3.1 is available" within a day of that release, from the tray and Options, and sends nothing else. | 2 days |
| U1 | **Download and hand over** (after D5). Download to the owner-only folder, the host list with the release page as a fallback, size limits, the project's signature over `SHA256SUMS` and the installer's SHA-256, the Mark of the Web. A copy of each site before the installer opens. "Install 0.3.0…" opens the checked installer. `schedule.TASK_FORMAT` and the `task-outdated` warning, which the move from a pip install to the installer needs anyway. | A clean PC on 0.3.0 downloads 0.3.1, refuses a file with a bad signature or checksum, copies each site, and opens the installer only after the click. A task left on the pip install's program is warned about. | 3 days |
| U2 | **Signed, in place** (after D6). Authenticode with its chain and version resource, the process check, "quit for update" over the local sockets, the `updating.json` marker and `run --due` skipping, the silent install from a helper, starting the windows and the tray again, "Updated to …", the audit entry, and going back as above. | A signed 0.3.1 installs over 0.3.0 with one click while the scheduled task keeps running on the hour, and the open windows and the tray come back. A wrongly signed file is deleted, and nothing runs. Going back to 0.3.0 leaves every feed loading, and a feed paused after the update stays paused. | 3 days |
| U3 | **Docs.** Updating in the guide and `installer/README.md`, the IT switches, release notes as part of the release checklist, the signing policy for D6 and the release key naming who can publish an update. | A librarian and an IT person can each find how updates work and how to stop them. | 0.5 days |

## Tests

- The check, against a faked GitHub (as `test_sources.py` fakes it): newest higher version chosen, drafts skipped, pre-releases only when chosen, never a lower or equal version, tags that aren't `vX.Y.Z` ignored, at most one request a day after a check that worked, a failed check tried two hours later and at most four times a day, `Retry-After` honored.
- Hosts: a redirect to any host but the allowed ones stops the download, saves nothing and offers the release page.
- Downloads: over the size limit, a stalled transfer, a missing or bad `SHA256SUMS.sig`, a checksum that differs: each deletes the file, says why and runs nothing. A file signed with another key is refused, whatever its Authenticode signature.
- Windows' signature (U2): a stand-in for `WinVerifyTrust` that answers unsigned, untrusted chain, another product in the version resource, or right; only the last proceeds.
- The Mark of the Web is written on the saved installer.
- Site copies: a history being written by another connection is copied whole (the backup API), the cache, run folders and `keys\` are left out, and a site that can't be copied stops the update.
- Closing: "quit for update" closes an idle window and the tray, a window with an action running refuses, and the install waits while another FOLIO-Exchange process runs from the program folder. The windows and the tray it closed are started again.
- Going back: settings never put back wholesale, a feed file put back comes back paused, history put back only when its format was raised, a cache stamped higher renamed.
- The task: a registered task with an older `TASK_FORMAT`, or another program path, warns `task-outdated`; the current one doesn't.
- Never during work: a running process, a running action, or minutes 5 to 15 each hold the install and say why.
- `run --due` skips while `updating.json` is new, runs once it's an hour old, and `check` warns then.
- Wording: the Options section and notifications follow [wording.md](wording.md), label in name, mnemonics unique. No request in `run --due` (a test patches `requests` to fail there).
- Privacy: the request's headers hold only the version.

## Risks

- **A compromised release.** Someone who takes over the GitHub account could publish a release. The project's signature over `SHA256SUMS`, made with a key that never sits on GitHub, is the defense, from U1 on. SignPath's signature adds Windows' trust but not a tie to this project.
- **The release key.** Lost, no update can be published until a version with a new public key is installed by hand; stolen, it can sign a bad update. It's kept offline with a backup, and the signing policy (U3) says who holds it and how it's replaced.
- **SmartScreen and antivirus** may flag a new, unsigned installer. Signing (D6) and the one-folder build reduce it. Until then, the release notes say how to get past SmartScreen once.
- **A run during an update.** The process check, the marker and the quiet minutes cover the scheduled task and the command line.
- **Proxies.** `requests` uses the system's proxy settings, but a proxy that needs a sign-in breaks the check. It fails quietly, and IT can turn checks off and deploy updates themselves.
- **pip installs** never update themselves. They get the command to run. The installer (D5) is the supported route for libraries.
- **A bad release.** Keeping the previous installer and each site's copy gives a way back. The additive-formats rule keeps the older version able to read the sites, and the format numbers stop it misreading a database it can't. A setting whose meaning changed is the gap the format numbers don't cover, which is why the release notes and going back list it.
- **Losing what happened since the update.** Putting a site's history back drops the runs and audit entries recorded after the update, including deliveries, so the next delivery may be compared with an older one. That's why the history is put back only when the older version couldn't read it otherwise, and why Options says so first.
- **No backup outside updates.** The copies here protect an update, not a failed disk. A `backup` command and its restore, for the site's settings and history, are a separate piece of work.

## Open questions

1. **Pre-releases:** until 1.0 every release is a pre-release. Should installs check pre-releases by default until then (recommended for the pilot), or only once someone ticks "Get pre-releases"?
2. **Always ask (recommended), or allow "install updates at night"** for PCs nobody signs in to? That would need the scheduled task to install, against the rule that it never does more than run feeds.
3. **Where the check runs:** the tray and the window (recommended), or the tray only, so a PC without the tray never checks?
4. **Going back:** keep the previous installer and site copies (recommended), or point to the release page only?
5. **Release notes:** the GitHub release text as written, or a short `CHANGES.md` section that the app shows?
6. **The release key:** who holds it, where it's kept offline, and who holds the backup?
