"""Field map from the STEP 2 raw samples in logs/raw/.

Mint used for every HTTP/RPC sample except getSlot and the PumpPortal ack:
    BEd8GoJccoDA9gpfYdxqGupVFZwKrMWWMHnJYqnrpump  (shiken / "shiken cult")

PumpPortal wss://pumpportal.fun/api/data accepted subscribeMigration
(ack in logs/raw/pumpportal_ack.json) but sent no migration in a 10-minute
wait, so every migration field is None. The mint above is a recently
graduated pump.fun token with a PumpSwap pair, found via
GET https://api.dexscreener.com/latest/dex/search?q=pumpswap.

Paths are dotted from the root of the named raw JSON file. A path is only
listed if that key appeared in the file. Missing fields stay None.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Sample identity
# ---------------------------------------------------------------------------
MINT_USED = "BEd8GoJccoDA9gpfYdxqGupVFZwKrMWWMHnJYqnrpump"
RPC_URL = "https://api.mainnet-beta.solana.com"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
# This mint's owner (getAccountInfo result.value.owner) is Token-2022.
# Classic Token getProgramAccounts returned [].

# ---------------------------------------------------------------------------
# Migration (PumpPortal websocket message)
# Source file: none. Only logs/raw/pumpportal_ack.json exists:
#   {"message": "Subscribed to 'migration' events."}
# ---------------------------------------------------------------------------
MIGRATION = {
    "mint": None,        # no migration message arrived
    "pool": None,        # no migration message arrived
    "signature": None,   # no migration message arrived
    "creator": None,     # no migration message arrived
    "name": None,        # no migration message arrived
    "symbol": None,      # no migration message arrived
}

# ---------------------------------------------------------------------------
# DexScreener  GET /tokens/v1/solana/{mint}
# Source: logs/raw/dexscreener_pair.json
# The body is a JSON list of pair objects, not {"pairs": [...]}.
# This mint had a single pair, dexId == "pumpswap".
# If several pairs appear, agents should pick dexId == "pumpswap", and if
# several of those, the one with the highest liquidity.usd.
# Prefix below is that selected list element.
# ---------------------------------------------------------------------------
DEXSCREENER = {
    "pairAddress": "pairAddress",
    "dexId": "dexId",
    "priceUsd": "priceUsd",                  # string in the sample, e.g. "0.001724"
    "liquidity.usd": "liquidity.usd",
    "marketCap": "marketCap",
    "volume.m5": "volume.m5",
    "volume.h1": "volume.h1",
    "volume.h24": "volume.h24",
    "txns.m5.buys": "txns.m5.buys",
    "txns.m5.sells": "txns.m5.sells",
    "txns.h1.buys": "txns.h1.buys",
    "txns.h1.sells": "txns.h1.sells",
    "priceChange.m5": "priceChange.m5",
    "priceChange.h1": "priceChange.h1",
    "priceChange.h24": "priceChange.h24",
    "pairCreatedAt": "pairCreatedAt",        # unix milliseconds
    "baseToken.name": "baseToken.name",
    "baseToken.symbol": "baseToken.symbol",
    "baseToken.address": "baseToken.address",
}

# ---------------------------------------------------------------------------
# RugCheck report  GET /v1/tokens/{mint}/report
# Source: logs/raw/rugcheck_report.json
# ---------------------------------------------------------------------------
RUGCHECK_REPORT = {
    "creator": "creator",
    # There is no key named "pool". The pool/pair id is markets[i].pubkey
    # (equals DexScreener pairAddress for the pump_fun_amm market).
    "markets.pool": "markets[].pubkey",
    "markets.marketType": "markets[].marketType",  # sample: "pump_fun_amm"
    "LP locked pct": "markets[].lp.lpLockedPct",   # 0-100; 100 on the PumpSwap market
    "LP burned pct": None,  # no lpBurned / lpBurnedPct / burned key in the report
    # Nearby LP fields (not a burned percent): markets[].lp.lpLocked,
    # lpUnlocked, lpTotalSupply, currentSupply, mintLPAccount.supply
    "totalSupply": None,  # no key named totalSupply; raw supply is at token.supply
    "token.supply": "token.supply",
    "holders": None,  # no top-level "holders"; count is at totalHolders
    "totalHolders": "totalHolders",
    # markets[].lp.holders was present but null in this sample
}

# ---------------------------------------------------------------------------
# RugCheck insiders graph  GET /v1/tokens/{mint}/insiders/graph
# Source: logs/raw/rugcheck_insiders_graph.json
# The body is a JSON list of networks, not a top-level {nodes, edges} object.
# Each element: {net_id, network_type, nodes, links, relatedMint}
# ---------------------------------------------------------------------------
RUGCHECK_GRAPH = {
    "nodes": "[].nodes",
    # node sub-keys observed: id, participant, holdings
    "nodes.id": "[].nodes[].id",
    "nodes.participant": "[].nodes[].participant",
    "nodes.holdings": "[].nodes[].holdings",
    "edges": None,  # no "edges" key in the sample
    # The graph uses "links" (not "edges"), each {source, target}:
    "links": "[].links",
    "links.source": "[].links[].source",
    "links.target": "[].links[].target",
}

# ---------------------------------------------------------------------------
# RPC
# getAccountInfo (jsonParsed): logs/raw/rpc_getAccountInfo.json
# getTokenSupply:              logs/raw/rpc_getTokenSupply.json
# getTokenLargestAccounts:     failed — see note below
# getProgramAccounts Token-2022, jsonParsed:
#                              logs/raw/rpc_getProgramAccounts_token2022.json
# result is a list of {pubkey, account:{data.parsed.info...}}
# ---------------------------------------------------------------------------
RPC = {
    "mintAuthority": "result.value.data.parsed.info.mintAuthority",  # getAccountInfo; null in sample
    "freezeAuthority": "result.value.data.parsed.info.freezeAuthority",  # getAccountInfo; null in sample
    "supply": "result.value.data.parsed.info.supply",  # getAccountInfo; string amount
    "decimals": "result.value.data.parsed.info.decimals",  # getAccountInfo
    # getTokenSupply (same figures, different shape — no key named "supply"):
    "getTokenSupply.amount": "result.value.amount",
    "getTokenSupply.decimals": "result.value.decimals",
    "getTokenSupply.uiAmount": "result.value.uiAmount",
    "getTokenSupply.uiAmountString": "result.value.uiAmountString",
    # delegate / tokenAmount live on token accounts, not the mint account.
    # Observed on Token-2022 getProgramAccounts accounts (jsonParsed).
    # "delegate" and "delegatedAmount" keys are omitted when unset.
    "delegate": "result[].account.data.parsed.info.delegate",
    "delegatedAmount": "result[].account.data.parsed.info.delegatedAmount",
    "tokenAmount": "result[].account.data.parsed.info.tokenAmount",
    "tokenAmount.amount": "result[].account.data.parsed.info.tokenAmount.amount",
    "tokenAmount.decimals": "result[].account.data.parsed.info.tokenAmount.decimals",
    "tokenAmount.uiAmount": "result[].account.data.parsed.info.tokenAmount.uiAmount",
    "tokenAmount.uiAmountString": "result[].account.data.parsed.info.tokenAmount.uiAmountString",
}

# getTokenLargestAccounts against https://api.mainnet-beta.solana.com
# returned this body on every attempt (6 retries after the first 429):
#   {"jsonrpc":"2.0","error":{"code":429,"message":"Too many requests for a specific RPC call"},"id":1}
# so no largest-accounts field paths were observed.
GET_TOKEN_LARGEST_ACCOUNTS = {
    "error": "result was not returned; error.code=429, error.message='Too many requests for a specific RPC call'",
}


def get_in(obj: Any, path: str, default: Any = None) -> Any:
    """Walk a dotted path. '[]' / '[0]' index into lists (first element)."""
    if obj is None or not path:
        return default
    cur = obj
    for part in path.replace("[0]", ".0").replace("[]", ".0").split("."):
        if cur is None:
            return default
        if isinstance(cur, list):
            if part.isdigit() and int(part) < len(cur):
                cur = cur[int(part)]
            else:
                return default
        elif isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur


def dexscreener_pair(payload: Any, dex_id: str = "pumpswap") -> dict | None:
    """Pick the PumpSwap pair from a /tokens/v1/solana/{mint} list (most liquidity.usd if several)."""
    pairs = payload if isinstance(payload, list) else None
    if not pairs:
        return None
    wanted = [p for p in pairs if isinstance(p, dict) and p.get("dexId") == dex_id]
    if not wanted:
        return None

    def liq(p: dict) -> float:
        usd = (p.get("liquidity") or {}).get("usd") if isinstance(p.get("liquidity"), dict) else 0
        try:
            return float(usd or 0)
        except (TypeError, ValueError):
            return 0.0

    return max(wanted, key=liq)


def rugcheck_market(report: dict, pool: str | None) -> dict | None:
    """Market whose pubkey equals the Scanner/DexScreener pool (pair address)."""
    markets = report.get("markets") if isinstance(report, dict) else None
    if not isinstance(markets, list) or not pool:
        return None
    for m in markets:
        if isinstance(m, dict) and m.get("pubkey") == pool:
            return m
    return None


def graph_networks(graph_payload: Any) -> list:
    """Insiders graph body is a list of networks with .nodes and .links (not .edges)."""
    return graph_payload if isinstance(graph_payload, list) else []


# ---------------------------------------------------------------------------
# Migration field candidates (UNVERIFIED — no raw sample yet)
# When the first real PumpPortal migration message arrives, Scanner saves it
# to logs/raw/pumpportal_migration.json and learn_migration_mapping() fills
# MIGRATION with the keys that were actually present.
# ---------------------------------------------------------------------------
MIGRATION_CANDIDATES_UNVERIFIED = {
    "mint": ["mint", "tokenMint", "token", "ca", "address", "mintAddress"],
    "pool": [
        "pool",
        "poolAddress",
        "poolId",
        "pumpSwapPool",
        "pumpswapPool",
        "market",
        "pair",
        "pairAddress",
        "bondingCurve",
        "bondingCurveKey",
    ],
    "signature": ["signature", "sig", "tx", "txSignature", "txn", "transaction"],
    "creator": [
        "creator",
        "traderPublicKey",
        "user",
        "dev",
        "owner",
        "deployer",
        "creatorAddress",
    ],
    "name": ["name", "tokenName", "coinName"],
    "symbol": ["symbol", "ticker", "tokenSymbol"],
    # Bonding curve (used to exclude the curve from top-10). Also UNVERIFIED.
    "curve": ["bondingCurveKey", "bondingCurve", "curve", "bonding_curve"],
}

# Filled at runtime once a real sample is seen (and persisted next to this file).
MIGRATION_VERIFIED: dict[str, str | None] = dict(MIGRATION)

_MIGRATION_STATE_PATH = __import__("pathlib").Path(__file__).resolve().parent / "logs" / "raw" / "pumpportal_migration_fields.json"


def _load_verified_migration() -> None:
    import json

    if not _MIGRATION_STATE_PATH.exists():
        return
    try:
        data = json.loads(_MIGRATION_STATE_PATH.read_text())
    except (OSError, ValueError):
        return
    if isinstance(data, dict):
        for k, v in data.items():
            if k in MIGRATION:
                MIGRATION[k] = v
                MIGRATION_VERIFIED[k] = v


_load_verified_migration()


def _first_present(obj: dict, keys: list[str]):
    for k in keys:
        if k in obj and obj[k] not in (None, ""):
            return k, obj[k]
        # dotted
        if "." in k:
            val = get_in(obj, k)
            if val not in (None, ""):
                return k, val
    return None, None


def read_migration(msg: Any) -> dict[str, Any]:
    """Defensive read of a PumpPortal migration message through the mapping.

    Unverified candidate keys are used only when MIGRATION[field] is still None.
    Every call returns the observed top-level keys (for logging).
    """
    out = {
        "mint": None,
        "pool": None,
        "signature": None,
        "creator": None,
        "name": None,
        "symbol": None,
        "curve": None,
        "keys": [],
        "unverified": [],
        "raw": msg,
    }
    if not isinstance(msg, dict):
        return out
    out["keys"] = sorted(str(k) for k in msg.keys())
    for field in ("mint", "pool", "signature", "creator", "name", "symbol"):
        path = MIGRATION.get(field)
        if path:
            val = get_in(msg, path) if not isinstance(path, str) or "." in str(path) else msg.get(path)
            if val not in (None, ""):
                out[field] = val
                continue
        key, val = _first_present(msg, MIGRATION_CANDIDATES_UNVERIFIED.get(field, []))
        if key is not None:
            out[field] = val
            out["unverified"].append(f"{field}:{key}")
    key, val = _first_present(msg, MIGRATION_CANDIDATES_UNVERIFIED.get("curve", []))
    if key is not None:
        out["curve"] = val
        out["unverified"].append(f"curve:{key}")
    return out


def learn_migration_mapping(msg: dict, raw_dir=None) -> dict[str, str | None]:
    """Persist the first real sample and freeze the keys that were present."""
    import json
    from pathlib import Path

    raw_path = Path(raw_dir) if raw_dir else Path(__file__).resolve().parent / "logs" / "raw"
    raw_path.mkdir(parents=True, exist_ok=True)
    sample = raw_path / "pumpportal_migration.json"
    if not sample.exists():
        sample.write_text(json.dumps(msg, indent=2, default=str))
    learned: dict[str, str | None] = {}
    for field, candidates in MIGRATION_CANDIDATES_UNVERIFIED.items():
        if field == "curve":
            continue
        key, val = _first_present(msg, candidates)
        learned[field] = key
        MIGRATION[field] = key
        MIGRATION_VERIFIED[field] = key
    state = raw_path / "pumpportal_migration_fields.json"
    state.write_text(json.dumps(learned, indent=2))
    return learned


def dex_value(pair: dict | None, field: str, default: Any = None) -> Any:
    """Read a DexScreener field through the mapping."""
    if not pair:
        return default
    path = DEXSCREENER.get(field, field)
    if path is None:
        return default
    return get_in(pair, path, default)


def dex_float(pair: dict | None, field: str, default: float | None = None) -> float | None:
    val = dex_value(pair, field, None)
    if val is None or val == "":
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def rpc_value(payload: dict | None, field: str, default: Any = None) -> Any:
    path = RPC.get(field, field)
    if path is None or payload is None:
        return default
    return get_in(payload, path, default)


def graph_holder_components(graph_payload: Any) -> list[list[str]]:
    """Connected components of wallets that hold this mint, joined by `links`.

    The insiders graph body is a list of networks; each has `nodes` and `links`
    (not `edges`). A node holds the mint when holdings > 0. Bundled size is
    the size of the biggest component.
    """
    from collections import defaultdict

    holders: set[str] = set()
    edges: list[tuple[str, str]] = []
    for net in graph_networks(graph_payload):
        if not isinstance(net, dict):
            continue
        for node in net.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            nid = node.get("id") or node.get("participant")
            holdings = node.get("holdings") or 0
            try:
                held = float(holdings)
            except (TypeError, ValueError):
                held = 0.0
            # participant is a bool in the sample; id is the wallet
            wallet = node.get("id")
            if wallet and held > 0:
                holders.add(str(wallet))
        for link in net.get("links") or []:
            if not isinstance(link, dict):
                continue
            a, b = link.get("source"), link.get("target")
            if a and b:
                edges.append((str(a), str(b)))

    adj: dict[str, set[str]] = defaultdict(set)
    for a, b in edges:
        if a in holders and b in holders and a != b:
            adj[a].add(b)
            adj[b].add(a)
    # include isolated holders
    seen: set[str] = set()
    components: list[list[str]] = []
    for h in holders:
        if h in seen:
            continue
        stack = [h]
        comp = []
        seen.add(h)
        while stack:
            cur = stack.pop()
            comp.append(cur)
            for nxt in adj.get(cur, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        components.append(comp)
    components.sort(key=len, reverse=True)
    return components

# ---------------------------------------------------------------------------
# Jupiter Price  GET https://api.jup.ag/price/v3?ids={mint}
# Source: logs/raw/jupiter_price.json
# Body is a JSON object keyed by mint (tokens with unreliable pricing are
# omitted entirely). Sample mint:
#     HYoM6KGcYhghx8zMRrU4DJT9xWrNn4jwGDGnDpFhpump  (Trump 1 / "Trump")
# Prefix below is that per-mint object, not the root.
# ---------------------------------------------------------------------------
JUPITER_MINT_USED = "HYoM6KGcYhghx8zMRrU4DJT9xWrNn4jwGDGnDpFhpump"
JUPITER_PRICE = {
    "usdPrice": "usdPrice",                  # float in the sample
    "liquidity": "liquidity",                # float USD
    "priceChange24h": "priceChange24h",      # float, can be negative
    "blockId": "blockId",
    "decimals": "decimals",
    "createdAt": "createdAt",
    "launchpad": "launchpad",                # present in this sample ("pump.fun")
}

# ---------------------------------------------------------------------------
# Jupiter Tokens  GET https://api.jup.ag/tokens/v2/search?query={mint}
# Source: logs/raw/jupiter_token_search.json
# Body is a JSON list of token objects. Pick the element whose `id` equals
# the mint (the sample had a single match).
# isVerified and audit.isSus did not appear on this sample: isSus is only
# present when true (per Jupiter docs); isVerified was simply absent.
# Those two mappings stay None until a sample actually contains them.
# Other audit fields below were present on this mint.
# ---------------------------------------------------------------------------
JUPITER_TOKEN = {
    "id": "id",
    "name": "name",
    "symbol": "symbol",
    "isVerified": None,                      # not in this sample
    "organicScore": "organicScore",
    "organicScoreLabel": "organicScoreLabel",
    "audit.isSus": None,                     # not in this sample (only when true)
    "audit.mintAuthorityDisabled": "audit.mintAuthorityDisabled",
    "audit.freezeAuthorityDisabled": "audit.freezeAuthorityDisabled",
    "audit.topHoldersPercentage": "audit.topHoldersPercentage",
    "audit.devBalancePercentage": "audit.devBalancePercentage",
}

# Runtime read paths: use these even when the mapping above is None, so a
# later payload that does include the key is not ignored.
JUPITER_TOKEN_READ = {
    "id": "id",
    "name": "name",
    "symbol": "symbol",
    "isVerified": "isVerified",
    "organicScore": "organicScore",
    "organicScoreLabel": "organicScoreLabel",
    "audit.isSus": "audit.isSus",
    "audit.mintAuthorityDisabled": "audit.mintAuthorityDisabled",
    "audit.freezeAuthorityDisabled": "audit.freezeAuthorityDisabled",
    "audit.topHoldersPercentage": "audit.topHoldersPercentage",
    "audit.devBalancePercentage": "audit.devBalancePercentage",
}


def jupiter_price_entry(payload: Any, mint: str) -> dict | None:
    """Per-mint object from GET /price/v3 (root is keyed by mint)."""
    if not isinstance(payload, dict) or not mint:
        return None
    entry = payload.get(mint)
    return entry if isinstance(entry, dict) else None


def jupiter_token_entry(payload: Any, mint: str | None = None) -> dict | None:
    """Matching token object from GET /tokens/v2/search (a list)."""
    items = payload if isinstance(payload, list) else ([payload] if isinstance(payload, dict) else [])
    if mint:
        for item in items:
            if isinstance(item, dict) and item.get("id") == mint:
                return item
        return None
    first = items[0] if items else None
    return first if isinstance(first, dict) else None


def jup_price_value(entry: dict | None, field: str, default: Any = None) -> Any:
    if not entry:
        return default
    path = JUPITER_PRICE.get(field, field)
    if path is None:
        return default
    return get_in(entry, path, default)


def jup_price_float(entry: dict | None, field: str, default: float | None = None) -> float | None:
    val = jup_price_value(entry, field, None)
    if val is None or val == "":
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def jup_token_value(token: dict | None, field: str, default: Any = None) -> Any:
    if not token:
        return default
    path = JUPITER_TOKEN_READ.get(field, JUPITER_TOKEN.get(field, field))
    if path is None:
        return default
    return get_in(token, path, default)


def jup_token_float(token: dict | None, field: str, default: float | None = None) -> float | None:
    val = jup_token_value(token, field, None)
    if val is None or val == "":
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def price_divergence_pct(dex_price: float | None, other_price: float | None) -> float | None:
    """Absolute percent difference of other vs DexScreener. None if either is missing."""
    if dex_price is None or other_price is None:
        return None
    try:
        dex = float(dex_price)
        other = float(other_price)
    except (TypeError, ValueError):
        return None
    if dex == 0:
        return None
    return abs(other - dex) / dex * 100.0
