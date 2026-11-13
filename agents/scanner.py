"""Scanner: graduate PumpSwap coins, wait, then pass or kill."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from fields import dex_float, dex_value, learn_migration_mapping, read_migration
from rpc import Rpc, RpcError
from sources import PUMPPORTAL_WS, HttpSourceError
from util import ROOT, agent_logger, as_float, copycat_key, iso_utc, utcnow

log = agent_logger("scanner")

SUBSCRIBE = {"method": "subscribeMigration"}


def kill_reasons(
    pair: dict | None,
    top10_pct: float | None,
    age_min: int | None,
    name: str | None,
    symbol: str | None,
    runners: list[dict],
    cfg,
) -> str | None:
    """Return the first kill reason, in spec order, or None if none hit yet.

    A missing DexScreener pair is not a kill: the coin waits in the room.
    """
    if pair is None:
        return None
    liq = dex_float(pair, "liquidity.usd")
    min_liq = float(cfg.get("scanner.min_liquidity_usd") or 40000)
    if liq is None:
        return None
    if liq < min_liq:
        return _liq_reason(liq)
    max_top = float(cfg.get("scanner.max_top10_pct") or 25)
    if top10_pct is not None and top10_pct > max_top:
        return f"top 10 hold {top10_pct:.0f}%" if top10_pct >= 10 else f"top 10 hold {top10_pct:.1f}%"
    h1 = dex_float(pair, "priceChange.h1")
    max_h1 = float(cfg.get("scanner.max_h1_change_pct") or 80)
    if h1 is not None and h1 > max_h1:
        sign = "+" if h1 > 0 else ""
        return f"{sign}{h1:.0f}% in 1h" if abs(h1) >= 10 else f"{sign}{h1:g}% in 1h"
    copied = copycat_of(name, symbol, runners)
    if copied:
        return f"copycat of {copied}"
    return None


def _liq_reason(liq: float) -> str:
    if liq >= 1000:
        return f"liq ${liq/1000:.1f}K"
    return f"liq ${liq:.0f}"


def copycat_of(name: str | None, symbol: str | None, runners: list[dict]) -> str | None:
    keys = [copycat_key(name), copycat_key(symbol)]
    keys = [k for k in keys if k]
    if not keys:
        return None
    for runner in runners:
        rname = runner.get("name") or runner.get("symbol") or ""
        rkeys = [copycat_key(runner.get("name")), copycat_key(runner.get("symbol"))]
        rkeys = [k for k in rkeys if k]
        for a in keys:
            for b in rkeys:
                if a == b or (a in b) or (b in a):
                    return rname or runner.get("mint")
    return None


def is_runner(pair: dict | None, cfg) -> bool:
    if pair is None:
        return False
    h24 = dex_float(pair, "priceChange.h24")
    thresh = float(cfg.get("scanner.runner_h24_pct") or 300)
    return h24 is not None and h24 >= thresh


def age_minutes(migrated_at: str | None, now: datetime | None = None) -> int | None:
    if not migrated_at:
        return None
    now = now or utcnow()
    s = str(migrated_at).replace("Z", "+00:00")
    try:
        t = datetime.fromisoformat(s)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return max(0, int((now - t).total_seconds() // 60))


def evaluate(
    mint: str,
    name: str | None,
    symbol: str | None,
    pool: str | None,
    migrated_at: str | None,
    pair: dict | None,
    top10_pct: float | None,
    runners: list[dict],
    cfg,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Decide pass/kill/wait for one coin. 1 coin passes once; caller enforces that."""
    now = now or utcnow()
    age = age_minutes(migrated_at, now)
    liq = dex_float(pair, "liquidity.usd") if pair else None
    h1 = dex_float(pair, "priceChange.h1") if pair else None
    reason = kill_reasons(pair, top10_pct, age, name, symbol, runners, cfg)
    min_age = int(cfg.get("scanner.min_pool_age_min") or 10)
    max_wait = int(cfg.get("scanner.max_wait_min") or 60)
    verdict = "wait"
    if reason:
        verdict = "kill"
    elif pair is None:
        verdict = "wait"
        if age is not None and age >= max_wait:
            verdict = "kill"
            reason = "never qualified"
    elif age is not None and age >= min_age:
        verdict = "pass"
    elif age is not None and age >= max_wait:
        verdict = "kill"
        reason = "never qualified"
    return {
        "agent": "scanner",
        "mint": mint,
        "name": name,
        "symbol": symbol,
        "pool": pool,
        "liquidityUsd": liq,
        "top10Pct": top10_pct,
        "h1ChangePct": h1,
        "ageMin": age,
        "verdict": verdict,
        "reason": reason,
        "ts": iso_utc(now),
    }


def _top10(rpc: Rpc | None, mint: str, pool: str | None, curve: str | None) -> float | None:
    if rpc is None:
        return None
    try:
        supply_body = rpc.get_token_supply(mint)
        from fields import get_in

        supply = as_float(get_in(supply_body, "result.value.uiAmount"))
        if not supply:
            amount = as_float(get_in(supply_body, "result.value.amount"))
            decimals = as_float(get_in(supply_body, "result.value.decimals"), 0) or 0
            supply = (amount / (10 ** decimals)) if amount else None
        if not supply:
            return None
        from agents.auditor import top10_pct

        return top10_pct(rpc, mint, supply, pool, curve)
    except (RpcError, Exception):
        return None


def _creator_balance(rpc: Rpc | None, creator: str | None, mint: str) -> float | None:
    if rpc is None or not creator:
        return None
    try:
        from agents.auditor import _creator_balance as bal

        return bal(rpc, creator, mint, {})
    except Exception:
        return None


_first_migration_saved = False


async def _handle_migration(msg: Any, db, cfg, rpc=None) -> None:
    global _first_migration_saved
    parsed = read_migration(msg)
    log.info("migration keys=%s unverified=%s", parsed.get("keys"), parsed.get("unverified"))
    if isinstance(msg, dict) and not _first_migration_saved:
        # Don't treat the subscribe ack as a migration.
        if "message" in msg and len(msg) == 1:
            return
        try:
            learn_migration_mapping(msg)
            _first_migration_saved = True
            log.info("saved first migration sample, mapping=%s", parsed.get("unverified"))
        except Exception as e:
            log.warning("could not save migration sample: %s", type(e).__name__)
    mint = parsed.get("mint")
    if not mint:
        return
    if db.get_coin(mint) or db.query_one("SELECT mint FROM waiting_room WHERE mint=?", (mint,)):
        # already seen tonight / ever — still don't send twice in a night
        return
    migrated_at = iso_utc()
    creator = parsed.get("creator")
    # Creator may come later from RugCheck; store null if unknown.
    bal = _creator_balance(rpc, creator, mint)
    name, symbol = parsed.get("name"), parsed.get("symbol")
    db.upsert_coin(
        mint=mint,
        name=name,
        symbol=symbol,
        pool=parsed.get("pool"),
        migrated_at=migrated_at,
        creator=creator,
        creator_balance_at_migration=bal,
    )
    db.waiting_put(
        mint=mint,
        name=name,
        symbol=symbol,
        pool=parsed.get("pool"),
        migrated_at=migrated_at,
        creator=creator,
        creator_balance=bal,
        curve=parsed.get("curve"),
        status="waiting",
        last_scan_ts=migrated_at,
    )
    log.info("waiting room + %s", mint)


async def _pumpportal_loop(db, cfg, rpc, stop_event: asyncio.Event | None) -> None:
    import websockets

    backoff = 1
    max_backoff = 60
    silence = float(cfg.get("scanner.ws_silence_s") or 120)
    while stop_event is None or not stop_event.is_set():
        try:
            async with websockets.connect(PUMPPORTAL_WS, open_timeout=20) as ws:
                await ws.send(json.dumps(SUBSCRIBE))
                log.info("pumpportal subscribed")
                backoff = 1
                while stop_event is None or not stop_event.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=silence)
                    except asyncio.TimeoutError:
                        log.warning("pumpportal silent %ss, reconnecting", int(silence))
                        break
                    try:
                        msg = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
                    except ValueError:
                        log.warning("pumpportal non-json keys=?")
                        continue
                    await _handle_migration(msg, db, cfg, rpc)
        except Exception as e:
            log.warning("pumpportal reconnect in %ss (%s)", backoff, type(e).__name__)
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                raise
            backoff = min(max_backoff, max(1, backoff * 2))


async def _poll_waiting_room(db, cfg, bus, rpc, sources, stop_event, passed_tonight: set[str]) -> None:
    runners: list[dict] = []
    while stop_event is None or not stop_event.is_set():
        db.heartbeat("scanner")
        poll_s = float(cfg.get("scanner.poll_s") or 60)
        try:
            if stop_event:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_s)
                if stop_event.is_set():
                    break
            else:
                await asyncio.sleep(poll_s)
        except asyncio.TimeoutError:
            pass
        if cfg.is_paused("scanner"):
            continue
        room = db.waiting_all("waiting")
        if not room:
            continue
        mints = [c["mint"] for c in room]
        pairs: dict[str, dict] = {}
        if sources is not None:
            # batched DexScreener, stay under 250 req/min
            try:
                # chunk if huge
                for i in range(0, len(mints), 30):
                    chunk = mints[i : i + 30]
                    pairs.update(sources.pumpswap_pairs(chunk))
            except HttpSourceError as e:
                if e.status == 429:
                    wait = float(cfg.get("scanner.dex_429_wait_s") or 30)
                    log.warning("dex 429, waiting %ss, keeping waiting room", wait)
                    await asyncio.sleep(wait)
                    continue
                log.warning("dex error: %s", type(e).__name__)
            except Exception as e:
                log.warning("dex error: %s", type(e).__name__)

        # refresh today's runners from coins we already know
        new_runners = []
        for mint, pair in pairs.items():
            if is_runner(pair, cfg):
                new_runners.append(
                    {
                        "mint": mint,
                        "name": dex_value(pair, "baseToken.name"),
                        "symbol": dex_value(pair, "baseToken.symbol"),
                    }
                )
        if new_runners:
            # merge, keep today
            known = {r["mint"]: r for r in runners}
            known.update({r["mint"]: r for r in new_runners})
            runners = list(known.values())

        for coin in room:
            mint = coin["mint"]
            pair = pairs.get(mint)
            # fill name/symbol/pool from DexScreener once known
            if pair:
                name = dex_value(pair, "baseToken.name") or coin.get("name")
                symbol = dex_value(pair, "baseToken.symbol") or coin.get("symbol")
                pool = dex_value(pair, "pairAddress") or coin.get("pool")
                db.upsert_coin(mint=mint, name=name, symbol=symbol, pool=pool)
                coin = {**coin, "name": name, "symbol": symbol, "pool": pool}
                db.add_price(mint, dex_float(pair, "priceUsd"), dex_float(pair, "volume.m5"))
                # creator from rugcheck if still null
                if not coin.get("creator") and sources is not None:
                    try:
                        report = sources.rugcheck_report(mint)
                        creator = report.get("creator") if isinstance(report, dict) else None
                        if creator:
                            bal = _creator_balance(rpc, creator, mint)
                            db.upsert_coin(
                                mint=mint,
                                creator=creator,
                                creator_balance_at_migration=bal
                                if coin.get("creator_balance") is None
                                else None,
                            )
                            coin["creator"] = creator
                            if bal is not None:
                                coin["creator_balance"] = bal
                    except Exception:
                        pass
            else:
                # DexScreener doesn't know it yet — wait, never pass.
                name, symbol, pool = coin.get("name"), coin.get("symbol"), coin.get("pool")

            top10 = None
            # Only spend an RPC call when we have a pair (otherwise we'd wait anyway)
            if pair is not None:
                top10 = _top10(rpc, mint, coin.get("pool"), coin.get("curve"))

            decision = evaluate(
                mint=mint,
                name=coin.get("name"),
                symbol=coin.get("symbol"),
                pool=coin.get("pool"),
                migrated_at=coin.get("migrated_at"),
                pair=pair,
                top10_pct=top10,
                runners=[r for r in runners if r.get("mint") != mint],
                cfg=cfg,
            )
            if decision["verdict"] == "wait":
                continue
            db.add_scan(
                mint=mint,
                liquidity_usd=decision["liquidityUsd"],
                top10_pct=decision["top10Pct"],
                h1_change_pct=decision["h1ChangePct"],
                age_min=decision["ageMin"],
                verdict=decision["verdict"],
                reason=decision["reason"],
            )
            log.info(
                "ts=%s mint=%s verdict=%s reason=%s liq=%s top10=%s h1=%s age=%s",
                decision["ts"],
                mint,
                decision["verdict"],
                decision["reason"],
                decision["liquidityUsd"],
                decision["top10Pct"],
                decision["h1ChangePct"],
                decision["ageMin"],
            )
            if decision["verdict"] == "kill":
                db.waiting_set_status(mint, "killed")
                await bus.emit("scan", decision)
                continue
            # pass once
            if mint in passed_tonight:
                db.waiting_set_status(mint, "passed")
                continue
            passed_tonight.add(mint)
            db.waiting_set_status(mint, "passed")
            payload = {
                **decision,
                "scanner": decision,
                "creator": coin.get("creator"),
                "creator_balance": coin.get("creator_balance")
                or (db.get_coin(mint) or {}).get("creator_balance_at_migration"),
                "migratedAt": coin.get("migrated_at"),
                "curve": coin.get("curve"),
                "pair": pair,
            }
            await bus.auditor.put(payload)
            await bus.emit("scan", decision)


async def run_agent(
    db,
    cfg,
    bus,
    rpc=None,
    sources=None,
    stop_event: asyncio.Event | None = None,
) -> None:
    passed_tonight: set[str] = set()
    # restore already-passed tonight so we never send twice
    night = db.current_night()
    if night:
        rows = db.query("SELECT mint FROM scans WHERE verdict='pass' AND ts>=?", (night["started_at"],))
        passed_tonight = {r["mint"] for r in rows}
    tasks = [
        asyncio.create_task(_pumpportal_loop(db, cfg, rpc, stop_event), name="scanner-ws"),
        asyncio.create_task(
            _poll_waiting_room(db, cfg, bus, rpc, sources, stop_event, passed_tonight),
            name="scanner-poll",
        ),
    ]
    try:
        await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            t.cancel()
