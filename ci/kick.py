"""Watchdog: dispatch desk.yml if no run is already in progress or queued."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
WORKFLOW = os.environ.get("DESK_WORKFLOW") or "desk.yml"


def _token() -> str:
    t = (os.environ.get("GIST_TOKEN") or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
    if not t:
        raise SystemExit("no token for Actions API")
    return t


def _repo() -> str:
    repo = (os.environ.get("GITHUB_REPOSITORY") or "").strip()
    if not repo:
        raise SystemExit("GITHUB_REPOSITORY is not set")
    return repo


def _request(method: str, url: str, body: dict | None = None) -> tuple[int, bytes]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {_token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "fomo-desk-runner",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() if e.fp else b""


def active_runs(repo: str) -> int:
    n = 0
    for status in ("in_progress", "queued", "waiting", "pending"):
        code, raw = _request(
            "GET",
            f"{API}/repos/{repo}/actions/workflows/{WORKFLOW}/runs?status={status}&per_page=5",
        )
        if code >= 300:
            raise SystemExit(f"list runs failed HTTP {code}")
        n += int(json.loads(raw.decode()).get("total_count") or 0)
    return n


def dispatch(repo: str) -> None:
    ref = (os.environ.get("GITHUB_REF_NAME") or "main").strip()
    code, raw = _request(
        "POST",
        f"{API}/repos/{repo}/actions/workflows/{WORKFLOW}/dispatches",
        {"ref": ref},
    )
    if code not in (204, 200):
        raise SystemExit(f"workflow_dispatch failed HTTP {code}")
    print(f"dispatched {WORKFLOW} on {ref}", flush=True)


def main() -> int:
    repo = _repo()
    n = active_runs(repo)
    if n > 0:
        print(f"desk already has {n} active/queued run(s); not dispatching", flush=True)
        return 0
    dispatch(repo)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
