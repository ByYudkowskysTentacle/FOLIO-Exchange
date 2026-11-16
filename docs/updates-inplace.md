# Updating a script-based install in place

_A route for in-app updates that needs neither the one-file installer (D5) nor Windows code signing (D6). Written on 16 November 2026. The engine (`selfupdate.py` and `swaphelper.py`) and its wiring to the app (`desktop/updating.py`, "Install X.Y.Z…" under Options, below) are built, with tests. It waits only for the release key: while `updates.RELEASE_KEY` is `None`, Options shows no Install button. It follows [updates-plan.md](updates-plan.md) for checking, telling, "never during work", site copies and going back, and replaces that plan's download-and-run-an-installer steps for installs made by `installer/install.ps1` (see that plan's "Two routes"). [design.md](design.md) has the modules' signatures._

## Why

`install.ps1` builds a Python environment in `<install folder>\program` and installs FOLIO-Exchange into it from a GitHub zip. Run again, it updates in place, so the scheduled task's program path never changes. Every library install today is one of these. The plan's U1 and U2 wait on an Inno Setup installer and on Authenticode, which exist for Windows' benefit: SmartScreen, antivirus programs and Windows' own trust. If nothing downloaded is ever run as a program, none of that applies. What ties an update to this project is the project's own Ed25519 signature over `SHA256SUMS` (the plan's step 3), and that works now.

## How it works

`selfupdate.prepare(tag, key=…)` does everything that can happen while the app is open. The old version keeps running throughout.

1. **Is this an install it can update?** `find_install` looks for `install.ps1`'s layout: the running Python is an environment called `program`, with a `pyvenv.cfg`, FOLIO-Exchange installed inside it (not run from source), and the base Python it was made from still there. Anything else is refused with a sentence and, for pip and editable installs, the command to run instead: `pip install "folio-exchange[desktop] @ git+https://github.com/ByYudkowskysTentacle/FOLIO-Exchange.git@vX.Y.Z"`.
2. **A key, a newer version, and no work going.** With no release key the engine refuses before downloading anything. The tag must be `vX.Y.Z` and higher than the running version. It refuses while another FOLIO-Exchange program runs from the program folder (the process list, as `install.ps1` reads it, allowing the asking window and the tray), during minutes 5 to 15 of the hour, while another update's marker is less than an hour old, and while `program.outgoing` is left from an update that didn't finish.
3. **Download and check.** Into `updates\X.Y.Z\` in `places.local_dir()`, a folder only this Windows user can change (`setupops._owner_only`), one update at a time (a lock file). `SHA256SUMS` and `SHA256SUMS.sig` come first. The signature is checked with the `cryptography` package against the key passed in. Only then is the zip downloaded and its SHA-256 compared with the signed list. Every file has a temporary `.part` name until every check passes. Downloads start at `https://github.com/ByYudkowskysTentacle/FOLIO-Exchange/releases/download/<tag>/`, redirects are followed by hand to `github.com`, `release-assets.githubusercontent.com` and `objects.githubusercontent.com` only, over https, with a size limit (64 KB for the small files, 50 MB for the zip) and a stall timeout (no bytes for 60 seconds). The request carries a `User-Agent` of `FOLIO-Exchange/<version>` and nothing else. Any failure deletes the files.
4. **Clean up the update before.** Once the new download passes, older downloads and site copies in `updates\` are deleted. There's always one way back: the copies are deleted only after a new set is ready to replace them.
5. **A copy of each site.** Each site the tray watches and each recent site is copied into `updates\from-<running version>\<site id>\`: `site.yaml`, the feeds folder, the site's own recipes folder (with `sources.json`), and `history.sqlite` through SQLite's backup API, which gives a consistent copy while another program writes it. `site.json` notes the site's folder. The cache, run folders, logs, `keys\` and Credential Manager aren't copied. A folder with no `site.yaml` is skipped. A site that can't be copied stops the update.
6. **Build beside the old one.** `program.new` is made with the base Python of the current program folder (`python -m venv`), FOLIO-Exchange is built into a wheel from the checked zip, then installed with the zip's own `constraints.txt`, and with Qt when the current program has it. Then `program.new`'s `folio-exchange --version` must start and report the new version. A failure deletes `program.new`.
7. **A plan for the swap**, `updates\X.Y.Z\plan.json`: the folders, both versions, the wheel, the base Python and where to write the result.

`selfupdate.start_swap(plan, restart=[…])` copies `swaphelper.py` into `updates\helper\`, writes the marker and starts the helper with the base Python (`-I`, so nothing from the program folder or the current folder is imported). The app then quits. `selfupdate.swap(plan)` runs the same step in-process, for tests and the command line.

The helper, which uses the standard library only:

1. waits up to two minutes for every program in the program folder to quit and for the minutes 5 to 15 to pass, and stops if either is still true;
2. renames `program` to `program.outgoing`, then `program.new` to `program`, so the scheduled task, the Startup entry and the launchers keep their paths;
3. reinstalls FOLIO-Exchange's wheel into the new `program` with `pip --no-index --no-deps --force-reinstall`, which downloads nothing. pip writes the program's full path into each command it makes, so `folio-exchange.exe` and `folio-exchange-app.exe` would otherwise still name `program.new`;
4. checks that `program\Scripts\folio-exchange.exe --version` reports the new version;
5. on success, removes the `program.previous` from the update before and renames `program.outgoing` to `program.previous`;
6. removes the marker, whatever happened, writes `updates\result.json`, and starts the programs in `restart` again (only programs inside the program folder).

## The marker

`updating.json` (the version and a start time) sits beside the program folder, not in it as the plan says, because the folder itself is swapped. `run --due` skips its pass and exits 0 while the marker is less than an hour old (`selfupdate.update_in_progress`, the one change to an existing file, `cli.py`). One that's older, or dated more than five minutes ahead, is ignored, so a failed update can't stop runs for good.

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
- **The first rename fails** (a program still has the folder open): nothing changed.
- **The second rename fails:** `program.outgoing` is renamed back to `program`.
- **The new program fails after the swap** (pip can't reinstall the wheel, or `--version` fails): the new folder is renamed to `program.failed` and deleted, and `program.outgoing` becomes `program` again.
- **Putting the old folder back fails**, or the helper is killed between the two renames: `program` may be missing, and the result says `program: none` with what to do. Rename `program.outgoing` to `program`, or run the installer again, which makes `program` afresh. Until then the scheduled task can't start, and `prepare` refuses to begin another update.
- **The marker** is removed in every outcome. If the helper dies, `run --due` ignores the marker after an hour.
- **Going back** after a good update: the previous version stays in `program.previous` until the next successful update, and its sites' copies in `updates\from-<version>\`. Going back is the plan's procedure (put history back only when the newer version raised its format, never settings wholesale); swapping `program.previous` back in is the in-place equivalent of reinstalling the older version and isn't wired yet.

## Limits

- Commands other than FOLIO-Exchange's own in `program\Scripts` (`pip.exe`, Qt's tools) still name `program.new` after the swap. Nothing in FOLIO-Exchange or `install.ps1` uses them: both run `python -m pip`.
- Building the new environment downloads FOLIO-Exchange's dependencies from PyPI, so an update needs pypi.org and files.pythonhosted.org as well as GitHub, as `install.ps1` does.
- The process check sees only this Windows user's programs. A run by another user from the same program folder isn't possible with `install.ps1`'s per-user layout.
- The helper is a Python script, not a signed program. It's copied from the running, already-installed version, never downloaded.

## In the app

`desktop/updating.py` wires the engine to the Updates section of Options.

- **When it's offered.** "Install X.Y.Z…" shows only in the "on" mode, for a newer version, on an install `find_install` says it can update, with a key in `updates.RELEASE_KEY`, the only key the app ever passes (no setting, file or environment variable supplies one). Otherwise U0's notice stays as it is, with a line saying why there's no button: "FOLIO-Exchange was installed with pip, so it can't update itself." (or `find_install`'s other reasons), or "This version of FOLIO-Exchange can't install updates itself." while there's no key. `FOLIO_EXCHANGE_UPDATES=notify` tells and never offers to install; `off` says IT manages updates.
- **The question.** The version, the release notes as plain text, and "FOLIO-Exchange closes, updates and opens again. Your sites, feeds and passwords stay as they are." Cancel is the default button. Nothing happens without Install.
- **The update is the window's running action** (a job, "program update"), so no other action starts in that window or its browser page meanwhile, and closing the window asks first. Its progress shows under Options as it logs: the download, the signature, each site copied, the build. Each of the engine's refusals shows there too, with what was done and what's left, and Install can be tried again.
- **Closing the others.** Before `prepare`, the window notes which windows (one per known site) and whether the tray are open on their local sockets (`single.probe`), so their processes don't count as "another FOLIO-Exchange program". Nor do their ancestors or this window's: on Windows the program folder's `folio-exchange-app.exe` starts the venv's `pythonw.exe`, which starts the base Python that answers on the socket, and both stay running from the program folder (`single.parent_pid`, up to three steps up). The downloads use U0's session, which sends no `.netrc` account and none of `requests`' default headers. Once the release is ready, and outside minutes 5 to 15, it sends each one `quit-for-update` (`single.ask_to_quit`). A window with nothing running answers `ok` and closes, along with its open dialogs; one with an action, a pause, a rename or a release question under way answers `busy`, and Options says "Waiting for the window for Lakeside to finish its action." After 15 minutes of that it stops, starts again the ones it closed, and says so. The tray always says yes.
- **The swap.** `start_swap` gets the commands that start again the windows it closed (`--site`), the tray and this window on Options, and the window quits. The helper starts them once the swap is done, or once the old version is back.
- **Afterwards.** For a day, Options says "Updated to X.Y.Z." or "The update to X.Y.Z didn't finish. FOLIO-Exchange A.B.C is still installed." with the helper's reason (`last_update`). The first window or tray to start writes "program updated" with both versions in each known site's audit log (`record_update`), once.

## Left to do

- `check` warning about a marker older than an hour.
- Going back: swapping `program.previous` back in, with the plan's rules for history and settings.
