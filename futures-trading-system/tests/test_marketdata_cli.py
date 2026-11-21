"""Sprint 6 CLI: init-secret / check / monitor --mock run end to end without NinjaTrader."""

import json
import os
import socket
import stat


import run_marketdata
import run_paper


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def write_cfg(tmp_path, port=None, contracts='{ MES = "MES 12-30", MNQ = "MNQ 12-30" }'):
    p = tmp_path / "md.toml"
    p.write_text(f"""[bridge]
host = "127.0.0.1"
port = {port or free_port()}
secret_file = "{tmp_path / 'bridge' / 'secret'}"
max_message_bytes = 4096
queue_max_events = 20000
connect_timeout_seconds = 5
heartbeat_timeout_seconds = 10
handshake_timeout_seconds = 3
max_pending_handshakes = 4
[instruments]
contracts = {contracts}
""")
    return p


def test_init_secret_and_check(tmp_path, capsys):
    cfg = write_cfg(tmp_path)
    assert run_marketdata.main(["--config", str(cfg), "check"]) == 1           # no secret yet
    assert run_marketdata.main(["--config", str(cfg), "init-secret"]) == 0
    secret = (tmp_path / "bridge" / "secret").read_text().strip()
    assert stat.S_IMODE(os.stat(tmp_path / "bridge" / "secret").st_mode) == 0o600
    assert run_marketdata.main(["--config", str(cfg), "init-secret"]) == 1     # never overwritten silently
    assert run_marketdata.main(["--config", str(cfg), "check"]) == 0
    out = capsys.readouterr()
    assert secret not in out.out + out.err and "loopback only" in out.out


def test_expired_contract_is_refused(tmp_path):
    cfg = write_cfg(tmp_path, contracts='{ MES = "MES 03-20" }')
    assert run_marketdata.main(["--config", str(cfg), "check"]) == 1


def test_monitor_with_mock_bridge_runs_read_only_and_writes_status(tmp_path, capsys):
    cfg = write_cfg(tmp_path)
    assert run_marketdata.main(["--config", str(cfg), "init-secret"]) == 0
    status = tmp_path / "out" / "status.json"
    rc = run_marketdata.main(["--config", str(cfg), "monitor", "--mock", "--duration", "3", "--status-file",
                              str(status), "--status-every", "0.2", "--state-dir", str(tmp_path / "state")])
    out = capsys.readouterr().out
    assert rc == 0 and "READ-ONLY" in out and "SYNTHETIC" in out
    st = json.loads(status.read_text())
    assert st["run_kind"] == "infrastructure_validation" and st["feed"]["provider"] == "NinjaTrader 8"
    assert st["validation"]["events_processed"] > 10
    assert st["instruments"]["MES"]["subscription"] == "subscribed" and st["instruments"]["MES"]["last"] is not None
    assert stat.S_IMODE(os.stat(status).st_mode) == 0o600


def test_paper_cli_ninjatrader_feed_needs_paper_mode(tmp_path, capsys):
    cfg = write_cfg(tmp_path)
    rc = run_paper.main(["--feed", "bridge", "--mode", "replay", "--marketdata-config", str(cfg),
                         "--state-dir", str(tmp_path / "s"), "--new-account"])
    assert rc == 1 and "--mode paper" in capsys.readouterr().err
