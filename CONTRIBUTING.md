# Contributing to FOLIO-Exchange

Thanks for your interest. FOLIO-Exchange is a small community project, and v1 is still in development, so the most useful contributions right now are:

- **Bug reports**, especially from a dry run against your own FOLIO tenant ([guide](docs/dry-run.md)).
- **Recipe requests**: a vendor feed your library needs, with whatever layout documentation the vendor gave you.
- **Fixes and small improvements**, each with a test.
- **Corrections to the docs.**

For anything larger than a small fix, open an issue first, so we can agree on the approach before you spend time on it. The [spec sheet](docs/spec.md) says where the project is going and the [build design](docs/design.md) says how v1 is put together.

## Never post private data

Issues, pull requests, commits and test fixtures are public. Never include:

- passwords, API tokens, SSH private keys or anything from Credential Manager or `.env`;
- your library's `site.yaml`, feed files, cache, run folders or the files built for a vendor;
- patron data of any kind, or real records you aren't free to publish.

Replace tenant names, host names and account names with made-up ones (`folio.example.org`, `diku`, `lib1@sftp.vendor.example`). If you post a password or key by mistake, change it straight away. Editing the post doesn't remove it, because GitHub keeps edit history. To report a security problem, follow [SECURITY.md](SECURITY.md) instead of opening an issue.

## Development setup

You need Python 3.10 or later and Git.

```bash
git clone https://github.com/ByYudkowskysTentacle/FOLIO-Exchange.git
```

```bash
cd FOLIO-Exchange
python -m venv .venv
.venv/Scripts/python -m pip install -c constraints.txt -e ".[dev,desktop]"
```

On Linux or macOS the interpreter is `.venv/bin/python`. `constraints.txt` holds the dependency versions the tests pass with; CI and the installer use it too. The `desktop` extra adds Qt (PySide6) for the desktop app; without it, the desktop app's widget tests are skipped.

Run the tests and the linter:

```bash
.venv/Scripts/python -m pytest -q
```

```bash
.venv/Scripts/ruff check .
```

The tests run offline against a mock FOLIO (`tests/mock_folio.py`). They never reach a real tenant, an SFTP server or your system keyring, and new tests must keep it that way. To run the Qt tests without a display, set `QT_QPA_PLATFORM=offscreen`, as CI does.

## Ground rules

These keep FOLIO-Exchange safe to run unattended against a library's data. A change that breaks one won't be merged.

- **Eureka first.** FOLIO on Okapi keeps working only while the same code serves both. No Okapi-only fixes, features or tests.
- **Read-only, and no patron data.** The FOLIO client reads only the records a recipe names. Don't add user or patron endpoints; patron data waits for a recipe that declares it and an admin who approves it.
- **Secrets** come only from environment variables or the system keyring (Windows Credential Manager). The one exception is an SFTP private key saved from the app, which is an owner-only file in the site's `keys` folder. Never put secrets in recipes, settings, logs or test fixtures.
- **Recipes stay declarative.** A recipe is data: no executable code, and it can't choose where files go or which server key to trust.
- **Nothing reaches a vendor unchecked.** Don't weaken the first-delivery hold, the layout-change hold, the drop-in-items check or the SFTP server key pin. A key may be pinned on first use when the vendor gives no fingerprint, but a login never accepts a key other than the pinned one.

## Making a change

- Keep modules small, use type hints, and don't add a heavy dependency without a reason.
- Every bug fix gets a regression test.
- [`docs/design.md`](docs/design.md) is the v1 build contract. When you change a module's behaviour, its signatures, the command line or a file format, update it in the same pull request.
- Text in the desktop app follows the [wording guide](docs/wording.md): plain US English with contractions, brief, and buttons whose accessible names contain their labels. Many tests check exact text; update them with it, without weakening what they check.
- In docs, use en dashes ( – ) for asides, never em dashes.
- **The collectionHQ Academic recipe** in `src/folio_exchange/recipes/chq-academic/` is a copy. Its source is [cHQ FOLIO Exchange Recipe](https://github.com/ByYudkowskysTentacle/cHQ-FOLIO-Exchange-Recipe/tree/main/recipes/chq-academic): change it there first, then copy the folder here unchanged.
- Commit with your GitHub noreply address (**Settings > Emails** on GitHub) if you don't want your email address published.

Open the pull request against `main`. CI runs the tests on Linux (Python 3.10 to 3.13) and Windows, runs `ruff`, and scans for secrets with gitleaks; all of it must pass.

## License

FOLIO-Exchange is released under the [MIT License](LICENSE). By contributing, you agree that your contribution is released under the same license.
