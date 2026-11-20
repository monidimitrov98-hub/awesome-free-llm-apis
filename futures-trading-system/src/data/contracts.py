"""Futures contract symbol identification.

Recognised formats (root must be configured, e.g. MES or MNQ):

* ``MESH25``, ``MESH5``, ``MESH2025``, ``MES H25``, ``/MESH25``  (CME month code)
* ``MES 03-25``                                                  (NinjaTrader style)
* ``MES MAR25``, ``MESMAR2025``                                   (month name)
* ``MES``, ``MES1!``, ``MES=F``                                   (no month: continuous/unknown)

Exchange suffixes such as ``.CME`` or ``:CME`` are ignored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, Mapping

from src.config import MONTH_CODES, ContractSpec
from src.data.errors import ContractError

MONTH_CODE_TO_NUM: dict[str, int] = {c: i + 1 for i, c in enumerate(MONTH_CODES)}
NUM_TO_MONTH_CODE: dict[int, str] = {v: k for k, v in MONTH_CODE_TO_NUM.items()}
MONTH_NAMES: dict[str, int] = {
    name: i + 1
    for i, name in enumerate(
        ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
    )
}
_EXCHANGE_SUFFIX = re.compile(r"[.:@\s](CME|GLOBEX|XCME|CBOT)$")


@dataclass(frozen=True)
class ContractInfo:
    """Identified contract.

    ``month``/``year`` are None when the symbol does not name a contract month
    (continuous series or unknown), in which case ``is_continuous`` is True.
    """

    raw: str
    root: str
    month: int | None
    year: int | None
    spec: ContractSpec | None = None

    @property
    def is_continuous(self) -> bool:
        return self.month is None

    @property
    def month_code(self) -> str | None:
        return NUM_TO_MONTH_CODE[self.month] if self.month else None

    @property
    def label(self) -> str:
        """Canonical label such as ``MESH25``, or just the root."""
        if self.month is None or self.year is None:
            return self.root
        return f"{self.root}{self.month_code}{self.year % 100:02d}"

    @property
    def expiry(self) -> date | None:
        """Nominal last trading day: third Friday of the contract month.

        This is the CME rule for quarterly equity index futures; holidays are not
        taken into account.
        """
        if self.month is None or self.year is None:
            return None
        first = date(self.year, self.month, 1)
        offset = (4 - first.weekday()) % 7  # 4 = Friday
        return first + timedelta(days=offset + 14)


def _body_pattern(roots: Iterable[str]) -> str:
    alt = "|".join(re.escape(r) for r in sorted({r.upper() for r in roots}, key=len, reverse=True))
    months = "|".join(MONTH_NAMES)
    return (
        rf"(?P<root>{alt})"
        rf"(?:[\s_-]*(?P<code>[{MONTH_CODES}])(?P<y1>\d{{4}}|\d{{1,2}})"
        rf"|\s+(?P<num>\d{{1,2}})-(?P<y2>\d{{4}}|\d{{2}})"
        rf"|[\s_-]*(?P<mon>{months})[\s_-]*(?P<y3>\d{{4}}|\d{{2}}))?"
        rf"(?P<cont>1!|!|=F)?"
    )


def _resolve_year(text: str, reference_year: int | None, raw: str) -> int:
    if len(text) == 4:
        return int(text)
    if len(text) == 2:
        return 2000 + int(text)
    # Single digit: choose the year with that last digit closest to the reference.
    if reference_year is None:
        raise ContractError(f"Symbol '{raw}' uses a 1-digit year; a reference year is required")
    digit = int(text)
    decade = reference_year - reference_year % 10
    candidates = [decade - 10 + digit, decade + digit, decade + 10 + digit]
    return min(candidates, key=lambda y: (abs(y - reference_year), -y))


def _build(match: re.Match[str], raw: str, specs: Mapping[str, ContractSpec], reference_year: int | None) -> ContractInfo:
    root = match["root"]
    month: int | None = None
    year: int | None = None
    if match["code"]:
        month = MONTH_CODE_TO_NUM[match["code"]]
        year = _resolve_year(match["y1"], reference_year, raw)
    elif match["num"]:
        month = int(match["num"])
        if not 1 <= month <= 12:
            raise ContractError(f"Invalid month {month} in symbol '{raw}'")
        year = _resolve_year(match["y2"], reference_year, raw)
    elif match["mon"]:
        month = MONTH_NAMES[match["mon"]]
        year = _resolve_year(match["y3"], reference_year, raw)
    return ContractInfo(raw=raw, root=root, month=month, year=year, spec=specs.get(root))


def parse_contract_symbol(
    symbol: str,
    specs: Mapping[str, ContractSpec],
    reference_year: int | None = None,
) -> ContractInfo:
    """Parse a complete symbol string.

    Args:
        symbol: e.g. ``"MESH25"`` or ``"MNQ 06-25"``.
        specs: Configured contract specifications (their keys are the known roots).
        reference_year: Year used to resolve single-digit years (e.g. data start year).

    Raises:
        ContractError: If the symbol is not recognised.
    """
    raw = str(symbol)
    text = raw.strip().upper().lstrip("/")
    text = _EXCHANGE_SUFFIX.sub("", text).strip()
    match = re.fullmatch(_body_pattern(specs), text)
    if match is None:
        raise ContractError(
            f"Unrecognised contract symbol '{raw}'. Known roots: {sorted(specs)}"
        )
    return _build(match, raw, specs, reference_year)


def find_contract_in_text(
    text: str,
    specs: Mapping[str, ContractSpec],
    reference_year: int | None = None,
) -> ContractInfo | None:
    """Search free text (e.g. a file name) for a contract symbol. Returns None if absent."""
    upper = text.upper()
    pattern = rf"(?<![A-Z0-9]){_body_pattern(specs)}(?![A-Z0-9])"
    match = re.search(pattern, upper)
    if match is None:
        return None
    return _build(match, text, specs, reference_year)


def identify_contract(
    *,
    specs: Mapping[str, ContractSpec],
    symbols: Iterable[str] | None = None,
    filename: str | Path | None = None,
    default_symbol: str | None = None,
    reference_year: int | None = None,
) -> tuple[ContractInfo, str, list[str]]:
    """Identify the contract of a dataset.

    Priority: symbol column > file name > configured default.

    Returns:
        ``(info, source, notes)`` where ``source`` is ``"symbol_column"``,
        ``"filename"`` or ``"default"`` and ``notes`` lists consistency warnings.

    Raises:
        ContractError: If no source identifies the contract, or the symbol column
            contains more than one contract.
    """
    notes: list[str] = []
    from_file: ContractInfo | None = None
    if filename is not None:
        from_file = find_contract_in_text(Path(filename).stem, specs, reference_year)

    info: ContractInfo | None = None
    source = ""
    if symbols is not None:
        unique = sorted({str(s) for s in symbols if s is not None and str(s).strip() and str(s) != "<NA>"})
        if len(unique) > 1:
            raise ContractError(
                f"File contains several contracts {unique}. Set data.symbol_filter to choose one; "
                "mixing contracts creates artificial price jumps at the roll."
            )
        if unique:
            info, source = parse_contract_symbol(unique[0], specs, reference_year), "symbol_column"

    if info is None and from_file is not None:
        info, source = from_file, "filename"
    if info is None and default_symbol:
        info, source = parse_contract_symbol(default_symbol, specs, reference_year), "default"
    if info is None:
        raise ContractError(
            "Could not identify the contract: no symbol column, no symbol in the file name "
            "and no data.default_symbol configured."
        )

    if from_file is not None and source != "filename" and from_file.root != info.root:
        notes.append(f"File name suggests root {from_file.root} but data says {info.root}")
    if info.spec is not None and info.month_code and info.month_code not in info.spec.listed_months:
        notes.append(
            f"{info.label}: month {info.month_code} is not a listed month for {info.root} "
            f"({info.spec.listed_months})"
        )
    return info, source, notes
