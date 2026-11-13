"""Jupiter field mapping, auditor layer 9 (fail-closed), and PAPER price check."""

from __future__ import annotations

import json
from pathlib import Path

from agents.auditor import audit_mint
from fields import (
    JUPITER_MINT_USED,
    JUPITER_PRICE,
    JUPITER_TOKEN,
    get_in,
    jup_price_float,
    jup_token_float,
    jup_token_value,
    jupiter_price_entry,
    jupiter_token_entry,
    price_divergence_pct,
)
from sources import maybe_note_jup_price

RAW = Path(__file__).resolve().parents[1] / "logs" / "raw"
PRICE_SAMPLE = json.loads((RAW / "jupiter_price.json").read_text())
TOKEN_SAMPLE = json.loads((RAW / "jupiter_token_search.json").read_text())


def _scanner():
    return {
        "pool": "Pool111",
        "liquidityUsd": 80_000,
        "currentLiquidityUsd": 80_000,
        "migratedAt": "2026-11-10T06:00:00Z",
        "creator": "Creator111",
        "creator_balance": 10,
    }


def _coin():
    return {
        "creator": "Creator111",
        "creator_balance_at_migration": 10,
        "pool": "Pool111",
        "migrated_at": "2026-11-10T06:00:00Z",
    }


class PassingRpc:
    def get_account_info(self, mint: str):
        return {
            "jsonrpc": "2.0",
            "result": {
                "value": {
                    "owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                    "data": {
                        "parsed": {
                            "info": {
                                "mintAuthority": None,
                                "freezeAuthority": None,
                                "supply": "1000000000",
                                "decimals": 6,
                            }
                        }
                    },
                }
            },
        }

    def get_token_supply(self, mint: str):
        return {"result": {"value": {"uiAmount": 1000.0, "amount": "1000000000", "decimals": 6}}}

    def get_token_accounts_by_owner(self, *a, **k):
        return {
            "result": {
                "value": [
                    {
                        "account": {
                            "data": {"parsed": {"info": {"tokenAmount": {"uiAmount": 10}}}}
                        }
                    }
                ]
            }
        }

    def get_token_largest_accounts(self, mint: str):
        return {"result": {"value": [{"address": "holder1", "uiAmount": 100}]}}

    def get_program_accounts(self, *a, **k):
        return {"result": []}


class JupSources:
    def __init__(self, token=None, raise_token=False, price=None):
        self.token = token
        self.raise_token = raise_token
        self.price = price if price is not None else {}
        self.token_calls = 0
        self.price_calls = 0

    def rugcheck_report(self, mint):
        return {"markets": [{"pubkey": "Pool111", "lp": {"lpLockedPct": 100}}]}

    def rugcheck_graph(self, mint):
        return [{"nodes": [{"id": "a", "holdings": 1}], "links": []}]

    def jupiter_token(self, mint):
        self.token_calls += 1
        if self.raise_token:
            raise RuntimeError("jupiter down")
        return self.token

    def jupiter_price(self, mints):
        self.price_calls += 1
        return self.price


def _ok_token(**overrides):
    token = {
        "id": "MintPass1",
        "organicScore": 50,
        "audit": {
            "mintAuthorityDisabled": True,
            "freezeAuthorityDisabled": True,
            "topHoldersPercentage": 16.0,
            "devBalancePercentage": 1.0,
        },
    }
    if "audit" in overrides:
        token["audit"] = {**token["audit"], **overrides.pop("audit")}
    token.update(overrides)
    return token


def _audit(cfg, sources, **kw):
    return audit_mint(
        mint=kw.get("mint", "MintPass1"),
        scanner=_scanner(),
        coin=_coin(),
        cfg=cfg,
        rpc=PassingRpc(),
        sources=sources,
        sleep=lambda _s: None,
    )


def test_price_sample_keys_match_mapping():
    entry = jupiter_price_entry(PRICE_SAMPLE, JUPITER_MINT_USED)
    assert entry is not None
    for field, path in JUPITER_PRICE.items():
        assert path is not None
        assert get_in(entry, path) is not None, field
    assert jup_price_float(entry, "usdPrice") > 0
    assert jup_price_float(entry, "liquidity") > 0
    assert isinstance(jup_price_float(entry, "priceChange24h"), float)


def test_token_sample_keys_match_mapping():
    token = jupiter_token_entry(TOKEN_SAMPLE, JUPITER_MINT_USED)
    assert token is not None
    # Present in the captured sample
    for field in (
        "organicScore",
        "audit.mintAuthorityDisabled",
        "audit.freezeAuthorityDisabled",
        "audit.topHoldersPercentage",
        "audit.devBalancePercentage",
    ):
        assert JUPITER_TOKEN[field] is not None
        assert jup_token_value(token, field) is not None, field
    # Documented as absent on this sample — do not invent them
    assert JUPITER_TOKEN["isVerified"] is None
    assert JUPITER_TOKEN["audit.isSus"] is None
    assert "isVerified" not in token
    assert "isSus" not in (token.get("audit") or {})
    assert jup_token_value(token, "audit.mintAuthorityDisabled") is True
    assert jup_token_value(token, "audit.freezeAuthorityDisabled") is True
    assert jup_token_float(token, "audit.topHoldersPercentage") > 0
    # Reader still looks up isSus if a later payload has it
    assert jup_token_value({**token, "audit": {**token["audit"], "isSus": True}}, "audit.isSus") is True


def test_jup_layer_pass(cfg):
    result = _audit(cfg, JupSources(token=_ok_token()))
    assert result["passed"] is True
    assert result["layersChecked"] == 9
    layers = {l["n"]: l for l in result["layers"]}
    assert layers[9]["pass"] is True
    assert layers[9]["value"] == "ok"
    assert layers[9]["source"] == "jupiter"


def test_jup_issus_is_red(cfg):
    token = _ok_token(audit={"isSus": True})
    result = _audit(cfg, JupSources(token=token))
    assert result["passed"] is False
    assert result["reason"] == "jup isSus"
    assert result["layersChecked"] == 9
    assert {l["n"]: l for l in result["layers"]}[9]["pass"] is False


def test_jup_no_data_is_red(cfg):
    result = _audit(cfg, JupSources(token=None))
    assert result["passed"] is False
    assert result["reason"] == "no data: jup"
    result = _audit(cfg, JupSources(raise_token=True))
    assert result["passed"] is False
    assert result["reason"] == "no data: jup"


def test_jup_mint_not_disabled_is_red_even_if_rpc_revoked(cfg):
    token = _ok_token(audit={"mintAuthorityDisabled": False})
    result = _audit(cfg, JupSources(token=token))
    assert result["passed"] is False
    assert result["reason"] == "jup mint not revoked"
    token = _ok_token(audit={"freezeAuthorityDisabled": False})
    result = _audit(cfg, JupSources(token=token))
    assert result["passed"] is False
    assert result["reason"] == "jup freeze not revoked"


def test_jup_missing_authority_fields_fail_closed(cfg):
    token = _ok_token()
    del token["audit"]["mintAuthorityDisabled"]
    result = _audit(cfg, JupSources(token=token))
    assert result["passed"] is False
    assert result["reason"] == "no data: jup mint"


def test_jup_top_holders_over_threshold_is_red(cfg):
    token = _ok_token(audit={"topHoldersPercentage": 25.85})
    result = _audit(cfg, JupSources(token=token))
    assert result["passed"] is False
    assert "jup top holders" in result["reason"]


def test_jup_layer_off_skips_jupiter_and_passes_8(cfg):
    cfg.set("auditor.jup_layer", False, who="test")
    sources = JupSources(raise_token=True)
    result = _audit(cfg, sources)
    assert sources.token_calls == 0
    assert result["passed"] is True
    assert result["layersChecked"] == 8
    assert all(l["n"] != 9 for l in result["layers"])


def test_price_divergence_helper():
    assert abs(price_divergence_pct(1.0, 1.06) - 6.0) < 1e-9
    assert abs(price_divergence_pct(1.0, 1.04) - 4.0) < 1e-9
    assert price_divergence_pct(1.0, None) is None
    assert price_divergence_pct(None, 1.0) is None


class _MemDb:
    def __init__(self):
        self.activity = []

    def add_activity(self, **kw):
        self.activity.append(kw)
        return len(self.activity)


def test_missing_jup_price_does_not_block_or_log(cfg, db):
    sources = JupSources(price={})  # mint omitted from Jupiter
    note = maybe_note_jup_price(db, sources, "MintFresh1", 0.001, "entry", "executor")
    assert note is None
    # no activity rows of kind note
    rows = db.query("SELECT * FROM activity")
    assert rows == []


def test_jup_entry_divergence_over_5_logs_activity(cfg):
    mem = _MemDb()
    mint = "MintDiv1"
    sources = JupSources(price={mint: {"usdPrice": 1.10}})
    note = maybe_note_jup_price(mem, sources, mint, 1.00, "entry", "executor")
    assert note is not None
    assert abs(note["divergence_pct"] - 10.0) < 1e-9
    assert len(mem.activity) == 1
    assert mem.activity[0]["kind"] == "note"
    assert "10.0%" in mem.activity[0]["title"]


def test_jup_entry_within_5_does_not_log(cfg):
    mem = _MemDb()
    mint = "MintOk1"
    sources = JupSources(price={mint: {"usdPrice": 1.04}})
    note = maybe_note_jup_price(mem, sources, mint, 1.00, "entry", "executor")
    assert abs(note["divergence_pct"] - 4.0) < 1e-9
    assert mem.activity == []
