#!/usr/bin/env python3
"""从 Solana 链上抓取 token 对逐笔交易（历史 + 实时）并落库。"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

GECKO_API = "https://api.geckoterminal.com/api/v2"
DEFAULT_RPC = "https://api.mainnet-beta.solana.com"


@dataclass
class Trade:
    signature: str
    block_time: int
    slot: int
    trader: str
    side: str
    amount_in: float
    mint_in: str
    amount_out: float
    mint_out: str
    source: str


def http_get_json(url: str) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={"accept": "application/json", "user-agent": "codex-cli/1.0"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def rpc_call(rpc_url: str, method: str, params: list[Any]) -> Any:
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode("utf-8")
    req = urllib.request.Request(
        rpc_url,
        data=payload,
        headers={"content-type": "application/json", "accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if "error" in data:
        raise RuntimeError(f"RPC error: {data['error']}")
    return data["result"]


def token_id(token_mint: str) -> str:
    return f"solana_{token_mint}"


def find_pool(token_a: str, token_b: str) -> tuple[str, str]:
    wanted = {token_id(token_a), token_id(token_b)}
    best = None
    for page in range(1, 8):
        url = f"{GECKO_API}/networks/solana/tokens/{token_a}/pools?page={page}"
        pools = http_get_json(url).get("data", [])
        if not pools:
            break
        for pool in pools:
            rel = pool.get("relationships", {})
            base = rel.get("base_token", {}).get("data", {}).get("id")
            quote = rel.get("quote_token", {}).get("data", {}).get("id")
            if {base, quote} != wanted:
                continue
            reserve = float(pool.get("attributes", {}).get("reserve_in_usd") or 0)
            if best is None or reserve > float(best.get("attributes", {}).get("reserve_in_usd") or 0):
                best = pool
    if best is None:
        raise RuntimeError("未找到该 token 对应池子。可手动传 --pool。")
    pool_addr = best["id"].split("_", 1)[1]
    dex = best.get("attributes", {}).get("dex_name") or "unknown"
    return pool_addr, dex


def ensure_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trades (
          signature TEXT PRIMARY KEY,
          block_time INTEGER NOT NULL,
          ts_utc TEXT NOT NULL,
          slot INTEGER NOT NULL,
          trader TEXT,
          side TEXT NOT NULL,
          amount_in REAL NOT NULL,
          mint_in TEXT NOT NULL,
          amount_out REAL NOT NULL,
          mint_out TEXT NOT NULL,
          source TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_time ON trades(block_time)")
    conn.commit()
    return conn


def get_signatures(rpc_url: str, address: str, before: str | None, limit: int = 1000) -> list[dict[str, Any]]:
    cfg: dict[str, Any] = {"limit": limit}
    if before:
        cfg["before"] = before
    return rpc_call(rpc_url, "getSignaturesForAddress", [address, cfg])


def get_tx(rpc_url: str, sig: str) -> dict[str, Any] | None:
    return rpc_call(
        rpc_url,
        "getTransaction",
        [sig, {"maxSupportedTransactionVersion": 0, "encoding": "jsonParsed", "commitment": "confirmed"}],
    )


def _token_bal_map(entries: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for e in entries:
        idx = e.get("accountIndex")
        if idx is not None:
            out[idx] = e
    return out


def _amount(entry: dict[str, Any] | None) -> float:
    if not entry:
        return 0.0
    ui = entry.get("uiTokenAmount", {})
    v = ui.get("uiAmount")
    if v is not None:
        return float(v)
    raw = float(ui.get("amount") or 0)
    dec = int(ui.get("decimals") or 0)
    return raw / (10 ** dec)


def parse_trade(tx: dict[str, Any], mint_a: str, mint_b: str, source: str) -> Trade | None:
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return None
    pre = _token_bal_map(meta.get("preTokenBalances") or [])
    post = _token_bal_map(meta.get("postTokenBalances") or [])

    owner_deltas: dict[str, dict[str, float]] = {}
    for idx in set(pre.keys()) | set(post.keys()):
        pre_e = pre.get(idx)
        post_e = post.get(idx)
        mint = (post_e or pre_e or {}).get("mint")
        if mint not in {mint_a, mint_b}:
            continue
        owner = (post_e or pre_e or {}).get("owner") or "unknown"
        delta = _amount(post_e) - _amount(pre_e)
        owner_deltas.setdefault(owner, {}).setdefault(mint, 0.0)
        owner_deltas[owner][mint] += delta

    best_owner = None
    best_score = 0.0
    for owner, d in owner_deltas.items():
        da = d.get(mint_a, 0.0)
        db = d.get(mint_b, 0.0)
        if da == 0 or db == 0:
            continue
        if da * db > 0:
            continue
        score = abs(da) + abs(db)
        if score > best_score:
            best_owner = owner
            best_score = score

    if not best_owner:
        return None

    da = owner_deltas[best_owner].get(mint_a, 0.0)
    db = owner_deltas[best_owner].get(mint_b, 0.0)
    if da > 0 and db < 0:
        side = "buy_token_a"
        amount_in, mint_in = abs(db), mint_b
        amount_out, mint_out = abs(da), mint_a
    elif da < 0 and db > 0:
        side = "buy_token_b"
        amount_in, mint_in = abs(da), mint_a
        amount_out, mint_out = abs(db), mint_b
    else:
        return None

    sig = tx.get("transaction", {}).get("signatures", [""])[0]
    block_time = int(tx.get("blockTime") or 0)
    slot = int(tx.get("slot") or 0)
    return Trade(sig, block_time, slot, best_owner, side, amount_in, mint_in, amount_out, mint_out, source)


def upsert_trade(conn: sqlite3.Connection, trade: Trade) -> None:
    ts = datetime.fromtimestamp(trade.block_time, tz=timezone.utc).isoformat() if trade.block_time else ""
    conn.execute(
        """
        INSERT OR REPLACE INTO trades
        (signature, block_time, ts_utc, slot, trader, side, amount_in, mint_in, amount_out, mint_out, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            trade.signature,
            trade.block_time,
            ts,
            trade.slot,
            trade.trader,
            trade.side,
            trade.amount_in,
            trade.mint_in,
            trade.amount_out,
            trade.mint_out,
            trade.source,
        ),
    )


def backfill_week(rpc_url: str, pool: str, mint_a: str, mint_b: str, source: str, conn: sqlite3.Connection, days: int) -> int:
    cutoff = int((datetime.now(tz=timezone.utc) - timedelta(days=days)).timestamp())
    before: str | None = None
    inserted = 0
    while True:
        sigs = get_signatures(rpc_url, pool, before=before, limit=1000)
        if not sigs:
            break
        stop = False
        for item in sigs:
            sig = item["signature"]
            bt = item.get("blockTime") or 0
            if bt and bt < cutoff:
                stop = True
                break
            tx = get_tx(rpc_url, sig)
            if not tx:
                continue
            t = parse_trade(tx, mint_a, mint_b, source)
            if t:
                upsert_trade(conn, t)
                inserted += 1
        conn.commit()
        if stop:
            break
        before = sigs[-1]["signature"]
        time.sleep(0.15)
    return inserted


def latest_signature(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT signature FROM trades ORDER BY block_time DESC LIMIT 1").fetchone()
    return row[0] if row else None


def poll_realtime(rpc_url: str, pool: str, mint_a: str, mint_b: str, source: str, conn: sqlite3.Connection, poll_seconds: int) -> None:
    last_seen = latest_signature(conn)
    print("开始实时抓取，Ctrl+C 结束 ...")
    while True:
        try:
            sigs = get_signatures(rpc_url, pool, before=None, limit=100)
            if not sigs:
                time.sleep(poll_seconds)
                continue
            new_items: list[dict[str, Any]] = []
            for s in sigs:
                if s["signature"] == last_seen:
                    break
                new_items.append(s)
            for s in reversed(new_items):
                tx = get_tx(rpc_url, s["signature"])
                if not tx:
                    continue
                t = parse_trade(tx, mint_a, mint_b, source)
                if t:
                    upsert_trade(conn, t)
                    print(f"[new] {t.signature[:10]}.. {t.side} in={t.amount_in:.6f} {t.mint_in[:6]} out={t.amount_out:.6f} {t.mint_out[:6]}")
                    last_seen = t.signature
            conn.commit()
            time.sleep(poll_seconds)
        except KeyboardInterrupt:
            print("\n已停止实时抓取。")
            return


def main() -> int:
    parser = argparse.ArgumentParser(description="从 Solana 链上抓取 token 对逐笔交易（历史+实时）")
    parser.add_argument("--token-a", required=True, help="token A mint")
    parser.add_argument("--token-b", required=True, help="token B mint")
    parser.add_argument("--pool", help="可选：指定池子地址（不填则自动查找）")
    parser.add_argument("--rpc-url", default=DEFAULT_RPC, help="Solana RPC URL")
    parser.add_argument("--days", type=int, default=7, help="回补历史天数，默认7")
    parser.add_argument("--db", default="pair_trades.db", help="SQLite 输出文件")
    parser.add_argument("--live", action="store_true", help="回补结束后持续实时抓取")
    parser.add_argument("--poll-seconds", type=int, default=15, help="实时抓取轮询间隔秒")
    args = parser.parse_args()

    try:
        if args.pool:
            pool, source = args.pool, "manual_pool"
        else:
            pool, dex = find_pool(args.token_a, args.token_b)
            source = f"{dex}:{pool}"
        print("使用池子:", pool)
        conn = ensure_db(args.db)
        inserted = backfill_week(args.rpc_url, pool, args.token_a, args.token_b, source, conn, days=args.days)
        total = conn.execute("SELECT COUNT(1) FROM trades").fetchone()[0]
        print(f"历史回补完成：新增/更新 {inserted} 条，库内总计 {total} 条 -> {args.db}")
        if args.live:
            poll_realtime(args.rpc_url, pool, args.token_a, args.token_b, source, conn, args.poll_seconds)
        conn.close()
        return 0
    except urllib.error.URLError as e:
        print(f"网络请求失败: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001
        print(f"执行失败: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
