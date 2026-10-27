# Dry run against your library's FOLIO tenant

A dry run builds real collectionHQ files from your library's own FOLIO data and sends them nowhere. It tests the connection, the account's permissions and the data itself before anything goes to a vendor. It takes about half an hour, plus the first download, whose length depends on the size of the catalogue.

You run every step yourself, on a PC that can reach your FOLIO API. FOLIO-Exchange only reads from FOLIO, never reads user or patron records, and in a dry run uploads nothing.

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
| FOLIO password | typed at the prompt (not shown); stored in Windows Credential Manager |
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
| `FOLIO refused the login …` | Check the user name and tenant id; store the password again with `folio-exchange -s … secret set folio`. FOLIO-Exchange tries a refused password only once per run, so it cannot lock the account. |
| `DENIED … the account lacks …` | Send the listed permissions to your administrator. |
| `Item note type … not found` | The legacy note type name does not match FOLIO; re-run `init` and pick it from the list. |

## Afterwards

Nothing is scheduled and nothing was sent. To undo everything, delete `C:\FOLIO-Exchange\mylibrary` and remove the entry whose name contains `folio-exchange/folio` from Windows Credential Manager (Control Panel > Credential Manager > Windows Credentials).

The same address, tenant and account also serve FOLIOHQ (phase 0), which stores its password separately, under `folio-chq-folio`.
