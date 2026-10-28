# Desktop app plan

_28 October 2026. Decisions taken; D1 (groundwork and shell) and D2 (feeds and activity) are built, D3 is next. Batch 3 (large catalogues) is tabled, because the pilot library's catalogue is about 70,000 records._

## Why

Today a librarian opens FOLIO-Exchange by double-clicking a `.cmd` launcher. A black console window opens and must stay open, and the page opens as a browser tab through a one-time link. A bookmark or a closed tab sends them back to the launcher. The installer also needs Python, installed through winget, and pip downloads from GitHub and PyPI. All of this works, but none of it looks or behaves like an ordinary Windows program, and each step is a place where staff get stuck.

A desktop app should:
- open from the Start menu or a desktop icon into a window of its own, with no console window and no browser;
- install from one signed installer, with no Python, Git or PowerShell steps;
- keep everything that works now: the command line, the hourly scheduled task, and the safety rules (dry runs, held first deliveries, secrets only in Credential Manager).

## Decisions

| # | Question | Decision | Why |
| --- | --- | --- | --- |
| 1 | How to build it | **A native Python app with PySide6 (Qt), now.** The browser page stays usable until the app matches it. | One language (the core is about 11,100 lines of tested Python). Mature screen reader support, native dialogs, tray and notifications built in. No local web server, so no localhost attack surface to defend. Compared with a page in a native window (pywebview: fast, but keeps the web code and server), a Rust interface over the Python core (two languages, still ships Python), a full Rust rewrite (4 to 6 months and byte parity re-proved), wxPython, Tkinter (weak accessibility), Flet and C#/WinUI. |
| 2 | How to install it | **Both, installer first.** A self-contained installer for library staff; the PowerShell and pip route stays, documented, for IT staff and developers. | No Python, winget or pip on a library PC. |
| 3 | Code signing | **SignPath Foundation** (free signing for open source projects). The pilot ships unsigned until SignPath accepts the project. | Free, and fits a public MIT project built on GitHub Actions. Windows names "SignPath Foundation" as the publisher. Azure Artifact Signing paid from the Microsoft for Nonprofits Azure grant remains the route to the organisation's own name later. |
| 4 | Platforms | **Windows only** for the installer. | The pilot runs Windows. PySide6 still runs from a pip install on Mac and Linux, untested. |
| 5 | Tray icon and notifications | **Now, in the first build.** | Failed, held and waiting runs show up without the app open. |

## Design

- **The app** (a `src/folio_exchange/desktop/` package, `folio-exchange-app` under `gui-scripts` so Windows opens no console) is one main window per site: a sidebar of feeds and views, as on the page today. PySide6 goes in a `desktop` extra, so the command line and the scheduled task never import Qt.
- **The core stays the core.** The app calls the same modules as the command line and the page: `feedops`, `setupops`, `siteops`, `wizard`, `sources`, `runner`, `history`. The data shaping that now sits in `ui.py` (`site_summary`, `feed_detail`, `recipes_overview`, `check_connection`, `read_rows`, the `Jobs` runner) moves first into a shared module, so the page and the app show the same facts while both exist.
- **Long actions** (sync, dry run, run, release, recipe install) run on a worker thread, one at a time per site as now, with progress and the log streamed to the window. Closing the window during one asks first. The site lock still stops a scheduled run and the app from clashing.
- **One window per site:** starting the app again for an open site brings its window to the front (a per-user named local socket, `QLocalServer`). This replaces the page's one-time link, cookie, tokens and launch handshake, which go when the page is retired.
- **The tray** is the same program started at sign-in with `--tray` (opt-in, a per-user Startup entry). It reads the run history the scheduled task writes, raises a Windows notification when a run fails, a delivery is held, or one waits for release, and opens the app when clicked. It never takes the run lock and never runs feeds itself.
- **Secrets** stay in Credential Manager; the SFTP key saved from the app is still an owner-only file in the site's `keys` folder. The app's password boxes never echo or log what is typed.
- **Accessibility:** every control has an accessible name, a sensible tab order and a keyboard shortcut where the page has one; checked with Narrator and NVDA, at 200% scaling and in a high-contrast theme.

## Phases

| # | Change | Done when | Size |
| --- | --- | --- | --- |
| D1 | **Groundwork and shell.** Move the page's data shaping and job runner out of `ui.py` into a shared module (the page keeps working, its tests unchanged). The `desktop` extra and `folio-exchange-app` entry point. Main window, sidebar, site picker (recent sites, "Set up a new site", open a folder), one window per site, the worker thread with progress and the close-while-running question. Widget tests with pytest-qt, run offscreen on CI. | The app opens a site and shows its feeds; a second start focuses the open window; CI runs the widget tests on Windows and Linux. | 4 days |
| D2 | **Feeds and activity.** Home, a feed's page (state, schedule, layout, last runs, read the rows sent), run now, dry run, pause and resume, release a held delivery, open a run's folder, the activity and audit views. | A librarian can do everything the page's feed and activity views allow. | 5 days |
| D3 | **Setup, recipes and options.** Connecting to FOLIO (with the connection check), the new-feed wizard (recipe, locations and statuses to leave out, schedule), SFTP with the host key check and a key file picker, alert addresses, the recipe sources view (new versions, what changed, install), and Options (rename the site folder, register the scheduled task). | A new library sets up a site and its first feed in the app alone. | 5 days |
| D4 | **Tray and notifications.** `--tray`, the opt-in Startup entry, notifications for failed, held and waiting runs, a menu with each feed's state. | A held delivery shows a notification within a few minutes, without the app open. | 2 days |
| D5 | **The installer.** PyInstaller one-folder build of the app and the command line (Python inside), wrapped in a per-user Inno Setup installer: no administrator rights, Start menu and desktop shortcuts, an uninstaller, the tray offer, and an offer to register the scheduled task against the bundled command line (it still asks for the Windows password). CI on `windows-latest` builds it for each **release tag** and attaches it to the GitHub release. The installer guide keeps the PowerShell and pip route for IT staff. | A clean Windows 10 PC with no Python installs and opens the app from one download, and the scheduled task runs the bundled command line. | 3 days |
| D6 | **Signing with SignPath Foundation.** Apply with the project's details; publish `docs/code-signing.md` (the signing policy SignPath asks for: who may approve a release, how builds are made); multi-factor sign-in for everyone with release rights; CI sends the built executables and installer to SignPath for signing before attaching them to the release. Until accepted, releases are unsigned and the guide says how to get past SmartScreen once. | A signed release shows "SignPath Foundation" as publisher. | 1 day plus SignPath's review |
| D7 | **Updates.** The app checks GitHub releases at most once a day, offers a newer signed installer, installs only after the person confirms, and never while a run is going. The program's path does not change, so the scheduled task keeps working. | An older install shows "Version x.y is available" and updates in place. | 1 day |
| D8 | **Retire the page** (a decision when D3 is done and the pilot has used the app). Remove `ui.py`'s server, `web/`, the `ui` and `launcher` commands and their tests; Mac and Linux users run the app from pip. | One interface left, and no local web server. | 1 day |

About five weeks to D7. D1 to D4 can be used from a pip install (`pip install "folio-exchange[desktop] @ git+https://github.com/ByYudkowskysTentacle/FOLIO-Exchange.git@main"`; FOLIO-Exchange is not on PyPI, so never the bare name) before the installer exists. While both interfaces exist, new features go into the app only, and the page gets fixes only.

## Risks

- **Size:** PySide6 makes the installer about 60 to 100 MB. The build keeps only QtCore, QtGui, QtWidgets and QtNetwork, and leaves out the rest of Qt.
- **LGPL:** Qt for Python is LGPL. The one-folder build keeps Qt's libraries as separate, replaceable files, and the installer ships the licence text and says where they are.
- **Antivirus false positives** on PyInstaller builds: the one-folder build (not one-file) and signing (D6) cut them down; until SignPath accepts the project, pilot IT may need to allow the program.
- **Two interfaces for a while:** the shared module (D1) keeps their facts the same, and features freeze on the page.
- **The scheduled task's program path** changes when a pip install moves to the installed app: the installer offers to register the task again. If that is missed, the existing warning (no scheduled run for two hours) still catches it.
- **CLAUDE.md asks for no heavy dependencies without reason.** PySide6 is the reason here, so it lives in the `desktop` extra and the core and command line do not import it.
- **SignPath's terms** require builds from the public repository on CI and a published signing policy; it may decline or take weeks. Azure Artifact Signing through the nonprofit grant is the fallback.
- **The spec** says the web UI comes in phase 2. A native desktop app replaces it, so the live spec's roadmap needs updating.
