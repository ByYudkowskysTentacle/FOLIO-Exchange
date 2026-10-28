"""Shared recipes, their versions, and feed files: a library's local settings over a shared recipe.

A shared recipe lives at ``<library dir>/<id>/<version>.yaml`` and is reviewed in Git.
A feed file, ``feeds/<name>.yaml``, picks a recipe version range with ``uses:`` and
overrides only what the recipe allows. Both are checked on every load, and every
problem is reported in plain language with the file and the setting's path.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import credentials, schedule
from .config import BUNDLED_RECIPES, merge, read_yaml, reject_secrets
from .dates import legacy_format_problem
from .delivery import KEY_TYPES
from .errors import ConfigError, RecipeError
from .fields import FIELDS, suggest
from .reads import READS

if TYPE_CHECKING:
    from .config import Site

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
RANGE_RE = re.compile(r"^([\^~])(0|[1-9]\d*)(?:\.(0|[1-9]\d*))?(?:\.(0|[1-9]\d*))?$")
PREFIX_RE = re.compile(r"^[A-Za-z0-9_-]+$")
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")
PLACEHOLDERS = {"prefix", "date"}

RECIPE_KEYS = (
    "recipe", "version", "vendor", "title", "description", "spec_source", "changelog", "reads", "patron_data",
    "link", "file_prefix", "selection", "options", "outputs", "checks", "delivery", "default_schedule",
)
REQUIRED_RECIPE_KEYS = ("recipe", "version", "vendor", "title", "changelog", "reads", "patron_data", "outputs")
FEED_KEYS = (
    "uses", "title", "enabled", "file_prefix", "filters", "options", "legacy", "outputs", "checks", "delivery",
    "schedule",
)
SELECTION_DEFAULTS: dict[str, Any] = {
    "include_suppressed_instances": False,
    "include_suppressed_items": False,
    "include_suppressed_holdings": True,
    "include_items_without_barcode": True,
    "bibs_without_items": False,
    "exclude_item_statuses": [],
    "include_location_codes": [],
    "exclude_location_codes": [],
}
OPTIONS_DEFAULTS: dict[str, Any] = {"last_circ_date_from": "checkout", "loan_window_months": 12, "timezone": "UTC"}
LEGACY_DEFAULTS: dict[str, Any] = {
    "created_date_note_type": None, "circ_count_note_type": None, "last_circ_date_note_type": None,
    "date_format": None,
}
CHECKS_DEFAULTS: dict[str, Any] = {"min_item_ratio": 0.9, "must_be_filled": []}
DELIVERY_DEFAULTS: dict[str, Any] = {
    "transport": "none", "upload_mode": "staged", "host": None, "port": 22, "username": None, "remote_dir": ".",
    "host_key_fingerprint": None, "host_key_type": None, "host_key_confirmed": True, "known_hosts": None, "secret": "keyring", "private_key": None, "directory": None,
}
# A shared recipe may only say how files travel; where they go and which server key to trust are local.
RECIPE_DELIVERY_KEYS = ("transport", "upload_mode")
ITEM_FIELD_DEFAULTS: dict[str, Any] = {"tag": "949", "strip_existing": True, "max_per_record": 500, "subfields": {}}
OUTPUT_DEFAULTS: dict[str, dict[str, Any]] = {
    "marc21": {
        "name": None, "format": "marc21", "file_name": "{prefix}_bibs_{date}", "split_every": 0,
        "embed_items": False, "item_field": ITEM_FIELD_DEFAULTS, "date_format": "%Y-%m-%d",
    },
    "delimited": {
        "name": None, "format": "delimited", "file_name": "{prefix}_items_{date}", "extension": "csv",
        "delimiter": ",", "header": True, "empty_value": "", "date_format": "%Y-%m-%d", "columns": [],
    },
}
COLUMN_KEYS = ("header", "field", "required")
LINKS = {"999 ff $i": "instance_id", "001": "instance_hrid"}
RESERVED_FEED_NAMES = {"folio", "smtp", "webhook", "heartbeat", "all"}


# ------------------------------------------------------------------ versions
def parse_version(text: object) -> tuple[int, int, int]:
    m = VERSION_RE.match(str(text).strip())
    if not m:
        raise RecipeError(f"{text!r} is not a version like 1.0.0 (MAJOR.MINOR.PATCH)")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def _bounds(spec: str) -> tuple[tuple[int, int, int], tuple[int, int, int] | None]:
    """(lowest, first excluded) for a version range; an exact version has no upper bound of its own."""
    spec = str(spec).strip()
    m = RANGE_RE.match(spec)
    if not m:
        v = parse_version(spec)
        return v, None
    op, major = m.group(1), int(m.group(2))
    minor, patch = int(m.group(3) or 0), int(m.group(4) or 0)
    low = (major, minor, patch)
    if op == "~":
        return low, (major, minor + 1, 0)
    return low, ((major + 1, 0, 0) if major > 0 else (0, minor + 1, 0))


def version_matches(version: tuple[int, int, int], spec: str) -> bool:
    low, high = _bounds(spec)
    if high is None:
        return version == low
    return low <= version < high


def parse_uses(uses: object) -> tuple[str, str]:
    """``"chq-academic@^1.0"`` -> ``("chq-academic", "^1.0")``. A bare id is an error: the operator chooses."""
    text = str(uses or "").strip()
    rid, sep, spec = text.partition("@")
    rid, spec = rid.strip(), spec.strip()
    if not rid:
        raise RecipeError("uses: name a recipe and a version range, e.g. uses: chq-academic@^1.0")
    if not sep or not spec:
        raise RecipeError(
            f"uses: {text!r} has no version range. Choose one, e.g. {rid}@^1.0 (minor updates automatically) "
            f"or {rid}@1.0.0 (exactly that version)."
        )
    try:
        _bounds(spec)
    except RecipeError:
        raise RecipeError(
            f"uses: {spec!r} is not a version range. Use 1.2.0 (exact), ^1.2 (1.x from 1.2) or ~1.2 (1.2.x)."
        ) from None
    return rid, spec


# ------------------------------------------------------------------ recipes
@dataclass(frozen=True, eq=False)
class Recipe:
    id: str
    version: tuple[int, int, int]
    data: dict
    path: Path

    @property
    def version_str(self) -> str:
        return ".".join(map(str, self.version))

    @property
    def title(self) -> str:
        return str(self.data.get("title") or self.id)

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version_str}"


def _type_name(value: object) -> str:
    return {bool: "true/false", int: "a whole number", float: "a number", str: "text", list: "a list",
            dict: "a mapping of settings", type(None): "empty"}.get(type(value), type(value).__name__)


class _Checker:
    """Collects plain-language problems, each prefixed with the file and the setting's path."""

    def __init__(self, where: str) -> None:
        self.where = where
        self.problems: list[str] = []

    def add(self, path: str, message: str) -> None:
        self.problems.append(f"{self.where}: {path}: {message}" if path else f"{self.where}: {message}")

    def keys(self, data: dict, allowed: Iterable[str], path: str) -> None:
        allowed = list(allowed)
        for k in data:
            if k not in allowed:
                near = [a for a in allowed if a.startswith(str(k)[:4])] if len(str(k)) >= 4 else []
                hint = f" (did you mean {near[0]!r}?)" if near else f" (allowed: {', '.join(allowed)})"
                self.add(f"{path}.{k}" if path else str(k), f"unknown setting{hint}")

    def mapping(self, value: object, path: str) -> dict:
        if value is None:
            return {}
        if not isinstance(value, dict):
            self.add(path, f"expected a mapping of settings, got {_type_name(value)}")
            return {}
        return value

    def boolean(self, value: object, path: str) -> None:
        if not isinstance(value, bool):
            self.add(path, f"must be true or false, got {value!r}")

    def whole(self, value: object, path: str, minimum: int) -> None:
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            self.add(path, f"must be a whole number of at least {minimum}, got {value!r}")

    def text(self, value: object, path: str) -> bool:
        if not isinstance(value, str) or not value.strip():
            self.add(path, f"must be text, got {value!r}")
            return False
        return True

    def text_list(self, value: object, path: str) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            self.add(path, "must be a list, e.g. [MAIN-STACKS, ONLINE]")
            return []
        bad = [v for v in value if not isinstance(v, str)]
        if bad:
            self.add(path, f"a value was read as {_type_name(bad[0])} ({bad[0]!r}), not text. YAML reads numbers such as "
                           "0123, and true or false, that way; put each code in quotes, e.g. [\"0123\"]")
            return []
        return value

    def field(self, name: object, path: str, reads: set[str]) -> None:
        if not isinstance(name, str):
            self.add(path, f"must be one field name, got {_type_name(name)}")
            return
        if name not in FIELDS:
            near = suggest(str(name))
            self.add(path, f"unknown field {name!r}" + (f" (did you mean {near!r}?)" if near else
                                                         "; `folio-exchange fields` lists them"))
            return
        missing = FIELDS[name].needs - reads
        if missing:
            self.add(path, f"field {name!r} needs reads: {', '.join(sorted(missing))}, which the recipe does not "
                           "declare")

    def date_format(self, value: object, path: str) -> None:
        if not self.text(value, path):
            return
        if "%" not in value:
            self.add(path, f"{value!r} has no date parts; use a pattern like %Y-%m-%d")
            return
        try:
            datetime(2024, 3, 15, 13, 45, 30).strftime(value)
        except ValueError as exc:
            self.add(path, f"{value!r} is not a valid date pattern ({exc})")

    def file_name(self, value: object, path: str) -> None:
        if not self.text(value, path):
            return
        for ph in PLACEHOLDER_RE.findall(value):
            if ph not in PLACEHOLDERS:
                self.add(path, f"unknown placeholder {{{ph}}}; only {{prefix}} and {{date}} exist")
        sample = PLACEHOLDER_RE.sub("x", value)
        if not SAFE_NAME_RE.match(sample):
            self.add(path, f"{value!r}: use only letters, digits, dots, hyphens and underscores")


def validate_recipe(data: dict, where: str, *, shared: bool = True) -> list[str]:
    """Every problem with a recipe (``shared``) or a feed's merged recipe, in plain language."""
    c = _Checker(where)
    if not isinstance(data, dict):
        return [f"{where}: expected a mapping of settings at the top level"]
    if shared:
        c.keys(data, RECIPE_KEYS, "")
        for key in REQUIRED_RECIPE_KEYS:
            if key not in data:
                c.add(key, "required")
        rid = data.get("recipe")
        if rid is not None and not (isinstance(rid, str) and ID_RE.match(rid)):
            c.add("recipe", "must be lowercase letters, digits and hyphens")
        version = None
        if "version" in data:
            try:
                version = parse_version(data["version"])
            except RecipeError as exc:
                c.add("version", str(exc))
        for key in ("vendor", "title"):
            if key in data:
                c.text(data[key], key)
        for key in ("description", "spec_source"):
            if data.get(key) is not None and not isinstance(data[key], str):
                c.add(key, "must be text")
        log = data.get("changelog")
        if "changelog" in data:
            if not isinstance(log, list) or not log:
                c.add("changelog", "must list one entry per version: {version, date, changes}")
            else:
                seen: list[tuple[int, int, int]] = []
                for i, entry in enumerate(log):
                    e = c.mapping(entry, f"changelog[{i}]")
                    c.keys(e, ("version", "date", "changes"), f"changelog[{i}]")
                    for key in ("version", "date", "changes"):
                        if key not in e:
                            c.add(f"changelog[{i}].{key}", "required")
                    try:
                        seen.append(parse_version(e.get("version")))
                    except RecipeError as exc:
                        c.add(f"changelog[{i}].version", str(exc))
                if version is not None and version not in seen:
                    c.add("changelog", f"has no entry for version {'.'.join(map(str, version))} (REC-1)")

    reads = c.text_list(data.get("reads"), "reads")
    read_set = set(reads)
    for i, r in enumerate(reads):
        if r not in READS:
            c.add(f"reads[{i}]", f"unknown read {r!r} (known: {', '.join(READS)})")
    if shared and not reads:
        c.add("reads", "list what the recipe reads, e.g. [instances, holdings, items]")
    if data.get("patron_data", "none") != "none":
        c.add("patron_data", "must be none: FOLIO-Exchange v1 never reads or exports patron data")

    if data.get("file_prefix") is not None and not (
        isinstance(data["file_prefix"], str) and PREFIX_RE.match(data["file_prefix"])
    ):
        c.add("file_prefix", "use only letters, digits, hyphens and underscores")

    sel = c.mapping(data.get("selection"), "selection")
    c.keys(sel, SELECTION_DEFAULTS, "selection")
    for key, default in SELECTION_DEFAULTS.items():
        if key in sel:
            if isinstance(default, bool):
                c.boolean(sel[key], f"selection.{key}")
            else:
                c.text_list(sel[key], f"selection.{key}")

    opts = c.mapping(data.get("options"), "options")
    c.keys(opts, OPTIONS_DEFAULTS, "options")
    if "last_circ_date_from" in opts and opts["last_circ_date_from"] not in ("checkout", "any"):
        c.add("options.last_circ_date_from", "must be checkout or any")
    if "loan_window_months" in opts:
        c.whole(opts["loan_window_months"], "options.loan_window_months", 1)
    if "timezone" in opts:
        try:
            ZoneInfo(str(opts["timezone"]))
        except (ZoneInfoNotFoundError, ValueError):
            c.add("options.timezone", f"unknown time zone {opts['timezone']!r} (use a name like America/New_York)")

    outputs = data.get("outputs")
    names: list[str] = []
    formats: list[str] = []
    delimited: list[dict] = []
    file_names: dict[str, str] = {}
    if not isinstance(outputs, list) or not outputs:
        if shared or outputs is not None:
            c.add("outputs", "list one or more outputs")
        outputs = []
    for i, out in enumerate(outputs):
        o = c.mapping(out, f"outputs[{i}]")
        name = o.get("name")
        label = f"outputs.{name}" if isinstance(name, str) and name else f"outputs[{i}]"
        if not (isinstance(name, str) and ID_RE.match(name.replace("_", "-"))):
            c.add(f"{label}.name", "required: lowercase letters, digits, hyphens or underscores")
        elif name in names:
            c.add(f"{label}.name", "used twice; output names must be unique")
        names.append(str(name))
        fmt = o.get("format")
        if not isinstance(fmt, str) or fmt not in OUTPUT_DEFAULTS:
            c.add(f"{label}.format", f"must be marc21 or delimited, got {fmt!r}")
            continue
        base = str(o.get("file_name", OUTPUT_DEFAULTS[fmt]["file_name"]))
        final = f"{base}.{'mrc' if fmt == 'marc21' else o.get('extension', 'csv')}"
        if final in file_names:
            c.add(f"{label}.file_name", f"{final} is also the file name of output {file_names[final]!r}; "
                                        "each output needs its own file name")
        file_names.setdefault(final, str(name))
        formats.append(fmt)
        c.keys(o, OUTPUT_DEFAULTS[fmt], label)
        if "file_name" in o:
            c.file_name(o["file_name"], f"{label}.file_name")
        if "date_format" in o:
            c.date_format(o["date_format"], f"{label}.date_format")
        if fmt == "marc21":
            if "split_every" in o:
                c.whole(o["split_every"], f"{label}.split_every", 0)
            if "embed_items" in o:
                c.boolean(o["embed_items"], f"{label}.embed_items")
            itf = c.mapping(o.get("item_field"), f"{label}.item_field")
            c.keys(itf, ITEM_FIELD_DEFAULTS, f"{label}.item_field")
            tag = str(itf.get("tag", "949"))
            if not (re.fullmatch(r"\d{3}", tag) and 10 <= int(tag) <= 999):
                c.add(f"{label}.item_field.tag", f"{tag!r} is not a data field tag (010 to 999)")
            if "strip_existing" in itf:
                c.boolean(itf["strip_existing"], f"{label}.item_field.strip_existing")
            if "max_per_record" in itf:
                c.whole(itf["max_per_record"], f"{label}.item_field.max_per_record", 1)
            subs = c.mapping(itf.get("subfields"), f"{label}.item_field.subfields")
            for code, fname in subs.items():
                if not re.fullmatch(r"[a-z0-9]", str(code)):
                    c.add(f"{label}.item_field.subfields.{code}", "subfield codes are one letter or digit")
                c.field(fname, f"{label}.item_field.subfields.{code}", read_set)
            if o.get("embed_items") and not subs:
                c.add(f"{label}.item_field.subfields", "embed_items is true, so list the subfields to write")
        else:
            delimited.append(o)
            ext = o.get("extension", "csv")
            if not (isinstance(ext, str) and ext.isascii() and ext.isalnum()) or ext.lower() == "mrc":
                c.add(f"{label}.extension", f"{ext!r}: use letters and digits, and not mrc")
            delim = o.get("delimiter", ",")
            if not isinstance(delim, str) or len(delim) != 1 or delim in "\r\n\"":
                c.add(f"{label}.delimiter", f"{delim!r}: use exactly one character, such as , | or a tab")
            if "header" in o:
                c.boolean(o["header"], f"{label}.header")
            if "empty_value" in o and not isinstance(o["empty_value"], str):
                c.add(f"{label}.empty_value", "must be text (quote it, e.g. \"None\" or \"\")")
            cols = o.get("columns")
            if not isinstance(cols, list) or not cols:
                c.add(f"{label}.columns", "list one or more columns: {header, field}")
                cols = []
            headers: set[str] = set()
            for j, col in enumerate(cols):
                cp = f"{label}.columns[{j}]"
                cd = c.mapping(col, cp)
                c.keys(cd, COLUMN_KEYS, cp)
                if c.text(cd.get("header"), f"{cp}.header"):
                    if cd["header"] in headers:
                        c.add(f"{cp}.header", f"{cd['header']!r} is used twice")
                    headers.add(cd["header"])
                c.field(cd.get("field"), f"{cp}.field", read_set)
                if "required" in cd:
                    c.boolean(cd["required"], f"{cp}.required")
    if formats.count("marc21") > 1:
        c.add("outputs", "at most one marc21 output in this version")
    if "marc21" in formats and "instances" not in read_set:
        c.add("reads", "a marc21 output needs the instances read")

    link = data.get("link")
    if link is not None:
        lk = c.mapping(link, "link")
        c.keys(lk, ("bib_field", "item_column", "value"), "link")
        bib_field, value = lk.get("bib_field"), lk.get("value")
        if not isinstance(bib_field, str) or bib_field not in LINKS:
            c.add("link.bib_field", f"must be one of {', '.join(repr(k) for k in LINKS)}")
        elif value != LINKS[bib_field]:
            c.add("link.value", f"for bib_field {bib_field!r} the value is {LINKS[bib_field]!r}")
        column = lk.get("item_column")
        if not any(
            isinstance(col, dict) and col.get("header") == column and col.get("field") == value
            for o in delimited for col in (o.get("columns") or [])
        ):
            c.add("link.item_column", f"no delimited output has a column {column!r} with field {value!r}; "
                                      "the vendor could not match items to bibs")
    elif "marc21" in formats and delimited:
        c.add("link", "required when a recipe has both marc21 and delimited outputs (how items match bibs)")

    chk = c.mapping(data.get("checks"), "checks")
    c.keys(chk, CHECKS_DEFAULTS, "checks")
    ratio = chk.get("min_item_ratio", 0)
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not 0 <= ratio <= 1:
        c.add("checks.min_item_ratio", f"must be a number from 0 (off) to 1, got {ratio!r}")
    for name in c.text_list(chk.get("must_be_filled"), "checks.must_be_filled"):
        c.field(name, "checks.must_be_filled", read_set)
        if shared and name == "barcode" and {**SELECTION_DEFAULTS, **sel}.get("include_items_without_barcode"):
            c.add("checks.must_be_filled", "barcode must be filled, but selection keeps items without a barcode, "
                                           "so every run would be blocked; set selection.include_items_without_"
                                           "barcode: false or remove barcode from checks.must_be_filled")

    dlv = c.mapping(data.get("delivery"), "delivery")
    c.keys(dlv, RECIPE_DELIVERY_KEYS if shared else DELIVERY_DEFAULTS, "delivery")
    transport = dlv.get("transport", "none")
    if transport == "ftp":
        c.add("delivery.transport", "plain FTP sends passwords and files unencrypted and is never offered; "
                                    "use sftp")
    elif transport not in ("sftp", "local", "none"):
        c.add("delivery.transport", f"must be sftp, local or none, got {transport!r}")
    if dlv.get("upload_mode", "staged") not in ("staged", "direct"):
        c.add("delivery.upload_mode", "must be staged (.part, then rename) or direct")
    if "port" in dlv:
        c.whole(dlv["port"], "delivery.port", 1)
    if "secret" in dlv and not credentials.is_secret_ref(dlv["secret"]):
        c.add("delivery.secret", "must be keyring or env:NAME")
    fp = dlv.get("host_key_fingerprint")
    if fp is not None and not (isinstance(fp, str) and fp.startswith("SHA256:") and len(fp) > 20):
        c.add("delivery.host_key_fingerprint", "must look like SHA256:... (run `folio-exchange check --delivery`)")
    kind = dlv.get("host_key_type")
    if kind is not None and kind not in KEY_TYPES:
        c.add("delivery.host_key_type", f"must be one of {', '.join(KEY_TYPES)}, got {kind!r}")
    if "host_key_confirmed" in dlv:
        c.boolean(dlv["host_key_confirmed"], "delivery.host_key_confirmed")
    for key in ("host", "username", "remote_dir", "known_hosts", "private_key", "directory"):
        if dlv.get(key) is not None and not isinstance(dlv[key], str):
            c.add(f"delivery.{key}", "must be text")
    for key in ("private_key", "known_hosts"):
        if key_text(dlv.get(key)):
            c.add(f"delivery.{key}", "holds the key itself, not the path of a file; give the path of the key "
                                     "file, or store the key from the page")

    if data.get("default_schedule") is not None:
        try:
            schedule.parse(data["default_schedule"])
        except ConfigError as exc:
            c.add("default_schedule", str(exc))
    return c.problems


def key_text(value: object) -> bool:
    """True when a delivery path setting holds key text (a line break, or PEM armour) instead of a file path."""
    return isinstance(value, str) and ("\n" in value or "\r" in value or "PRIVATE KEY" in value)


def load_recipe_file(path: Path) -> Recipe:
    """Load and check one shared recipe file. RecipeError lists every problem."""
    path = Path(path)
    where = f"{path.parent.name}/{path.name}"
    try:
        data = read_yaml(path, where)
        reject_secrets(data, where)
    except ConfigError as exc:
        raise RecipeError(str(exc)) from None
    problems = validate_recipe(data, where)
    if not problems:
        if data["recipe"] != path.parent.name:
            problems.append(f"{where}: recipe: {data['recipe']!r} must match its folder name {path.parent.name!r}")
        if str(data["version"]) != path.stem:
            problems.append(f"{where}: version: {data['version']!r} must match the file name {path.stem!r}")
    if problems:
        raise RecipeError("\n".join(problems))
    return Recipe(id=data["recipe"], version=parse_version(data["version"]), data=data, path=path)


def _normalised(content: bytes) -> bytes:
    """A recipe file's bytes apart from line endings and a byte-order mark (a Windows checkout may store CRLF)."""
    return content.removeprefix(b"\xef\xbb\xbf").replace(b"\r\n", b"\n")


def recipe_digest(recipe: Recipe) -> str | None:
    """SHA-256 of the recipe file, ignoring line endings and a byte-order mark, so two libraries can tell
    whether they run the same recipe text. None if the file cannot be read."""
    try:
        return hashlib.sha256(_normalised(recipe.path.read_bytes())).hexdigest()
    except OSError:
        return None


class RecipeLibrary:
    """Every recipe version in the given folders. Earlier folders win on an identical id and version."""

    def __init__(self, dirs: list[Path]) -> None:
        self.dirs = [Path(d) for d in dirs]
        self._recipes: dict[tuple[str, tuple[int, int, int]], Recipe] = {}
        self._problems: list[str] = []
        self._shadowed: list[tuple[Recipe, Path]] = []      # (the copy used, a later copy it hides)
        for d in self.dirs:
            if not d.is_dir():
                continue
            for path in sorted(d.glob("*/*.yaml")):
                if not ID_RE.match(path.parent.name):
                    continue
                try:
                    r = load_recipe_file(path)
                except RecipeError as exc:
                    self._problems.append(f"{path}:\n  " + str(exc).replace("\n", "\n  "))
                    continue
                kept = self._recipes.setdefault((r.id, r.version), r)
                if kept is not r:
                    self._shadowed.append((kept, path))

    def all(self) -> list[Recipe]:
        return sorted(self._recipes.values(), key=lambda r: (r.id, r.version))

    def problems(self) -> list[str]:
        return list(self._problems)

    def shadow_warnings(self) -> list[str]:
        """Plain warnings for each recipe file used in place of another copy of the same id and version (usually
        the one bundled with FOLIO-Exchange) whose content differs. Overriding is allowed, so this only warns."""
        out = []
        for kept, hidden in self._shadowed:
            try:
                same = _normalised(kept.path.read_bytes()) == _normalised(hidden.read_bytes())
            except OSError:
                same = False
            if same:
                continue
            other = ("the copy that comes with FOLIO-Exchange" if hidden.resolve().is_relative_to(
                BUNDLED_RECIPES.resolve()) else str(hidden))
            out.append(f"{kept.path} is used for {kept.ref}, but it differs from {other}. Feeds on {kept.ref} send "
                       f"what this local copy says. If the change is meant, give it its own version number so the "
                       "run history can tell the two apart; if not, delete the local copy.")
        return out

    def ids(self) -> list[str]:
        return sorted({r.id for r in self._recipes.values()})

    def versions(self, recipe_id: str) -> list[Recipe]:
        return sorted((r for r in self._recipes.values() if r.id == recipe_id), key=lambda r: r.version)

    def latest(self, recipe_id: str) -> Recipe | None:
        found = self.versions(recipe_id)
        return found[-1] if found else None

    def resolve(self, uses: str) -> Recipe:
        rid, spec = parse_uses(uses)
        found = self.versions(rid)
        if not found:
            known = ", ".join(self.ids()) or "none"
            raise RecipeError(f"uses: no recipe called {rid!r} (recipes available: {known})")
        matches = [r for r in found if version_matches(r.version, spec)]
        if not matches:
            have = ", ".join(r.version_str for r in found)
            raise RecipeError(f"uses: no version of {rid} matches {spec!r} (versions available: {have})")
        return matches[-1]

    def newer_major(self, uses: str) -> Recipe | None:
        """The highest version newer than every version the range allows (held for review), if any."""
        rid, spec = parse_uses(uses)
        found = self.versions(rid)
        matches = [r for r in found if version_matches(r.version, spec)]
        if not matches:
            return None
        newer = [r for r in found if r.version > matches[-1].version and not version_matches(r.version, spec)]
        return newer[-1] if newer else None


# ------------------------------------------------------------------ feeds
@dataclass
class Feed:
    name: str
    path: Path
    recipe: Recipe
    uses: str
    title: str
    enabled: bool
    file_prefix: str
    reads: tuple[str, ...]
    selection: dict
    options: dict
    legacy: dict
    link: dict | None
    outputs: list[dict]
    checks: dict
    delivery: dict
    schedule: str

    def output(self, name: str) -> dict:
        for o in self.outputs:
            if o["name"] == name:
                return o
        raise KeyError(name)

    @property
    def delivers(self) -> bool:
        return self.delivery.get("transport") not in (None, "none")


def _overlay_outputs(recipe_outputs: list[dict], local: object, c: _Checker) -> list[dict]:
    outs = [copy.deepcopy(o) for o in recipe_outputs]
    loc = c.mapping(local, "outputs")
    by_name = {o.get("name"): o for o in outs}
    for name, settings in loc.items():
        if name not in by_name:
            c.add(f"outputs.{name}", f"the recipe has no output called {name!r} (it has: {', '.join(map(str, by_name))})")
            continue
        s = c.mapping(settings, f"outputs.{name}")
        for key in ("name", "format"):
            if key in s:
                c.add(f"outputs.{name}.{key}", "is set by the recipe and cannot be changed locally")
        target = by_name[name]
        fmt = target.get("format")
        allowed = [k for k in OUTPUT_DEFAULTS.get(fmt, {}) if k not in ("name", "format")]
        c.keys({k: v for k, v in s.items() if k not in ("name", "format")}, allowed, f"outputs.{name}")
        for key, value in s.items():
            if key in ("name", "format") or key not in allowed:
                continue
            if key == "columns":
                cols = value if isinstance(value, list) else []
                have = {col.get("field") for col in cols if isinstance(col, dict) and isinstance(col.get("field"), str)}
                headers = {col.get("header"): col.get("field") for col in cols
                           if isinstance(col, dict) and isinstance(col.get("header"), str)}
                for col in target.get("columns") or []:
                    if not col.get("required") or col.get("field") in have:
                        continue
                    found = headers.get(col.get("header"))
                    if col.get("header") in headers and not (isinstance(found, str) and found in FIELDS):
                        continue                    # a misspelled field: the unknown-field message says so
                    if col.get("header") in headers:
                        c.add(f"outputs.{name}.columns", f"column {col.get('header')!r} must keep the field "
                                                         f"{col.get('field')!r} (the recipe requires it)")
                    else:
                        c.add(f"outputs.{name}.columns",
                              f"the recipe requires the column {col.get('header')!r} (field {col.get('field')!r}); a "
                              "columns list replaces the recipe's whole list, so include it")
                target["columns"] = copy.deepcopy(value)
            elif isinstance(value, dict) and isinstance(target.get(key), dict):
                target[key] = merge(target[key], value)
            else:
                target[key] = copy.deepcopy(value)
    return outs


def _with_defaults(out: dict) -> dict:
    fmt = out.get("format")
    base = OUTPUT_DEFAULTS.get(fmt)
    if not base:
        return out
    full = merge(base, out)
    if fmt == "marc21":
        full["item_field"] = merge(ITEM_FIELD_DEFAULTS, out.get("item_field") or {})
    if fmt == "delimited":
        full["columns"] = [{"required": False, **col} for col in full.get("columns") or []]
    return full


def feed_name(path: Path) -> str:
    return Path(path).stem


def load_feed(path: Path, library: RecipeLibrary, root: Path | None = None) -> Feed:
    """Load a feed file: resolve its recipe, apply the local settings, and check the result. Relative paths
    in ``delivery`` are taken from ``root`` (the site folder; default: the feed folder's parent)."""
    path = Path(path)
    name = feed_name(path)
    where = f"{path.parent.name}/{path.name}"
    if not ID_RE.match(name):
        raise ConfigError(f"{where}: feed names use lowercase letters, digits and hyphens (rename the file)")
    if name in RESERVED_FEED_NAMES:
        raise ConfigError(f"{where}: {name!r} is reserved for `folio-exchange secret set {name}`; rename the file")
    raw = read_yaml(path, where)
    return load_feed_data(raw, path, library, root)


def load_feed_data(raw: dict, path: Path, library: RecipeLibrary, root: Path | None = None) -> Feed:
    """Check and resolve feed settings already read from ``path`` (used by `upgrade` to try a new range)."""
    path = Path(path)
    name = feed_name(path)
    where = f"{path.parent.name}/{path.name}"
    reject_secrets(raw, where)
    c = _Checker(where)
    c.keys(raw, FEED_KEYS, "")
    if "uses" not in raw:
        raise ConfigError(f"{where}: uses: required, e.g. uses: chq-academic@^1.0 (`folio-exchange recipes` lists "
                          "them)")
    try:
        recipe = library.resolve(raw["uses"])
    except RecipeError as exc:
        raise ConfigError(f"{where}: {exc}") from None
    r = recipe.data

    if "enabled" in raw:
        c.boolean(raw["enabled"], "enabled")
    if raw.get("title") is not None:
        c.text(raw["title"], "title")
    filters = c.mapping(raw.get("filters"), "filters")
    c.keys(filters, SELECTION_DEFAULTS, "filters")
    options = c.mapping(raw.get("options"), "options")
    c.keys(options, OPTIONS_DEFAULTS, "options")
    legacy = c.mapping(raw.get("legacy"), "legacy")
    c.keys(legacy, LEGACY_DEFAULTS, "legacy")
    for key, value in legacy.items():
        if key == "date_format":
            problem = legacy_format_problem(value) if value not in (None, "") else None
            if problem:
                c.add("legacy.date_format", problem)
        elif value is not None and not isinstance(value, str):
            c.add(f"legacy.{key}", "must be an item note type name, or empty")
    checks = c.mapping(raw.get("checks"), "checks")
    c.keys(checks, CHECKS_DEFAULTS, "checks")
    delivery = c.mapping(raw.get("delivery"), "delivery")
    sched = raw.get("schedule") or r.get("default_schedule") or "manual"
    try:
        schedule.parse(sched)
    except ConfigError as exc:
        c.add("schedule", str(exc))

    outputs = _overlay_outputs(r.get("outputs") or [], raw.get("outputs"), c)
    must = list(dict.fromkeys(list((r.get("checks") or {}).get("must_be_filled") or [])
                              + list(checks.get("must_be_filled") or [])))
    merged = {
        "reads": list(r["reads"]),
        "patron_data": r.get("patron_data"),
        "link": r.get("link"),
        "file_prefix": raw.get("file_prefix", r.get("file_prefix")),
        "selection": merge(merge(SELECTION_DEFAULTS, r.get("selection")), filters),
        "options": merge(merge(OPTIONS_DEFAULTS, r.get("options")), options),
        "outputs": outputs,
        "checks": {**merge(CHECKS_DEFAULTS, {k: v for k, v in (r.get("checks") or {}).items()}),
                   **({"min_item_ratio": checks["min_item_ratio"]} if "min_item_ratio" in checks else {}),
                   "must_be_filled": must},
        "delivery": merge(merge(DELIVERY_DEFAULTS, r.get("delivery")), delivery),
    }
    problems = c.problems + [p.replace(f"{where}: selection.", f"{where}: filters.")
                             for p in validate_recipe(merged, where, shared=False)]
    if "barcode" in must and merged["selection"].get("include_items_without_barcode"):
        # checked here, not in validate_recipe: only the feed's own additions to must_be_filled can be removed
        in_recipe = "barcode" in ((r.get("checks") or {}).get("must_be_filled") or [])
        problems.append(f"{where}: checks.must_be_filled: barcode must be filled"
                        + (f" (the recipe {recipe.ref} requires it)" if in_recipe else "")
                        + ", but filters.include_items_without_barcode is true, so every run would be blocked; "
                        "set filters.include_items_without_barcode: false"
                        + ("" if in_recipe else " or remove barcode from checks.must_be_filled"))
    d = merged["delivery"]
    if d["transport"] == "sftp":
        for key in ("host", "username"):
            if not str(d.get(key) or "").strip():
                problems.append(f"{where}: delivery.{key}: required for transport sftp")
        if not d.get("host_key_fingerprint") and not d.get("known_hosts"):
            problems.append(f"{where}: delivery.host_key_fingerprint: required for transport sftp, so the vendor's "
                            "server is recognised (`folio-exchange check --delivery` shows its key)")
    if d["transport"] == "local" and not str(d.get("directory") or "").strip():
        problems.append(f"{where}: delivery.directory: required for transport local")
    if problems:
        raise ConfigError("\n".join(dict.fromkeys(problems)))
    base = Path(root) if root else path.resolve().parent.parent
    for key in ("directory", "private_key", "known_hosts"):
        if d.get(key):
            p = Path(os.path.expanduser(str(d[key])))
            d[key] = str(p if p.is_absolute() else (base / p).resolve())
    if not merged["file_prefix"]:
        merged["file_prefix"] = recipe.id
    return Feed(
        name=name,
        path=path,
        recipe=recipe,
        uses=str(raw["uses"]).strip(),
        title=str(raw.get("title") or recipe.title),
        enabled=bool(raw.get("enabled", True)),
        file_prefix=str(merged["file_prefix"]),
        reads=tuple(merged["reads"]),
        selection=merged["selection"],
        options=merged["options"],
        legacy=merge(LEGACY_DEFAULTS, legacy),
        link=copy.deepcopy(merged["link"]),
        outputs=[_with_defaults(o) for o in outputs],
        checks=merged["checks"],
        delivery=d,
        schedule=str(sched),
    )


def feed_paths(site: Site) -> list[Path]:
    if not site.feeds_dir.is_dir():
        return []
    return sorted(p for p in site.feeds_dir.glob("*.yaml") if p.is_file())


def load_all(site: Site, library: RecipeLibrary | None = None) -> tuple[list[Feed], dict[str, str]]:
    """Every feed that loads, and the problems with every one that does not (name -> message)."""
    library = library or RecipeLibrary(site.recipe_dirs)
    feeds: list[Feed] = []
    errors: dict[str, str] = {}
    for path in feed_paths(site):
        try:
            feeds.append(load_feed(path, library, site.root))
        except ConfigError as exc:
            errors[feed_name(path)] = str(exc)
        except Exception as exc:  # noqa: BLE001 - one odd file must never stop the other feeds
            errors[feed_name(path)] = (f"{path.parent.name}/{path.name}: could not be read "
                                       f"({type(exc).__name__}: {exc}); check it against examples/feeds/chq.yaml")
    for name, problem in file_collisions(feeds).items():
        errors[name] = problem
    return [f for f in feeds if f.name not in errors], errors


def output_names(feed: Feed) -> list[str]:
    """The file names a feed sends, with {date} left as it is (it is the same for every feed on a day)."""
    names = []
    for o in feed.outputs:
        base = o["file_name"].replace("{prefix}", feed.file_prefix)
        names.append(f"{base}.mrc" if o["format"] == "marc21" else f"{base}.{o.get('extension', 'csv')}")
    return names


# ------------------------------------------------------------------ layout signature
LAYOUT_LABELS = {
    "format": "format", "file_name": "file name", "extension": "file extension", "delimiter": "delimiter",
    "header": "header row", "empty_value": "empty value", "date_format": "date format",
    "split_every": "records per file", "embed_items": "items written into the MARC records",
    "item_field": "MARC item field", "file_prefix": "file prefix", "link": "how items link to bibs",
    "timezone": "time zone",
}


def layout_signature(feed: Feed) -> dict:
    """Everything that decides the layout of what the vendor receives, as plain JSON data. A delivery whose
    signature differs from the last delivery to the same destination is held for review.

    Dates are written in ``options.timezone``, so it goes with each output's date format. Without a header row
    a column's header is never written: columns are then recorded by field alone."""
    tz = str(feed.options.get("timezone") or "UTC")
    outputs: dict[str, dict] = {}
    for o in feed.outputs:
        sig: dict[str, Any] = {"format": o["format"], "file_name": o["file_name"]}
        if o["format"] == "delimited":
            sig.update({k: o.get(k) for k in ("extension", "delimiter", "header", "empty_value", "date_format")})
            sig["timezone"] = tz
            sig["columns"] = [[c.get("header") if o.get("header", True) else None, c.get("field")]
                              for c in o.get("columns") or []]
        else:
            itf = o.get("item_field") or {}
            embed = bool(o.get("embed_items"))
            item_field: dict[str, Any] = {"tag": str(itf.get("tag")), "strip_existing": bool(itf.get("strip_existing"))}
            if embed:                       # subfields, their limit and dates are written only when items are embedded
                item_field["subfields"] = [[str(k), v] for k, v in (itf.get("subfields") or {}).items()]
                item_field["max_per_record"] = itf.get("max_per_record")
                sig["date_format"] = o.get("date_format")
                sig["timezone"] = tz
            sig.update({"split_every": int(o.get("split_every") or 0), "embed_items": embed, "item_field": item_field})
        outputs[str(o["name"])] = sig
    return json.loads(json.dumps({"file_prefix": feed.file_prefix, "link": feed.link, "outputs": outputs}))


def _show(value: object) -> str:
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, str):
        return {"": "nothing (empty)", "\t": "tab", " ": "a space"}.get(value, value)
    if isinstance(value, list):
        return ", ".join(f"${a} {b}" if isinstance(a, str) and len(a) == 1 else f"{a} {b}" for a, b in value)
    if isinstance(value, dict):
        return ", ".join(f"{k} {_show(v)}" for k, v in value.items())
    return str(value)


def _changed(label: str, before: object, after: object) -> str:
    if isinstance(before, dict) and isinstance(after, dict) and label != LAYOUT_LABELS["link"]:
        return "; ".join(_changed(f"{label} {str(k).replace('_', ' ')}", before.get(k), after.get(k))
                         for k in dict.fromkeys([*before, *after]) if before.get(k) != after.get(k))
    return f"{label} changed from {_show(before)} to {_show(after)}"


def _column_name(c: tuple) -> str:
    """A column as the change list names it: its header, or its field when no header row is written."""
    return repr(c[0] if c[0] is not None else c[1])


def _column_changes(before: list, after: list) -> str:
    old, new = [tuple(c) for c in before], [tuple(c) for c in after]
    gone, added = [c for c in old if c not in new], [c for c in new if c not in old]
    parts: list[str] = []
    renamed: dict[tuple, tuple] = {}
    for c in list(gone):
        match = (next((n for n in added if n[1] == c[1]), None)          # same field under a new header
                 or next((n for n in added if c[0] is not None and n[0] == c[0]), None))   # same header, new field
        if match is None:
            continue
        parts.append(f"{_column_name(c)} renamed to {_column_name(match)}" if match[1] == c[1]
                     else f"{_column_name(c)} now holds {match[1]} (was {c[1]})")
        renamed[c] = match
        gone.remove(c)
        added.remove(match)
    parts += [f"{_column_name(c)} removed" for c in gone] + [f"{_column_name(c)} added" for c in added]
    if [renamed.get(c, c) for c in old if c not in gone] != [c for c in new if c not in added]:
        parts.append("the order changed")
    return ", ".join(parts)


def layout_changes(before: dict, after: dict) -> list[str]:
    """What differs between two layout signatures, in plain words. A setting one side does not record (a
    signature from an older version) is not compared."""
    out = [_changed(LAYOUT_LABELS[k], before[k], after[k]) for k in ("file_prefix", "link")
           if k in before and k in after and before[k] != after[k]]
    old, new = before.get("outputs") or {}, after.get("outputs") or {}
    out += [f"new {n} file added" for n in new if n not in old]
    out += [f"{n} file no longer sent" for n in old if n not in new]
    for name in (n for n in new if n in old):
        headerless = old[name].get("header") is False or new[name].get("header") is False
        for key, value in new[name].items():
            if key not in old[name]:
                continue
            was = old[name][key]
            if key == "columns" and headerless:     # headers that are never written are not compared
                was, value = _fields_only(was), _fields_only(value)
            if was == value:
                continue
            text = (f"columns changed: {_column_changes(was, value)}" if key == "columns"
                    else _changed(LAYOUT_LABELS.get(key, key), was, value))
            out.append(f"{name} file: {text}")
    return out


def _plain(value: object) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(map(str, value)) or "none"
    return _show(value)


def settings_changes(before: Feed, after: Feed) -> list[str]:
    """What a recipe change does to a feed besides its file layout, in plain words: the checks, which records
    are selected, the options, what is read from FOLIO, the schedule, the delivery method and the item link."""
    out: list[str] = []
    for label, a, b in (
        ("check", before.checks, after.checks),
        ("selection", before.selection, after.selection),
        ("option", before.options, after.options),
        ("delivery", {k: before.delivery.get(k) for k in RECIPE_DELIVERY_KEYS},
         {k: after.delivery.get(k) for k in RECIPE_DELIVERY_KEYS}),
    ):
        out += [f"{label} {k} changed from {_plain(a.get(k))} to {_plain(b.get(k))}"
                for k in dict.fromkeys([*a, *b]) if a.get(k) != b.get(k)]
    gone = [r for r in before.reads if r not in after.reads]
    added = [r for r in after.reads if r not in before.reads]
    if gone:
        out.append(f"no longer reads {', '.join(gone)} from FOLIO"
                   + (" (bibs go out as brief records, without their MARC)" if "marc" in gone else ""))
    if added:
        out.append(f"now reads {', '.join(added)} from FOLIO")
    if before.schedule != after.schedule:
        out.append(f"schedule changed from {before.schedule} to {after.schedule}")
    if before.link != after.link:
        out.append(_changed(LAYOUT_LABELS["link"], before.link, after.link))
    return out


def _fields_only(columns: list) -> list:
    return [[None, c[1]] for c in columns or []]


def file_collisions(feeds: list[Feed]) -> dict[str, str]:
    """Enabled feeds that would send a file of the same name to the same place: the later one would overwrite
    the earlier at the vendor, and every check would still pass. Both feeds are reported."""
    from .delivery import destination_key

    seen: dict[tuple[str, str], str] = {}
    problems: dict[str, str] = {}
    for f in feeds:
        if not f.enabled or not f.delivers:
            continue
        dest = destination_key(f.delivery)
        for name in output_names(f):
            other = seen.setdefault((dest, name), f.name)
            if other != f.name:
                text = _collision_text(other, f.name, name, dest)
                problems.setdefault(other, text)
                problems.setdefault(f.name, text)
    return problems


def _collision_text(first: str, second: str, name: str, dest: str) -> str:
    return (f"feeds/{first}.yaml and feeds/{second}.yaml both send {name} to {dest}, so one would overwrite the "
            f"other at the vendor. Give one of them its own file_prefix (for example file_prefix: {second}) or a "
            "different folder.")


def collision_with(feed: Feed, feeds: list[Feed]) -> str | None:
    """Whether another delivering feed in ``feeds``, paused or not, sends one of ``feed``'s file names to the same
    place. file_collisions compares enabled feeds only (a clash there stops both from loading), so a paused copy
    is checked with this before it sends anything."""
    from .delivery import destination_key

    if not feed.delivers:
        return None
    dest = destination_key(feed.delivery)
    mine = set(output_names(feed))
    for other in feeds:
        if other.name == feed.name or not other.delivers or destination_key(other.delivery) != dest:
            continue
        same = [n for n in output_names(other) if n in mine]
        if same:
            return _collision_text(other.name, feed.name, same[0], dest)
    return None


def load_feeds(site: Site, library: RecipeLibrary | None = None) -> list[Feed]:
    """Every feed, sorted by name. ConfigError lists every feed that has a problem."""
    feeds, errors = load_all(site, library)
    if errors:
        raise ConfigError("\n".join(errors.values()))
    return feeds


def feed_errors(site: Site, library: RecipeLibrary) -> dict[str, str]:
    return load_all(site, library)[1]
