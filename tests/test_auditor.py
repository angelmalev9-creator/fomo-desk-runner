"""Auditor: freeze authority set → red at layer 2, layers 3-8 not checked."""

from __future__ import annotations

from agents.auditor import audit_mint


class FakeRpc:
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
                                "freezeAuthority": "FreezeAuth1111111111111111111111111111111",
                                "supply": "1000000",
                                "decimals": 6,
                            }
                        }
                    },
                }
            },
        }

    def get_token_supply(self, mint: str):
        raise AssertionError("layer 3 must not be called after a red layer 2")

    def get_token_largest_accounts(self, mint: str):
        raise AssertionError("layer 4 must not be called after a red layer 2")

    def get_token_accounts_by_owner(self, *a, **k):
        raise AssertionError("dev balance must not be called after a red layer 2")

    def get_program_accounts(self, *a, **k):
        raise AssertionError("layer 8 must not be called after a red layer 2")


class BoomSources:
    def rugcheck_report(self, mint):
        raise AssertionError("rugcheck must not be called after a red layer 2")

    def rugcheck_graph(self, mint):
        raise AssertionError("graph must not be called after a red layer 2")


def test_freeze_authority_red_layer_2_rest_not_checked(cfg):
    result = audit_mint(
        mint="MintFreeze1",
        scanner={"pool": "Pool111", "liquidityUsd": 80_000, "migratedAt": "2026-11-10T06:00:00Z"},
        coin={"creator": "Creator111", "creator_balance_at_migration": 0, "pool": "Pool111"},
        cfg=cfg,
        rpc=FakeRpc(),
        sources=BoomSources(),
        sleep=lambda _s: None,
    )
    assert result["passed"] is False
    assert result["layersChecked"] == 2
    assert result["reason"] == "freeze not revoked"
    layers = {l["n"]: l for l in result["layers"]}
    assert layers[1]["pass"] is True
    assert layers[1]["value"] == "Revoked"
    assert layers[2]["pass"] is False
    assert layers[2]["value"] == "freeze not revoked"
    for n in range(3, 9):
        assert layers[n]["value"] == "not checked"
        assert layers[n]["pass"] is None
