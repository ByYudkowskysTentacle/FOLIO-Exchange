# Security policy

FOLIO-Exchange holds a library's FOLIO login and vendor SFTP credentials, and sends library data to outside servers on a schedule. A security problem in it affects every library that runs it, so please report one privately.

## Supported versions

There's no release yet. Fixes go to the `main` branch, and installs from the [installer](installer/README.md) or the guides take `main`. Once there are release tags, this section will say which ones get fixes.

## Reporting a vulnerability

Please don't open a public issue. Report it privately through GitHub: on the repository's **Security** tab, choose **Report a vulnerability**. If that option isn't there, open an issue that asks for a private contact and leave out every detail of the problem.

Include what you found, how to reproduce it (against the mock FOLIO in `tests/mock_folio.py` or one of FOLIO's public reference systems, if you can) and what it could expose. Never send real passwords, keys or patron data, even your own.

We aim to reply within a week, keep you posted while it's fixed, and credit you in the fix unless you'd rather not be named.

## What counts

Anything that could, for example:

- expose a password, token or private key (in files, logs, run history, alerts or the app);
- read patron or user data from FOLIO, or write anything to FOLIO;
- send files to a destination, or trust an SFTP server key, that the library didn't set up and confirm;
- get a delivery past the first-delivery or layout-change hold, or the checks that block a run;
- let another user or a web page on the same computer use the local browser page or the desktop app;
- let a shared recipe run code or change where files go.

## Testing safely

Test against the mock FOLIO, FOLIO's public reference systems, or a tenant and SFTP server you own. Don't test against another library's FOLIO tenant or a vendor's servers without their permission.
