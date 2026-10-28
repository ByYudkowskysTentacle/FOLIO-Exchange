# Dry run against your library's FOLIO tenant

A dry run builds real collectionHQ files from your library's own FOLIO data and sends them nowhere. It tests the connection, the account's permissions and the data itself before anything goes to a vendor. It takes about half an hour, plus the first download, whose length depends on the size of the catalogue.

Prefer not to use the command line? The [installer](../installer/README.md) sets FOLIO-Exchange up and opens a page where you connect to FOLIO and add the collectionHQ feed; a feed added there starts as a dry run, and the page's Preview and Run do what sections 3 and 4 below do. This guide takes the same route at the command line.

You run every step yourself, on a PC that can reach your FOLIO API. FOLIO-Exchange only reads from FOLIO, never reads user or patron records, and in a dry run uploads nothing. The whole route to a live collectionHQ feed, from this dry run to the first delivery and the schedule, is FOLIOHQ's [setup guide](https://github.com/ByYudkowskysTentacle/FOLIOHQ/blob/main/docs/setup.md).

## Before you start

Ask your FOLIO administrator or hosting provider for:

1. **The API address.** On Eureka this is the Kong gateway URL, which is not the address of the FOLIO web pages. It starts with `https://`.
2. **The tenant id**, a short name such as `fs00001234`.
3. **A dedicated, read-only service account** with these permissions (on Eureka, the matching capabilities with view access, in a role given to the account):

   ```
   circulation-storage.loans.collection.get
   inventory-storage.call-number-types.collection.get
   inventory-storage.classification-types.collection.get
   inventory-storage.holdings.collection.get
   inventory-storage.identifier-types.collection.get
   inventory-storage.instances.collection.get
   inventory-storage.item-note-types.collection.get
   inventory-storage.items.collection.get
   inventory-storage.loan-types.collection.get
   inventory-storage.location-units.campuses.collection.get
   inventory-storage.location-units.institutions.collection.get
   inventory-storage.location-units.libraries.collection.get
   inventory-storage.locations.collection.get
   inventory-storage.material-types.collection.get
   source-storage.source-records.collection.get
   ```

   The loans permission is for loan counts and dates. FOLIO-Exchange drops the borrower from each loan the moment it arrives and never stores it.

Also check that the PC can reach the API: many FOLIO hosts accept API traffic only from known networks, so use a PC inside the library's network, or on its VPN.

### How the password should travel

- **From the FOLIO administrator to you:** through the library's password manager, or a one-time share link (Bitwarden Send, 1Password, or similar). Not by plain email, chat or a shared document.
- **From you to FOLIO-Exchange:** type it at the setup prompt (or into the page's password field). It is not shown on screen and goes straight into Windows Credential Manager, stored for your Windows user. It never goes into a file, a log, a report, or a conversation with an AI assistant.
- **Nobody else needs it.** If someone is helping you, share the address, tenant id and user name, and the output of the commands below. Those never contain the password.

## 1. Install

You need Python 3.10 or later (from python.org; it includes the `py` launcher) and Git.

Install into your own Windows profile, under `%LOCALAPPDATA%\FOLIO-Exchange`, where only your Windows user can change the files. Anyone who can change the program or the site folder could make the hourly run do something else as you, and read the passwords it keeps. A folder such as `C:\FOLIO-Exchange` can be changed by every user of the PC. In PowerShell:

```powershell
py -3 -m venv $env:LOCALAPPDATA\FOLIO-Exchange\venv
& $env:LOCALAPPDATA\FOLIO-Exchange\venv\Scripts\python -m pip install -c https://raw.githubusercontent.com/ByYudkowskysTentacle/FOLIO-Exchange/main/constraints.txt "folio-exchange @ git+https://github.com/ByYudkowskysTentacle/FOLIO-Exchange.git@main"
```

The `-c` part installs the other packages FOLIO-Exchange needs at the versions it was tested with.

Then, in each new PowerShell window, make the command available:

```powershell
$env:Path = "$env:LOCALAPPDATA\FOLIO-Exchange\venv\Scripts;" + $env:Path
folio-exchange --version
```

`--version` shows the version and the commit installed, for example `folio-exchange 0.1.0.dev0 (commit 4e1f2c1a9b3d)`. If someone is helping you, tell them both.

## 2. Guided setup

Pick a folder for your library's settings next to the program, outside OneDrive or any synced folder, for example `%LOCALAPPDATA%\FOLIO-Exchange\mylibrary`:

```powershell
folio-exchange init $env:LOCALAPPDATA\FOLIO-Exchange\mylibrary
```

Answer the questions:

| Question | Answer for the dry run |
| --- | --- |
| FOLIO API address, tenant id, user name | from your administrator |
| FOLIO password | typed at the prompt (not shown); stored in Windows Credential Manager only once FOLIO accepts it. If you mistype it, run `init` again |
| Which vendor feed | 1, collectionHQ Academic |
| A short name for this feed | `chq` (press Enter) |
| Leave out any locations | the locations you would not send to collectionHQ, such as online resources; `none` is fine for a first look |
| Leave out items with these statuses | `none` (collectionHQ wants every status) |
| Migrated item notes | yes only if loan counts or dates from your old system are kept in item notes; then pick those note types |
| Build a preview | `y` |
| How are the files delivered | **3, not yet** (nothing is sent anywhere) |
| When should it run | `manual` |
| Alerts | 3, skip for now |

If setup stops at the permissions step, it lists exactly which permissions are missing. Send that list to your administrator and run the same `init` command again later: your answers so far are kept.

To leave out `-s $env:LOCALAPPDATA\FOLIO-Exchange\mylibrary` in the commands below, set it once for your Windows user:

```powershell
setx FOLIO_EXCHANGE_SITE $env:LOCALAPPDATA\FOLIO-Exchange\mylibrary
```

(It takes effect in new windows.)

## 3. Check, preview and build

```powershell
folio-exchange -s $env:LOCALAPPDATA\FOLIO-Exchange\mylibrary check
folio-exchange -s $env:LOCALAPPDATA\FOLIO-Exchange\mylibrary preview chq
folio-exchange -s $env:LOCALAPPDATA\FOLIO-Exchange\mylibrary run chq --no-deliver
```

- `check` logs in, tests every endpoint the recipe needs and shows record counts. If it warns `Other Windows users can change …`, other people who sign in to this PC can change the folder it names. Install under `%LOCALAPPDATA%` as in sections 1 and 2, or run the `icacls` command at the end of the warning, which limits the folder to you, SYSTEM and Administrators.
- `preview` builds files from a small sample and prints a few item rows, one per block.
- `run --no-deliver` downloads everything into the local cache and builds the full files. The first run fetches the whole catalogue: on FOLIO's reference system, 10,000 bibs with their MARC took 30 seconds, so 300,000 might take around a quarter of an hour, depending on your host. Later runs fetch only what changed.

The files land in `%LOCALAPPDATA%\FOLIO-Exchange\mylibrary\out\chq\<date-time>\`: `chq_bibs_<date>.mrc` (MARC) and `chq_items_<date>.psv` (one row per item), with `run-report.json` beside them.

## 4. What to look at

Prefer a page to the command line? `folio-exchange -s $env:LOCALAPPDATA\FOLIO-Exchange\mylibrary ui` opens one in your browser, with the runs, their rows and the log. For safety it opens through a link that works only once, so typing the page's address or using a bookmark shows a page that says to open it from FOLIO-Exchange instead. The address ends in `.localhost` (each start has a new one), which Chrome, Edge and Firefox send to this computer themselves. To open the page in a second browser, use the other one-time link the black window shows. To open it later without typing anything, run `folio-exchange -s $env:LOCALAPPDATA\FOLIO-Exchange\mylibrary launcher` once: it puts `FOLIO-Exchange (mylibrary).cmd` on your desktop. Double-click it to open the page, and keep the black window it opens while you use the page (press Ctrl+C in it to stop). If you close the browser, double-click the launcher again: the black window that is still running opens the page in your browser.

```powershell
folio-exchange -s $env:LOCALAPPDATA\FOLIO-Exchange\mylibrary history
```

It lists every run with a number; show one in full with `history --show <number>`.

- **Counts:** bibs and items written, and items left out with the reason (location excluded, suppressed, no barcode). Do they match what you expect from FOLIO?
- **Locations and call numbers:** open the `.psv` in a text editor, or a copy of it in Excel (Data > From Text/CSV, delimiter `|`). Are the location names right? Do items show a call number?
- **MARC:** open the `.mrc` in MarcEdit. Each record's 999 ff $i is the instance UUID, which collectionHQ uses to link items to bibs.
- **Last circulation dates and loan counts:** spot-check a few items you know well.

If someone is helping you review the results, share the console output and `run-report.json` (counts and reasons, no records). The `.psv` holds your holdings data but no patron data; share it only if you are comfortable to.

## If something goes wrong

| Message | What to do |
| --- | --- |
| `Cannot reach FOLIO at …` | Check the API address and that this PC is on the library network or VPN. |
| `… FOLIO's certificate is not trusted by this computer` | Your campus or a network proxy probably issues its own certificates. In `site.yaml`, under `folio:`, add `ca_bundle: windows` to trust the certificates Windows trusts (as your web browser does), or ask IT for the certificate as a PEM file, save it in the site folder and add `ca_bundle: <file name>`. Do not set `verify_tls: false`: anyone on the network could then read the FOLIO password, and `check` warns about it. |
| `FOLIO refused the login …` | Check the user name and tenant id; during setup run `init` again and answer **n** to "Keep it?" to type the password again (or, once setup is done, `folio-exchange -s … secret set folio`). FOLIO-Exchange tries a refused password only once per run, so it cannot lock the account. |
| `DENIED … the account lacks …` | Send the listed permissions to your administrator. |
| `Item note type … not found` | The legacy note type name does not match FOLIO; re-run `init` and pick it from the list. |
| `WARNING  … notes (legacy.…) could not be read …` | Some migrated notes are not in a form FOLIO-Exchange reads, so those items carry FOLIO's own date or loan count. Dates need a day, a month and a year (`1998-03-15`, `3/15/1998`, or `Created: 1998-03-15`); counts need one whole number (`12`, `Checkouts: 1,234`). If your dates are written day first, add `date_format: "%d/%m/%Y"` under `legacy:` in `feeds\chq.yaml` (a label in it, as in `"Created: %d/%m/%Y"`, does no harm). The warning lists a few items to look at; on the page it is under *Warnings* in the run's *Summary* tab, for previews too. |
| `FOLIO's login service did not answer as expected …` | FOLIO may be part way through an upgrade or restart; the password was not checked. Run again later (a scheduled run tries again at the next hourly start). If it continues, check the API address and ask your FOLIO host. |
| `The disk is full …` | Free some space on the drive that holds the site folder (and on the system drive), then run again. The local cache is kept, so the next run carries on from it. |

## Afterwards

Nothing is scheduled and nothing was sent. In `history` a dry run shows as `success (not sent: transport none)`, and `release` refuses the feed, because there is nothing to send. To undo everything, delete `%LOCALAPPDATA%\FOLIO-Exchange\mylibrary` and remove the entry whose name contains `folio-exchange/folio` from Windows Credential Manager (Control Panel > Credential Manager > Windows Credentials).

The same address, tenant and account also serve FOLIOHQ's legacy tool, `folio-chq-extract`, which stores its password separately, under `folio-chq-folio`.

## From dry run to first delivery

For collectionHQ Academic, FOLIOHQ's [setup guide](https://github.com/ByYudkowskysTentacle/FOLIOHQ/blob/main/docs/setup.md) covers going live in more depth; the steps below are the short version.

When the dry-run files look right and collectionHQ has sent you its SFTP details, switch the same feed to a real destination. Your FOLIO settings, the feed's filters and the local cache are all kept, so the first real run only fetches what changed. You need from the vendor:

- the SFTP server name, port (usually 22), user name and password, and the folder to upload into;
- the server's **host key fingerprint**, a line starting `SHA256:`. Ask for it in writing; it is how you know you are talking to the real server.

**1. Set up the delivery.** In a new PowerShell window, first make the command available as in section 1. Then run the guided setup again on the same folder:

```powershell
folio-exchange init $env:LOCALAPPDATA\FOLIO-Exchange\mylibrary
```

Your earlier answers are offered as defaults; press Enter to keep each one. Answer **n** to "Change the FOLIO connection?" and **y** to "A FOLIO password is already stored. Keep it?". Give the same short name (`chq`), so the existing feed is changed rather than a new one made. Then:

| Question | Answer |
| --- | --- |
| How are the files delivered | **1, upload to the vendor's SFTP server** |
| SFTP server name, port, user name, folder | from the vendor |
| Does it match the fingerprint the vendor gave you? | compare the `SHA256:` line on screen with the vendor's, character by character. **yes** only if they are the same. If you don't have the vendor's value yet, pick the second answer: the feed is saved but stays paused, and nothing logs in until you run `init` again and confirm it. If they differ, answer **no** and ask the vendor |
| SFTP password | typed at the prompt (not shown); it goes into Windows Credential Manager |
| When should it run | the vendor's schedule, for example `weekly` or `Sun 03:30` |

Setup tests the login without uploading anything. If the test fails, or the key is not yet confirmed, it leaves the feed paused and says so.

**2. Test the delivery.**

```powershell
folio-exchange -s $env:LOCALAPPDATA\FOLIO-Exchange\mylibrary check --delivery chq
```

It logs in to the vendor's server and lists the folder (or says that listing is not allowed, which is normal for upload-only accounts). It uploads nothing. If the feed is paused, the last line says whether it can be resumed. If the line says `FAILED`, fix that first: a wrong password can be stored again with `folio-exchange -s $env:LOCALAPPDATA\FOLIO-Exchange\mylibrary secret set chq`.

**3. Run it; the first delivery is held.**

```powershell
folio-exchange -s $env:LOCALAPPDATA\FOLIO-Exchange\mylibrary run chq
```

The run builds the files as in the dry run, then stops before sending: the first delivery to a new destination always waits for a person. It ends with `"status": "held"` and a `next` line naming the run folder. Look at the files there as in section 4, and at the counts in `history`.

**4. Release it.**

```powershell
folio-exchange -s $env:LOCALAPPDATA\FOLIO-Exchange\mylibrary release chq
```

It shows the files, their sizes, the item count and the destination, and asks `Send these files now?`. Answer **y** to upload them. Leaving the question open does not hold up scheduled runs; if a run happens meanwhile, `release` sends nothing and asks you to run it again, so you see what it would send now. Later runs to the same destination are sent without waiting, unless the item count drops sharply (that run is blocked for you to look at), the recipe moves to a new major version, or the files' layout changes.

**After changing columns or formats.** If you edit the `outputs` in `feeds\chq.yaml` (columns, headers, delimiter, date format, empty value, file names), the next delivery is held, and the hold message says what changed, for example `items file: columns changed: 'Item record ID' renamed to 'Item ID'`. Check the vendor expects the change, then send it with `release chq` as above.

**After pointing the site at another FOLIO.** If you change the FOLIO address or tenant (with `init`, the page's Connection view, or the `FOLIO_URL` and `FOLIO_TENANT` environment variables), for example to try a test tenant, the next run downloads everything from that FOLIO and its delivery is held: `first delivery from FOLIO <new>; the last came from <old>`. Release it only if you mean to send that FOLIO's catalogue to the vendor. If that FOLIO has fewer items, the run is blocked instead, and the block message and the release summary still name the change of FOLIO; a *Build only* run names it when you review and send it. When you point the site back, the next run downloads everything again; its delivery is held too if you released one from the other FOLIO.

**5. Resume the feed and schedule it.** If setup left the feed paused, resume it once `check --delivery` passes:

```powershell
folio-exchange -s $env:LOCALAPPDATA\FOLIO-Exchange\mylibrary feeds resume chq
```

Feeds only run on their schedule when Windows starts FOLIO-Exchange every hour. Print the command that sets that up:

```powershell
folio-exchange -s $env:LOCALAPPDATA\FOLIO-Exchange\mylibrary schedule
```

It prints the command that registers the task, `schedule --register`, in two forms. Copy the one under **In PowerShell** (it starts `& '`) if you are in PowerShell, or the **Command Prompt** one if you are in a Command Prompt. Run it once, signed in to Windows as the person who stored the passwords, since Credential Manager entries belong to that Windows user. It asks for your Windows password, so the task can run while nobody is signed in. The task also runs on battery power, and catches up after the PC was off or asleep. (Below that, `schedule` also prints PowerShell lines that register the same task by hand, with the same settings, if you prefer to see each step.) Then check it with the `last result` command printed below it: "Task To Run" should show the folio-exchange.exe path and the `--site` folder, each in quotes.

**When your Windows password changes,** the task stops starting, because it still holds the old password. A task registered by an earlier version of FOLIO-Exchange also never starts while a laptop runs on battery power. In either case, register it again:

```powershell
folio-exchange -s $env:LOCALAPPDATA\FOLIO-Exchange\mylibrary schedule --register
```

**Noticing when the task stops.** `check` and `feeds` say when the task last ran. After more than two hours without a run, they and the page's Connection view warn that no feed has run and no alert could be sent since: the PC may be asleep, off or on battery (for a task registered by an earlier version), the task may be disabled in Task Scheduler, or the Windows password changed. They also warn when the task runs but fails, for example because the disk is full and the run history cannot be written, and when less than 1 GB of disk space is free. A scheduled pass that fails that way sends an alert itself, once per distinct error. A stopped task cannot send an alert itself, so you can also use a heartbeat monitor (a "dead man's switch" service, such as Healthchecks.io): it gives you a web address to call and emails you when the calls stop. FOLIO-Exchange calls the address when every hourly run starts and again when it ends. Set the monitor to expect a call every hour, with a grace time long enough for your longest run: the first full sync of a large catalogue can take several hours, with no call in between. While such a run is going, `check`, `feeds` and the Connection view say "A run has been going since …" instead of warning that the task stopped. After four hours they warn that one run has held the site that long, so any feed falling due is skipped, and if a feed was due meanwhile you get one alert saying scheduled runs are being skipped: a first full download can take that long, but the run may also be stuck. The address is a secret, so store it in Windows Credential Manager:

```powershell
folio-exchange -s $env:LOCALAPPDATA\FOLIO-Exchange\mylibrary secret set heartbeat
```

Then add `heartbeat: keyring` under `alerts:` in `site.yaml`. The address must start with `https://`.

**6. Make sure you hear about failures.** If you set up email or a chat webhook for alerts, send a test:

```powershell
folio-exchange -s $env:LOCALAPPDATA\FOLIO-Exchange\mylibrary check --alerts
```

If you skipped alerts during the dry run, run `init` again and choose email or webhook at the alerts question.

An alert that cannot be sent when it is raised (the PC's network was down, say) is kept and sent at the next hourly run that can reach the channel, starting `First raised <time>`, so it may arrive late. A test from `check --alerts` is never kept: if it fails, fix the channel and test again. When a feed's scheduled run fails on every attempt its slot allows (`runs.max_attempts_per_slot`, 3 by default), you get one alert saying the slot was given up and when the next one is; run the feed by hand once the problem is fixed.

From now on, `folio-exchange -s $env:LOCALAPPDATA\FOLIO-Exchange\mylibrary feeds` shows each feed's last run and next step, and `history` lists every run.

## Renaming the site folder

The site folder's name is the site's name: the page shows it at the top, alert messages carry it, and the scheduled task and the desktop launcher are named after it. To give it a proper name (say, from `demo-eureka` to the library's name), use **Options** on the page, or `folio-exchange rename-site "Lakeside College Library"`. The folder stays where it is. Close every other FOLIO-Exchange window first, and wait for any run to finish: Windows will not rename a folder while a file in it is open, and you are told so if that happens (nothing is changed). The rename then:

- replaces the launcher on the desktop with one for the new name (a file you made yourself is left alone);
- copies the webhook and heartbeat addresses stored in Windows Credential Manager to the new name;
- tells you if the scheduled task still runs the old folder. Register it again, which asks for your Windows password, and delete the old task. `rename-site` in a Command Prompt offers to do both; on the page you get the two commands to run;
- tells you if `FOLIO_EXCHANGE_SITE` still names the old folder.

The FOLIO and vendor passwords are stored by account, not by folder, so they carry on. Settings that name a place inside the folder by its full path (for example a `paths.data` of `C:\...\demo-eureka\data`) would still point at the old folder, so the rename refuses until they are changed to paths relative to the site folder.

## Moving the site folder

If you move the site folder somewhere else (for example into `%LOCALAPPDATA%\FOLIO-Exchange`, as `check` may suggest), or rename it by hand, runs held before the move can still be released and their folders are still kept. Afterwards:

- Register the hourly task again for the new folder with `schedule --register`. The task is named after the folder (`FOLIO-Exchange (mylibrary)`), so if the name changed, delete the old task in Task Scheduler.
- Store the webhook and heartbeat addresses again with `secret set webhook` and `secret set heartbeat`: Windows Credential Manager keeps them under the folder's name, so after a rename they are not found. Until then `check` and the page warn that alerts or the heartbeat cannot be sent.
- If you had set `FOLIO_EXCHANGE_SITE`, set it again with `setx` for the new folder.

## Updating FOLIO-Exchange

A new release only replaces the program in `%LOCALAPPDATA%\FOLIO-Exchange\venv`. Everything else stays as it is: the site folder (`site.yaml`, the feed files in `feeds\`, the cache and run history in `data\`, the files in `out\`, the logs) and the passwords in Windows Credential Manager. The scheduled task keeps working, because it points at the same `folio-exchange.exe`.

To update, in PowerShell, at a time when no run is due (in a new window, first make the command available as in section 1):

```powershell
& $env:LOCALAPPDATA\FOLIO-Exchange\venv\Scripts\python -m pip install --upgrade -c https://raw.githubusercontent.com/ByYudkowskysTentacle/FOLIO-Exchange/main/constraints.txt "folio-exchange @ git+https://github.com/ByYudkowskysTentacle/FOLIO-Exchange.git@main"
folio-exchange --version
folio-exchange -s $env:LOCALAPPDATA\FOLIO-Exchange\mylibrary check
```

`--version` should show the new version and commit, and `check` should end without problems. Right after updating from an earlier version, `check` may warn that the scheduled task has not run for some hours; the warning clears after the next hourly run.

**Recipe updates.** The file layouts (recipes) come with the program. A feed names a range of recipe versions, such as `chq-academic@^1.0`. Fixes and small additions inside that range are used from the next run. A new major version (2.0 and up) changes the files the vendor receives, so it is never picked up on its own: `feeds` shows an `update` line saying the new version is available. To look at it and move to it:

```powershell
folio-exchange -s $env:LOCALAPPDATA\FOLIO-Exchange\mylibrary upgrade chq
folio-exchange -s $env:LOCALAPPDATA\FOLIO-Exchange\mylibrary preview chq
```

`upgrade` lists what changes, including any column changes, and asks before it rewrites `feeds\chq.yaml`; the change is recorded in `history --audit`. Tell the vendor before the next delivery: the first delivery on the new version is held for you to `release`, as the very first one was. Despite the name, `upgrade` only moves a feed to another recipe version; it does not update FOLIO-Exchange itself.
