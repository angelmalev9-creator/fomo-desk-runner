"""gist_push reads GIST_TOKEN and GIST_ID without printing them."""

from __future__ import annotations

import json

import gist_push


def test_token_prefers_gist_token(monkeypatch):
    monkeypatch.setenv("GIST_TOKEN", "gist-pat-test")
    monkeypatch.setenv("GITHUB_TOKEN", "actions-auto-token")
    assert gist_push.token() == "gist-pat-test"


def test_token_falls_back_to_github_token(monkeypatch):
    monkeypatch.delenv("GIST_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "actions-auto-token")
    assert gist_push.token() == "actions-auto-token"


def test_load_gist_id_env_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("GIST_ID", "env-gist-id")
    monkeypatch.setattr(gist_push, "CONFIG_PATH", tmp_path / "missing.json")
    monkeypatch.setattr(gist_push, "ROOT", tmp_path)
    assert gist_push.load_gist_id() == "env-gist-id"


def test_load_gist_id_from_bootstrap(monkeypatch, tmp_path):
    monkeypatch.delenv("GIST_ID", raising=False)
    (tmp_path / "settings.bootstrap.json").write_text(json.dumps({"GIST_ID": "boot-gist-id"}))
    monkeypatch.setattr(gist_push, "ROOT", tmp_path)
    monkeypatch.setattr(gist_push, "CONFIG_PATH", tmp_path / "no-gist-config.json")
    assert gist_push.load_gist_id() == "boot-gist-id"
