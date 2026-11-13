"""HTTP sources: DexScreener, RugCheck, and read-only Jupiter."""

from __future__ import annotations

import asyncio
import os
import random
import threading
import time
from collections import deque
from typing import Any, Callable
from urllib.parse import quote

import httpx

from fields import (
    dexscreener_pair,
    jupiter_price_entry,
    jup_price_float,
    price_divergence_pct,
)
from util import agent_logger

log = agent_logger("sources")

DEX_TOKENS = "https://api.dexscreener.com/tokens/v1/solana/{mints}"
RUGCHECK_REPORT = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"
RUGCHECK_GRAPH = "https://api.rugcheck.xyz/v1/tokens/{mint}/insiders/graph"
JEV_URL = "https://api.typesafe.ai/v1/systemone"
PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"
JUPITER_PRICE_URL = "https://api.jup.ag/price/v3"
JUPITER_TOKEN_SEARCH_URL = "https://api.jup.ag/tokens/v2/search"

# Free-plan Jupiter: 1 request/s and 60 requests/min, shared by all callers.
_JUP_MIN_INTERVAL_S = 1.0
_JUP_RPM = 60
_JUP_WINDOW_S = 60.0
_jup_sync_lock = threading.Lock()
_jup_times: deque[float] = deque()
_jup_async_lock: asyncio.Lock | None = None


def _jup_api_key() -> str:
    return os.environ.get("JUPITER_API_KEY") or ""


def _jup_headers() -> dict[str, str]:
    key = _jup_api_key()
    return {"x-api-key": key} if key else {}


def _jup_prune(now: float) -> None:
    while _jup_times and now - _jup_times[0] >= _JUP_WINDOW_S:
        _jup_times.popleft()


def _jup_wait_needed(now: float) -> float:
    _jup_prune(now)
    wait = 0.0
    if _jup_times:
        wait = max(wait, _JUP_MIN_INTERVAL_S - (now - _jup_times[-1]))
    if len(_jup_times) >= _JUP_RPM:
        wait = max(wait, _JUP_WINDOW_S - (now - _jup_times[0]))
    return wait


def _jup_wait_sync() -> None:
    """Block until a Jupiter call is allowed (1 rps / 60 rpm)."""
    with _jup_sync_lock:
        while True:
            wait = _jup_wait_needed(time.monotonic())
            if wait <= 0:
                _jup_times.append(time.monotonic())
                return
            time.sleep(wait)


async def _jup_wait_async() -> None:
    global _jup_async_lock
    if _jup_async_lock is None:
        _jup_async_lock = asyncio.Lock()
    async with _jup_async_lock:
        while True:
            wait = _jup_wait_needed(time.monotonic())
            if wait <= 0:
                _jup_times.append(time.monotonic())
                return
            await asyncio.sleep(wait)


class HttpSourceError(Exception):
    def __init__(self, message: str, status: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class HttpSources:
    def __init__(
        self,
        timeout: float = 15.0,
        transport=None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.timeout = timeout
        self._transport = transport
        self._sleep = sleep or time.sleep

    def _client(self, timeout: float | None = None) -> httpx.Client:
        kw: dict[str, Any] = {"timeout": timeout or self.timeout}
        if self._transport is not None:
            kw["transport"] = self._transport
        return httpx.Client(**kw)

    def dexscreener_tokens(self, mints: list[str]) -> Any:
        if not mints:
            return []
        url = DEX_TOKENS.format(mints=",".join(mints))
        with self._client() as c:
            res = c.get(url)
            if res.status_code == 429:
                raise HttpSourceError("dexscreener 429", status=429, body=res.text)
            res.raise_for_status()
            return res.json()

    def pumpswap_pair(self, mint: str) -> dict | None:
        payload = self.dexscreener_tokens([mint])
        return dexscreener_pair(payload)

    def pumpswap_pairs(self, mints: list[str]) -> dict[str, dict]:
        """mint -> selected PumpSwap pair (most liquidity)."""
        payload = self.dexscreener_tokens(mints)
        pairs = payload if isinstance(payload, list) else []
        by_mint: dict[str, list[dict]] = {}
        for p in pairs:
            if not isinstance(p, dict):
                continue
            mint = ((p.get("baseToken") or {}).get("address")) or ""
            by_mint.setdefault(mint, []).append(p)
        out: dict[str, dict] = {}
        for mint, group in by_mint.items():
            picked = dexscreener_pair(group)
            if picked:
                out[mint] = picked
        return out

    def any_dex_pairs(self, mints: list[str]) -> dict[str, dict]:
        """mint -> most liquid Solana pair on any DEX (copy trades are not only PumpSwap)."""
        payload = self.dexscreener_tokens(mints)
        pairs = payload if isinstance(payload, list) else []
        out: dict[str, dict] = {}

        def liq(p: dict) -> float:
            try:
                return float(((p.get("liquidity") or {}).get("usd")) or 0)
            except (TypeError, ValueError, AttributeError):
                return 0.0

        for p in pairs:
            if not isinstance(p, dict) or p.get("chainId", "solana") != "solana":
                continue
            mint = ((p.get("baseToken") or {}).get("address")) or ""
            if mint and (mint not in out or liq(p) > liq(out[mint])):
                out[mint] = p
        return out

    def rugcheck_report(self, mint: str) -> dict:
        url = RUGCHECK_REPORT.format(mint=quote(mint, safe=""))
        with self._client() as c:
            res = c.get(url)
            res.raise_for_status()
            return res.json()

    def rugcheck_graph(self, mint: str) -> Any:
        url = RUGCHECK_GRAPH.format(mint=quote(mint, safe=""))
        with self._client() as c:
            res = c.get(url)
            res.raise_for_status()
            return res.json()

    def jev(self, state: str, questions: dict, api_key: str, timeout: float) -> dict:
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        body = {"model": "jev-latest", "state": state, "questions": questions}
        with self._client(timeout=timeout) as c:
            res = c.post(JEV_URL, headers=headers, json=body)
            if res.status_code == 429:
                raise HttpSourceError("jev 429", status=429, body=res.text)
            res.raise_for_status()
            return res.json()

    def _jupiter_get(self, url: str, params: dict[str, str], retries: int = 3) -> Any:
        """GET a Jupiter read-only endpoint. Key is sent only as x-api-key."""
        last: Exception | None = None
        delay = 1.0
        for i in range(max(1, retries)):
            _jup_wait_sync()
            try:
                with self._client() as c:
                    res = c.get(url, headers=_jup_headers(), params=params)
                    if res.status_code == 429:
                        retry_after = res.headers.get("Retry-After")
                        try:
                            wait = float(retry_after) if retry_after else delay
                        except ValueError:
                            wait = delay
                        wait = min(max(wait, 0.0) + random.random(), 20.0)
                        last = HttpSourceError("jupiter 429", status=429, body=res.text)
                        if i + 1 < retries:
                            log.warning("jupiter 429, backing off")
                            self._sleep(wait)
                            delay = min(delay * 2, 16.0)
                            continue
                        raise last
                    if res.status_code >= 400:
                        raise HttpSourceError(
                            f"jupiter HTTP {res.status_code}",
                            status=res.status_code,
                            body=res.text,
                        )
                    return res.json()
            except (httpx.HTTPError, ValueError) as e:
                last = HttpSourceError(f"jupiter {type(e).__name__}")
                if i + 1 < retries:
                    self._sleep(delay)
                    delay = min(delay * 2, 16.0)
                    continue
                raise last
        raise last or HttpSourceError("jupiter failed")

    def jupiter_price(self, mints: list[str]) -> dict:
        """GET /price/v3?ids=...  Returns the object keyed by mint (may omit some)."""
        wanted = [m for m in mints if m]
        if not wanted:
            return {}
        payload = self._jupiter_get(JUPITER_PRICE_URL, {"ids": ",".join(wanted)})
        return payload if isinstance(payload, dict) else {}

    def jupiter_token(self, mint: str) -> dict | None:
        """GET /tokens/v2/search?query={mint}. Returns the matching token or None."""
        if not mint:
            return None
        from fields import jupiter_token_entry

        payload = self._jupiter_get(JUPITER_TOKEN_SEARCH_URL, {"query": mint})
        return jupiter_token_entry(payload, mint)


# --- Four-agent desk (2026-11-13): free, read-only market data, politely rate-limited ---------
JUPITER_TRENDING_URL = "https://api.jup.ag/tokens/v2/{category}/{interval}"
GECKO_OHLCV_URL = "https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/ohlcv/minute"
_GECKO_GAP_S = 2.2  # GeckoTerminal free tier: 30 calls/min
_RUG_GAP_S = 1.5
_gap_lock = threading.Lock()
_gap_last: dict[str, float] = {}


def _gap_wait(name: str, gap: float, sleep: Callable[[float], None] = time.sleep) -> None:
    with _gap_lock:
        now = time.monotonic()
        wait = gap - (now - _gap_last.get(name, 0.0))
        _gap_last[name] = now + max(0.0, wait)
    if wait > 0:
        sleep(wait)


def _jupiter_trending(self, category: str = "toptrending", interval: str = "5m", limit: int = 50) -> list[dict]:
    """GET /tokens/v2/{toptrending|toptraded|toporganicscore}/{5m|1h|6h|24h}. Read-only."""
    url = JUPITER_TRENDING_URL.format(category=category, interval=interval)
    payload = self._jupiter_get(url, {"limit": str(int(limit))})
    return [t for t in payload if isinstance(t, dict)] if isinstance(payload, list) else []


def _jupiter_tokens(self, mints: list[str]) -> list[dict]:
    """GET /tokens/v2/search?query=m1,m2,... (ticker tape). Read-only."""
    wanted = [m for m in mints if m]
    if not wanted:
        return []
    payload = self._jupiter_get(JUPITER_TOKEN_SEARCH_URL, {"query": ",".join(wanted[:100])})
    return [t for t in payload if isinstance(t, dict)] if isinstance(payload, list) else []


def _gecko_ohlcv(self, pool: str, limit: int = 90) -> list[list[float]]:
    """1-minute candles [t, o, h, l, c, v] oldest first, from free GeckoTerminal. [] on any error."""
    if not pool:
        return []
    _gap_wait("gecko", _GECKO_GAP_S, self._sleep)
    try:
        with self._client() as c:
            res = c.get(GECKO_OHLCV_URL.format(pool=quote(pool, safe="")),
                        params={"aggregate": 1, "limit": int(limit), "currency": "usd", "token": "base"},
                        headers={"accept": "application/json"})
        if res.status_code != 200:
            return []
        rows = (((res.json() or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        out = [[float(x) for x in r[:6]] for r in rows if isinstance(r, list) and len(r) >= 6]
        return sorted(out, key=lambda r: r[0])
    except Exception:
        return []


def _rugcheck_report_polite(self, mint: str) -> dict:
    _gap_wait("rugcheck", _RUG_GAP_S, self._sleep)
    return self.rugcheck_report(mint)


HttpSources.jupiter_trending = _jupiter_trending
HttpSources.jupiter_tokens = _jupiter_tokens
HttpSources.gecko_ohlcv = _gecko_ohlcv
HttpSources.rugcheck_report_polite = _rugcheck_report_polite


def maybe_note_jup_price(
    db,
    sources: HttpSources | None,
    mint: str,
    dex_price: float | None,
    side: str,
    agent: str,
) -> dict[str, Any] | None:
    """PAPER entry/exit cross-check vs Jupiter. Missing Jupiter price does not block."""
    if sources is None or not mint or dex_price is None:
        return None
    try:
        payload = sources.jupiter_price([mint])
        entry = jupiter_price_entry(payload, mint)
        jup_px = jup_price_float(entry, "usdPrice")
    except Exception:
        return None
    if jup_px is None:
        return None
    div = price_divergence_pct(dex_price, jup_px)
    if div is None or div <= 5.0:
        return {"jupiter_price": jup_px, "dex_price": float(dex_price), "divergence_pct": div or 0.0}
    title = f"Jupiter {side} price diverged {div:.1f}%"
    subtitle = f"Dex ${float(dex_price):.6g} · Jup ${jup_px:.6g}"
    try:
        db.add_activity(
            kind="note",
            mint=mint,
            agent=agent,
            title=title,
            subtitle=subtitle,
            value=f"{div:.1f}%",
        )
    except Exception:
        log.warning("could not record jup divergence for mint=%s", mint)
    log.warning(
        "jup %s divergence mint=%s pct=%.1f dex=%s jup=%s",
        side,
        mint,
        div,
        dex_price,
        jup_px,
    )
    return {"jupiter_price": jup_px, "dex_price": float(dex_price), "divergence_pct": div}


sources = HttpSources()
