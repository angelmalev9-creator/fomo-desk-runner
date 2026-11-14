"""Auditor: eight safety layers plus an optional Jupiter layer, stop at the first red."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from fields import (
    TOKEN_2022_PROGRAM,
    TOKEN_PROGRAM,
    graph_holder_components,
    get_in,
    rpc_value,
    rugcheck_market,
    dex_float,
    jup_token_value,
    jup_token_float,
)
from rpc import Rpc, RpcError, mint_program_id, token_account_delegate, token_account_ui_amount
from util import agent_logger, as_float, iso_utc

log = agent_logger("auditor")

LAYER_NAMES = {
    1: "Mint authority",
    2: "Freeze authority",
    3: "Dev wallet",
    4: "Top 10 holders",
    5: "Liquidity",
    6: "Pool age",
    7: "Bundled wallets",
    8: "Delegations",
    9: "Jupiter",
}


def _layer(n: int, passed: bool | None, value: str, source: str) -> dict[str, Any]:
    return {"n": n, "name": LAYER_NAMES[n], "pass": passed, "value": value, "source": source}


def _jup_on(cfg) -> bool:
    if cfg is None:
        return True
    val = cfg.get("auditor.jup_layer")
    return True if val is None else bool(val)


def _layer_source(n: int) -> str:
    if n == 9:
        return "jupiter"
    if n in (5, 7):
        return "rugcheck"
    return "rpc"


def _unchecked(from_n: int, jup: bool = True) -> list[dict[str, Any]]:
    last = 9 if jup else 8
    out = []
    for n in range(from_n, last + 1):
        out.append(_layer(n, None, "not checked", _layer_source(n)))
    return out


def _ok_audit(layers: list[dict], mint: str, holders: int | None = None) -> dict[str, Any]:
    checked = max((l["n"] for l in layers if l.get("pass") is not None), default=0)
    red = next((l for l in layers if l.get("pass") is False), None)
    need = 9 if any(l.get("n") == 9 for l in layers) else 8
    return {
        "agent": "auditor",
        "mint": mint,
        "ts": iso_utc(),
        "passed": red is None and checked == need,
        "layersChecked": checked,
        "layers": layers,
        "holders": holders,
        "reason": None if red is None else red["value"],
        "bundled": _bundled_from_layers(layers),
    }


def _bundled_from_layers(layers: list[dict]) -> int:
    for l in layers:
        if l["n"] == 7 and l.get("pass") is not None:
            raw = str(l.get("value") or "")
            if raw.lower() in ("none", "not checked"):
                return 0
            for tok in raw.replace(",", " ").split():
                try:
                    return int(float(tok))
                except ValueError:
                    continue
    return 0


class _Retryable(Exception):
    pass


def _with_retry(fn: Callable, retries: int, wait_s: float, sleep: Callable[[float], None], layer: str):
    last = None
    attempts = max(1, retries)
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            last = e
            if i + 1 < attempts:
                sleep(wait_s)
    raise _Retryable(f"no data: {layer}") from last


def audit_mint(
    mint: str,
    scanner: dict[str, Any] | None,
    coin: dict[str, Any] | None,
    cfg,
    rpc: Rpc | None = None,
    sources=None,
    now=None,
    sleep: Callable[[float], None] = time.sleep,
    emit_layer: Callable[[dict], None] | None = None,
) -> dict[str, Any]:
    """Run layers 1-8, then Jupiter if jup_layer is on. Stop at the first red. No data is never a pass."""
    retries = int(cfg.get("auditor.retries") or 3)
    wait_s = float(cfg.get("auditor.retry_wait_s") or 2)
    jup = _jup_on(cfg)
    layers: list[dict[str, Any]] = []
    holders: int | None = None
    t0 = time.monotonic()
    timings: list[str] = []

    def emit(layer: dict) -> None:
        layers.append(layer)
        if emit_layer:
            emit_layer(layer)

    def fail(layer: dict) -> dict[str, Any]:
        emit(layer)
        layers.extend(_unchecked(layer["n"] + 1, jup=jup))
        result = _ok_audit(layers, mint, holders)
        _log_result(mint, layers, timings, result)
        return result

    # --- fetch shared RPC mint account (layers 1-2) ---
    try:
        def _acct():
            if rpc is None:
                raise _Retryable("no data: mint authority")
            return rpc.get_account_info(mint)

        t = time.monotonic()
        acct = _with_retry(_acct, retries, wait_s, sleep, "mint authority")
        timings.append(f"account={int((time.monotonic()-t)*1000)}ms")
    except _Retryable:
        return fail(_layer(1, False, "no data: mint authority", "rpc"))

    mint_auth = rpc_value(acct, "mintAuthority")
    freeze_auth = rpc_value(acct, "freezeAuthority")

    # Layer 1
    if mint_auth in (None, "", "null"):
        emit(_layer(1, True, "Revoked", "rpc"))
    else:
        return fail(_layer(1, False, "mint not revoked", "rpc"))

    # Layer 2
    if freeze_auth in (None, "", "null"):
        emit(_layer(2, True, "Revoked", "rpc"))
    else:
        return fail(_layer(2, False, "freeze not revoked", "rpc"))

    # Layer 3 — supply + creator balance
    try:
        def _supply():
            if rpc is None:
                raise _Retryable("no supply")
            return rpc.get_token_supply(mint)

        t = time.monotonic()
        supply_body = _with_retry(_supply, retries, wait_s, sleep, "dev")
        timings.append(f"supply={int((time.monotonic()-t)*1000)}ms")
        supply = as_float(get_in(supply_body, "result.value.uiAmount"))
        if supply is None:
            # fall back to amount / 10**decimals
            amount = as_float(get_in(supply_body, "result.value.amount"))
            decimals = as_float(get_in(supply_body, "result.value.decimals"), 0) or 0
            supply = (amount / (10 ** decimals)) if amount is not None else None
    except _Retryable:
        return fail(_layer(3, False, "no supply", "rpc"))
    if not supply or supply <= 0:
        return fail(_layer(3, False, "no supply", "rpc"))

    creator = (coin or {}).get("creator") or (scanner or {}).get("creator")
    creator_at_mig = (coin or {}).get("creator_balance_at_migration")
    if creator_at_mig is None and scanner:
        creator_at_mig = scanner.get("creator_balance") or scanner.get("creatorBalance")
    if not creator:
        return fail(_layer(3, False, "no data: dev", "rpc"))

    try:
        def _dev_bal():
            if rpc is None:
                raise _Retryable("no data: dev")
            return _creator_balance(rpc, creator, mint, acct)

        t = time.monotonic()
        dev_bal = _with_retry(_dev_bal, retries, wait_s, sleep, "dev")
        timings.append(f"dev={int((time.monotonic()-t)*1000)}ms")
    except _Retryable:
        return fail(_layer(3, False, "no data: dev", "rpc"))

    dev_pct = (dev_bal / supply) * 100.0
    max_dev = float(cfg.get("auditor.max_dev_pct") or 5)
    sold = False
    if creator_at_mig is not None and dev_bal + 1e-9 < float(creator_at_mig):
        sold = True
    if dev_pct >= max_dev:
        return fail(_layer(3, False, f"dev holds {dev_pct:.1f}%", "rpc"))
    if sold:
        return fail(_layer(3, False, "dev sold", "rpc"))
    sold_txt = "sold 0" if (creator_at_mig is None or dev_bal + 1e-9 >= float(creator_at_mig)) else "sold"
    emit(_layer(3, True, f"{dev_pct:.1f}% · {sold_txt}", "rpc"))

    # Layer 4 — top 10 (RPC first, RugCheck report as fallback)
    prefetched_report = None
    pool = (coin or {}).get("pool") or (scanner or {}).get("pool")
    curve = (coin or {}).get("curve") or (scanner or {}).get("curve")
    try:
        def _top10():
            if rpc is None:
                raise _Retryable("no data: top 10")
            return top10_pct(rpc, mint, supply, pool, curve)

        t = time.monotonic()
        pct = _with_retry(_top10, retries, wait_s, sleep, "top 10")
        timings.append(f"top10={int((time.monotonic()-t)*1000)}ms")
        top_src = "rpc"
    except _Retryable:
        # Public RPC rate-limits getTokenLargestAccounts. Fall back to RugCheck's free
        # report (topHolders); still fail closed when neither source has data.
        pct = None
        top_src = "rugcheck"
        if sources is not None:
            try:
                t = time.monotonic()
                prefetched_report = _with_retry(lambda: sources.rugcheck_report(mint), retries, wait_s, sleep, "top 10")
                timings.append(f"top10_rug={int((time.monotonic()-t)*1000)}ms")
                pct = rugcheck_top10_pct(prefetched_report, pool, curve)
            except _Retryable:
                pct = None
        if pct is None:
            return fail(_layer(4, False, "no data: top 10", "rpc+rugcheck"))
        log.info("auditor top10 fallback=rugcheck mint=%s pct=%.1f", mint[:8], pct)
    max_top = float(cfg.get("auditor.max_top10_pct") or 25)
    if pct >= max_top:
        return fail(_layer(4, False, f"top 10 hold {pct:.0f}%" if pct >= 10 else f"top 10 hold {pct:.1f}%", top_src))
    emit(_layer(4, True, f"{pct:.1f}%", top_src))

    # Layer 5 — LP locked + liquidity not pulled
    try:
        def _report():
            if prefetched_report is not None:
                return prefetched_report
            if sources is None:
                raise _Retryable("no data: liquidity")
            return sources.rugcheck_report(mint)

        t = time.monotonic()
        report = _with_retry(_report, retries, wait_s, sleep, "liquidity")
        timings.append(f"rug_report={int((time.monotonic()-t)*1000)}ms")
        market = rugcheck_market(report, pool)
        locked = None
        if market:
            locked = as_float(get_in(market, "lp.lpLockedPct"))
        if locked is None:
            return fail(_layer(5, False, "no data: liquidity", "rugcheck"))
        min_locked = float(cfg.get("auditor.min_lp_locked_pct") or 99)
        if locked < min_locked:
            return fail(_layer(5, False, "liquidity unlocked", "rugcheck"))
        # liquidity not down > 50% since Scanner
        scanner_liq = as_float((scanner or {}).get("liquidityUsd") or (scanner or {}).get("liquidity_usd"))
        current_liq = None
        if scanner:
            current_liq = as_float((scanner or {}).get("currentLiquidityUsd"))
        # optional fresh pair on the payload
        if current_liq is None and scanner:
            current_liq = as_float((scanner or {}).get("liq_now"))
        drop_max = float(cfg.get("auditor.max_liq_drop_pct") or 50)
        if scanner_liq and current_liq is not None and scanner_liq > 0:
            drop = (scanner_liq - current_liq) / scanner_liq * 100.0
            if drop > drop_max:
                return fail(_layer(5, False, "liquidity pulled", "rugcheck"))
        emit(_layer(5, True, f"{locked:.0f}% locked", "rugcheck"))
    except _Retryable:
        return fail(_layer(5, False, "no data: liquidity", "rugcheck"))

    # Layer 6 — pool age
    from datetime import datetime, timezone

    migrated_at = (coin or {}).get("migrated_at") or (scanner or {}).get("migratedAt") or (scanner or {}).get("migrated_at")
    min_age = int(cfg.get("auditor.min_pool_age_min") or 10)
    age_min = _age_min(migrated_at, now)
    if age_min is None:
        return fail(_layer(6, False, "no data: pool age", "rpc"))
    if age_min < min_age:
        return fail(_layer(6, False, "pool too young", "rpc"))
    emit(_layer(6, True, _age_label(age_min), "rpc"))

    # Layer 7 — bundled wallets via links (not edges)
    try:
        def _graph():
            if sources is None:
                raise _Retryable("no data: bundles")
            return sources.rugcheck_graph(mint)

        t = time.monotonic()
        graph = _with_retry(_graph, retries, wait_s, sleep, "bundles")
        timings.append(f"graph={int((time.monotonic()-t)*1000)}ms")
        # Graph has no links/edges key at all → red "no data: bundles"
        if not _graph_has_links(graph):
            return fail(_layer(7, False, "no data: bundles", "rugcheck"))
        comps = graph_holder_components(graph)
        biggest = max((len(c) for c in comps), default=0)
        max_bundled = int(cfg.get("auditor.max_bundled") or 5)
        if biggest >= max_bundled:
            return fail(_layer(7, False, f"{biggest} bundled wallets", "rugcheck"))
        emit(_layer(7, True, "None" if biggest < 2 else str(biggest), "rugcheck"))
    except _Retryable:
        return fail(_layer(7, False, "no data: bundles", "rugcheck"))

    # Layer 8 — delegations on Token AND Token-2022
    try:
        def _gpa():
            if rpc is None:
                raise _Retryable("no data: delegations")
            return gather_token_accounts(rpc, mint, acct)

        t = time.monotonic()
        accounts = _with_retry(_gpa, retries, wait_s, sleep, "delegations")
        timings.append(f"gpa={int((time.monotonic()-t)*1000)}ms")
    except _Retryable:
        return fail(_layer(8, False, "no data: delegations", "rpc"))

    holders, worst, worst_n = summarize_delegations(accounts)
    max_del = int(cfg.get("auditor.max_delegated_accounts") or 10)
    if worst is not None and worst_n >= max_del:
        short = _short_key(worst)
        return fail(_layer(8, False, f"{worst_n} wallets → {short}", "rpc"))
    emit(_layer(8, True, "None", "rpc"))

    # Layer 9 — Jupiter (optional extra, default on). Fail closed.
    if jup:
        try:
            def _jup_token():
                if sources is None or not hasattr(sources, "jupiter_token"):
                    raise _Retryable("no data: jup")
                return sources.jupiter_token(mint)

            t = time.monotonic()
            token = _with_retry(_jup_token, retries, wait_s, sleep, "jup")
            timings.append(f"jup={int((time.monotonic()-t)*1000)}ms")
        except _Retryable:
            return fail(_layer(9, False, "no data: jup", "jupiter"))
        if not isinstance(token, dict):
            return fail(_layer(9, False, "no data: jup", "jupiter"))
        if jup_token_value(token, "audit.isSus") is True:
            return fail(_layer(9, False, "jup isSus", "jupiter"))
        mint_dis = jup_token_value(token, "audit.mintAuthorityDisabled")
        freeze_dis = jup_token_value(token, "audit.freezeAuthorityDisabled")
        if mint_dis is None:
            return fail(_layer(9, False, "no data: jup mint", "jupiter"))
        if mint_dis is not True:
            return fail(_layer(9, False, "jup mint not revoked", "jupiter"))
        if freeze_dis is None:
            return fail(_layer(9, False, "no data: jup freeze", "jupiter"))
        if freeze_dis is not True:
            return fail(_layer(9, False, "jup freeze not revoked", "jupiter"))
        top = jup_token_float(token, "audit.topHoldersPercentage")
        if top is None:
            return fail(_layer(9, False, "no data: jup top holders", "jupiter"))
        max_top = float(cfg.get("auditor.max_top10_pct") or 25)
        if top >= max_top:
            label = f"{top:.0f}%" if top >= 10 else f"{top:.1f}%"
            return fail(_layer(9, False, f"jup top holders {label}", "jupiter"))
        emit(_layer(9, True, "ok", "jupiter"))

    result = _ok_audit(layers, mint, holders)
    _log_result(mint, layers, timings, result)
    return result


def _graph_has_links(graph) -> bool:
    if not isinstance(graph, list) or not graph:
        # empty list of networks still has the `links` concept? If the body is
        # a dict without links/edges, that's "no data". An empty list is "no
        # bundles" (links key present on no networks). Treat list as having the
        # documented shape. A dict with neither links nor edges is no data.
        if isinstance(graph, dict):
            return "links" in graph or "edges" in graph
        return True
    # If any network is a dict and none has a links (or edges) key, no data.
    dicts = [n for n in graph if isinstance(n, dict)]
    if not dicts:
        return True
    if all(("links" not in n and "edges" not in n) for n in dicts):
        return False
    return True


def _age_min(migrated_at, now=None) -> int | None:
    from datetime import datetime, timezone

    if migrated_at is None:
        return None
    if now is None:
        now = datetime.now(timezone.utc)
    if isinstance(migrated_at, (int, float)):
        # ms or s
        ts = float(migrated_at)
        if ts > 1e12:
            ts /= 1000.0
        migrated = datetime.fromtimestamp(ts, tz=timezone.utc)
    else:
        s = str(migrated_at).replace("Z", "+00:00")
        try:
            migrated = datetime.fromisoformat(s)
        except ValueError:
            return None
        if migrated.tzinfo is None:
            migrated = migrated.replace(tzinfo=timezone.utc)
    return max(0, int((now - migrated).total_seconds() // 60))


def _age_label(age_min: int) -> str:
    if age_min < 60:
        return f"{age_min}m"
    h = age_min // 60
    m = age_min % 60
    return f"{h}h{m}m" if m else f"{h}h"


def _short_key(key: str) -> str:
    if not key or len(key) < 12:
        return key
    return f"{key[:4]}...{key[-3:]}"


def _creator_balance(rpc: Rpc, creator: str, mint: str, acct_body: dict) -> float:
    """Sum uiAmount of creator's token accounts for this mint (Token + Token-2022)."""
    total = 0.0
    # Official method filters by mint; try once. If the RPC rejects, raise.
    body = rpc.get_token_accounts_by_owner(creator, mint)
    value = get_in(body, "result.value") or []
    if isinstance(value, list):
        for acc in value:
            if isinstance(acc, dict):
                total += token_account_ui_amount(acc)
    return total


_TOP10_EXCLUDE_TYPES = {"AMM", "LOCKER", "BURN", "CURVE"}


def rugcheck_top10_pct(report, pool: str | None = None, curve: str | None = None) -> float | None:
    """Top-10 holders % of supply from a RugCheck report, excluding pools, curves, lockers.

    Returns None when the report has no usable holder data (caller fails closed)."""
    if not isinstance(report, dict):
        return None
    holders = report.get("topHolders")
    if not isinstance(holders, list) or not holders:
        return None
    skip = {a for a in (pool, curve) if a}
    known = report.get("knownAccounts") or {}
    if isinstance(known, dict):
        for addr, info in known.items():
            if isinstance(info, dict) and str(info.get("type", "")).upper() in _TOP10_EXCLUDE_TYPES:
                skip.add(addr)
    for m in report.get("markets") or []:
        if isinstance(m, dict):
            for k in ("pubkey", "liquidityA", "liquidityB", "liquidityAAccount", "liquidityBAccount"):
                if isinstance(m.get(k), str) and m.get(k):
                    skip.add(m[k])
    pcts = []
    for h in holders:
        if not isinstance(h, dict):
            continue
        if h.get("owner") in skip or h.get("address") in skip:
            continue
        v = as_float(h.get("pct"))
        if v is not None:
            pcts.append(v)
    if not pcts:
        return None
    pcts.sort(reverse=True)
    return round(sum(pcts[:10]), 2)


def top10_pct(rpc: Rpc, mint: str, supply: float, pool: str | None, curve: str | None) -> float:
    """Sum of top 10 holder accounts excluding pool and bonding curve, as % of supply."""
    body = rpc.get_token_largest_accounts(mint)
    value = get_in(body, "result.value") or []
    if not isinstance(value, list):
        raise RpcError("no largest accounts")
    skip = {s for s in (pool, curve, mint) if s}
    amounts = []
    for acc in value:
        if not isinstance(acc, dict):
            continue
        address = acc.get("address") or acc.get("pubkey")
        if address in skip:
            continue
        ui = as_float(acc.get("uiAmount") or get_in(acc, "amount.uiAmount") or acc.get("amount"), 0) or 0
        amounts.append(ui)
    # Already roughly sorted largest-first; take 10 after exclusion.
    amounts.sort(reverse=True)
    top = sum(amounts[:10])
    return (top / supply) * 100.0 if supply else 0.0


def gather_token_accounts(rpc: Rpc, mint: str, acct_body: dict | None) -> list[dict]:
    accounts: list[dict] = []
    # Query both programs, as Token-2022 mints exist.
    for program in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        try:
            body = rpc.get_program_accounts(program, mint)
        except RpcError:
            continue
        result = body.get("result") if isinstance(body, dict) else None
        if isinstance(result, list):
            accounts.extend(result)
    return accounts


def summarize_delegations(accounts: list[dict]) -> tuple[int, str | None, int]:
    holders = 0
    by_delegate: dict[str, int] = {}
    for acc in accounts:
        if not isinstance(acc, dict):
            continue
        bal = token_account_ui_amount(acc)
        if bal > 0:
            holders += 1
        dele = token_account_delegate(acc)
        if dele and bal > 0:
            by_delegate[dele] = by_delegate.get(dele, 0) + 1
    if not by_delegate:
        return holders, None, 0
    worst, n = max(by_delegate.items(), key=lambda kv: kv[1])
    return holders, worst, n


def _log_result(mint: str, layers: list[dict], timings: list[str], result: dict) -> None:
    parts = [f"L{l['n']}={l['value']}" for l in layers]
    log.info(
        "mint=%s %s verdict=%s %s",
        mint,
        " ".join(parts),
        "pass" if result.get("passed") else "reject",
        " ".join(timings),
    )


async def run_agent(
    db,
    cfg,
    bus,
    rpc=None,
    sources=None,
    notifier=None,
    stop_event: asyncio.Event | None = None,
    rejected_tonight: set[str] | None = None,
) -> None:
    seen = rejected_tonight if rejected_tonight is not None else set()
    gpa_alerted = False
    killed_by_l8 = 0
    while stop_event is None or not stop_event.is_set():
        try:
            item = await asyncio.wait_for(bus.auditor.get(), timeout=2.0)
        except asyncio.TimeoutError:
            db.heartbeat("auditor")
            continue
        db.heartbeat("auditor")
        if cfg.is_paused("auditor"):
            continue
        mint = item.get("mint")
        if not mint:
            continue
        if mint in seen:
            continue
        # never re-audit a rejected mint the same night
        prior = db.query(
            "SELECT pass FROM audits WHERE mint=? AND pass=0 ORDER BY id DESC LIMIT 1",
            (mint,),
        )
        if prior:
            seen.add(mint)
            continue
        if not cfg.get("auditor.enabled"):
            # Still pass the coin through with a note? Spec: toggle off is a UI
            # setting. If disabled, skip safety and send to Analyst.
            fake = {
                "agent": "auditor",
                "mint": mint,
                "ts": iso_utc(),
                "passed": True,
                "layersChecked": 0,
                "layers": _unchecked(1, jup=False),
                "holders": None,
                "reason": None,
            }
            await bus.analyst.put({**item, "audit": fake})
            continue

        def on_layer(layer: dict) -> None:
            db.add_audit_layer(
                mint=mint,
                layer=layer["n"],
                name=layer["name"],
                value_text=layer["value"],
                passed=layer["pass"],
                source=layer["source"],
            )
            # SSE hook
            try:
                asyncio.get_event_loop().create_task(bus.emit("audit_layer", {"mint": mint, "layer": layer}))
            except Exception:
                pass

        coin = db.get_coin(mint) or {}
        result = audit_mint(
            mint=mint,
            scanner=item,
            coin=coin,
            cfg=cfg,
            rpc=rpc,
            sources=sources,
            emit_layer=on_layer,
        )
        if result.get("holders") is not None:
            db.upsert_coin(mint=mint, holders=result["holders"])
        if not result["passed"]:
            seen.add(mint)
            reason = result.get("reason") or "rejected"
            if reason == "no data: delegations":
                killed_by_l8 += 1
                db.kv_set("auditor.l8_killed", str(killed_by_l8))
                if not gpa_alerted and notifier is not None:
                    gpa_alerted = True
                    try:
                        res = notifier.send(
                            "layer 8 down, every coin is red until rpc_url changes",
                            buttons=None,
                        )
                        if asyncio.iscoroutine(res):
                            await res
                    except Exception:
                        pass
            name = item.get("name") or coin.get("name") or mint
            db.add_activity(
                kind="rejected",
                mint=mint,
                agent="auditor",
                title=f"Rejected {name}",
                subtitle=f"Auditor · {reason}",
                value="Skipped",
            )
            await bus.emit("audit", result)
            continue
        await bus.analyst.put({**item, "audit": result})
        await bus.emit("audit", result)
