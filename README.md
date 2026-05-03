# Disable Growatt 7000 TL3-S during negative Tibber prices

## Context

When Dutch day-ahead prices go negative (frequent on sunny midday hours since 2024–2025), exporting solar costs money — both the wholesale price is negative *and* most NL suppliers add `terugleverkosten`. The user wants the inverter to stop pushing power during those hours and resume automatically.

Setup:
- **Inverter**: Growatt 7000 TL3-S (3-phase string inverter, grid-tie)
- **Link**: stock ShineWiFi-X dongle → `server.growatt.com`
- **Host**: Debian server (always on, on the same LAN)
- **Price feed**: Tibber (GraphQL, 15-min day-ahead with NL taxes)
- **Action**: clamp inverter output to 0% during negative-price slots

## Why not Growatt's own scheduler

- ShinePhone has no price-aware scheduling for grid-tie inverters. Time-of-use schedules target the SPH/MIN/MOD hybrid + battery line, not the TL3-S.
- No webhook / IFTTT / external trigger.
- What the cloud *does* expose is a remote-control API: you can set `pv_active_p_rate` (0–100%) via [Growatt's OpenAPI v1](https://growatt.pl/wp-content/uploads/2020/01/Growatt-Server-Open-API-protocol-standards.pdf).

So "use the Growatt cloud" still means writing your own price logic — the cloud is just the on/off transport.

## Architecture

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

A single Python script fired from cron every 5 minutes. Two pieces of on-disk state make each tick cheap:

- **`tibber_prices.json`** — pyTibber's `home.price_total` dict cached verbatim. Refetched only on cache miss (cold start, day rollover) — typically 1–2 Tibber GraphQL calls per 24 h.
- **`state.json`** — last-applied inverter state (`ON`/`OFF`) plus `updated_at`. Drives idempotency (no write when `desired == last`) and the deadman re-assert (force a re-write if `state.json` is older than 30 min, as silent-cloud-write insurance).

## What we learned (verified 2026-05-02)

Things the documentation didn't tell us:

1. **Use OpenAPI v1, not the unofficial cloud login.** Generate a token at `server.growatt.com` and authenticate with a `token: <T>` HTTP header. The unofficial `newTwoLoginAPI.do` path 403s behind Cloudflare and would also invalidate the ShinePhone session on every login.
2. **The 7000 TL3-S registers as `device_type=1` (INVERTER)** in v1's `device/list` taxonomy, not `MIN`/`SPH`. There's no Python wrapper for type-1 writes, so we call `POST /v1/inverterSet` directly with `aiohttp`.
3. **The daily set-password (`growatt` + `YYYYMMDD`) is required in `command_2`** even though the official 2020 PDF says `command_2=""`. Empty `command_2` returns `error_code: 0, "Set Successful"` but the inverter never actually changes — silent no-op. With today's password (rotated at the inverter's local midnight, computed via `ZoneInfo("Europe/Amsterdam")`) the inverter responds within 60 s.
4. **`shinemonitor.com` is unrelated** to Growatt's account system despite branding overlap. Account creds live on `server.growatt.com` / ShinePhone.
