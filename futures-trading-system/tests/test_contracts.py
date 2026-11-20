from datetime import date

import pytest

from src.data.contracts import find_contract_in_text, identify_contract, parse_contract_symbol
from src.data.errors import ContractError


@pytest.mark.parametrize(
    "symbol,root,month,year",
    [
        ("MESH25", "MES", 3, 2025),
        ("MESZ5", "MES", 12, 2025),
        ("MNQM2026", "MNQ", 6, 2026),
        ("MES H25", "MES", 3, 2025),
        ("/MNQU25", "MNQ", 9, 2025),
        ("MES 03-25", "MES", 3, 2025),
        ("MNQ 12-2025", "MNQ", 12, 2025),
        ("MES MAR25", "MES", 3, 2025),
        ("mnqsep2025", "MNQ", 9, 2025),
        ("MESH25.CME", "MES", 3, 2025),
        ("MES", "MES", None, None),
        ("MNQ1!", "MNQ", None, None),
        ("MES=F", "MES", None, None),
    ],
)
def test_parse_formats(config, symbol, root, month, year):
    info = parse_contract_symbol(symbol, config.contracts, reference_year=2025)
    assert (info.root, info.month, info.year) == (root, month, year)
    assert info.is_continuous == (month is None)
    assert info.spec is config.contracts[root]


@pytest.mark.parametrize("bad", ["ES", "MESX", "MES 13-25", "XYZH25", "MESH25X", ""])
def test_unrecognised(config, bad):
    with pytest.raises(ContractError):
        parse_contract_symbol(bad, config.contracts, reference_year=2025)


def test_single_digit_year_resolution(config):
    assert parse_contract_symbol("MESH9", config.contracts, reference_year=2025).year == 2029
    assert parse_contract_symbol("MESH4", config.contracts, reference_year=2025).year == 2024
    assert parse_contract_symbol("MESH1", config.contracts, reference_year=2029).year == 2031
    with pytest.raises(ContractError):
        parse_contract_symbol("MESH5", config.contracts)


def test_label_and_expiry(config):
    info = parse_contract_symbol("MES 06-25", config.contracts)
    assert info.label == "MESM25"
    assert info.expiry == date(2025, 6, 20)  # third Friday of June 2025
    assert parse_contract_symbol("MESH25", config.contracts).expiry == date(2025, 3, 21)


@pytest.mark.parametrize(
    "name,label",
    [
        ("MESH25_1m.csv", "MESH25"),
        ("MES 03-25.Last.txt", "MESH25"),
        ("export-MNQZ5-2025.csv", "MNQZ25"),
        ("SYNTHETIC_MES_1m.csv", "MES"),
    ],
)
def test_find_in_filename(config, name, label):
    info = find_contract_in_text(name.rsplit(".", 1)[0] if name.endswith(".csv") else name, config.contracts, 2025)
    assert info is not None and info.label == label


def test_find_none(config):
    assert find_contract_in_text("prices_2025", config.contracts) is None
    assert find_contract_in_text("NQES", config.contracts) is None  # ES inside other text not matched


def test_identify_priority_and_notes(config):
    info, src, notes = identify_contract(specs=config.contracts, symbols=["MNQH25"], filename="MESH25.csv", reference_year=2025)
    assert info.root == "MNQ" and src == "symbol_column"
    assert any("File name suggests" in n for n in notes)

    info, src, _ = identify_contract(specs=config.contracts, symbols=None, filename="MESM25.csv", reference_year=2025)
    assert src == "filename" and info.label == "MESM25"

    info, src, _ = identify_contract(specs=config.contracts, filename="data.csv", default_symbol="MNQU25")
    assert src == "default" and info.label == "MNQU25"


def test_identify_unlisted_month_note(config):
    _, _, notes = identify_contract(specs=config.contracts, symbols=["MESF25"])
    assert any("not a listed month" in n for n in notes)


def test_identify_errors(config):
    with pytest.raises(ContractError, match="several contracts"):
        identify_contract(specs=config.contracts, symbols=["MESH25", "MESM25"])
    with pytest.raises(ContractError, match="Could not identify"):
        identify_contract(specs=config.contracts, filename="prices.csv")
