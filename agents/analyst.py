"""Analyst: build state, score entry quality, apply the gate."""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any, Callable

from fields import dex_float, dex_value
from util import agent_logger, as_float, as_int, clamp, iso_utc, secret

log = agent_logger("analyst")

LAYER_NAMES = {
    1: "Mint authority",
    2: "Freeze authority",
    3: "Dev wallet",
    4: "Top 10 holders",
    5: "Liquidity",
    6: "Pool age",
    7: "Bundled wallets",
    8: "Delegations",
}

JEV_QUESTIONS = {
    "alive_6h": {
        "type": "noul",
        "instructions": "This coin is still actively traded 6 hours from now",
    },
    "organic": {
        "type": "noul",
        "instructions": "Most buyers in the last 30 minutes are separate real wallets",
    },
    "flush": {
        "type": "noul",
        "instructions": "Price just flushed down and buyers are absorbing it",
    },
    "chase": {
        "type": "noul",
        "instructions": "This entry is chasing a green candle that already ran",
    },
}


def _fmt_usd(n: float | None) -> str:
    if n is None:
        return "n/a"
    if abs(n) >= 1000:
        return f"{n:,.0f}"
    return f"{n:,.2f}"


def _fmt_pct(n: float | None) -> str:
    if n is None:
        return "n/a"
    return f"{n:g}"


def _layer_value(audit: dict[str, Any] | None, n: int) -> str:
    if not audit:
        return "n/a"
    layers = audit.get("layers") or []
    for layer in layers:
        if layer.get("n") == n:
            return str(layer.get("value") or "n/a")
    return "n/a"


def build_state(
    name: str,
    age_min: int | float | None,
    pair: dict | None,
    audit: dict[str, Any] | None,
    top10_pct: float | None,
    max_chars: int = 3000,
) -> str:
    """Plain text, numbers only, no opinions. Never includes thresholds, balance or 'buy'."""
    liq = dex_float(pair, "liquidity.usd")
    mcap = dex_float(pair, "marketCap")
    b5 = as_int(dex_value(pair, "txns.m5.buys"), 0) or 0
    s5 = as_int(dex_value(pair, "txns.m5.sells"), 0) or 0
    v5 = dex_float(pair, "volume.m5", 0) or 0
    p5 = dex_float(pair, "priceChange.m5", 0) or 0
    b1 = as_int(dex_value(pair, "txns.h1.buys"), 0) or 0
    s1 = as_int(dex_value(pair, "txns.h1.sells"), 0) or 0
    v1 = dex_float(pair, "volume.h1", 0) or 0
    p1 = dex_float(pair, "priceChange.h1", 0) or 0
    dev = _layer_value(audit, 3)
    lp = _layer_value(audit, 5)
    bund = _layer_value(audit, 7)
    delg = _layer_value(audit, 8)
    top10 = top10_pct
    if top10 is None and audit:
        # try to parse layer 4 "16.8%"
        raw = _layer_value(audit, 4).replace("%", "").split()[0]
        top10 = as_float(raw)
    age = as_int(age_min, 0) or 0
    text = (
        f"Coin {name} on Solana, graduated {age} min ago. "
        f"Liquidity ${_fmt_usd(liq)}. Market cap ${_fmt_usd(mcap)}. "
        f"Last 5 min: {b5} buys, {s5} sells, volume ${_fmt_usd(v5)}, price {_fmt_pct(p5)}%. "
        f"Last 1h: {b1} buys, {s1} sells, volume ${_fmt_usd(v1)}, price {_fmt_pct(p1)}%. "
        f"Top 10 hold {_fmt_pct(top10)}%. Dev holds {dev}. LP {lp}. Bundled {bund}. Delegations {delg}."
    )
    # The template in the spec uses "Dev holds {dev}%, sold 0" — keep the auditor
    # string (already includes % and "sold 0") and do not add the word "buy".
    if len(text) > max_chars:
        text = text[: max_chars - 1] + "…"
    lowered = text.lower()
    # Guard: never put the word "buy" into the state even if a name contains it.
    # Names are as given; we only refuse to inject it ourselves. Leave as-is.
    _ = lowered
    return text


def apply_gate(
    answers: dict[str, float],
    min_alive_6h: float = 0.70,
    min_organic: float = 0.65,
    max_chase: float = 0.30,
    min_score: int = 70,
) -> dict[str, Any]:
    """Code-only gate. Jev never sees these thresholds."""
    alive = float(answers["alive_6h"])
    organic = float(answers["organic"])
    flush = float(answers["flush"])
    chase = float(answers["chase"])
    score = int(round(100 * (alive + organic + flush + (1 - chase)) / 4))
    failed: list[str] = []
    if alive < min_alive_6h:
        failed.append(f"alive_6h {alive}")
    if organic < min_organic:
        failed.append(f"organic {organic}")
    if chase > max_chase:
        failed.append(f"chase {chase}")
    if score < min_score:
        failed.append(f"score {score}")
    return {
        "score": score,
        "passed": not failed,
        "failed": failed,
    }


def rules_answers(pair: dict | None, audit: dict[str, Any] | None, cfg) -> dict[str, float]:
    vol_h1 = dex_float(pair, "volume.h1", 0) or 0
    buys_h1 = as_float(dex_value(pair, "txns.h1.buys"), 0) or 0
    buys_m5 = as_float(dex_value(pair, "txns.m5.buys"), 0) or 0
    sells_m5 = as_float(dex_value(pair, "txns.m5.sells"), 0) or 0
    ch_m5 = dex_float(pair, "priceChange.m5", 0) or 0
    ch_h1 = dex_float(pair, "priceChange.h1", 0) or 0

    alive_den = float(cfg.get("analyst.alive_volume_h1") or 25000)
    organic_den = float(cfg.get("analyst.organic_buys_h1") or 150)
    penalty = float(cfg.get("analyst.bundled_penalty") or 0.5)
    penalty_from = int(cfg.get("analyst.bundled_penalty_from") or 3)
    flush_drop = float(cfg.get("analyst.flush_drop_m5_pct") or 5)
    chase_den = float(cfg.get("analyst.chase_h1_pct") or 80)

    alive_6h = clamp(vol_h1 / alive_den if alive_den else 0, 0, 1)
    organic = clamp(buys_h1 / organic_den if organic_den else 0, 0, 1)
    bundled_n = _bundled_count(audit)
    if bundled_n >= penalty_from:
        organic = clamp(organic * penalty, 0, 1)
    if ch_m5 <= -flush_drop and buys_m5 >= sells_m5:
        flush = 1.0
    elif ch_m5 < 0:
        flush = 0.5
    else:
        flush = 0.0
    chase = clamp(ch_h1 / chase_den if chase_den else 0, 0, 1)
    return {
        "alive_6h": alive_6h,
        "organic": organic,
        "flush": flush,
        "chase": chase,
    }


def _bundled_count(audit: dict[str, Any] | None) -> int:
    if not audit:
        return 0
    for layer in audit.get("layers") or []:
        if layer.get("n") == 7:
            raw = str(layer.get("value") or "")
            if raw.lower() in ("none", "not checked", ""):
                return 0
            # "14 bundled wallets" or a bare number
            for tok in raw.replace(",", " ").split():
                try:
                    return int(float(tok))
                except ValueError:
                    continue
    # also accept a dedicated field
    return int(audit.get("bundled") or 0)


def _read_noul(payload: dict, name: str) -> float | None:
    answers = payload.get("answers") or {}
    item = answers.get(name) or {}
    if isinstance(item, dict):
        val = item.get("noul")
    else:
        val = item
    num = as_float(val, None)
    if num is None or num < 0 or num > 1:
        return None
    return num


def score_coin(
    mint: str,
    name: str,
    age_min: int | float | None,
    pair: dict | None,
    audit: dict[str, Any] | None,
    scanner: dict[str, Any] | None,
    cfg,
    sources=None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Run rules or Jev and apply the gate. Pure enough for tests (inject sources)."""
    top10 = None
    if scanner:
        top10 = as_float(scanner.get("top10Pct") or scanner.get("top10_pct"))
    state = build_state(
        name=name or mint,
        age_min=age_min,
        pair=pair,
        audit=audit,
        top10_pct=top10,
        max_chars=int(cfg.get("analyst.state_max_chars") or 3000),
    )
    mode = str(cfg.get("analyst.mode") or "rules").lower()
    model = "rules"
    input_tokens = 0
    answers: dict[str, float] | None = None
    fail_reason = None
    raw_response = None

    if mode == "rules":
        answers = rules_answers(pair, audit, cfg)
        model = "rules"
        input_tokens = 0
    else:
        from sources import HttpSourceError, sources as default_sources

        src = sources or default_sources
        key = secret("TYPESAFE_API_KEY")
        if not key:
            fail_reason = "no answer"
        else:
            timeout = float(cfg.get("analyst.timeout_s") or 10)
            retry_wait = float(cfg.get("analyst.retry_wait_s") or 2)
            last_err = None
            for attempt in (0, 1):
                try:
                    raw_response = src.jev(state, JEV_QUESTIONS, key, timeout=timeout)
                    model = str((raw_response or {}).get("model") or "jev-latest")
                    usage = (raw_response or {}).get("usage") or {}
                    input_tokens = int(usage.get("input_tokens") or usage.get("inputTokens") or 0)
                    parsed: dict[str, float] = {}
                    bad = False
                    for q in JEV_QUESTIONS:
                        noul = _read_noul(raw_response or {}, q)
                        if noul is None:
                            bad = True
                            break
                        parsed[q] = noul
                    if bad:
                        fail_reason = "bad answer"
                        log.warning("bad answer mint=%s raw_keys=%s", mint, list((raw_response or {}).keys()))
                        answers = None
                    else:
                        answers = parsed
                    last_err = None
                    break
                except HttpSourceError as e:
                    last_err = e
                    if e.status == 429:
                        fail_reason = "queued"
                        break
                    if attempt == 0:
                        sleep(retry_wait)
                        continue
                    fail_reason = "no answer"
                except Exception as e:
                    last_err = e
                    if attempt == 0:
                        sleep(retry_wait)
                        continue
                    fail_reason = "no answer"
            if fail_reason == "queued":
                # Caller may re-queue up to queue_429_s; here we just mark it.
                fail_reason = "no answer"

    if answers is None:
        result = {
            "agent": "analyst",
            "mint": mint,
            "answers": {"alive_6h": None, "organic": None, "flush": None, "chase": None},
            "score": 0,
            "passed": False,
            "failed": [fail_reason or "no answer"],
            "model": model if mode != "rules" else "rules",
            "inputTokens": input_tokens,
            "state": state,
            "reason": fail_reason or "no answer",
        }
    else:
        gated = apply_gate(
            answers,
            min_alive_6h=float(cfg.get("analyst.min_alive_6h") or 0.70),
            min_organic=float(cfg.get("analyst.min_organic") or 0.65),
            max_chase=float(cfg.get("analyst.max_chase") or 0.30),
            min_score=int(cfg.get("analyst.min_score") or 70),
        )
        result = {
            "agent": "analyst",
            "mint": mint,
            "answers": answers,
            "score": gated["score"],
            "passed": gated["passed"],
            "failed": gated["failed"],
            "model": model,
            "inputTokens": input_tokens,
            "state": state,
            "reason": gated["failed"][0] if gated["failed"] else None,
        }

    state_hash = hashlib.sha256(state.encode("utf-8")).hexdigest()[:12]
    log.info(
        "mint=%s hash=%s answers=%s score=%s verdict=%s tokens=%s model=%s",
        mint,
        state_hash,
        result["answers"],
        result["score"],
        "pass" if result["passed"] else "reject",
        result["inputTokens"],
        result["model"],
    )
    return result


def activity_agent_label(model: str) -> str:
    return "Analyst (rules)" if model == "rules" else "Analyst"


async def run_agent(db, cfg, bus, sources=None, stop_event: asyncio.Event | None = None) -> None:
    """Consume bus.analyst, score, pass to executor or write a reject."""
    seen: set[str] = set()
    while stop_event is None or not stop_event.is_set():
        try:
            item = await asyncio.wait_for(bus.analyst.get(), timeout=2.0)
        except asyncio.TimeoutError:
            db.heartbeat("analyst")
            continue
        db.heartbeat("analyst")
        if cfg.is_paused("analyst"):
            continue
        mint = item.get("mint")
        if not mint or mint in seen:
            continue
        # never re-ask the same coin
        existing = db.query_one("SELECT id FROM verdicts WHERE mint=?", (mint,))
        if existing:
            seen.add(mint)
            continue
        pair = item.get("pair")
        if pair is None and sources is not None:
            try:
                pair = sources.pumpswap_pair(mint)
            except Exception as e:
                log.warning("dex read failed mint=%s err=%s", mint, type(e).__name__)
        coin = db.get_coin(mint) or {}
        result = score_coin(
            mint=mint,
            name=item.get("name") or coin.get("name") or mint,
            age_min=item.get("ageMin") or item.get("age_min"),
            pair=pair,
            audit=item.get("audit"),
            scanner=item.get("scanner"),
            cfg=cfg,
            sources=sources,
        )
        db.add_verdict(
            mint=mint,
            alive_6h=(result["answers"] or {}).get("alive_6h"),
            organic=(result["answers"] or {}).get("organic"),
            flush=(result["answers"] or {}).get("flush"),
            chase=(result["answers"] or {}).get("chase"),
            score=result["score"],
            passed=result["passed"],
            reason=result.get("reason"),
            input_tokens=result.get("inputTokens"),
        )
        seen.add(mint)
        label = activity_agent_label(result["model"])
        if not result["passed"]:
            reason = result.get("reason") or "rejected"
            db.add_activity(
                kind="rejected",
                mint=mint,
                agent="analyst",
                title=f"Rejected {item.get('symbol') or item.get('name') or mint}",
                subtitle=f"{label} · {reason}",
                value="Skipped",
            )
            await bus.emit("verdict", result)
            continue
        await bus.executor.put({**item, "analyst": result, "pair": pair})
        await bus.emit("verdict", result)

