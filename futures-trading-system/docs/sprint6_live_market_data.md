# Sprint 6 — Live market data and NinjaTrader 8 integration (read-only)

This sprint adds the infrastructure to receive **real-time MES/MNQ market data from NinjaTrader 8**.
It is **read-only**: nothing added here can send, change or cancel an order. NinjaTrader's order
APIs are never referenced. The paper broker still cannot transmit anything.

> **Status.** The Python side is implemented and tested against a mock bridge that speaks the exact
> wire protocol. The NinjaTrader add-on (`ninjatrader/FtsMarketDataBridge.cs`) follows the official
> NinjaScript documentation, but it has **not been compiled or run against a real NinjaTrader
> installation or data feed** in this repository. Nothing here has been tested on live market data.
> Before relying on it, do the first-run checks in [First run on a real NinjaTrader](#first-run-on-a-real-ninjatrader).

## 1. Architecture

```
NinjaTrader 8 (Windows)                                Python backend (same PC)
┌───────────────────────────────────────┐             ┌─────────────────────────────────────────┐
│ FtsMarketDataBridge Add On (C#)       │  TCP to     │ BridgeServer (listens on 127.0.0.1 only)│
│  Instrument.MarketData.Update (L1)    │ 127.0.0.1   │  auth (HMAC challenge), per-line MAC,   │
│  Connection.ConnectionStatusUpdate    │ ──────────► │  sequence, strict schema, size limits,  │
│  bounded queue -> sender thread       │  data only  │  instrument + timestamp verification    │
│  NO orders, NO accounts, reads nothing│  (one way)  │  -> bounded ordered queue               │
└───────────────────────────────────────┘             │ NinjaTraderBridgeFeed (MarketDataFeed)  │
                                                      └──────────────────┬──────────────────────┘
                                                                         ▼  (existing Sprint 5 runtime)
            EventValidator ─► DataHealth ─► journal ─► BarBuilder ─► indicators ─► regime ─► strategy
                                                         ─► EntryAuthorizer (NewsGuard, AccountGuard,
                                                            RiskManager) ─► PaperBroker (simulated)
```

**Why this architecture.** I compared these options:

| Option | Assessment |
| --- | --- |
| **A + B: NinjaTrader Add On as a read-only bridge, plus loopback IPC to Python (chosen)** | Uses the documented Add On market-data subscription (`MarketData.Update`) and connection events. Python owns validation, health, persistence and strategy, so nothing is duplicated in C#. The add-on stays small and auditable (~300 lines, no orders). Loopback TCP is available in .NET Framework 4.8, which NinjaTrader 8 uses. |
| Strategy/indicator `OnMarketData()` override inside a chart | Tied to a chart's lifetime and data series, and Tick Replay replays only `Last` events. An Add On runs in the background, independent of UI. |
| NinjaTrader ATI / DLL interfaces | Designed for order automation. That is exactly what this sprint must not touch. |
| Named pipes | Equally local, but not portable to the Linux-based tests and CI. TCP on 127.0.0.1 plus authentication gives the same isolation. |
| Python as the client, NinjaTrader listening | Would open a listener inside NinjaTrader. The chosen direction keeps NinjaTrader listener-free, and Python controls the bind address. |

**Provider independence.** Every provider implements the existing `MarketDataFeed` interface
(`connect / poll / disconnect / health_updates / status`) and emits the existing `MarketEvent`.
A future provider (Rithmic, CQG, another bridge) needs only a new feed class. The validator, data
health, journal, bars, strategy and protections stay unchanged.

The required roles map onto the smallest practical set of classes:

| Role | Implementation |
| --- | --- |
| MarketDataProvider | `NinjaTraderBridgeFeed` (a `MarketDataFeed`) |
| MarketDataConnection | `BridgeServer` (one authenticated connection at a time) |
| MarketDataEvent | `MarketEvent` (extended with provenance fields) |
| InstrumentResolver | `src/marketdata/instruments.py` |
| ConnectionHealth | `BridgeStats` plus `DataHealth` blockers |
| SubscriptionManager | Subscription state per instrument in `BridgeServer`, mapped to DataHealth blocks |
| ReconnectionManager | The runtime's existing reconnect and back-off (`[data] reconnect_*`) plus the add-on's own back-off |

## 2. Official NinjaTrader documentation used

The NinjaTrader documentation sites are not reachable from this build environment. The facts
below come from official-documentation search results. Items marked *forum* come from NinjaTrader
support-forum answers, not the reference, and are **unverified**.

| Topic | Fact used | Source |
| --- | --- | --- |
| Level-1 subscription in an Add On | `instrument.MarketData.Update += OnMarketData;` with `-=` on cleanup; snapshot data is delivered on subscription; only unsubscribe when subscribed | [MarketData](https://docs.ninjatrader.com/ninjascript/marketdata) ([mirror](https://ninjatrader-live.ninjatrader.com/support/helpguides/nt8/marketdata.htm)) |
| Event payload | `MarketDataEventArgs`: Ask, Bid, Instrument, IsReset, MarketDataType, Price, Time, Volume | [MarketDataEventArgs](https://docs.ninjatrader.com/ninjascript/marketdataeventargs) |
| Event types | `MarketDataType.Last / Bid / Ask` (+ DailyHigh, DailyLow, DailyVolume, LastClose, Opening, OpenInterest, Settlement); not all providers support all types | [OnMarketData()](https://ninjatrader.com/support/helpguides/nt8/onmarketdata.htm) |
| Connection status | static `Connection.ConnectionStatusUpdate` event; `ConnectionStatusEventArgs.Status` = order feed, `.PriceStatus` = price feed; values Connected, Connecting, ConnectionLost, Disconnecting, Disconnected | [ConnectionStatusUpdate](https://docs.ninjatrader.com/ninjascript/connectionstatusupdate), [Connection](https://docs.ninjatrader.com/ninjascript/connection_class) |
| Add On lifecycle | Add Ons run in the background, pass through SetDefaults/Terminated on every compile and start/stop; release resources in `State.Terminated` (which can be called on clones: guard with a flag) | [NinjaScript lifecycle](https://developer.ninjatrader.com/docs/desktop/understanding_the_lifecycle_of) |
| Instrument names | `Instrument.GetInstrument("MES 12-26")`: the full name including the expiry for futures | [GetInstrument](https://ninjatrader.com/es/support/helpGuides/nt8/getinstrument.htm) |
| *forum*: event time | `MarketDataEventArgs.Time` is local wall time (`DateTimeKind.Unspecified`) in the time zone of Tools > Options > General; `Core.Globals.GeneralOptions.TimeZoneInfo` holds it | [forum](https://forum.ninjatrader.com/forum/ninjatrader-8/indicator-development/95987-ho-to-get-timezoneinfo-for-control-center-s-tools-options-general-time-zone) |
| *forum*: .NET | NinjaTrader 8 targets .NET Framework 4.8 (since 8.0.23.0); sockets from NinjaScript work but are "unsupported C#" | [forum](https://forum.ninjatrader.com/forum/ninjatrader-8/strategy-development/1148878-how-can-i-make-socket-calls-from-within-ninjascript) |
| Data fees | Real-time CME data needs a connected data provider and exchange fees (NinjaTrader lists non-professional Level I CME data as a paid subscription) | [Account and exchange fees](https://ninjatrader.com/pricing/account-fees/), [CME fee list](https://www.cmegroup.com/market-data/files/january-2026-market-data-fee-list.pdf) |

**To check when first compiling the add-on** (each is marked `VERIFY` in the code):
- the state in which an Add On becomes active (`State.Active`);
- `Core.Globals.GeneralOptions.TimeZoneInfo`;
- `Instrument.FullName`;
- that `Volume` on Bid/Ask events is the size at that price;
- that `Print` may be called from the sender thread.

## 3. Requirements

**On the Windows PC:**
- NinjaTrader 8 (current release) with a connection that delivers **real-time CME data** for
  MES/MNQ, for example a funded data subscription from your broker or data vendor. Market-data
  licensing and exchange fees are your responsibility. A simulated data feed is not market data.
- Python 3.12+ with this project installed (`pip install -r requirements.txt`).

**Not required for tests:** NinjaTrader, credentials, network access. All automated tests use the
mock bridge on 127.0.0.1.

## 4. Step-by-step setup (beginner)

1. **Create the shared secret.** In the project folder, run:
   ```
   python run_marketdata.py init-secret
   ```
   This writes `data/bridge/bridge_secret` (64 hex characters, owner-only, ignored by Git). It is
   never printed.
2. **Check the contracts** in `config/marketdata.toml` under `[instruments] contracts`. Use the
   exact NinjaTrader names, for example `MES 12-26` and `MNQ 12-26`. Change them at every quarterly
   roll: MES/MNQ expire on the third Friday of Mar/Jun/Sep/Dec, and volume moves about a week
   earlier. `python run_marketdata.py check` warns 10 days before expiry and refuses expired
   contracts.
3. **Prepare the add-on folder.** Create `Documents\FtsBridge\` and copy into it:
   - `data/bridge/bridge_secret`, keeping the name `bridge_secret`. Only your Windows user should
     be able to read this folder.
   - `ninjatrader/bridge.cfg.example`, renamed to `bridge.cfg`. Set `port` to match
     `config/marketdata.toml` and list the same contracts.
4. **Install the add-on.** Copy `ninjatrader/FtsMarketDataBridge.cs` into
   `Documents\NinjaTrader 8\bin\Custom\AddOns\`. Open *New > NinjaScript Editor* and compile (F5).
   Fix anything flagged `VERIFY` if the compiler reports it.
5. **Start the backend first:**
   ```
   python run_marketdata.py monitor
   ```
   It listens on 127.0.0.1 and waits up to `connect_timeout_seconds` per attempt, with the
   back-off from `config/paper.toml`.
6. **Start NinjaTrader** and connect your data connection. The add-on connects to the backend
   within a few seconds and reconnects automatically. Watch the monitor's status file at
   `output/marketdata/status.json`.

**Try everything without NinjaTrader:**
```
python run_marketdata.py init-secret          # once
python run_marketdata.py monitor --mock --duration 30
```
The mock prints `*** MOCK BRIDGE: SYNTHETIC prices, not market data ***`.

**Paper/shadow trading on live data** (Sprint 8 preparation; entries still need every protection, and
the operational NewsGuard blocks them while the news calendar is incomplete):
```
python run_paper.py --new-account --mode paper --feed bridge --bars MES=history.csv MNQ=history.csv
```
Here `--bars` is warm-up history only.

## 5. Timestamps

All internal timestamps are UTC, ISO-8601 in files and the journal.

| Field | Meaning | Source |
| --- | --- | --- |
| `exchange_ts` | exchange time | **not available** from NinjaTrader `MarketDataEventArgs`; always `None`, **never substituted** |
| `provider_ts` | NinjaTrader's event time | `MarketDataEventArgs.Time` (local wall time), converted |
| `ts` / `ts_source` | canonical event time and its origin | `provider_ts`, `ts_source = "provider"` |
| `bridge_ts` | when the add-on handled the event | add-on clock, UTC |
| `received_at` | when the backend read the message | backend clock, UTC |
| processed | when the runtime processed it | runtime clock (latency statistics) |

**Conversion.** The add-on sends the local time, the Windows time-zone id and its own UTC
conversion. The backend converts the local time again with the IANA database and refuses the
event if any of the following holds:
- the two conversions differ by more than 1 ms;
- the local time is **ambiguous** (DST fall-back hour);
- the local time does **not exist** (spring-forward gap);
- the time-zone id is unknown. In that case the whole connection is refused at the handshake.

Exchange sessions are evaluated in America/New_York rules (existing `SessionConfig`), DST-aware.

## 6. Data integrity and data-health states

Every live event still passes the Sprint 5 `EventValidator`. That covers duplicates, out of order,
future timestamps against the clock, non-finite values, impossible prices and sizes, off-tick
prices, crossed quotes, spreads beyond `max_spread_ticks`, jumps and level resets. Sprint 6 adds:

| Condition | Detection | Effect |
| --- | --- | --- |
| Not subscribed / subscription failed / partial subscription | `subscription` messages per instrument; everything starts blocked at each connection | entries blocked for that instrument (`data_blocked:not_subscribed`) |
| Provider connection lost | `status` per NinjaTrader connection (`PriceStatus`) | all instruments blocked until that connection is `Connected` again (or switched off) |
| Events dropped in the add-on | gap in the add-on's event counter `eseq` | recovery pending (fresh data required) |
| Book reset | `MarketDataEventArgs.IsReset` | recovery pending for that instrument |
| Provider error | `error` message | recovery pending |
| Delayed data | `received_at − ts > max_event_delay_seconds` (3 s) | recovery pending; the data is still used for marks and protective exits |
| Bridge dead / silent | no message for `heartbeat_timeout_seconds` (the add-on sends a heartbeat every second) | connection dropped -> `FeedError` -> runtime reconnect |
| Slow consumer | bounded queue full | connection dropped (never silent loss); the bridge reconnects; fresh data required |
| Instrument mismatch | name is not exactly the configured contract | event refused and counted; never mapped |
| Timestamp problems | see section 5 | event refused, instrument flagged |
| Stale data | no event for `stale_after_seconds` | entries blocked (`market_data_stale`) |

**Rules:**
- Every block or flag is journaled (`feed_health`, `data_delayed`, `data_rejected`).
- Clearing a block always requires `resume_after_events` fresh, validated events before entries
  are authorized again.
- Protective exits are never blocked.
- Stale quotes are never used for fills (`quote_max_age_seconds`).
- Nothing is fabricated: no tick is interpolated, and missing size is never read as zero.

## 7. Reconnection

- **Add-on:** reconnects to 127.0.0.1 with a 1/2/5/10 s back-off. Messages queued while it was
  disconnected are discarded as stale. After each handshake it re-sends the subscription states
  and the last provider status.
- **Backend:** a lost connection surfaces as `FeedError` after every event received before the
  loss has been delivered. The runtime marks the feed disconnected, waits for a new authenticated
  connection (`reconnect_max_attempts`, `reconnect_backoff_seconds`, `connect_timeout_seconds`),
  then requires fresh data. If no bridge returns, it performs a safe shutdown and protective
  orders stay working.
- A newer authenticated connection replaces a half-open one.

## 8. Security boundaries

| Boundary | Control |
| --- | --- |
| Network exposure | The listener binds only to a literal loopback address. The config loader refuses anything else, and the server re-checks after binding. On Windows it uses `SO_EXCLUSIVEADDRUSE`. The add-on connects to `IPAddress.Loopback`, which is hard-coded. |
| Authentication | Mutual HMAC-SHA256 challenge-response with a 32-byte shared secret. The secret never goes on the wire; each connection gets fresh nonces and its own session key. |
| Integrity and replay | Every line is `MAC(session_key, json)`, with sequence 1, 2, 3, ... per connection. A wrong MAC, a replay, a reorder or an injected line drops the connection. |
| Input handling | Lines are limited to `max_message_bytes`. JSON is parsed strictly: no NaN/Infinity, no nesting, exact key sets, bounded strings, printable text only. Everything is untrusted until validated. |
| Resource limits | Bounded event queue, at most `max_pending_handshakes` unauthenticated connections, handshake timeout, bounded histories. |
| No control channel | After the handshake the backend never sends anything, and the add-on never reads. Message types are data only; an unknown type such as `order` is a protocol violation. |
| No execution | The add-on references no Account or Order API. `tests/test_marketdata_security.py` scans for order, account and listener APIs. The feed has no order methods. `PaperBroker.TRANSMITS_REAL_ORDERS = False`. |
| Secrets | The secret file is owner-only: the backend refuses a group- or world-readable file. It is never printed, logged or journaled (tested), and it is never in Git (`data/*` is ignored). |

**Out of scope.** A process running as your own Windows user can read the secret, so it is
within the trust boundary. Such a process could also occupy the handshake slots; this is a local
denial of service, documented.

## 9. Status interface for the Sprint 7 GUI

`PaperSession.status()` returns a JSON-compatible snapshot (schema in `src/marketdata/status.py`).
The monitor writes it atomically to `output/marketdata/status.json` (owner-only) every
`--status-every` seconds. The snapshot contains:
- feed and provider connection, subscriptions per instrument;
- last event time and freshness;
- bid, ask, spread and last price;
- processing latency and data delay (p50/p99/max);
- rejected events by reason, reconnection count;
- data-health state and whether entries are allowed by data;
- the current market session.

No HTTP or WebSocket server was added in this sprint. For Sprint 7:
- serve this snapshot from a local-only API (127.0.0.1 by default);
- keep LAN access off unless explicitly configured with authentication and TLS;
- never expose it to the internet;
- never add order controls to the API.

The same backend can serve both the Windows desktop and the browser front-ends.

## 10. Shadow trading (Sprint 8 preparation)

`run_paper.py --mode paper --feed bridge` already runs the full chain on live data:
- the existing strategy and regime gate;
- signals and rejection reasons;
- **simulated** fills only;
- AccountGuard, RiskManager and the operational NewsGuard.

Executions are journaled as simulated. Market events keep their provider provenance.
Real-time shadow results are not equivalent to historical replay: fills, latency, queue position
and data gaps differ. Strategy parameters were not changed.

## 11. Testing

```
python -m pytest tests/test_marketdata_units.py tests/test_marketdata_integration.py \
    tests/test_marketdata_security.py tests/test_marketdata_stress.py tests/test_marketdata_audit.py \
    tests/test_marketdata_cli.py -q
python scripts/benchmark_marketdata.py         # throughput / latency with a separate-process mock bridge
```

## 12. Troubleshooting

| Symptom | Check |
| --- | --- |
| `no authenticated NinjaTrader bridge connected` | Is NinjaTrader running with the add-on compiled? Does the port match `bridge.cfg`? Is the secret identical in both places? |
| `refused_handshakes` increases | Wrong secret, or the NinjaTrader time zone has no IANA mapping: set a common zone in Tools > Options > General. |
| Instrument stays `data_blocked:not_subscribed` | Contract names differ between `bridge.cfg`, `config/marketdata.toml` and NinjaTrader, or `GetInstrument` failed (see `subscriptions` in the status). |
| `data_delayed` | PC clock drift, an overloaded PC, or a provider delay. Synchronise the Windows clock. |
| `instrument_mismatch` counts | NinjaTrader sends another contract (roll): update both configs. |
| `protocol_violation` in `lost_connections` | Version mismatch between add-on and backend, or a corrupted add-on. |
| Repeated `backpressure_overflow` | The backend PC is too slow for the data rate. Close other programs, or raise `queue_max_events` with care. |

## 13. First run on a real NinjaTrader

1. Compile the add-on; resolve any `VERIFY` item the compiler rejects.
2. Run `monitor`. Compare the status `last` and `bid`/`ask` with NinjaTrader's Market Analyzer.
3. Compare `last_event_time` with Windows' clock during an active market. The data delay should be
   well under 1 s. A delay of whole hours means a time-zone problem: stop and investigate.
4. Disconnect and reconnect the NinjaTrader data connection. The status should show
   `provider_not_connected` and then recovery.
5. Restart NinjaTrader and then the backend, each while the other keeps running.

## 14. Known limitations

- The add-on is uncompiled and untested against NinjaTrader. No live-market verification has been
  done.
- No exchange timestamps (not exposed by `MarketDataEventArgs`). Latency is measured from
  NinjaTrader's event time.
- No exchange sequence numbers. Only the add-on's own counter detects drops inside the bridge;
  drops inside NinjaTrader or the data provider cannot be detected.
- Level 1 only: no depth, no order-book model. Quote sizes come from Bid/Ask `Volume` (`VERIFY`).
- Each time zone needs a Windows-to-IANA mapping (common zones included).
- A local process running as the same user is trusted (it can read the secret).
- On Windows the backend cannot verify the secret file's permissions (the check is POSIX-only).
  Keep `data\bridge\` and `Documents\FtsBridge\` readable only by your user.
- Contract rolls are manual.
- The NewsGuard manual calendar is unresolved, so the operational NewsGuard blocks every paper entry.
- Shadow/paper results on live data are simulations, not evidence of profitability or of
  live-trading readiness.
