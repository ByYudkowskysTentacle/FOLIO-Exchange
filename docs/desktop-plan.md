# Desktop app plan

_28 October 2026. A plan for review: nothing here is built yet. Batch 3 (large catalogues) is tabled, because the pilot library's catalogue is about 70,000 records._

## Why

Today a librarian opens FOLIO-Exchange by double-clicking a `.cmd` launcher. A black console window opens and must stay open, and the page opens as a browser tab through a one-time link. A bookmark or a closed tab sends them back to the launcher. The installer also needs Python, installed through winget, and pip downloads from GitHub and PyPI. All of this works, but none of it looks or behaves like an ordinary Windows program, and each step is a place where staff get stuck.

A desktop app should:
- open from the Start menu or a desktop icon into a window of its own, with no console window and no browser;
- install from one signed installer, with no Python, Git or PowerShell steps;
- keep everything that works now: the page's views, the command line, the hourly scheduled task, and the security model.

## Recommended approach: a native window around the page we have

The page (`ui.py` with `web/app.js`, about 3,800 lines) already has every view and action, and its tests. The app shows that same page in a native window, so no view is rewritten.

| Option | What it means | Verdict |
| --- | --- | --- |
| **A. Native window around the page** (pywebview, using Microsoft Edge WebView2 on Windows) | A small `desktop.py` starts the local server, as `ui` does now, and opens its one-time link in a window rather than a browser tab. | **Recommended.** It reuses every view and test. WebView2 comes with Windows 11 and current Windows 10. pywebview is BSD-licensed and adds about 15 MB. |
| B. Rewrite the views in a native toolkit (Qt/PySide6, Tkinter, wx) | A second UI to write and keep in step with the first. | No. Weeks of work to reach what exists, and Qt adds about 100 MB and LGPL obligations. |
| C. Electron or Tauri shell with Python running beside it | A second language and toolchain (Node or Rust) for one window. | No. Too much to maintain for a project this size. |

What stays the same:
- **Scheduled runs** still come from Task Scheduler, which runs the command-line program with no window. Closing the app never stops a run.
- **The command line** stays for IT staff and for the guide.
- **`folio-exchange ui`** still serves the page to a browser on Mac and Linux, and wherever WebView2 is missing.
- **The security model stays.** The server listens on 127.0.0.1 only, with a random `fx-….localhost` name, a one-time link swapped for a cookie, and a token on every action. The app is the only thing that sees the link.

## Phases

| # | Change | Done when | Size |
| --- | --- | --- | --- |
| D1 | **The app window.** A new `folio-exchange-app` entry point under `gui-scripts`, so Windows opens no console. It serves the page on a free port and opens it in a pywebview window titled with the site's name. Starting it again for the same site brings the open window to the front (the existing `whoami` and `launch` handshake). Closing the window while a run or release is going asks first, as Ctrl+C does now. Without WebView2 it falls back to the browser and says why. pywebview goes in a `desktop` extra, so the command-line install stays light. | Double-clicking opens one window, with no console, for the right site. A second double-click focuses that window. Tests cover the window layer with a fake webview. | 2 days |
| D2 | **Desktop touches in the page.** Native dialogs for choosing a site folder, an SFTP key file and an export location (the page asks the window for them, and in a browser it keeps its text boxes). An About box with the version and commit. A site picker when no site is given, listing recent sites and offering "Set up a new site", which runs the `init` steps in the page. | A new user picks or creates a site without typing a path. | 2 days |
| D3 | **A self-contained Windows installer.** Build the app and the command line with PyInstaller (one folder, both executables, Python inside) on CI's `windows-latest`, wrapped in a per-user Inno Setup installer: no administrator rights, Start menu and desktop shortcuts, an uninstaller, and an offer to register the scheduled task (it still asks for the Windows password). The PowerShell installer stays for IT staff and for updating pip installs. Needs **release tags** (open item D1 in the improvement list): each tag builds an installer attached to the GitHub release. | A clean Windows 10 PC with no Python installs and opens the app from one download. The scheduled task runs the bundled command line. | 3 days |
| D4 | **Code signing.** Sign both executables and the installer, so SmartScreen does not warn "unknown publisher". Azure Trusted Signing costs about USD 10 a month and needs an organisation it can validate. An OV certificate costs about USD 200 to 400 a year. | Windows shows a named publisher and no SmartScreen block. | 1 day plus the validation wait |
| D5 | **Updates.** The app checks the GitHub releases (once a day, at most) and offers a newer signed installer. It installs only after the person confirms, and never while a run is going. The scheduled task keeps working across an update because the program's path does not change. | An older install shows "Version x.y is available" and updates in place. | 1 day |
| D6 | **Optional: a tray icon and notifications.** A small resident process started at sign-in shows each feed's state and raises a Windows notification when a run fails, a delivery is held, or one waits for release. It reads the run history the scheduled task writes, and opening it opens the app. | A held delivery shows a notification within a few minutes, without the app open. | 2 days |

D1 and D2 can ship before D3, inside today's pip install (the installer would add the `desktop` extra and an app shortcut). D3 and D4 go together, since an unsigned installer is blocked by SmartScreen on many library PCs.

## Decisions needed

1. **Approach:** a native window around the page (A, recommended), or a rewrite.
2. **Distribution:** a self-contained installer (D3, recommended), or keep the Python-and-pip installer and only add the window.
3. **Code signing:** Azure Trusted Signing, a bought certificate, or unsigned for the pilot only (staff then click through SmartScreen once).
4. **Platforms:** the packaged app for Windows only (recommended), with Mac and Linux keeping the browser page.
5. **Tray and notifications (D6):** now, or after gate 1.

## Risks

- **WebView2 missing or old** on locked-down PCs: the browser fallback (D1) covers this, and the installer can install the Evergreen runtime per user.
- **Antivirus false positives** on PyInstaller builds: signing (D4) and the one-folder build (not one-file) cut them down a lot.
- **The scheduled task's program path** changes when a pip install moves to the packaged app: the installer re-registers the task. If that fails, the existing warning (no scheduled run for two hours) still catches it.
- **CLAUDE.md asks for no heavy dependencies without reason.** pywebview (and pythonnet on Windows) is the reason here, so it lives in an optional extra, and the core and command line do not import it.
- **The spec** says the web UI comes in phase 2. A desktop app changes how that UI is delivered, so the live spec's roadmap should be updated if this plan is accepted.
