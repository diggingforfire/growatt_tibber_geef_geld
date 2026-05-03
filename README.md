# Disable Growatt 7000 TL3-S during negative Tibber prices

## Context

When Dutch day-ahead prices go negative (frequent on sunny midday hours since 2024–2025), exporting solar costs money — both the wholesale price is negative *and* most NL suppliers add `terugleverkosten`. The user wants the inverter to stop pushing power during those hours and resume automatically.

Setup we're building around:
- **Inverter**: Growatt 7000 TL3-S (3-phase string inverter, grid-tie)
- **Link**: stock ShineWiFi-X dongle → `server.growatt.com` (no RS485, no CT clamp assumed)
- **Host**: Debian server (always on, on the same LAN)
- **Price feed**: Tibber (GraphQL, hourly day-ahead with NL taxes)
- **Action**: clamp inverter output to 0% (effectively "stop export"; self-consumption from solar is also paused, which is acceptable since negative-price hours are midday surplus)

There is no codebase yet — this is a greenfield service on the Debian box.

## Why not Growatt's own scheduler

Worth answering up front since "can the cloud do it?" is the obvious question:

- Growatt's cloud (ShinePhone / OSS portal) has **no price-aware scheduling** for grid-tie inverters. Time-of-use schedules in the app target the SPH/MIN/MOD hybrid + battery line, not the TL3-S.
- It also has no webhook/IFTTT/API trigger that fires on external events.
- What the cloud *does* give you is a **remote-control API**: you can set `pv_active_p_rate` (0–100%) and it propagates to the inverter via the dongle. Growatt publishes this as the [Server Open API v1](https://growatt.pl/wp-content/uploads/2020/01/Growatt-Server-Open-API-protocol-standards.pdf) — token-authenticated, documented, and stable.

So "use the Growatt cloud" still means you write your own price logic — the cloud is just the transport for the on/off command. We use it as the primary path because it requires zero local network changes.

## What we learned (verified 2026-05-02)

Things the documentation didn't tell us, that one round-trip test on the live inverter clarified:

1. **Use OpenAPI v1, not the unofficial cloud login.** Generate a token at `server.growatt.com` (account → API access) and authenticate with a `token: <T>` header against `https://openapi.growatt.com/v1/`. The unofficial `newTwoLoginAPI.do` path 403s behind Cloudflare and would also invalidate the ShinePhone session on every login. The v1 path has neither problem.
2. **The 7000 TL3-S registers as `device_type=1` (INVERTER)** in the v1 API's `device/list` taxonomy, not `MIN`/`SPH`. There's no Python wrapper library for type-1 writes, so we call `POST /v1/inverterSet` directly with `aiohttp`.
3. **Endpoint shape**: `POST /v1/inverterSet` with body `device_sn`, `paramId=pv_active_p_rate`, `command_1="0".."100"`, `command_2=<daily password>`. Response is `{error_code, error_msg, data}` — `error_code=0` means accepted.
4. **The daily-rotating set-password (`growatt` + `YYYYMMDD`) is required in `command_2` even though the official 2020 PDF says `command_2=""`**. With `command_2=""` the cloud returned `error_code: 0, "Set Successful"` *but the inverter never actually changed* — silent no-op. With `command_2="growatt20260502"` (today's date) the inverter responded within 60 s. **This is the highest-risk gotcha in the whole project.**
5. **`shinemonitor.com` is unrelated** to Growatt's account system (despite branding overlap). Account creds live on `server.growatt.com` / ShinePhone.
6. **Verification: `GET /v1/device/inverter/last_new_data?device_sn=…`** returns live per-phase AC power (`pacr`+`pacs`+`pact` in W) and `status` (0=Waiting, 1=Normal, 3=Fault). Rate-limited to roughly once per 5 min per device on the free tier — **don't poll it tightly**, use it for sanity checks only.
7. **Sub-account is no longer required.** The v1 token is independent of any ShinePhone web/app session; logging via the token doesn't kick anyone else out.

## Architecture (primary: cloud API)

```
 Tibber GraphQL ──┐
                  ▼
        ┌─────────────────────────┐  HTTPS (token header + /v1/inverterSet)
        │  growatt-price (cron)   │ ──────────────────────────▶ openapi.growatt.com/v1/
        │  Debian host            │                                       │
        └─────────────────────────┘                                       ▼
                                                              ShineWiFi-X dongle
                                                                          │
                                                                          ▼
                                                                Inverter (TL3-S)
```

A Python script invoked from cron every 5 minutes. No daemon, no systemd unit, no Grott, no MQTT, no DNS hijack.

## Components

### 1. growatt-price controller
Single Python file, ~250 lines.

- `/opt/growatt-price/controller.py`
- `/opt/growatt-price/config.toml` — Tibber token, Growatt OpenAPI v1 token, inverter serial, threshold (no daily set-password stored — it's computed at runtime as `growatt` + `YYYYMMDD`)
- `/var/lib/growatt-price/state.json` — last-applied state + `updated_at` (idempotency + deadman source)
- `/var/lib/growatt-price/tibber_prices.json` — cached price slots (today + tomorrow once published)

Dependencies (one venv under `/opt/growatt-price/.venv`):
- `pyTibber` ([Danielhiversen/pyTibber](https://github.com/Danielhiversen/pyTibber)) — community-standard async Tibber client (the one Home Assistant uses); handles auth, parsing, and price-info access. There is no official Tibber SDK; this is the de-facto choice. Pulls in `aiohttp` transitively.
- `aiohttp` — used directly for the two Growatt v1 calls (`POST /v1/inverterSet` and `GET /v1/device/inverter/last_new_data`). Hand-written, no Growatt-specific library needed.
- `tomllib` (stdlib on 3.11+) — config

**Logic** (executed once per cron tick — every 5 min):
1. Read `tibber_prices.json` from disk. Find the slot containing `now` (largest TZ-aware ISO key ≤ `now`, within a 1-hour window). **Cache hit** → use the cached `total` and skip to step 4. **Cache miss** (cold start, day rollover, or stale window) → spin up `pyTibber.Tibber(...)`, `await home.update_info_and_price_info()`, save the returned `home.price_total` dict back to `tibber_prices.json`, retry the lookup. Typically 1–2 cache misses per 24 h.
2. Compare current `total` against threshold (default `0.00` EUR/kWh, configurable). `total` already includes NL taxes — `< 0` means "I'm paying to export this slot".
3. Desired state: `OFF` (rate=0) if below threshold, `ON` (rate=100) otherwise.
4. Read `state.json` for the last-applied state and its `updated_at`. The deadman is "due" if `now - updated_at >= deadman_reassert_seconds` (or the file is missing).
5. If `desired != last_state` OR deadman is due, `POST /v1/inverterSet` with `{device_sn, paramId="pv_active_p_rate", command_1="0"|"100", command_2=<today's daily password>}`. **`command_2` is required** despite the 2020 PDF saying it should be empty — see "What we learned" §4. Treat the response as accepted iff `error_code == 0`.
6. On success, write `state.json` with the new state + fresh `updated_at`. Process exits.
7. (Optional) Verification step: `GET /v1/device/inverter/last_new_data?device_sn=…` ~60 s after a write — sum `pacr+pacs+pact`, expect a clear drop after a 0% write. Skipped from the normal control path because this endpoint is rate-limited to ~once per 5 min per device on the free tier; useful for ad-hoc debugging.

**Failure handling** (boundary only):
- Tibber API down on a cache miss → exception propagates, controller exits non-zero, `state.json` untouched. Next tick (5 min later) retries.
- Growatt OpenAPI HTTP error or `error_code != 0` on `inverterSet` → log, exit non-zero, `state.json` untouched. Next tick retries. Documented error codes worth special-casing: 10003 (inverter dropped — dongle offline, retry next tick), 10006 (paramId unknown — config error, alert), 10008 (value out of range — bug, alert).
- **Dead-man check**: every cron tick checks `state.json`'s `updated_at`. If older than `deadman_reassert_seconds` (default 30 min), force a re-apply even if desired matches last. Cheap insurance against the cloud silently ignoring a write — and fires automatically across the daily-password midnight rollover, so no special clock logic.
- **Reboot recovery**: cron starts running again after the host comes up, picks up `state.json`, finds the deadman due (no recent update), and re-asserts on the very next tick.

### 2. Tibber API access
- Personal access token from `developer.tibber.com` (free).
- Used only on cache miss. Per-tick flow: spin up `tibber.Tibber(token)`, `await t.update_info()`, `_pick_home(...)`, `await home.update_info_and_price_info()`, copy `home.price_total` to disk, close the session. Process exits at end of tick — no long-lived Tibber instance.
- `home.price_total` is a dict mapping TZ-aware ISO timestamp → total EUR/kWh, covering today + tomorrow once published (around 13:00 CET). We persist it verbatim to `tibber_prices.json` so subsequent ticks read from disk and skip the GraphQL call entirely.
- **Slot granularity is 15 minutes** in NL as of 2026 (confirmed 2026-05-02, post EU ENTSO-E quarter-hour rollout). 5-min cron cadence catches every slot boundary within ≤5 min; 30-min deadman spans 2 slots — fine.
- Library is async (`aiohttp`-based), so the controller's entry point is `asyncio.run(...)`. Growatt v1 calls also go through `aiohttp` — same event loop within the tick.

### 3. Growatt OpenAPI token & daily inverter password

Two secrets are involved, only one of which we store:

- **OpenAPI v1 token** — generate at `server.growatt.com` (account / API access page). Sent as `token: <T>` HTTP header against `https://openapi.growatt.com/v1/`. Stored in `config.toml`. This is the only persistent Growatt credential the service holds. Don't reuse the ShinePhone account password — the v1 path doesn't need it. ⚠ `shinemonitor.com` is a different, unrelated platform — don't confuse them.
- **Daily set-password** — `growatt` + today's date in `YYYYMMDD` (confirmed `growatt20260502` accepted on 2026-05-02). Required as `command_2` on every `/v1/inverterSet` write. Rotates at the **inverter's** local midnight, not the host's — so the controller computes "today" via `datetime.now(ZoneInfo("Europe/Amsterdam")).date()` regardless of the host TZ. Override via `[growatt] timezone = "..."` in config if the inverter is installed outside NL. Not stored — computed at runtime per call.

Why `command_2` and not the documented empty string: empirically, with `command_2=""` the cloud returns `error_code: 0` *but the inverter never receives the command* — a silent no-op. With `command_2=<today's password>` the inverter responds within ~60 s. The 2020 PDF documenting the empty-string contract is wrong (or out of date) for the type-1 inverter family.

## Critical files / artefacts to create

| Path | Purpose |
| --- | --- |
| `/opt/growatt-price/controller.py` | The price → state → cloud-API script (one tick per invocation) |
| `/opt/growatt-price/config.toml` | Secrets + threshold + serial (mode 600) |
| `/opt/growatt-price/.venv/` | Python venv |
| `/etc/cron.d/growatt-price` | Cron line that fires the script every 5 min (see Deployment) |
| `/var/lib/growatt-price/state.json` | Last-applied state + `updated_at` (deadman source) |
| `/var/lib/growatt-price/tibber_prices.json` | Cached Tibber price slots, written on cache miss |

## Deployment (cron, single line)

The whole production install boils down to one cron entry. Drop this in `/etc/cron.d/growatt-price`:

```cron
*/5 * * * * growatt-price /opt/growatt-price/.venv/bin/growatt-price --config /opt/growatt-price/config.toml 2>&1 | /usr/bin/logger -t growatt-price
```

- `*/5 * * * *` — every 5 min. Match this to `deadman_reassert_seconds` in `config.toml` (a tick gap longer than the deadman window means the reassert fires twice).
- `growatt-price` — system user that owns `/opt/growatt-price` and `/var/lib/growatt-price`. Created during install; not a login user.
- `2>&1 | logger -t growatt-price` — pipe both streams to syslog/journald with a recognisable tag. View ticks with `journalctl -t growatt-price -f`. Avoids cron's default of mailing every output line to root.

One-time setup on the Debian host:
1. `sudo useradd --system --home /opt/growatt-price --shell /usr/sbin/nologin growatt-price`
2. `sudo install -d -o growatt-price -g growatt-price /opt/growatt-price /var/lib/growatt-price`
3. Copy the project tree to `/opt/growatt-price`, create the venv (`python3.11 -m venv .venv && .venv/bin/pip install .`).
4. Copy `config.example.toml` to `/opt/growatt-price/config.toml`, fill in tokens + serial, `chmod 600`.
5. Drop the cron line above into `/etc/cron.d/growatt-price` (mode 644).

## Verification

End-to-end checks before trusting it on a live negative-price day:

1. **Manual API write works** ✅ DONE 2026-05-02. Round-trip clamped the inverter from ~2.5 kW to ~250 W within 60 s and restored cleanly; confirmed in ShinePhone. The successful call shape:
   - `POST https://openapi.growatt.com/v1/inverterSet`
   - headers: `token: <OpenAPI token>`
   - body: `device_sn=<your serial>&paramId=pv_active_p_rate&command_1=10&command_2=growatt20260502`
   - response: `{"error_code": 0, "error_msg": "Set Successful", "data": ""}`

   With `command_2=""` the same call returned `error_code: 0` but the inverter did not change — see "What we learned" §4.
2. **Tibber price fetch**: `python -m growatt_price.controller --config config.toml --dry-run` prints current slot `total` + chosen state and writes `tibber_prices.json`.
3. **Cache hit**: re-run immediately. Log shows no Tibber fetch (cache served the lookup). Same price + decision.
4. **Threshold flip**: temporarily raise threshold above current price, run without `--dry-run`. Inverter clamps to 0% within ~60 s (confirm in ShinePhone). Restore threshold, run again, confirm 100%.
5. **Deadman trigger**: backdate `state.json`'s `updated_at` by 31 minutes (`jq` or hand-edit), re-run with same threshold. Log shows `reassert=True` and a re-applied `inverterSet` POST.
6. **Reboot survival**: `reboot` the host. After it's back up, the next cron tick (≤5 min) finds `state.json` stale and re-asserts.
7. **Live observation**: pick a known negative-price slot from `tibber_prices.json` (or any forecast tool), watch the OFF→ON transition.
8. **Midnight rollover**: at least one cron tick needs to land on a 00:00–00:30 window to confirm the daily-password recompute works across the date boundary. The 30-min deadman covers this in production automatically; worth observing once via `journalctl -t growatt-price`.

## Fallback path: Grott local proxy

Keep this in your back pocket. The OpenAPI v1 path is more stable than the reverse-engineered alternative, but it's still Growatt's cloud — a token revocation, an undocumented endpoint change, or the daily-password convention changing would all break us. The cleanest local-only pivot:

- Install [Grott](https://github.com/johanmeijer/grott) on the same Debian box.
- Redirect dongle to Grott (router-level DNS override of `server.growatt.com` → Debian IP, or reconfigure dongle in AP mode). Grott forwards traffic onward, so ShinePhone keeps working.
- Replace the `inverterSet` call in `controller.py` with an MQTT publish to Grott's command topic that writes Modbus holding register 3 (Active Power Rate).
- Fully local — survives a Growatt cloud outage.

Cost of switching is ~half a day of setup; the controller's price logic is unchanged.

## Repo bootstrap (first action on plan approval)

Repo location on Windows dev machine: `C:\dev\growatt_tibber_geef_geld`
Language: Python 3.11+ (stdlib `tomllib`)

Repo tree:

```
C:\dev\growatt_tibber_geef_geld\
├── .gitignore                       # Python-flavoured (venv, __pycache__, .env, state.json)
├── README.md                        # This plan
├── pyproject.toml                   # deps: pyTibber, aiohttp; Python >=3.11
├── config.example.toml              # Tibber token, Growatt OpenAPI token, inverter SN, threshold
└── src/
    └── growatt_price/
        ├── __init__.py
        └── controller.py            # Single-shot script run from cron every 5 min
```

No `deploy/` directory — the deployment is one cron line (see Deployment section).

## Open considerations (decide before implementation, not blockers)

- **Threshold semantics**: `total` (consumer-perspective, includes tax) vs `energy` (closer to spot). Default to `total < 0` since that's literally "I'm being paid to consume / paying to export". Easy to retune in config later.
- **Slot granularity**: Tibber NL is on 15-minute slots as of 2026 (confirmed 2026-05-02). 5-min polling means transitions happen within 5 min of any slot boundary. Good enough. Worst-case negative-price churn (alternating 15-min slots above/below threshold) would be 4 transitions/hour — still well within OpenAPI rate budget.
- **Re-enable lag**: TL3-S takes 30–60 s to ramp back up from a 0% clamp. Acceptable.
- ~~**Account isolation**: log in with a dedicated sub-account so the script can't fight the ShinePhone app for sessions.~~ Resolved by the v1 token path — no session conflict.
- **OpenAPI rate limits**: free tier is roughly per-5-min on read endpoints, generous on writes. Write budget on a heavy day: 1 OFF + 8 dead-man re-asserts across a 4-h negative window + 1 ON ≈ 10. Pathological-churn day with 15-min slots flipping at every boundary: bounded at ~4/hr × 24 = 96. Still within bounds. Reads on `last_new_data` should be ≤ once per 5 min.
- **No CT-clamp export limiting** (out of scope): if you later add a Growatt ShineLink + CT, true export limiting (allow self-consumption, zero export) becomes possible. For now we accept "0% generation during negative hours."
- **Backup kill switch**: an AC contactor on the inverter output controlled by a smart relay is a worthwhile *physical* fallback. Not part of v1.
- **Token revocation**: if the OpenAPI token is rotated (account compromise, manual rotation), every cron tick will fail and log to journald until the new token is dropped into `config.toml`. No automated handling. Acceptable for a personal-use service.
