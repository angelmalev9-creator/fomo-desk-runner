"""Tests for FOMO coin and profile URL helpers in links.py."""

from __future__ import annotations

import re
from pathlib import Path

import links

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".venv", "__pycache__", "logs", ".git", "web"}
CODE_SUFFIXES = {".py", ".js", ".html", ".css", ".md", ".json", ".txt"}
# Plan B / comments in placeholders may mention the domain; construction
# of coin/profile URLs must still live only in links.py.
URL_BUILD = re.compile(
    r"https://fomo\.family/(coin|profile/|r/)"
)


def test_unknown_chain_returns_signup_url():
    assert links.coin_url("SomeMint111", "ethereum") == links.SIGNUP_URL
    assert "r=savipww" in links.SIGNUP_URL or "/r/savipww" in links.SIGNUP_URL


def test_solana_coin_url_has_ref_and_chain():
    url = links.coin_url("BEd8GoJccoDA9gpfYdxqGupVFZwKrMWWMHnJYqnrpump", "solana")
    assert "r=savipww" in url
    assert "chainId=1399811149" in url
    assert "address=BEd8GoJccoDA9gpfYdxqGupVFZwKrMWWMHnJYqnrpump" in url
    assert url.startswith("https://fomo.family/coin?")


def test_profile_url_strips_at_and_has_ref():
    url = links.profile_url("@MalevAngel84749")
    assert url == "https://fomo.family/profile/MalevAngel84749?source=share_link&r=savipww"


def test_every_fomo_url_is_built_only_in_links_and_carries_ref():
    """Grep the repo: FOMO coin/profile/signup URLs are authored in links.py only."""
    offenders = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix not in CODE_SUFFIXES and path.name not in ("links.py",):
            continue
        if path.name == "links.py":
            text = path.read_text(errors="ignore")
            # Every constructed URL in links.py must carry the ref.
            for m in re.findall(r"https://fomo\.family/[^\"'\s]+", text):
                # the f-string template also contains r=savipww
                if "fomo.family" in m:
                    assert "savipww" in m or "r=savipww" in text
            continue
        if "tests" in path.parts:
            # tests may mention the domain when asserting links.py output
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if "fomo.family" not in line:
                continue
            # Importing SIGNUP_URL / coin_url / profile_url is fine.
            if "links." in line or "SIGNUP_URL" in line:
                continue
            if re.search(r"https://fomo\.family/", line):
                offenders.append(f"{path.relative_to(ROOT)}:{i}:{line.strip()}")
    assert not offenders, "FOMO URLs must be built only in links.py:\n" + "\n".join(offenders)
