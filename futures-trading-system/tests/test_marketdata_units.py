"""Sprint 6 unit tests: timestamps, instruments, protocol, config, secret, DataHealth blockers."""

import json
import os
import stat
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.config import ConfigError
from src.marketdata.bridge import BridgeSecretError, create_secret, load_secret
from src.marketdata.config import load_marketdata_config
from src.marketdata.instruments import InstrumentError, InstrumentResolver, third_friday
from src.marketdata.protocol import (ProtocolError, encode_data_line, hello_mac, loads_strict, mac, parse_data_line,
                                     parse_hello, session_key)
from src.marketdata.timestamps import (TimestampConversionError, iana_zone, local_to_utc, parse_iso,
                                       provider_time_to_utc)
from src.paper.validation import DataHealth
from tests.paper_helpers import APP

NY = ZoneInfo("America/New_York")
KEY = b"k" * 32


# ----------------------------------------------------------------- timestamps
def test_local_time_converts_with_dst():
    assert local_to_utc(datetime(2026, 1, 15, 9, 30), NY) == datetime(2026, 1, 15, 14, 30, tzinfo=pd.Timestamp(0, tz="UTC").tzinfo)
    summer = local_to_utc(datetime(2026, 7, 15, 9, 30), NY)
    assert summer.hour == 13  # EDT = UTC-4


def test_ambiguous_and_nonexistent_local_times_are_refused():
    with pytest.raises(TimestampConversionError, match="ambiguous"):
        local_to_utc(datetime(2026, 11, 1, 1, 30), NY)       # fall back: 01:30 happens twice
    with pytest.raises(TimestampConversionError, match="nonexistent"):
        local_to_utc(datetime(2026, 3, 8, 2, 30), NY)        # spring forward: 02:30 never happens


def test_bridge_and_backend_conversions_must_agree():
    ok = provider_time_to_utc("2026-01-15T09:30:00.1234567", "2026-01-15T14:30:00.1234567Z", NY)
    assert ok == pd.Timestamp("2026-01-15 14:30:00.123456", tz="UTC")
    with pytest.raises(TimestampConversionError, match="disagree"):  # bridge used the wrong zone / offset
        provider_time_to_utc("2026-01-15T09:30:00", "2026-01-15T13:30:00Z", NY)


@pytest.mark.parametrize("text,utc", [("2026-01-15 09:30:00", False), ("2026-01-15T09:30:00+01:00", False),
                                      ("2026-01-15T09:30:00Z", False), ("2026-01-15T09:30:00", True),
                                      ("2026-01-15T09:30:00.12345678Z", True), ("garbage", True), ("", False)])
def test_malformed_timestamps_are_refused(text, utc):
    with pytest.raises(TimestampConversionError):
        parse_iso(text, utc_required=utc)


def test_unknown_windows_time_zone_is_refused():
    assert iana_zone("Eastern Standard Time").key == "America/New_York"
    with pytest.raises(TimestampConversionError):
        iana_zone("Mars Standard Time")


# ----------------------------------------------------------------- instruments
def test_only_the_configured_contract_resolves():
    r = InstrumentResolver({"MES": "MES 12-26", "MNQ": "MNQ 12-26"}, APP.contracts)
    assert r.resolve("MES 12-26").root == "MES" and r.resolve("MNQ 12-26").root == "MNQ"
    for bad in ("MES 03-27", "ES 12-26", "MES", "mes 12-26", "MNQ 12-26 ", None):
        with pytest.raises(InstrumentError):
            r.resolve(bad)


@pytest.mark.parametrize("contracts", [{"MES": "MES"}, {"MES": "MNQ 12-26"}, {"MES": "MES  12-26"}, {"MES": "XYZ"}])
def test_invalid_contract_configuration_is_refused(contracts):
    with pytest.raises(InstrumentError):
        InstrumentResolver(contracts, APP.contracts)


def test_expiry_and_roll_warnings():
    assert third_friday(2026, 12) == date(2026, 12, 18)
    r = InstrumentResolver({"MES": "MES 12-26"}, APP.contracts)
    assert r.roll_warnings(date(2026, 11, 1)) == []
    assert "plan the roll" in r.roll_warnings(date(2026, 12, 10))[0]
    assert "expired" in r.roll_warnings(date(2026, 12, 19))[0]


# ----------------------------------------------------------------- protocol
def line(obj, key=KEY):
    return encode_data_line(key, obj)[:-1]


HB = {"type": "heartbeat", "seq": 1, "t_bridge": "2026-01-15T14:30:00Z"}


def test_valid_line_parses():
    assert parse_data_line(line(HB), KEY, 1)["type"] == "heartbeat"


def test_tampered_mac_sequence_and_schema_are_refused():
    good = line(HB)
    with pytest.raises(ProtocolError, match="authentication"):
        parse_data_line(good.replace(b"heartbeat", b"heartbeaT"), KEY, 1)
    with pytest.raises(ProtocolError, match="authentication"):
        parse_data_line(good, b"x" * 32, 1)                           # another session's key
    with pytest.raises(ProtocolError, match="sequence"):
        parse_data_line(good, KEY, 2)                                 # replayed line
    with pytest.raises(ProtocolError, match="unknown message type"):
        parse_data_line(line({**HB, "type": "order"}), KEY, 1)       # no such thing as a command
    with pytest.raises(ProtocolError, match="wrong fields"):
        parse_data_line(line({**HB, "extra": 1}), KEY, 1)
    with pytest.raises(ProtocolError):
        parse_data_line(b"not a line", KEY, 1)


@pytest.mark.parametrize("body", [b'{"a":NaN}', b'{"a":Infinity}', b'{"a":{"b":1}}', b'{"a":[1]}', b'[1,2]',
                                  b'{"a":"' + b"x" * 300 + b'"}', b'{"a":12345678901234567890}', b"{bad"])
def test_strict_json(body):
    with pytest.raises(ProtocolError):
        loads_strict(body.decode())


def test_md_message_rules():
    md = {"type": "md", "seq": 1, "eseq": 1, "instrument": "MES 12-26", "kind": "trade", "price": 6000.0,
          "size": 1, "t_local": "2026-01-15T09:30:00", "t_utc": "2026-01-15T14:30:00Z", "t_bridge": "2026-01-15T14:30:00Z"}
    assert parse_data_line(line(md), KEY, 1)["price"] == 6000.0
    for bad in ({"kind": "trade", "price": None}, {"kind": "order"}, {"price": "6000"}, {"price": True},
                {"eseq": 0}, {"instrument": "MES\n12-26"}):
        with pytest.raises(ProtocolError):
            parse_data_line(line({**md, **bad}), KEY, 1)
    q = {**md, "kind": "quote", "bid": 5999.75, "ask": 6000.0}
    del q["price"], q["size"]
    assert parse_data_line(line(q), KEY, 1)["kind"] == "quote"


def test_hello_parsing():
    h = {"type": "hello", "protocol": 1, "client": "x", "client_version": "1", "provider": "p",
         "tz": "Eastern Standard Time", "client_nonce": "a" * 64, "mac": "b" * 64}
    assert parse_hello(json.dumps(h).encode()).tz == "Eastern Standard Time"
    for bad in ({"protocol": 2}, {"client_nonce": "A" * 64}, {"mac": "short"}, {"type": "md"}, {"x": 1}):
        with pytest.raises(ProtocolError):
            parse_hello(json.dumps({**h, **bad}).encode())


def test_session_keys_are_per_connection():
    assert session_key(KEY, "a" * 64, "b" * 64) != session_key(KEY, "a" * 64, "c" * 64)
    assert hello_mac(KEY, "a" * 64, "b" * 64, "UTC") != hello_mac(KEY, "a" * 64, "b" * 64, "Eastern Standard Time")
    assert len(mac(KEY, b"x")) == 64


# ----------------------------------------------------------------- config / secret
def _cfg(tmp_path, **bridge):
    b = {"host": "127.0.0.1", "port": 50555, "secret_file": "s", "max_message_bytes": 4096, "queue_max_events": 100,
         "connect_timeout_seconds": 5, "heartbeat_timeout_seconds": 10, "handshake_timeout_seconds": 3,
         "max_pending_handshakes": 4} | bridge
    body = "[bridge]\n" + "\n".join(f"{k} = {json.dumps(v)}" for k, v in b.items())
    body += '\n[instruments]\ncontracts = { MES = "MES 12-26" }\n'
    p = tmp_path / "md.toml"
    p.write_text(body)
    return p


def test_repository_config_is_safe_by_default():
    cfg = load_marketdata_config()
    assert cfg.host == "127.0.0.1" and set(cfg.contracts) == {"MES", "MNQ"}


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "localhost", "::", "example.com", "10.0.0.1"])
def test_non_loopback_listener_is_refused(tmp_path, host):
    with pytest.raises(ConfigError, match="loopback"):
        load_marketdata_config(_cfg(tmp_path, host=host))


@pytest.mark.parametrize("override", [{"port": 80}, {"port": 70000}, {"max_message_bytes": 0},
                                      {"queue_max_events": -1}, {"unknown": 1}])
def test_invalid_bridge_config_is_refused(tmp_path, override):
    with pytest.raises(ConfigError):
        load_marketdata_config(_cfg(tmp_path, **override))


def test_secret_file_rules(tmp_path):
    p = tmp_path / "bridge" / "secret"
    create_secret(p)
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600 and stat.S_IMODE(os.stat(p.parent).st_mode) == 0o700
    assert len(load_secret(p)) == 32
    with pytest.raises(BridgeSecretError, match="already exists"):
        create_secret(p)
    os.chmod(p, 0o644)
    with pytest.raises(BridgeSecretError, match="readable by other users"):
        load_secret(p)
    os.chmod(p, 0o600)
    p.write_text("nothex")
    with pytest.raises(BridgeSecretError, match="hex"):
        load_secret(p)
    with pytest.raises(BridgeSecretError, match="not found"):
        load_secret(tmp_path / "missing")


# ----------------------------------------------------------------- DataHealth blockers
def test_blockers_hold_until_cleared_and_fresh_data_follows():
    h = DataHealth(["MES"], pd.Timedelta(seconds=60), resume_after_events=2)
    t = pd.Timestamp("2026-01-15 14:30", tz="UTC")
    h.on_connect(t)
    for i in range(3):
        h.on_event("MES", t + pd.Timedelta(seconds=i))
    assert h.healthy("MES", t + pd.Timedelta(seconds=3))[0]
    h.block("MES", "not_subscribed", t)
    for i in range(10):
        h.on_event("MES", t + pd.Timedelta(seconds=4 + i))
    assert h.healthy("MES", t + pd.Timedelta(seconds=14)) == (False, "data_blocked:not_subscribed")
    h.unblock("MES", "not_subscribed", t + pd.Timedelta(seconds=15))
    assert not h.healthy("MES", t + pd.Timedelta(seconds=15))[0]  # fresh data still required
    h.on_event("MES", t + pd.Timedelta(seconds=16))
    h.on_event("MES", t + pd.Timedelta(seconds=17))
    assert h.healthy("MES", t + pd.Timedelta(seconds=17))[0]
    h.unblock("MES", "never_set", t)  # clearing an absent blocker changes nothing
    assert h.healthy("MES", t + pd.Timedelta(seconds=17))[0]
