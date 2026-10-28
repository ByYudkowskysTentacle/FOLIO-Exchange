"""Checks that stand between a build and a delivery (VAL-2, VAL-3).

A failed check raises ``SafetyCheckFailed``: the files stay in the run folder, nothing
is sent, and the run's status is ``blocked`` until a person looks.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import TYPE_CHECKING

from pymarc import MARCReader

from .errors import SafetyCheckFailed

if TYPE_CHECKING:
    from .build import BuildReport
    from .recipe import Feed


def required_fields(feed: Feed, report: BuildReport, *, preview: bool = False) -> None:
    """Block when any delivered row has a ``must_be_filled`` field empty.

    The message says which fields the recipe requires and which the feed file added: a feed file can
    add to the recipe's list but not take from it, so only the feed's own are offered for removal. A
    preview cannot be released, so its message leaves out `release`."""
    empty = {name: n for name, n in report.unfilled.items() if n}
    if not empty:
        return
    listed = ", ".join(f"{name}: {n:,} rows" for name, n in sorted(empty.items()))
    recipe_must = set((feed.recipe.data.get("checks") or {}).get("must_be_filled") or [])
    from_recipe = [name for name in sorted(empty) if name in recipe_must]
    from_feed = [name for name in sorted(empty) if name not in recipe_must]
    text = [f"Some rows are missing values the vendor needs ({listed})."]
    text.append("A full run would be blocked." if preview else "Files were built but not delivered.")
    if from_recipe:
        text.append(f"The recipe {feed.recipe.ref} requires {', '.join(from_recipe)}; the feed file cannot "
                    f"remove {'it' if len(from_recipe) == 1 else 'them'}.")
    if from_feed:
        text.append(f"The feed file's checks.must_be_filled adds {', '.join(from_feed)}.")
    fix = "Fix the records in FOLIO and run the feed again"
    if from_feed:
        fix += f", or remove {', '.join(from_feed)} from checks.must_be_filled in the feed file"
    text.append(fix + ".")
    if not preview:                    # its own sentence: the page drops the ones that name a command
        text.append(f"If the files are right as they are, send them with `folio-exchange release {feed.name}`.")
    raise SafetyCheckFailed(" ".join(text))


def item_ratio(feed: Feed, items_written: int, last_delivered: int | None) -> None:
    """Block when the item count fell below ``min_item_ratio`` of the last delivery (VAL-3)."""
    ratio = float(feed.checks.get("min_item_ratio") or 0)
    if last_delivered and ratio and items_written < last_delivered * ratio:
        raise SafetyCheckFailed(
            f"Item count fell from {last_delivered:,} to {items_written:,} (below {ratio:.0%} of the last "
            "delivery). Files were built but not delivered. If the drop is not expected, fix the cause in FOLIO "
            "and run the feed again. If it is expected (weeding, a location removed), look at the files, then "
            f"send them with `folio-exchange release {feed.name}`."
        )


def check_marc_file(path: Path) -> list[str]:
    """Problems that stop ``path`` being read as binary MARC 21; an empty list means it parses."""
    with open(path, "rb") as fh:
        data = fh.read(4 << 20)           # the separators show up in the first records
    if not data:
        return []
    problems: list[str] = []
    if b"\x1d" not in data or b"\x1e" not in data:
        problems.append(
            f"{Path(path).name}: has no MARC record or field separators; it looks converted to text in transit. "
            "Ask the vendor for the original file, zipped."
        )
        return problems
    with open(path, "rb") as fh:
        reader = MARCReader(fh, to_unicode=True, force_utf8=True)
        for n, rec in enumerate(reader, 1):
            if rec is None:
                problems.append(f"{Path(path).name}: record {n} could not be read ({reader.current_exception})")
                if len(problems) >= 5:
                    break
    return problems


def _delimited_shape(path: Path, delimiter: str) -> tuple[list[str], int]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh, delimiter=delimiter)
        header = next(reader, [])
        widths = {len(row) for row in reader if row}
    return header, max(widths | {len(header)})


def conformance(feed: Feed, run_dir: Path, samples: dict[str, Path],
                files: dict[str, list[str]] | None = None) -> list[str]:
    """Compare a run's files with vendor samples, by output name (VAL-2).

    Delimited: the same header row, column count and delimiter. MARC: both parse as binary MARC."""
    problems: list[str] = []
    for out in feed.outputs:
        sample = samples.get(out["name"])
        if sample is None:
            continue
        if files is not None:
            built = [Path(run_dir) / n for n in files.get(out["name"], [])]
        else:
            built = sorted(Path(run_dir).glob(f"*.{'mrc' if out['format'] == 'marc21' else out['extension']}"))
        if not built:
            problems.append(f"{out['name']}: no file was built to compare with {sample.name}")
            continue
        if out["format"] == "marc21":
            problems += [f"sample {p}" for p in check_marc_file(sample)]
            for f in built:
                problems += check_marc_file(f)
            continue
        delim = out["delimiter"]
        s_head, s_width = _delimited_shape(sample, delim)
        b_head, b_width = _delimited_shape(built[0], delim)
        if s_width <= 1 and b_width > 1:
            problems.append(f"{out['name']}: the sample {sample.name} does not use the delimiter {delim!r}")
        if out.get("header", True) and s_head != b_head:
            missing = [h for h in s_head if h not in b_head]
            extra = [h for h in b_head if h not in s_head]
            detail = "; ".join(x for x in (
                f"missing {', '.join(missing)}" if missing else "",
                f"extra {', '.join(extra)}" if extra else "",
                "same columns in a different order" if not missing and not extra else "",
            ) if x)
            problems.append(f"{out['name']}: header row differs from the sample {sample.name} ({detail})")
        if s_width != b_width:
            problems.append(f"{out['name']}: {b_width} columns, but the sample {sample.name} has {s_width}")
    return problems


def sample_files(feed: Feed) -> dict[str, Path]:
    """Vendor samples kept with the recipe: ``<recipe folder>/samples/<output name>.<ext>``."""
    folder = feed.recipe.path.parent / "samples"
    out: dict[str, Path] = {}
    if folder.is_dir():
        for o in feed.outputs:
            found = sorted(folder.glob(f"{o['name']}.*"))
            if found:
                out[o["name"]] = found[0]
    return out
