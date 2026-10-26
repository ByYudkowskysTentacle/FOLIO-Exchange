"""Delimited text output (MAP-3): one row per item, with the columns, header, delimiter,
empty value and date format the recipe and feed set. Quoting is minimal, as in
folio-chq-extract: a value is quoted only when it holds the delimiter, a quote or a
line break."""
from __future__ import annotations

import csv
from pathlib import Path
from zoneinfo import ZoneInfo

from .dates import fmt
from .fields import DATE_FIELDS


class DelimitedWriter:
    def __init__(self, output: dict, path: Path, tz: ZoneInfo) -> None:
        self.path = Path(path)
        self.columns = output["columns"]
        self.date_format = output["date_format"]
        self.empty_value = output.get("empty_value", "")
        self.tz = tz
        self.rows = 0
        self._fh = open(self.path, "w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._fh, delimiter=output["delimiter"], quoting=csv.QUOTE_MINIMAL)
        if output.get("header", True):
            self._writer.writerow([c["header"] for c in self.columns])

    def value(self, name: str, value) -> str:
        if name in DATE_FIELDS:
            value = fmt(value, self.date_format, self.tz)
        return self.empty_value if value in (None, "") else value

    def write(self, row: dict) -> None:
        self._writer.writerow([self.value(c["field"], row.get(c["field"])) for c in self.columns])
        self.rows += 1

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()
