# Dry run against your library's FOLIO tenant

A dry run builds real collectionHQ files from your library's own FOLIO data and sends them nowhere. It tests the connection, the account's permissions and the data itself before anything goes to a vendor. It takes about half an hour, plus the first download, whose length depends on the size of the catalogue.

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
- **From you to FOLIO-Exchange:** type it at the setup prompt. It is not shown on screen and goes straight into Windows Credential Manager, stored for your Windows user. It never goes into a file, a log, a report, or a conversation with an AI assistant.
- **Nobody else needs it.** If someone is helping you, share the address, tenant id and user name, and the output of the commands below. Those never contain the password.

## 1. Install

You need Python 3.10 or later (from python.org; it includes the `py` launcher) and Git. In PowerShell:

```powershell
py -3 -m venv C:\FOLIO-Exchange\venv
C:\FOLIO-Exchange\venv\Scripts\python -m pip install "folio-exchange @ git+https://github.com/ByYudkowskysTentacle/FOLIO-Exchange.git@main"
```

Then, in each new PowerShell window, make the command available:

```powershell
$env:Path = "C:\FOLIO-Exchange\venv\Scripts;" + $env:Path
folio-exchange --version
```

## 2. Guided setup

Pick a folder for your library's settings, outside OneDrive or any synced folder, for example `C:\FOLIO-Exchange\mylibrary`:

```powershell
folio-exchange init C:\FOLIO-Exchange\mylibrary
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

To leave out `-s C:\FOLIO-Exchange\mylibrary` in the commands below, set it once for your Windows user:

```powershell
setx FOLIO_EXCHANGE_SITE C:\FOLIO-Exchange\mylibrary
```

(It takes effect in new windows.)

## 3. Check, preview and build

```powershell
folio-exchange -s C:\FOLIO-Exchange\mylibrary check
folio-exchange -s C:\FOLIO-Exchange\mylibrary preview chq
folio-exchange -s C:\FOLIO-Exchange\mylibrary run chq --no-deliver
```

- `check` logs in, tests every endpoint the recipe needs and shows record counts.
- `preview` builds files from a small sample and prints a few item rows, one per block.
- `run --no-deliver` downloads everything into the local cache and builds the full files. The first run fetches the whole catalogue: on FOLIO's reference system, 10,000 bibs with their MARC took 30 seconds, so 300,000 might take around a quarter of an hour, depending on your host. Later runs fetch only what changed.

The files land in `C:\FOLIO-Exchange\mylibrary\out\chq\<date-time>\`: `chq_bibs_<date>.mrc` (MARC) and `chq_items_<date>.psv` (one row per item), with `run-report.json` beside them.

## 4. What to look at

Prefer a page to the command line? `folio-exchange -s C:\FOLIO-Exchange\mylibrary ui` opens one in your browser, with the runs, their rows and the log.

```powershell
folio-exchange -s C:\FOLIO-Exchange\mylibrary history
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
| `FOLIO refused the login …` | Check the user name and tenant id; during setup run `init` again and answer **n** to "Keep it?" to type the password again (or, once setup is done, `folio-exchange -s … secret set folio`). FOLIO-Exchange tries a refused password only once per run, so it cannot lock the account. |
| `DENIED … the account lacks …` | Send the listed permissions to your administrator. |
| `Item note type … not found` | The legacy note type name does not match FOLIO; re-run `init` and pick it from the list. |

## Afterwards

Nothing is scheduled and nothing was sent. In `history` a dry run shows as `success (not sent: transport none)`, and `release` refuses the feed, because there is nothing to send. To undo everything, delete `C:\FOLIO-Exchange\mylibrary` and remove the entry whose name contains `folio-exchange/folio` from Windows Credential Manager (Control Panel > Credential Manager > Windows Credentials).

The same address, tenant and account also serve FOLIOHQ's legacy tool, `folio-chq-extract`, which stores its password separately, under `folio-chq-folio`.

## From dry run to first delivery

For collectionHQ Academic, FOLIOHQ's [setup guide](https://github.com/ByYudkowskysTentacle/FOLIOHQ/blob/main/docs/setup.md) covers going live in more depth; the steps below are the short version.

When the dry-run files look right and collectionHQ has sent you its SFTP details, switch the same feed to a real destination. Your FOLIO settings, the feed's filters and the local cache are all kept, so the first real run only fetches what changed. You need from the vendor:

- the SFTP server name, port (usually 22), user name and password, and the folder to upload into;
- the server's **host key fingerprint**, a line starting `SHA256:`. Ask for it in writing; it is how you know you are talking to the real server.

**1. Set up the delivery.** In a new PowerShell window, first make the command available as in section 1. Then run the guided setup again on the same folder:

```powershell
folio-exchange init C:\FOLIO-Exchange\mylibrary
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
folio-exchange -s C:\FOLIO-Exchange\mylibrary check --delivery chq
```

It logs in to the vendor's server and lists the folder (or says that listing is not allowed, which is normal for upload-only accounts). It uploads nothing. If the feed is paused, the last line says whether it can be resumed. If the line says `FAILED`, fix that first: a wrong password can be stored again with `folio-exchange -s C:\FOLIO-Exchange\mylibrary secret set chq`.

**3. Run it; the first delivery is held.**

```powershell
folio-exchange -s C:\FOLIO-Exchange\mylibrary run chq
```

The run builds the files as in the dry run, then stops before sending: the first delivery to a new destination always waits for a person. It ends with `"status": "held"` and a `next` line naming the run folder. Look at the files there as in section 4, and at the counts in `history`.

**4. Release it.**

```powershell
folio-exchange -s C:\FOLIO-Exchange\mylibrary release chq
```

It shows the files, their sizes, the item count and the destination, and asks `Send these files now?`. Answer **y** to upload them. Later runs to the same destination are sent without waiting, unless the item count drops sharply (that run is blocked for you to look at) or the recipe moves to a new major version.

**5. Resume the feed and schedule it.** If setup left the feed paused, resume it once `check --delivery` passes:

```powershell
folio-exchange -s C:\FOLIO-Exchange\mylibrary feeds resume chq
```

Feeds only run on their schedule when Windows starts FOLIO-Exchange every hour. Print the command that sets that up:

```powershell
folio-exchange -s C:\FOLIO-Exchange\mylibrary schedule
```

It prints two versions of the same `schtasks` command. Copy the one under **In PowerShell** (it starts `cmd --% /c schtasks`) if you are in PowerShell, or the **Command Prompt** one if you are in a Command Prompt; the other one fails in the wrong window. Run it once, signed in to Windows as the person who stored the passwords, since Credential Manager entries belong to that Windows user. It asks for your Windows password, so the task can run while nobody is signed in. Then check it with the `last result` command printed below it: "Task To Run" should show the folio-exchange.exe path and the `--site` folder, each in quotes.

**6. Make sure you hear about failures.** If you set up email or a chat webhook for alerts, send a test:

```powershell
folio-exchange -s C:\FOLIO-Exchange\mylibrary check --alerts
```

If you skipped alerts during the dry run, run `init` again and choose email or webhook at the alerts question. From now on, `folio-exchange -s C:\FOLIO-Exchange\mylibrary feeds` shows each feed's last run and next step, and `history` lists every run.

## Updating FOLIO-Exchange

A new release only replaces the program in `C:\FOLIO-Exchange\venv`. Everything else stays as it is: the site folder (`site.yaml`, the feed files in `feeds\`, the cache and run history in `data\`, the files in `out\`, the logs) and the passwords in Windows Credential Manager. The scheduled task keeps working, because it points at the same `folio-exchange.exe`.

To update, in PowerShell, at a time when no run is due (in a new window, first make the command available as in section 1):

```powershell
C:\FOLIO-Exchange\venv\Scripts\python -m pip install --upgrade "folio-exchange @ git+https://github.com/ByYudkowskysTentacle/FOLIO-Exchange.git@main"
folio-exchange --version
folio-exchange -s C:\FOLIO-Exchange\mylibrary check
```

`--version` should show the new version, and `check` should end without problems.

**Recipe updates.** The file layouts (recipes) come with the program. A feed names a range of recipe versions, such as `chq-academic@^1.0`. Fixes and small additions inside that range are used from the next run. A new major version (2.0 and up) changes the files the vendor receives, so it is never picked up on its own: `feeds` shows an `update` line saying the new version is available. To look at it and move to it:

```powershell
folio-exchange -s C:\FOLIO-Exchange\mylibrary upgrade chq
folio-exchange -s C:\FOLIO-Exchange\mylibrary preview chq
```

`upgrade` lists what changes, including any column changes, and asks before it rewrites `feeds\chq.yaml`; the change is recorded in `history --audit`. Tell the vendor before the next delivery: the first delivery on the new version is held for you to `release`, as the very first one was. Despite the name, `upgrade` only moves a feed to another recipe version; it does not update FOLIO-Exchange itself.
