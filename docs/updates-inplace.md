# Updating a script-based install in place

_A route for in-app updates that needs neither the one-file installer (D5) nor Windows code signing (D6). Written on 16 November 2026. The engine (`selfupdate.py` and `swaphelper.py`) and its wiring to the app (`desktop/installing.py`, "Install X.Y.Z…" under Options, below) are built, with tests. It waits only for the release key: while `updates.RELEASE_KEY` is `None`, Options shows no Install button. It follows [updates-plan.md](updates-plan.md) for checking, telling, "never during work", site copies and going back, and replaces that plan's download-and-run-an-installer steps for installs made by `installer/install.ps1` (see that plan's "Two routes"). [design.md](design.md) has the modules' signatures._

## Why

`install.ps1` builds a Python environment in `<install folder>\program` and installs FOLIO-Exchange into it from a GitHub zip. Run again, it updates in place, so the scheduled task's program path never changes. Every library install today is one of these. The plan's U1 and U2 wait on an Inno Setup installer and on Authenticode, which exist for Windows' benefit: SmartScreen, antivirus programs and Windows' own trust. If nothing downloaded is ever run as a program, none of that applies. What ties an update to this project is the project's own Ed25519 signature over `SHA256SUMS` (the plan's step 3), and that works now.

## How it works

`selfupdate.prepare(tag, key=…)` does everything that can happen while the app is open. The old version keeps running throughout.

1. **Is this an install it can update?** `find_install` looks for `install.ps1`'s layout: the running Python is an environment called `program`, with a `pyvenv.cfg`, FOLIO-Exchange installed inside it (not run from source), and the base Python it was made from still there. Anything else is refused with a sentence and, for pip and editable installs, the command to run instead: `pip install "folio-exchange[desktop] @ git+https://github.com/ByYudkowskysTentacle/FOLIO-Exchange.git@vX.Y.Z"`.
2. **A key, a newer version, and no work going.** With no release key the engine refuses before downloading anything. The tag must be `vX.Y.Z` and higher than the running version. It refuses while another FOLIO-Exchange program runs from the program folder (the process list, as `install.ps1` reads it, allowing this process and the launchers that started it from the program folder, and any other process the caller names, such as the tray). A process list that can't be read counts as busy, never as "nothing is running", during minutes 5 to 15 of the hour, while another update's marker is less than an hour old, and while `program.outgoing` is left from an update that didn't finish.
3. **Download and check.** Into `updates\X.Y.Z\` in `places.local_dir()`, a folder only this Windows user can change (`setupops._owner_only`), one update at a time (a lock file). `SHA256SUMS` and `SHA256SUMS.sig` come first. The signature is checked with the `cryptography` package against the key passed in. Only then is the zip downloaded and its SHA-256 compared with the signed list. Every file has a temporary `.part` name until every check passes. Downloads start at `https://github.com/ByYudkowskysTentacle/FOLIO-Exchange/releases/download/<tag>/`, redirects are followed by hand to `github.com`, `release-assets.githubusercontent.com` and `objects.githubusercontent.com` only, over https, with a size limit (64 KB for the small files, 50 MB for the zip) and a stall timeout (no bytes for 60 seconds). The session sends only a `User-Agent` of `FOLIO-Exchange/<version>` and an `Accept` header: requests' default headers are cleared, and an `auth` that adds nothing keeps it from sending an account from the person's `.netrc`. The system's proxy settings still apply. Any failure deletes the files.
4. **Clean up the update before.** Once the new download passes, older downloads and site copies in `updates\` are deleted. There's always one way back: the copies are deleted only after a new set is ready to replace them.
5. **A copy of each site.** Each site the tray watches and each recent site is copied into `updates\from-<running version>\<site id>\`: `site.yaml`, the feeds folder, the site's own recipes folder (with `sources.json`), and `history.sqlite` through SQLite's backup API, which gives a consistent copy while another program writes it. `site.json` notes the site's folder. The cache, run folders, logs, `keys\` and Credential Manager aren't copied. A folder with no `site.yaml` is skipped. A site that can't be copied stops the update.
6. **Build beside the old one.** `program.new` is made with the base Python of the current program folder (`python -m venv`), FOLIO-Exchange is built into a wheel from the checked zip, then installed with the zip's own `constraints.txt`, and with Qt when the current program has it. Then `program.new`'s `folio-exchange --version` must start and report the new version. Last, every command in `program.new\Scripts` is pointed at `program` (`retarget_scripts`): pip writes the full path of the environment's Python into the `#!` line of each command it makes, which on Windows sits between a small launcher and a zip of the script. Only that path changes. If FOLIO-Exchange's own two commands can't be changed, the update stops. A failure deletes `program.new`.
7. **A plan for the swap**, `updates\X.Y.Z\plan.json`: the folders, both versions, the base Python and where to write the result.

`selfupdate.start_swap(plan, restart=[…])` copies `swaphelper.py` into `updates\helper\`, writes the marker and starts the helper with the base Python (`-I`, so nothing from the program folder or the current folder is imported). The app then quits. `selfupdate.swap(plan)` runs the same step in-process, for tests and the command line.

The helper, which uses the standard library only:

1. waits up to two minutes for every program in the program folder to quit and for the minutes 5 to 15 to pass, and stops if either is still true or if the list of running programs can't be read;
2. renames `program` to `program.outgoing` and waits up to ten seconds for any program caught running from it to quit. A program counts if it's listed under either name, since Windows goes on listing a running program under the path it started from. If one is still there, or the list can't be read, it renames the folder back and stops;
3. renames `program.new` to `program`, so the scheduled task, the Startup entry and the launchers keep their paths. Its commands already name `program`, so there's no moment when they point at a folder that's gone;
4. checks that `program\Scripts\folio-exchange.exe --version` reports the new version;
5. on success, removes the `program.previous` from the update before and renames `program.outgoing` to `program.previous`;
6. removes the marker, whatever happened (even when the plan can't be read: `start_swap` passes the install folder and the result file on the command line too), writes `updates\result.json`, and starts the programs in `restart` again (only programs inside the program folder).

## The marker

`updating.json` (the version and a start time) sits beside the program folder, not in it as the plan says, because the folder itself is swapped. While it's less than an hour old, nothing starts from the program folder (`updating.py`, read first by `cli.main` and the desktop app's `main`, which also starts the tray). `run --due` skips its pass and exits 0; every other command and the app say "FOLIO-Exchange is being updated. Try again in a few minutes." and exit 1. `--version` and `--help` still answer, since the swap's own check runs `--version`.

This matters because Windows does let a folder be renamed while a program runs from it, as long as nothing holds a data file open there or has its current folder inside. A program that kept running across the swap would load its next modules from the other version, so a delivery could finish on a mix of the two. The process check before the rename, the marker for programs started after it, and the check of `program.outgoing` after it close that gap together: a program that started before the marker was written, and wasn't listed, is caught running from `program.outgoing`.

A marker more than an hour old, or dated more than five minutes ahead, is ignored, so a failed update can't stop runs for good.

## What it trusts

- **The release key** passed to `prepare`. The engine holds no key and makes none. U0 adds `updates.RELEASE_KEY`, the public half of a key the maintainer keeps off GitHub, and the wiring passes it in. Tests make a throwaway key pair in memory.
- **`SHA256SUMS`, once its signature checks out.** It must list `folio-exchange-X.Y.Z.zip` for the version asked for, so an older release's signed files can't be passed off as a newer one. The new program must also report that version before it's swapped in.
- **The source zip**, as a release asset. GitHub's own "Source code (zip)" is made on request, and GitHub doesn't promise the same bytes each time, so a signed checksum of it could stop matching. The release checklist adds a step: `git archive --format=zip --prefix=FOLIO-Exchange-X.Y.Z/ -o folio-exchange-X.Y.Z.zip vX.Y.Z`, list it in `SHA256SUMS`, sign that file offline, and attach all three to the release. (`git archive` writes the commit into the zip's comment, which `install.ps1` reads.)
- **PyPI, for the parts FOLIO-Exchange needs**, at the versions in the zip's `constraints.txt`, as `install.ps1` installs them. The signature covers FOLIO-Exchange's own code and the versions it asks for, not the bytes PyPI serves for them. Hash-pinned constraints would close that gap and are a separate change.
- **The base Python** the program folder was made with, read from `pyvenv.cfg`.

## What it refuses

Each refusal returns a sentence for the person and leaves the old version running:

- an install it can't update (pip, editable, or a base Python that's gone), with the command or "Run the installer again.";
- no release key, or one that isn't a 32-byte Ed25519 key;
- a tag that isn't `vX.Y.Z`, or a version that isn't higher than the running one;
- another FOLIO-Exchange program running from the program folder, minutes 5 to 15, an update less than an hour old, or `program.outgoing` left from an unfinished update;
- a download sent to a host off the list or over plain http, more than four redirects, a file over its size limit, a transfer that stalls for 60 seconds, a missing file, or GitHub out of reach;
- a missing or bad signature, signed checksums that don't list this version's zip, or a zip whose SHA-256 differs;
- a site that can't be copied;
- a build that fails, or a new program that doesn't start or reports another version.

Nothing downloaded runs until the signature and the checksum have passed: the builder is never called before, and the tests check that.

## Results

Every step returns a dict a window can show, in the style of `siteops.rename_site`'s result:

| Key | Meaning |
| --- | --- |
| `ok` | The step finished. |
| `stage` | Where it got to or stopped: `install`, `key`, `version`, `busy`, `download`, `verify`, `copy`, `build`, `check`, `ready` or `swap`. |
| `program` | Which version is installed now: `old`, `new`, or `none` when a failed swap couldn't put the old folder back. |
| `done`, `left` | Sentences: what was done ("Downloaded FOLIO-Exchange 0.3.0 and checked its signature.") and what's left for the person. |
| `reason` | Why it stopped, as one or two sentences that follow [wording.md](wording.md). |
| `plan`, `command` | The plan file for `start_swap`, and the pip command for an install that can't update itself. |

`selfupdate.last_result()` reads what the last swap did, for "Updated to 0.3.0." or its reason in Options.

## Recovering from a failed update

- **Before the swap** (any check, the copies, the build): the old program was never touched. `program.new` and any unchecked download are deleted.
- **The first rename fails** (a program holds a file open in the folder, or has its current folder there): nothing changed.
- **A program is caught running from `program.outgoing`** after the first rename: the folder is renamed back, and nothing changed.
- **The second rename fails:** `program.outgoing` is renamed back to `program`.
- **The new program fails after the swap** (`--version` fails or reports another version): the new folder is renamed to `program.failed` and deleted, and `program.outgoing` becomes `program` again.
- **Putting the old folder back fails**, or the helper is killed between the two renames (a window of milliseconds): `program` may be missing, and the result says `program: none` with what to do. Rename `program.outgoing` to `program`, or run the installer again, which makes `program` afresh. Until then the scheduled task can't start, and `prepare` refuses to begin another update.
- **The marker** is removed in every outcome. If the helper dies, `run --due` ignores the marker after an hour.
- **Going back** after a good update: the previous version stays in `program.previous` until the next successful update, and its sites' copies in `updates\from-<version>\`. Going back is the plan's procedure (put history back only when the newer version raised its format, never settings wholesale); swapping `program.previous` back in is the in-place equivalent of reinstalling the older version and isn't wired yet.

## Limits

- On Linux, pip writes a `/bin/sh` wrapper instead of a `#!` line for a path over 127 characters or with spaces. `retarget_scripts` doesn't change those, so such an update stops before the swap. `install.ps1` installs are Windows only.
- A process this user can't open (another user's, or Windows' own) is left out of the process list, as `install.ps1` leaves it out.
- Building the new environment downloads FOLIO-Exchange's dependencies from PyPI, so an update needs pypi.org and files.pythonhosted.org as well as GitHub, as `install.ps1` does.
- The helper is a Python script, not a signed program. It's copied from the running, already-installed version, never downloaded.

## In the app

`desktop/installing.py` wires the engine to the Updates section of Options.

- **When it's offered.** "Install X.Y.Z…" shows only in the "on" mode, for a newer version, on an install `find_install` says it can update, with a key in `updates.RELEASE_KEY`, the only key the app ever passes (no setting, file or environment variable supplies one). Otherwise U0's notice stays as it is, with a line saying why there's no button: "FOLIO-Exchange was installed with pip, so it can't update itself." (or `find_install`'s other reasons), or "This version of FOLIO-Exchange can't install updates itself." while there's no key. `FOLIO_EXCHANGE_UPDATES=notify` tells and never offers to install; `off` says IT manages updates.
- **The question.** The version, the release notes as plain text, and "FOLIO-Exchange closes, updates and opens again. Your sites, feeds and passwords stay as they are." Cancel is the default button. Nothing happens without Install.
- **The update is the window's running action** (a job, "program update"), so no other action starts in that window or its browser page meanwhile, and closing the window asks first. Its progress shows under Options as it logs: the download, the signature, each site copied, the build. Each of the engine's refusals shows there too, with what was done and what's left, and Install can be tried again.
- **Closing the others.** Before `prepare`, the window notes which windows (one per known site) and whether the tray are open on their local sockets (`single.probe`), so their processes don't count as "another FOLIO-Exchange program". Nor do their ancestors or this window's: on Windows the program folder's `folio-exchange-app.exe` starts the venv's `pythonw.exe`, which starts the base Python that answers on the socket, and both stay running from the program folder (found by walking `swaphelper.parent_pids()`, up to three steps up, as `own_chain` does for this window). The downloads use the engine's `download_session()`. Once the release is ready, and outside minutes 5 to 15, it sends each one `quit-for-update` (`single.ask_to_quit`). A window with nothing running answers `ok` and closes, along with its open dialogs; one with an action, a pause, a rename or a release question under way answers `busy`, and Options says "Waiting for the window for Lakeside to finish its action." After 15 minutes of that it stops, starts again the ones it closed, and says so. The tray always says yes.
- **The swap.** `start_swap` gets the commands that start again the windows it closed (`--site`), the tray and this window on Options, and the window quits. The helper starts them once the swap is done, or once the old version is back.
- **Afterwards.** For a day, Options says "Updated to X.Y.Z." or "The update to X.Y.Z didn't finish. FOLIO-Exchange A.B.C is still installed." with the helper's reason (`last_update`). Once its first reading of the sites has ended, the first window or tray to start writes "program updated" with both versions in each known site's audit log (`record_update`), once per site. A history that's busy is tried again a few times, and a site that still can't be written is tried again at the next start that day.

## Left to do

- `check` warning about a marker older than an hour.
- Going back: swapping `program.previous` back in, with the plan's rules for history and settings.
