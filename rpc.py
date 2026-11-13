"""Solana JSON-RPC helper with retries and a small global rate limiter."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

import httpx

from fields import TOKEN_2022_PROGRAM, TOKEN_PROGRAM
from util import agent_logger, as_float

log = agent_logger("rpc")

# Public RPC 429s getTokenLargestAccounts quickly. Keep everyone polite.
_MIN_INTERVAL_S = 0.35
_lock: asyncio.Lock | None = None
_last: float = 0.0
_sync_lock = __import__("threading").Lock()
_sync_last = 0.0


def _sync_wait() -> None:
    global _sync_last
    with _sync_lock:
        now = time.monotonic()
        wait = _MIN_INTERVAL_S - (now - _sync_last)
        if wait > 0:
            time.sleep(wait)
        _sync_last = time.monotonic()


async def _async_wait() -> None:
    global _last, _lock
    if _lock is None:
        _lock = asyncio.Lock()
    async with _lock:
        now = time.monotonic()
        wait = _MIN_INTERVAL_S - (now - _last)
        if wait > 0:
            await asyncio.sleep(wait)
        _last = time.monotonic()


class RpcError(Exception):
    def __init__(self, message: str, code: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.body = body


def _raise_if_error(body: Any) -> Any:
    if isinstance(body, dict) and body.get("error"):
        err = body["error"]
        code = err.get("code") if isinstance(err, dict) else None
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise RpcError(msg or "rpc error", code=code, body=body)
    return body


class Rpc:
    def __init__(
        self,
        url: str,
        timeout: float = 30.0,
        retries: int = 3,
        retry_wait_s: float = 2.0,
        sleep: Callable[[float], None] | None = None,
        transport=None,
    ) -> None:
        self.url = url
        self.timeout = timeout
        self.retries = retries
        self.retry_wait_s = retry_wait_s
        self._sleep = sleep or time.sleep
        self._transport = transport

    def _client(self) -> httpx.Client:
        kwargs: dict[str, Any] = {"timeout": self.timeout}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.Client(**kwargs)

    def call(self, method: str, params: list[Any] | None = None) -> Any:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
        last_err: Exception | None = None
        attempts = max(1, self.retries)
        for i in range(attempts):
            try:
                _sync_wait()
                with self._client() as client:
                    res = client.post(self.url, json=payload)
                    if res.status_code == 429:
                        raise RpcError("Too many requests", code=429, body=res.text)
                    res.raise_for_status()
                    body = res.json()
                    return _raise_if_error(body)
            except (httpx.HTTPError, RpcError, ValueError) as e:
                last_err = e
                code = getattr(e, "code", None)
                # 429: still retry with the same gap
                if i + 1 < attempts:
                    self._sleep(self.retry_wait_s)
                    continue
                raise
        raise last_err or RpcError("rpc failed")

    def get_account_info(self, pubkey: str) -> dict:
        return self.call(
            "getAccountInfo",
            [pubkey, {"encoding": "jsonParsed", "commitment": "confirmed"}],
        )

    def get_token_supply(self, mint: str) -> dict:
        return self.call("getTokenSupply", [mint])

    def get_token_largest_accounts(self, mint: str) -> dict:
        return self.call("getTokenLargestAccounts", [mint])

    def get_token_accounts_by_owner(self, owner: str, mint: str, program: str | None = None) -> dict:
        filt = {"mint": mint}
        opts = {"encoding": "jsonParsed"}
        # Try the given program, else the two standard ones at the call site.
        return self.call("getTokenAccountsByOwner", [owner, filt, opts])

    def get_program_accounts(self, program_id: str, mint: str) -> dict:
        return self.call(
            "getProgramAccounts",
            [
                program_id,
                {
                    "encoding": "jsonParsed",
                    "filters": [{"memcmp": {"offset": 0, "bytes": mint}}],
                },
            ],
        )

    def get_slot(self) -> Any:
        body = self.call("getSlot", [])
        return body.get("result") if isinstance(body, dict) else body


def token_account_ui_amount(account: dict) -> float:
    info = (
        ((account.get("account") or {}).get("data") or {}).get("parsed") or {}
    ).get("info") or {}
    amt = (info.get("tokenAmount") or {}).get("uiAmount")
    return as_float(amt, 0.0) or 0.0


def token_account_delegate(account: dict) -> str | None:
    info = (
        ((account.get("account") or {}).get("data") or {}).get("parsed") or {}
    ).get("info") or {}
    return info.get("delegate")


def mint_program_id(account_info_body: dict) -> str | None:
    val = (account_info_body.get("result") or {}).get("value") or {}
    return val.get("owner")


__all__ = [
    "Rpc",
    "RpcError",
    "TOKEN_PROGRAM",
    "TOKEN_2022_PROGRAM",
    "token_account_ui_amount",
    "token_account_delegate",
    "mint_program_id",
]
