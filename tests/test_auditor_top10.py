"""Auditor layer 4: RugCheck topHolders fallback when RPC getTokenLargestAccounts fails."""

from __future__ import annotations

from agents.auditor import audit_mint, rugcheck_top10_pct

POOL = "Pool1111111111111111111111111111111111111"


class OkRpc:
    def __init__(self, largest_fails=True):
        self.largest_fails = largest_fails

    def get_account_info(self, mint):
        return {"result": {"value": {"owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "data": {"parsed": {
            "info": {"mintAuthority": None, "freezeAuthority": None, "supply": "1000000000", "decimals": 6}}}}}}

    def get_token_supply(self, mint):
        return {"result": {"value": {"uiAmount": 1000.0, "amount": "1000000000", "decimals": 6}}}

    def get_token_accounts_by_owner(self, owner, mint):
        return {"result": {"value": []}}

    def get_token_largest_accounts(self, mint):
        if self.largest_fails:
            raise RuntimeError("429")
        return {"result": {"value": [{"address": "h1", "uiAmount": 50.0}]}}

    def get_program_accounts(self, *a, **k):
        return {"result": []}


class Rug:
    def __init__(self, report):
        self.report = report
        self.calls = 0

    def rugcheck_report(self, mint):
        self.calls += 1
        if isinstance(self.report, Exception):
            raise self.report
        return self.report

    def rugcheck_graph(self, mint):
        return [{"nodes": [], "links": []}]


def _report(holders):
    return {
        "topHolders": holders,
        "knownAccounts": {POOL: {"type": "AMM"}},
        "markets": [{"pubkey": POOL, "lp": {"lpLockedPct": 100}}],
    }


def _audit(cfg, rpc, src):
    return audit_mint(
        mint="MintTop10",
        scanner={"pool": POOL, "liquidityUsd": 80_000, "migratedAt": "2026-11-10T06:00:00Z"},
        coin={"creator": "Creator111", "creator_balance_at_migration": 0, "pool": POOL},
        cfg=cfg, rpc=rpc, sources=src, sleep=lambda _s: None,
    )


def _layer(res, n):
    return {l["n"]: l for l in res["layers"]}[n]


def test_helper_excludes_pool_known_amm_and_caps_ten():
    rep = _report([{"owner": POOL, "pct": 80.0}] + [{"owner": f"w{i}", "pct": 1.5} for i in range(12)])
    assert rugcheck_top10_pct(rep, POOL) == 15.0
    rep2 = {"topHolders": [{"owner": "LOCK", "pct": 40}, {"owner": "a", "pct": 3}],
            "knownAccounts": {"LOCK": {"type": "LOCKER"}}}
    assert rugcheck_top10_pct(rep2) == 3.0


def test_helper_fails_closed():
    assert rugcheck_top10_pct(None) is None
    assert rugcheck_top10_pct({}) is None
    assert rugcheck_top10_pct({"topHolders": []}) is None
    assert rugcheck_top10_pct(_report([{"owner": POOL, "pct": 99}]), POOL) is None


def test_rpc_fails_rugcheck_fallback_passes_layer4_and_reuses_report(cfg):
    src = Rug(_report([{"owner": POOL, "pct": 80.0}, {"owner": "a", "pct": 5.0}, {"owner": "b", "pct": 4.0}]))
    res = _audit(cfg, OkRpc(largest_fails=True), src)
    l4 = _layer(res, 4)
    assert l4["pass"] is True and l4["source"] == "rugcheck" and l4["value"] == "9.0%"
    assert _layer(res, 5)["pass"] is True
    assert src.calls == 1  # layer 5 reused the fallback report


def test_rpc_fails_rugcheck_shows_concentration_red(cfg):
    src = Rug(_report([{"owner": "a", "pct": 30.0}, {"owner": "b", "pct": 10.0}]))
    res = _audit(cfg, OkRpc(largest_fails=True), src)
    assert res["passed"] is False and res["reason"] == "top 10 hold 40%"
    assert _layer(res, 4)["source"] == "rugcheck"


def test_both_sources_empty_fail_closed(cfg):
    for src in (Rug({"topHolders": []}), Rug(RuntimeError("down")), None):
        res = _audit(cfg, OkRpc(largest_fails=True), src)
        assert res["passed"] is False and res["reason"] == "no data: top 10"
        assert _layer(res, 4)["pass"] is False


def test_rpc_ok_does_not_touch_rugcheck_for_layer4(cfg):
    src = Rug(_report([{"owner": "a", "pct": 99.0}]))
    res = _audit(cfg, OkRpc(largest_fails=False), src)
    l4 = _layer(res, 4)
    assert l4["source"] == "rpc" and l4["pass"] is True and l4["value"] == "5.0%"
