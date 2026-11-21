// FtsMarketDataBridge -- NinjaTrader 8 Add On: READ-ONLY level-1 market-data bridge (Sprint 6).
//
// WHAT IT DOES: subscribes to level-1 market data (Last, Bid, Ask) of the configured MES/MNQ
// contracts and to connection-status events, and streams them to the Python backend over ONE
// authenticated TCP connection to 127.0.0.1 (loopback, hard-coded). Protocol: see
// src/marketdata/protocol.py.
//
// WHAT IT NEVER DOES: it references no Account and no order object, submits/changes/cancels no
// order, and does not read anything from the backend after the handshake (there is no command
// channel). tests/test_marketdata_security.py scans this file for order-related APIs.
//
// STATUS: written against the official NinjaTrader 8 NinjaScript documentation (MarketData.Update,
// MarketDataEventArgs, MarketDataType, Instrument.GetInstrument, Connection.ConnectionStatusUpdate,
// ConnectionStatusEventArgs.PriceStatus) but NOT compiled or run in this repository (no NinjaTrader
// installation). Members marked "VERIFY" come from forum guidance and must be checked when the file is
// first compiled in the NinjaScript Editor. See docs/sprint6_live_market_data.md.
//
// SETUP: put bridge.cfg and bridge_secret in Documents\FtsBridge\ (see the docs), copy this file to
// Documents\NinjaTrader 8\bin\Custom\AddOns\, compile (F5 in the NinjaScript Editor).

#region Using declarations
using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript;
#endregion

namespace NinjaTrader.NinjaScript.AddOns
{
    public class FtsMarketDataBridge : AddOnBase
    {
        private const int Protocol = 1;
        private const int QueueCapacity = 20000;          // bounded: overflow drops events (eseq gap -> backend flags it)
        private const int MaxLineBytes = 4096;

        private readonly object bookLock = new object();
        private BlockingCollection<string> outbox;        // JSON bodies awaiting a sequence number + MAC
        private Thread sender;
        private volatile bool running;
        private bool started;                             // guards Terminated on cloned instances
        private long eseq;                                // event counter (gaps = dropped events)
        private readonly List<Instrument> subscribed = new List<Instrument>();
        private readonly List<string> failed = new List<string>();
        private volatile string lastStatus;                // last provider status line (re-sent after every handshake)
        private readonly Dictionary<string, double[]> book = new Dictionary<string, double[]>(); // bid, bidSize, ask, askSize
        private byte[] secret;
        private int port = 50555;
        private string[] contracts = new string[0];
        private TimeZoneInfo timeZone;

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Name = "FtsMarketDataBridge";
                Description = "Read-only level-1 market-data bridge to the local FTS backend (no orders).";
            }
            else if (State == State.Active)                 // VERIFY: Add On active state name
            {
                Start();
            }
            else if (State == State.Terminated)
            {
                Stop();
            }
        }

        // ------------------------------------------------------------------ lifecycle
        private void Start()
        {
            if (started) return;
            try
            {
                LoadConfig();
                timeZone = Core.Globals.GeneralOptions.TimeZoneInfo;   // VERIFY: NinjaTrader's configured time zone
            }
            catch (Exception ex)
            {
                Print("FtsMarketDataBridge disabled: " + ex.Message);   // never prints the secret
                return;
            }
            started = true;
            running = true;
            outbox = new BlockingCollection<string>(new ConcurrentQueue<string>(), QueueCapacity);
            Connection.ConnectionStatusUpdate += OnConnectionStatusUpdate;
            foreach (string name in contracts)
            {
                Instrument instrument = Instrument.GetInstrument(name);
                if (instrument == null)
                {
                    failed.Add(name);   // reported after every handshake
                    continue;
                }
                instrument.MarketData.Update += OnMarketData;   // official Add On subscription pattern
                subscribed.Add(instrument);
            }
            sender = new Thread(SendLoop) { IsBackground = true, Name = "FtsMarketDataBridge" };
            sender.Start();
        }

        private void Stop()
        {
            if (!started) return;
            started = false;
            running = false;
            Connection.ConnectionStatusUpdate -= OnConnectionStatusUpdate;
            foreach (Instrument instrument in subscribed)
                instrument.MarketData.Update -= OnMarketData;
            subscribed.Clear();
            if (outbox != null) outbox.CompleteAdding();
            if (sender != null) sender.Join(2000);
        }

        private void LoadConfig()
        {
            string dir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments), "FtsBridge");
            foreach (string raw in File.ReadAllLines(Path.Combine(dir, "bridge.cfg")))
            {
                string line = raw.Trim();
                if (line.Length == 0 || line.StartsWith("#")) continue;
                int eq = line.IndexOf('=');
                if (eq <= 0) throw new InvalidDataException("bridge.cfg: expected key=value");
                string key = line.Substring(0, eq).Trim(), value = line.Substring(eq + 1).Trim();
                if (key == "port") port = int.Parse(value, CultureInfo.InvariantCulture);
                else if (key == "contracts") contracts = value.Split(new[] { ';' }, StringSplitOptions.RemoveEmptyEntries);
                else throw new InvalidDataException("bridge.cfg: unknown key " + key);
            }
            if (port < 1024 || port > 65535) throw new InvalidDataException("bridge.cfg: port out of range");
            for (int i = 0; i < contracts.Length; i++)
            {
                contracts[i] = contracts[i].Trim();
                if (!(contracts[i].StartsWith("MES ") || contracts[i].StartsWith("MNQ ")))
                    throw new InvalidDataException("bridge.cfg: only MES/MNQ contracts are allowed");
            }
            string hex = File.ReadAllText(Path.Combine(dir, "bridge_secret")).Trim();
            if (hex.Length != 64) throw new InvalidDataException("bridge_secret must hold 64 hex characters");
            secret = new byte[32];
            for (int i = 0; i < 32; i++) secret[i] = Convert.ToByte(hex.Substring(2 * i, 2), 16);
        }

        // ------------------------------------------------------------------ NinjaTrader events (fast: enqueue only)
        private void OnConnectionStatusUpdate(object sender, ConnectionStatusEventArgs e)
        {
            string name = e.Connection != null && e.Connection.Options != null ? e.Connection.Options.Name : "";
            string status = "{\"type\":\"status\",\"provider_connection\":" + Str(e.PriceStatus.ToString())
                    + ",\"connection_name\":" + Str(Clean(name)) + ",\"t_bridge\":\"{T}\"}";
            lastStatus = status;
            Enqueue(status.Replace("{T}", UtcNow()));
            if (e.PriceStatus == ConnectionStatus.Connected)
                foreach (Instrument instrument in subscribed)
                    Enqueue("{\"type\":\"subscription\",\"instrument\":" + Str(instrument.FullName) + ",\"state\":\"subscribed\",\"t_bridge\":" + Str(UtcNow()) + "}");
        }

        private void OnMarketData(object sender, MarketDataEventArgs e)
        {
            string name = e.Instrument.FullName;
            if (e.IsReset)
            {
                lock (bookLock) book.Remove(name);
                Enqueue("{\"type\":\"book_reset\",\"instrument\":" + Str(name) + ",\"t_bridge\":" + Str(UtcNow()) + "}");
                return;
            }
            string times;
            if (!Times(e.Time, out times)) return;
            if (e.MarketDataType == MarketDataType.Last)
            {
                EnqueueMd("{\"type\":\"md\",\"eseq\":{ESEQ},\"instrument\":" + Str(name) + ",\"kind\":\"trade\",\"price\":"
                          + Num(e.Price) + ",\"size\":" + e.Volume.ToString(CultureInfo.InvariantCulture) + times + "}");
            }
            else if (e.MarketDataType == MarketDataType.Bid || e.MarketDataType == MarketDataType.Ask)
            {
                double[] b;
                lock (bookLock)
                {
                    if (!book.TryGetValue(name, out b)) { b = new[] { double.NaN, 0.0, double.NaN, 0.0 }; book[name] = b; }
                    if (e.MarketDataType == MarketDataType.Bid) { b[0] = e.Price; b[1] = e.Volume; }
                    else { b[2] = e.Price; b[3] = e.Volume; }
                    b = (double[])b.Clone();
                }
                if (double.IsNaN(b[0]) || double.IsNaN(b[2])) return;  // a quote needs both sides; never invented
                EnqueueMd("{\"type\":\"md\",\"eseq\":{ESEQ},\"instrument\":" + Str(name) + ",\"kind\":\"quote\",\"bid\":" + Num(b[0])
                          + ",\"ask\":" + Num(b[2]) + ",\"bid_size\":" + Num(b[1]) + ",\"ask_size\":" + Num(b[3]) + times + "}");
            }
            // other MarketDataType values (DailyHigh, Settlement, ...) are not forwarded
        }

        private bool Times(DateTime local, out string times)
        {
            // MarketDataEventArgs.Time is a local wall time in NinjaTrader's configured time zone. The
            // backend re-converts it with the IANA database and refuses ambiguous/nonexistent times.
            times = null;
            DateTime unspecified = DateTime.SpecifyKind(local, DateTimeKind.Unspecified);
            if (timeZone.IsInvalidTime(unspecified) || timeZone.IsAmbiguousTime(unspecified))
            {
                Enqueue("{\"type\":\"error\",\"detail\":\"ambiguous or invalid local event time skipped\",\"t_bridge\":" + Str(UtcNow()) + "}");
                return false;
            }
            DateTime utc = TimeZoneInfo.ConvertTimeToUtc(unspecified, timeZone);
            times = ",\"t_local\":" + Str(unspecified.ToString("yyyy-MM-dd'T'HH:mm:ss.fffffff", CultureInfo.InvariantCulture))
                    + ",\"t_utc\":" + Str(utc.ToString("yyyy-MM-dd'T'HH:mm:ss.fffffff'Z'", CultureInfo.InvariantCulture))
                    + ",\"t_bridge\":" + Str(UtcNow());
            return true;
        }

        private void EnqueueMd(string bodyWithPlaceholder)
        {
            long n = Interlocked.Increment(ref eseq);   // counted even when dropped: the backend sees the gap
            Enqueue(bodyWithPlaceholder.Replace("{ESEQ}", n.ToString(CultureInfo.InvariantCulture)));
        }

        private void Enqueue(string body)
        {
            if (outbox == null || outbox.IsAddingCompleted) return;
            try { outbox.TryAdd(body); } catch (InvalidOperationException) { }   // full or completed: dropped, never blocks NinjaTrader
        }

        // ------------------------------------------------------------------ sender thread
        private void SendLoop()
        {
            int[] backoff = { 1, 2, 5, 10 };
            int attempt = 0;
            while (running)
            {
                try
                {
                    using (TcpClient client = new TcpClient(AddressFamily.InterNetwork))
                    {
                        client.Connect(IPAddress.Loopback, port);          // loopback only, never configurable
                        NetworkStream stream = client.GetStream();
                        stream.ReadTimeout = 5000;
                        byte[] key = Handshake(stream);
                        attempt = 0;
                        string ignored;
                        while (outbox.TryTake(out ignored)) { }           // queued while disconnected = stale: dropped
                        long seq = 0;
                        // after EVERY handshake: the current subscription states and the last known provider
                        // status (NinjaTrader raises no event for a connection that was already up)
                        var initial = new List<string>();
                        foreach (Instrument instrument in subscribed)
                            initial.Add("{\"type\":\"subscription\",\"instrument\":" + Str(instrument.FullName) + ",\"state\":\"subscribed\",\"t_bridge\":" + Str(UtcNow()) + "}");
                        foreach (string name in failed)
                            initial.Add("{\"type\":\"subscription\",\"instrument\":" + Str(name) + ",\"state\":\"failed\",\"detail\":\"instrument not found\",\"t_bridge\":" + Str(UtcNow()) + "}");
                        string st = lastStatus;
                        if (st != null) initial.Add(st.Replace("{T}", UtcNow()));
                        foreach (string body0 in initial) Send(stream, key, ++seq, body0);
                        DateTime nextHeartbeat = DateTime.UtcNow;
                        while (running)
                        {
                            string body;
                            if (!outbox.TryTake(out body, 250))
                            {
                                if (outbox.IsCompleted) break;
                                if (DateTime.UtcNow < nextHeartbeat) continue;
                                body = "{\"type\":\"heartbeat\",\"t_bridge\":" + Str(UtcNow()) + "}";
                            }
                            if (DateTime.UtcNow >= nextHeartbeat) nextHeartbeat = DateTime.UtcNow.AddSeconds(1);
                            if (Encoding.UTF8.GetByteCount(body) > MaxLineBytes - 100) continue;  // never send an oversized line
                            Send(stream, key, ++seq, body);
                            if (client.Client.Poll(0, SelectMode.SelectRead) && client.Available == 0) break; // backend closed
                        }
                    }
                }
                catch (Exception ex)
                {
                    if (running) Print("FtsMarketDataBridge: backend connection: " + Clean(ex.Message));
                }
                if (!running) break;
                Thread.Sleep(1000 * backoff[Math.Min(attempt++, backoff.Length - 1)]);
            }
        }

        private static void Send(NetworkStream stream, byte[] key, long seq, string body)
        {
            string json = "{\"seq\":" + seq.ToString(CultureInfo.InvariantCulture) + "," + body.Substring(1);
            byte[] payload = Encoding.UTF8.GetBytes(json);
            byte[] line = Encoding.ASCII.GetBytes(Hex(Hmac(key, payload)) + " ");
            stream.Write(line, 0, line.Length);
            stream.Write(payload, 0, payload.Length);
            stream.WriteByte((byte)'\n');
        }

        private byte[] Handshake(NetworkStream stream)
        {
            string challenge = ReadLine(stream);
            string serverNonce = Field(challenge, "server_nonce");
            if (serverNonce.Length != 64) throw new InvalidDataException("bad challenge");
            byte[] cn = new byte[32];
            using (var rng = RandomNumberGenerator.Create()) rng.GetBytes(cn);
            string clientNonce = Hex(cn);
            string tz = timeZone.Id;
            string mac = Hex(Hmac(secret, Encoding.UTF8.GetBytes("hello|" + Protocol + "|" + serverNonce + "|" + clientNonce + "|" + tz)));
            string hello = "{\"type\":\"hello\",\"protocol\":" + Protocol + ",\"client\":\"fts-nt8-bridge\",\"client_version\":\"1.0.0\","
                         + "\"provider\":\"NinjaTrader 8\",\"tz\":" + Str(tz) + ",\"client_nonce\":\"" + clientNonce + "\",\"mac\":\"" + mac + "\"}\n";
            byte[] hb = Encoding.UTF8.GetBytes(hello);
            stream.Write(hb, 0, hb.Length);
            string welcome = ReadLine(stream);
            string expected = Hex(Hmac(secret, Encoding.UTF8.GetBytes("welcome|" + Protocol + "|" + serverNonce + "|" + clientNonce)));
            if (Field(welcome, "type") != "welcome" || Field(welcome, "mac") != expected)
                throw new InvalidDataException("backend authentication failed");   // not our backend: send nothing
            return Hmac(secret, Encoding.UTF8.GetBytes("session|" + Protocol + "|" + serverNonce + "|" + clientNonce));
        }

        // ------------------------------------------------------------------ helpers
        private static string ReadLine(NetworkStream stream)
        {
            var sb = new StringBuilder();
            while (sb.Length < 1024)
            {
                int b = stream.ReadByte();
                if (b < 0) throw new IOException("closed");
                if (b == '\n') return sb.ToString();
                sb.Append((char)b);
            }
            throw new InvalidDataException("line too long");
        }

        private static string Field(string json, string key)
        {
            // minimal reader for the two fixed handshake messages ("key":"value", no escapes)
            string k = "\"" + key + "\":\"";
            int i = json.IndexOf(k, StringComparison.Ordinal);
            if (i < 0) return "";
            int j = json.IndexOf('"', i + k.Length);
            return j < 0 ? "" : json.Substring(i + k.Length, j - i - k.Length);
        }

        private static byte[] Hmac(byte[] key, byte[] data)
        {
            using (var h = new HMACSHA256(key)) return h.ComputeHash(data);
        }

        private static string Hex(byte[] bytes)
        {
            var sb = new StringBuilder(bytes.Length * 2);
            foreach (byte b in bytes) sb.Append(b.ToString("x2", CultureInfo.InvariantCulture));
            return sb.ToString();
        }

        private static string Num(double v)
        {
            if (double.IsNaN(v) || double.IsInfinity(v)) return "null";   // the backend then rejects the message
            return v.ToString("R", CultureInfo.InvariantCulture);
        }

        private static string UtcNow()
        {
            return DateTime.UtcNow.ToString("yyyy-MM-dd'T'HH:mm:ss.fffffff'Z'", CultureInfo.InvariantCulture);
        }

        private static string Clean(string s)
        {
            var sb = new StringBuilder();
            foreach (char c in s ?? "") if (c >= 0x20 && c <= 0x7e && c != '"' && c != '\\') sb.Append(c);
            return sb.Length > 120 ? sb.ToString(0, 120) : sb.ToString();
        }

        private static string Str(string s)
        {
            return "\"" + Clean(s) + "\"";
        }
    }
}
