"""Price-aware Growatt inverter controller (cron-style, single-shot).

Each invocation: read disk cache → look up current 15-min slot price →
fetch from Tibber only on cache miss → compare against threshold →
POST /v1/inverterSet only on state change or deadman re-assert. Then exit.

Runs from cron every 5 minutes (see README §Deployment for the cron line).
See README.md for the full design and the verified call shapes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import tomllib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
import tibber

logger = logging.getLogger("growatt_price")

GROWATT_API_BASE = "https://openapi.growatt.com/v1"
USER_AGENT = "growatt-price/0.1"
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=20)
SLOT_LOOKUP_WINDOW = timedelta(hours=1)

# Inverter's local timezone — controls when the daily set-password rolls over.
# Configurable via [growatt] timezone in config.toml; defaults to NL (CEST/CET).
DEFAULT_INVERTER_TZ = ZoneInfo("Europe/Amsterdam")


# ---------- config ----------

@dataclass(frozen=True)
class Config:
    tibber_token: str
    tibber_home_id: str | None
    growatt_api_token: str
    inverter_serial: str
    inverter_timezone: ZoneInfo
    threshold_eur_per_kwh: float
    deadman_reassert_seconds: int
    state_file: Path
    price_cache_file: Path


def load_config(path: Path) -> Config:
    with path.open("rb") as f:
        raw = tomllib.load(f)
    tz_name = raw["growatt"].get("timezone")
    inverter_tz = ZoneInfo(tz_name) if tz_name else DEFAULT_INVERTER_TZ
    return Config(
        tibber_token=raw["tibber"]["token"],
        tibber_home_id=raw["tibber"].get("home_id"),
        growatt_api_token=raw["growatt"]["api_token"],
        inverter_serial=raw["growatt"]["inverter_serial"],
        inverter_timezone=inverter_tz,
        threshold_eur_per_kwh=float(raw["control"]["threshold_eur_per_kwh"]),
        deadman_reassert_seconds=int(raw["control"]["deadman_reassert_seconds"]),
        state_file=Path(raw["paths"]["state_file"]),
        price_cache_file=Path(raw["paths"]["price_cache_file"]),
    )


# ---------- inverter password ----------

def compute_inverter_password(
    today: date | None = None,
    tz: ZoneInfo | None = None,
) -> str:
    """Daily-rotating Growatt inverter set-password: growatt + YYYYMMDD.

    The "today" date is taken in the inverter's local timezone (default
    Europe/Amsterdam) so the password rolls over at the inverter's local
    midnight regardless of the host server's TZ. This matters because
    Growatt's firmware itself rolls the password using its installed
    local time — a UTC-clocked Debian box would compute yesterday's
    password for the first 1–2 hours of every local day.
    """
    if today is None:
        today = datetime.now(tz or DEFAULT_INVERTER_TZ).date()
    return f"growatt{today:%Y%m%d}"


# ---------- price cache ----------

def load_price_cache(path: Path) -> dict[str, float]:
    """Return {ISO timestamp: total EUR/kWh}, or {} if missing/corrupt."""
    try:
        with path.open() as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        logger.warning("price cache unreadable, treating as empty: %r", e)
        return {}


def save_price_cache(path: Path, prices: dict[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as f:
        json.dump(prices, f)
    tmp.replace(path)


def find_current_slot_price(
    prices: dict[str, float],
    now: datetime,
) -> tuple[float | None, datetime | None]:
    """Return (price, slot_start) for the slot containing `now`, or (None, None).

    Tibber's price_total keys are TZ-aware ISO timestamps for slot starts
    (e.g. "2026-05-03T13:30:00.000+02:00"). The matching slot is the largest
    start ≤ now. We additionally require it to be within SLOT_LOOKUP_WINDOW
    of now to avoid picking up a stale entry across a long outage.
    """
    best: tuple[datetime, float] | None = None
    for key, value in prices.items():
        try:
            slot_start = datetime.fromisoformat(key)
        except ValueError:
            continue
        if slot_start.tzinfo is None or slot_start > now:
            continue
        if best is None or slot_start > best[0]:
            best = (slot_start, value)
    if best is None:
        return None, None
    slot_start, price = best
    if now - slot_start > SLOT_LOOKUP_WINDOW:
        return None, None
    return float(price), slot_start


# ---------- Tibber fetch (only on cache miss) ----------

def _pick_home(t: tibber.Tibber, home_id: str | None):
    homes = t.get_homes()
    if not homes:
        raise RuntimeError("Tibber: no homes attached to this account")
    if home_id is None:
        if len(homes) > 1:
            logger.warning(
                "multiple Tibber homes (%d); using the first. "
                "Pin one with [tibber] home_id in config.",
                len(homes),
            )
        return homes[0]
    for h in homes:
        if h.home_id == home_id:
            return h
    raise RuntimeError(
        f"Tibber: home_id {home_id!r} not found; available: "
        f"{[h.home_id for h in homes]}"
    )


async def _fetch_prices_via_pytibber(config: Config) -> dict[str, float]:
    t = tibber.Tibber(config.tibber_token, user_agent=USER_AGENT)
    try:
        await t.update_info()
        home = _pick_home(t, config.tibber_home_id)
        await home.update_info_and_price_info()
        return dict(home.price_total)
    finally:
        await t.close_connection()


async def get_current_price(config: Config) -> tuple[float, datetime]:
    """Return (total EUR/kWh, slot_start). Network only on cache miss."""
    now = datetime.now(timezone.utc)
    prices = load_price_cache(config.price_cache_file)
    price, slot = find_current_slot_price(prices, now)
    if price is None:
        logger.info("price cache miss; fetching from Tibber")
        prices = await _fetch_prices_via_pytibber(config)
        save_price_cache(config.price_cache_file, prices)
        price, slot = find_current_slot_price(prices, now)
        if price is None:
            raise RuntimeError("Tibber: no current-slot price even after refetch")
    else:
        logger.debug("price cache hit: slot=%s entries=%d", slot, len(prices))
    assert price is not None and slot is not None
    return price, slot


# ---------- decision ----------

def desired_state(price: float, threshold: float) -> str:
    return "OFF" if price < threshold else "ON"


def _state_to_rate(state: str) -> str:
    return "0" if state == "OFF" else "100"


# ---------- inverter control ----------

async def apply_state(
    session: aiohttp.ClientSession,
    api_token: str,
    serial: str,
    state: str,
    inverter_password: str,
) -> bool:
    """POST /v1/inverterSet. Return True iff the cloud accepted (error_code=0).

    The daily set-password goes in command_2 — required despite the 2020
    PDF saying it should be empty (README "What we learned" §4). Caller is
    responsible for computing it (see compute_inverter_password) so this
    function is free of clock dependence and easy to test.
    """
    body = {
        "device_sn": serial,
        "paramId": "pv_active_p_rate",
        "command_1": _state_to_rate(state),
        "command_2": inverter_password,
    }
    headers = {"token": api_token, "User-Agent": USER_AGENT}
    try:
        async with session.post(
            f"{GROWATT_API_BASE}/inverterSet",
            headers=headers,
            data=body,
            timeout=HTTP_TIMEOUT,
        ) as resp:
            payload = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.error("inverterSet HTTP error: %r", e)
        return False
    code = payload.get("error_code")
    if code != 0:
        logger.error(
            "inverterSet rejected: error_code=%s msg=%r",
            code, payload.get("error_msg"),
        )
        return False
    logger.info("inverterSet ok: state=%s response=%s", state, payload)
    return True


async def verify_ac_power(
    session: aiohttp.ClientSession,
    api_token: str,
    serial: str,
) -> float | None:
    """GET /v1/device/inverter/last_new_data. Returns summed AC W or None."""
    headers = {"token": api_token, "User-Agent": USER_AGENT}
    try:
        async with session.get(
            f"{GROWATT_API_BASE}/device/inverter/last_new_data",
            headers=headers,
            params={"device_sn": serial},
            timeout=HTTP_TIMEOUT,
        ) as resp:
            payload = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning("verify HTTP error: %r", e)
        return None
    if payload.get("error_code") != 0:
        logger.warning("verify rejected: %s", payload)
        return None
    data = payload.get("data") or {}
    try:
        return (
            float(data.get("pacr") or 0)
            + float(data.get("pacs") or 0)
            + float(data.get("pact") or 0)
        )
    except (TypeError, ValueError):
        return None


# ---------- last-applied state ----------

def load_last_state(path: Path) -> str | None:
    try:
        with path.open() as f:
            return json.load(f).get("state")
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        logger.warning("could not read state file %s: %r", path, e)
        return None


def save_last_state(path: Path, state: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "state": state,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as f:
        json.dump(payload, f)
    tmp.replace(path)


def deadman_due(state_path: Path, threshold_seconds: int) -> bool:
    """True if state.json's updated_at is older than threshold (or absent)."""
    try:
        with state_path.open() as f:
            updated = datetime.fromisoformat(json.load(f)["updated_at"])
    except (FileNotFoundError, KeyError, ValueError, OSError):
        return True
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - updated).total_seconds() >= threshold_seconds


# ---------- one cycle ----------

async def run(config: Config, *, dry_run: bool) -> None:
    last_state = load_last_state(config.state_file)
    force_reassert = deadman_due(config.state_file, config.deadman_reassert_seconds)

    price, slot = await get_current_price(config)
    desired = desired_state(price, config.threshold_eur_per_kwh)
    state_changed = desired != last_state

    logger.info(
        "slot=%s price=%.4f threshold=%.4f desired=%s last=%s reassert=%s",
        slot, price, config.threshold_eur_per_kwh,
        desired, last_state, force_reassert,
    )

    if not (state_changed or force_reassert):
        logger.info("no-op: desired matches last-applied and deadman not due")
        return

    if dry_run:
        logger.info("dry-run: would apply %s", desired)
        return

    password = compute_inverter_password(tz=config.inverter_timezone)
    async with aiohttp.ClientSession() as session:
        ok = await apply_state(
            session,
            config.growatt_api_token,
            config.inverter_serial,
            desired,
            password,
        )
    if not ok:
        # Don't update state.json — next tick will retry. Caller exits non-zero.
        raise RuntimeError(f"inverterSet failed for state={desired}")
    save_last_state(config.state_file, desired)


# ---------- CLI ----------

def main() -> int:
    parser = argparse.ArgumentParser(prog="growatt-price")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("/opt/growatt-price/config.toml"),
        help="Path to config.toml",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute desired state but do not call the Growatt API",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config(args.config)
    try:
        asyncio.run(run(config, dry_run=args.dry_run))
    except Exception:
        logger.exception("run failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
