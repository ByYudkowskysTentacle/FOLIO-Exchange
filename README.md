# FOLIO-Exchange

An open-source platform, in design, that lets libraries set up, test and run scheduled data exchanges between [FOLIO](https://www.folio.org/) and vendor platforms – collectionHQ first – from shared, tested recipes, without writing code.

**Status:** spec stage, no code yet. Read the [spec sheet](docs/spec.md) and the [working notes](docs/notes.md).

![Every feed runs one pipeline; only the recipe changes](docs/images/architecture.png)

- **Phase 0** is [FOLIOHQ](https://github.com/ByYudkowskysTentacle/FOLIOHQ) (`folio-chq-extract`), a working one-vendor tool for collectionHQ Academic, which this project generalizes.
- **Eureka-based FOLIO** is the target. Okapi keeps working only where the same code covers it.
- **Patron data** is never sent unless a recipe declares it and an admin approves it.

This is a community project. It is not affiliated with or endorsed by the FOLIO project or any vendor.

**License:** to be decided (MIT or Apache-2.0). Until a license is added, no reuse rights are granted.
