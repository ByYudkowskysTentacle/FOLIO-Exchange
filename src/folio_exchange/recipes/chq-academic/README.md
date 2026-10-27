# chq-academic: collectionHQ Academic

The FOLIO-Exchange recipe for collectionHQ Academic: full MARC 21 bibs and a pipe-delimited item file with loan and renewal counts, over SFTP. The layout is the one collectionHQ's implementation team gave in October 2026; the [file layout](https://github.com/ByYudkowskysTentacle/FOLIOHQ/blob/main/docs/layout.md) describes every column, and the [setup guide](https://github.com/ByYudkowskysTentacle/FOLIOHQ/blob/main/docs/setup.md) covers a library's setup.

**Where this folder lives.** The source is [FOLIOHQ](https://github.com/ByYudkowskysTentacle/FOLIOHQ/tree/main/recipes/chq-academic). FOLIO-Exchange ships a copy in `src/folio_exchange/recipes/chq-academic/`. Change FOLIOHQ first, then copy the folder across unchanged; FOLIOHQ's tests fail when a file FOLIO-Exchange ships differs from the source.

## Versions

| Version | Date | Change |
| --- | --- | --- |
| 1.0.0 | 2026-10-26 | First version, from folio-chq-extract's collectionHQ Academic layout |

Each version's `changelog` inside its file is the record; this table repeats it.

## Making a new version

1. **Pick the number.** A minor or patch version must not change a single byte of the files collectionHQ receives. Fixes to descriptions, validation or defaults that apply only to new feeds are minor or patch. Anything collectionHQ would notice is a new **major** version: a column added, removed, renamed or moved, a delimiter, empty value, date format or file name, or the link. A feed on `^1.0` takes 1.x updates automatically, so collectionHQ must never see an unreviewed change.
2. **Get the layout agreed first.** A major version follows a change collectionHQ asked for or accepted. Record who and when in `spec_source`.
3. **Add a file; never edit a released one.** Copy the latest `<version>.yaml` to the new version's file name, set `version`, and add a `changelog` entry at the top.
4. **Test.** In FOLIOHQ, `pip install -r requirements-dev.txt`, then `pytest -q`. The tests validate every version with FOLIO-Exchange, run the examples, check that `docs/layout.md` matches the newest 1.x, and check that 1.x still writes byte-identical files to the legacy tool. `folio-exchange recipes validate recipes/chq-academic/<version>.yaml` checks one file.
5. **Ship it.** After the change is merged here, copy the folder into FOLIO-Exchange in a pull request there.

A library that wants to keep a new major version back stays on its current range: `folio-exchange upgrade chq` shows the changes and moves the feed when the library is ready. The first delivery on a new major version waits for a person to release it.

## Using a version before FOLIO-Exchange ships it

A site reads its own recipe folders before the ones FOLIO-Exchange ships. Clone FOLIOHQ and add its `recipes` folder to the site's `site.yaml`:

```yaml
paths:
  recipes: [recipes, C:/FOLIO-Exchange/FOLIOHQ/recipes]
```

`folio-exchange recipes list` then shows every version found, and `folio-exchange feeds` says which one each feed uses.
