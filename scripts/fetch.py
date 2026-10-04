#!/usr/bin/env python3
"""Refresh public USDC balances and transfers for the Vest monitor.

Reads public RPCs (eth_call balanceOf) and Blockscout v2. BSC has no working
keyless Blockscout instance here, so BSC transfers use eth_getLogs. Failed
reads never replace a previously stored balance or transfer history.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
SEED = os.path.join(DATA, "seed-history.csv")
UA = "vest-watch/1.0 (+https://github.com/diatle-glitch/vest-watch)"

TRANSFER_LOOKBACK_DAYS = 90
TRANSFER_RETAIN_DAYS = 120
LARGE_USD = Decimal("10000")
MAX_BLOCKSCOUT_PAGES = int(os.environ.get("VEST_MAX_PAGES", "160"))
BSC_CHUNK = 7000
BSC_TIME_BUDGET_S = int(os.environ.get("VEST_BSC_BUDGET", "240"))

CORE_KEYS = [
    "treasury_safe_eth",
    "safe2_eth",
    "base_contract",
    "arb_contract",
    "eth_deposit",
    "arb_old",
    "bsc_old",
]

USDC = {
    "ethereum": {"address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", "decimals": 6, "symbol": "USDC"},
    "base": {"address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "decimals": 6, "symbol": "USDC"},
    "arbitrum": {"address": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", "decimals": 6, "symbol": "USDC"},
    "bsc": {"address": "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d", "decimals": 18, "symbol": "USDC"},
    "optimism": {"address": "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85", "decimals": 6, "symbol": "USDC"},
    "polygon": {"address": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", "decimals": 6, "symbol": "USDC"},
}

USDC_ALT = {
    "optimism": {"address": "0x7F5c764cBc14f9669B88837ca1490cCa17c31607", "decimals": 6, "symbol": "USDC.e"},
    "polygon": {"address": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174", "decimals": 6, "symbol": "USDC.e"},
}

RPCS = {
    "ethereum": [
        "https://ethereum-rpc.publicnode.com",
        "https://eth.llamarpc.com",
        "https://1rpc.io/eth",
        "https://rpc.ankr.com/eth",
    ],
    "base": [
        "https://base-rpc.publicnode.com",
        "https://mainnet.base.org",
        "https://1rpc.io/base",
    ],
    "arbitrum": [
        "https://arbitrum-one-rpc.publicnode.com",
        "https://arb1.arbitrum.io/rpc",
        "https://1rpc.io/arb",
    ],
    "bsc": [
        "https://bsc-rpc.publicnode.com",
        "https://bsc-dataseed.binance.org",
        "https://1rpc.io/bnb",
    ],
    "optimism": [
        "https://optimism-rpc.publicnode.com",
        "https://mainnet.optimism.io",
        "https://1rpc.io/op",
    ],
    "polygon": [
        "https://polygon-bor-rpc.publicnode.com",
        "https://polygon-rpc.com",
        "https://1rpc.io/matic",
    ],
}

BLOCKSCOUT = {
    "ethereum": "https://eth.blockscout.com",
    "base": "https://base.blockscout.com",
    "arbitrum": "https://arbitrum.blockscout.com",
    "optimism": "https://optimism.blockscout.com",
    "polygon": "https://polygon.blockscout.com",
}

EXPLORER = {
    "ethereum": "https://eth.blockscout.com",
    "base": "https://base.blockscout.com",
    "arbitrum": "https://arbitrum.blockscout.com",
    "optimism": "https://optimism.blockscout.com",
    "polygon": "https://polygon.blockscout.com",
    "bsc": "https://bscscan.com",
}

# Counterparties resolved from public transfers / eth_call.
COINBASE_PRIME = "0xCD531Ae9EFCCE479654c4926dec5F6209531Ca7b"
COINBASE_HOT = "0x4B5c71082d027D16d2A146465d66f9EEC11634F6"
LIFI = "0x1231DEB6f5749EF6cE6943a275A1D3E7486F4EaE"
OPERATOR = "0xBbBACC96be3d045b9Ca71bd422c78AFb80CF5970"
ROUTER_SELECTOR = "0xf887ea40"
ROLE_HASH = "7045adfe67d5f94dbfddcdb901e44bef55baacabb398c7cddda1bfd7620b1568"
HAS_ROLE_SELECTOR = "91d14854"
ZERO = "0x0000000000000000000000000000000000000000"

WALLETS = [
    {
        "id": "treasury_safe_eth",
        "chain": "ethereum",
        "address": "0x267EbfbcBb7020F200c2749Ee441E12574d60C6c",
        "role": "Treasury Safe",
        "attribution": "inferred",
        "note": "Attribution to Vest is an inference. Inbound USDC on Ethereum comes from 0xCD531Ae9EFCCE479654c4926dec5F6209531Ca7b (labeled Coinbase Prime 1 in the source notes for this monitor). Outbound USDC goes to the LI.FI diamond 0x1231DEB6f5749EF6cE6943a275A1D3E7486F4EaE (Blockscout name LiFiDiamond). The Vest pages reviewed for this site do not publish this address.",
        "history_key": "treasury_safe_eth",
        "flows": True,
    },
    {
        "id": "treasury_safe_base",
        "chain": "base",
        "address": "0x267EbfbcBb7020F200c2749Ee441E12574d60C6c",
        "role": "Treasury Safe (same address)",
        "attribution": "inferred",
        "note": "Same address as the Ethereum Treasury Safe, on Base. Attribution to Vest is an inference.",
        "flows": True,
    },
    {
        "id": "treasury_safe_arb",
        "chain": "arbitrum",
        "address": "0x267EbfbcBb7020F200c2749Ee441E12574d60C6c",
        "role": "Treasury Safe (same address)",
        "attribution": "inferred",
        "note": "Same address as the Ethereum Treasury Safe, on Arbitrum. Attribution to Vest is an inference.",
        "flows": True,
    },
    {
        "id": "safe2_eth",
        "chain": "ethereum",
        "address": "0x0e66f5443Db92423b692866CFDd9D9F6AcDe8597",
        "role": "Second Safe",
        "attribution": "inferred",
        "note": "Second Ethereum Safe in the monitored set. The Vest pages reviewed for this site do not name this address. Attribution to Vest is not confirmed by those pages.",
        "history_key": "safe2_eth",
        "flows": True,
    },
    {
        "id": "base_contract",
        "chain": "base",
        "address": "0x55133c825603E6A5b9E911ABAb23e75Dc3Bb07aF",
        "role": "Current payout/deposit contract",
        "attribution": "observed",
        "note": "Base contract in the monitored payout/deposit set. Outbound USDC transfers use method withdraw. eth_call hasRole(BRIDGE_OPERATOR_ROLE) for the operator address is checked each run.",
        "history_key": "base_contract",
        "kind": "current",
        "flows": True,
    },
    {
        "id": "arb_contract",
        "chain": "arbitrum",
        "address": "0xB2F86eAE1197032Fa85389cc6c0F3b06B58Dd1Ea",
        "role": "Current payout/deposit contract",
        "attribution": "observed",
        "note": "Arbitrum contract in the monitored payout/deposit set.",
        "history_key": "arb_contract",
        "kind": "current",
        "flows": True,
    },
    {
        "id": "eth_deposit",
        "chain": "ethereum",
        "address": "0xE80F92077131b9890599E418AE323de71cE1C35a",
        "role": "Current payout/deposit contract",
        "attribution": "contract source",
        "note": "TransparentUpgradeableProxy. Blockscout lists implementation 0xEcD91C77B98d507E3C20Bac86D2541ECbDc881E3, verified as SrcBridge. That source contains string public constant NAME = \"VestRouterV2\".",
        "history_key": "eth_deposit",
        "kind": "current",
        "flows": True,
        "read_router": True,
    },
    {
        "id": "op_deposit",
        "chain": "optimism",
        "address": "0xE80F92077131b9890599E418AE323de71cE1C35a",
        "role": "Same address on Optimism",
        "attribution": "contract source",
        "note": "Same address as the Ethereum payout/deposit proxy. Included when it holds USDC. Blockscout names the implementation SrcBridge (0x7600192EC07444Df13b041D23A598B885ff4018d).",
        "kind": "current",
        "optional": True,
        "flows": True,
    },
    {
        "id": "polygon_deposit",
        "chain": "polygon",
        "address": "0xE80F92077131b9890599E418AE323de71cE1C35a",
        "role": "Same address on Polygon",
        "attribution": "observed",
        "note": "Same address as the Ethereum payout/deposit proxy. Included when it holds USDC.",
        "kind": "current",
        "optional": True,
        "flows": True,
    },
    {
        "id": "base_e80f",
        "chain": "base",
        "address": "0xE80F92077131b9890599E418AE323de71cE1C35a",
        "role": "Same address on Base",
        "attribution": "observed",
        "note": "Same address as the Ethereum payout/deposit proxy, on Base. Included when it holds USDC. Distinct from the current Base contract 0x55133c82….",
        "kind": "current",
        "optional": True,
        "flows": True,
    },
    {
        "id": "bsc_contract",
        "chain": "bsc",
        "address": "0x74dD5d9ca86CE7FAec8403210dd37ea9122F0879",
        "role": "Current payout/deposit contract",
        "attribution": "observed",
        "note": "BNB Smart Chain contract in the monitored payout/deposit set. USDC on BSC uses 18 decimals.",
        "kind": "current",
        "flows": True,
    },
    {
        "id": "arb_old",
        "chain": "arbitrum",
        "address": "0x80C526d1c2fddADB3Cd39810cd7A79E07b0EDa00",
        "role": "Older contract",
        "attribution": "observed",
        "note": "Older Arbitrum contract in the monitored set.",
        "history_key": "arb_old",
        "kind": "legacy",
        "flows": True,
    },
    {
        "id": "base_old",
        "chain": "base",
        "address": "0x32d95F243F9E2c1344E4BAa91a8D32711527ef7e",
        "role": "Older contract",
        "attribution": "observed",
        "note": "Older Base contract in the monitored set.",
        "kind": "legacy",
        "flows": True,
    },
    {
        "id": "bsc_old",
        "chain": "bsc",
        "address": "0xef14da66876476C1A75dC057343B97b6Bd372c41",
        "role": "Older contract",
        "attribution": "observed",
        "note": "Older BNB Smart Chain contract in the monitored set. USDC on BSC uses 18 decimals.",
        "history_key": "bsc_old",
        "kind": "legacy",
        "flows": True,
    },
]


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def money(value) -> float:
    q = Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(q)


def money_d(value) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def load_json(name, default):
    path = os.path.join(DATA, name)
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(name, obj):
    os.makedirs(DATA, exist_ok=True)
    path = os.path.join(DATA, name)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def http_json(url, timeout=30, retries=4):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            return json.loads(raw.decode("utf-8"))
        except Exception as exc:
            last = exc
            time.sleep(min(8, 1.5 ** i))
    raise last


def rpc_call(chain, method, params, timeout=20):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    errors = []
    for url in RPCS[chain]:
        for attempt in range(2):
            try:
                req = urllib.request.Request(
                    url,
                    data=body,
                    headers={"content-type": "application/json", "user-agent": UA},
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                if data.get("error"):
                    errors.append(f"{url}: {data['error']}")
                    break
                if "result" not in data:
                    errors.append(f"{url}: no result")
                    break
                return data["result"], url
            except Exception as exc:
                errors.append(f"{url}: {exc}")
                time.sleep(0.4 * (attempt + 1))
    raise RuntimeError("; ".join(errors[:4]))


def balance_of(chain, token, holder):
    addr = holder.lower().replace("0x", "").rjust(64, "0")
    data = "0x70a08231" + addr
    result, source = rpc_call(chain, "eth_call", [{"to": token, "data": data}, "latest"])
    if not result or result == "0x":
        raise RuntimeError("empty balanceOf result")
    return int(result, 16), source


def read_balance(wallet):
    chain = wallet["chain"]
    tokens = [USDC[chain]]
    if wallet.get("optional") and chain in USDC_ALT:
        tokens.append(USDC_ALT[chain])
    errors = []
    best = None
    for token in tokens:
        try:
            raw, source = balance_of(chain, token["address"], wallet["address"])
            human = Decimal(raw) / (Decimal(10) ** token["decimals"])
            row = {
                "raw": str(raw),
                "usdc": money(human),
                "usdc_exact": str(money_d(human)),
                "token": token["address"],
                "token_symbol": token["symbol"],
                "decimals": token["decimals"],
                "rpc": source,
                "read_ok": True,
            }
            if best is None or human > Decimal(best["usdc_exact"]):
                best = row
            if human > 0 and token["symbol"] == "USDC":
                break
        except Exception as exc:
            errors.append(f"{token['symbol']}: {exc}")
    if best is None:
        raise RuntimeError(" | ".join(errors) or "balance failed")
    best["errors"] = errors
    return best


def addr_url(chain, address):
    return f"{EXPLORER[chain]}/address/{address}"


def tx_url(chain, txhash):
    return f"{EXPLORER[chain]}/tx/{txhash}"


def normalize_transfer(wallet, item):
    frm = ((item.get("from") or {}).get("hash") or ZERO)
    to = ((item.get("to") or {}).get("hash") or ZERO)
    total = item.get("total") or {}
    decimals = int(total.get("decimals") or USDC[wallet["chain"]]["decimals"])
    raw = Decimal(total.get("value") or "0") / (Decimal(10) ** decimals)
    direction = "out" if frm.lower() == wallet["address"].lower() else "in"
    if frm.lower() == wallet["address"].lower() and to.lower() == wallet["address"].lower():
        direction = "self"
    counterparty = to if direction == "out" else frm
    method = item.get("method") or ""
    ts = item.get("timestamp")
    if ts.endswith("Z"):
        ts = ts.replace(".000000Z", "Z")
        if "." in ts:
            ts = ts.split(".")[0] + "Z"
    log_index = item.get("log_index")
    if log_index is None:
        log_index = 0
    return {
        "t": ts,
        "w": wallet["id"],
        "c": wallet["chain"],
        "d": direction,
        "a": float(money_d(raw)),
        "p": counterparty,
        "m": method,
        "h": item.get("transaction_hash"),
        "i": int(log_index),
        "pn": (item.get("to") if direction == "out" else item.get("from") or {}).get("name"),
    }


def transfer_key(tr):
    return f"{tr['h']}:{tr['i']}"


def _blockscout_url(wallet):
    base = BLOCKSCOUT.get(wallet["chain"])
    if not base:
        raise RuntimeError("no blockscout host")
    token = USDC[wallet["chain"]]["address"]
    return (
        f"{base}/api/v2/addresses/{wallet['address']}/token-transfers"
        f"?type=ERC-20&token={token}"
    ), base, token


def _consume_page(wallet, items, token, cutoff, known_keys, mode):
    """mode 'head' stops after a run of already-stored rows. mode 'backfill' stops at cutoff."""
    rows = []
    reached = False
    known_run = 0
    stop = False
    for item in items:
        ts = parse_iso(item["timestamp"])
        if ts < cutoff:
            reached = True
            stop = True
            break
        tok = (item.get("token") or {}).get("address_hash") or (item.get("token") or {}).get("address")
        if tok and tok.lower() != token.lower():
            continue
        tr = normalize_transfer(wallet, item)
        if mode == "head" and transfer_key(tr) in known_keys:
            known_run += 1
            if known_run >= 8:
                stop = True
                break
            continue
        known_run = 0
        rows.append(tr)
    return rows, reached, stop


def fetch_blockscout(wallet, cutoff: datetime, known_keys, resume_params, head_pages=8):
    url, base, token = _blockscout_url(wallet)
    rows = []
    pages = 0
    # Newest pages, so a later run picks up transfers since the previous cursor.
    params = ""
    head_done = False
    for _ in range(head_pages):
        data = http_json(url + params, timeout=35, retries=4)
        pages += 1
        items = data.get("items") or []
        if not items:
            return rows, {"pages": pages, "reached_cutoff": True, "source": base, "resume": None}
        got, reached, stop = _consume_page(wallet, items, token, cutoff, known_keys, "head")
        rows.extend(got)
        nxt = data.get("next_page_params")
        if reached or stop or not nxt:
            head_done = True
            if reached or not nxt:
                return rows, {"pages": pages, "reached_cutoff": True, "source": base, "resume": None}
            break
        params = "&" + urllib.parse.urlencode({k: v for k, v in nxt.items() if v is not None})
        time.sleep(0.1)
    # Older history. Resume a saved Blockscout cursor when one exists.
    if resume_params:
        params = "&" + urllib.parse.urlencode({k: v for k, v in resume_params.items() if v is not None})
    elif head_done:
        return rows, {"pages": pages, "reached_cutoff": False, "source": base, "resume": None}
    resume = None
    reached = False
    for _ in range(MAX_BLOCKSCOUT_PAGES):
        data = http_json(url + params, timeout=35, retries=4)
        pages += 1
        items = data.get("items") or []
        if not items:
            reached = True
            resume = None
            break
        got, reached, stop = _consume_page(wallet, items, token, cutoff, set(), "backfill")
        rows.extend(got)
        nxt = data.get("next_page_params")
        if reached or stop or not nxt:
            resume = None if (reached or not nxt) else nxt
            if not nxt:
                reached = True
            break
        params = "&" + urllib.parse.urlencode({k: v for k, v in nxt.items() if v is not None})
        resume = nxt
        time.sleep(0.1)
    return rows, {"pages": pages, "reached_cutoff": reached, "source": base, "resume": resume}


def pad_addr(address):
    return "0x" + address.lower().replace("0x", "").rjust(64, "0")


def bsc_block_time():
    latest_hex, _ = rpc_call("bsc", "eth_blockNumber", [])
    latest = int(latest_hex, 16)
    block, _ = rpc_call("bsc", "eth_getBlockByNumber", [hex(latest), False])
    ts = int(block["timestamp"], 16)
    return latest, ts, 3.0


def fetch_bsc_logs(wallet, cutoff: datetime, sync_entry, budget_s):
    token = USDC["bsc"]["address"]
    decimals = USDC["bsc"]["decimals"]
    topic = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    padded = pad_addr(wallet["address"])
    latest, latest_ts, step = bsc_block_time()
    cutoff_block = max(1, latest - int((latest_ts - cutoff.timestamp()) / step) - 1000)
    start = time.time()
    rows = []
    low = int(sync_entry.get("low_block") or latest)
    high = int(sync_entry.get("high_block") or (latest - 1))
    reached = bool(sync_entry.get("reached_cutoff"))
    # New head.
    scan_from = max(high + 1, cutoff_block)
    if scan_from <= latest:
        rows.extend(_bsc_range(token, topic, padded, scan_from, latest, decimals, wallet, start, budget_s))
        high = latest
    # Older backfill.
    cursor = min(low, latest)
    while cursor > cutoff_block and (time.time() - start) < budget_s:
        frm = max(cutoff_block, cursor - BSC_CHUNK)
        to = cursor - 1
        if to < frm:
            break
        rows.extend(_bsc_range(token, topic, padded, frm, to, decimals, wallet, start, budget_s))
        cursor = frm
        low = frm
        if low <= cutoff_block:
            reached = True
            break
    if low <= cutoff_block:
        reached = True
    try:
        edge, _ = rpc_call("bsc", "eth_getBlockByNumber", [hex(low), False], timeout=20)
        covered_from = datetime.fromtimestamp(int(edge["timestamp"], 16), timezone.utc)
    except Exception:
        covered_from = None
    return rows, {
        "reached_cutoff": reached,
        "low_block": low,
        "high_block": high,
        "latest_block": latest,
        "covered_from": iso(covered_from) if covered_from else None,
        "source": "eth_getLogs",
        "seconds": round(time.time() - start, 1),
    }


def _bsc_range(token, topic, padded, frm, to, decimals, wallet, start, budget_s):
    rows = []
    span = to - frm
    # Split if the window is larger than a provider accepts.
    if span > BSC_CHUNK:
        mid = (frm + to) // 2
        if time.time() - start > budget_s:
            return rows
        rows.extend(_bsc_range(token, topic, padded, frm, mid, decimals, wallet, start, budget_s))
        rows.extend(_bsc_range(token, topic, padded, mid + 1, to, decimals, wallet, start, budget_s))
        return rows
    queries = [
        [topic, padded, None],  # out
        [topic, None, padded],  # in
    ]
    directions = ["out", "in"]
    for topics, direction in zip(queries, directions):
        if time.time() - start > budget_s:
            break
        result = None
        for attempt in range(3):
            try:
                result, _ = rpc_call(
                    "bsc",
                    "eth_getLogs",
                    [{"fromBlock": hex(frm), "toBlock": hex(to), "address": token, "topics": topics}],
                    timeout=30,
                )
                break
            except Exception:
                time.sleep(0.6 * (attempt + 1))
                result = None
        if result is None:
            continue
        # Approximate timestamps from the end block of the chunk.
        try:
            end_block, _ = rpc_call("bsc", "eth_getBlockByNumber", [hex(to), False], timeout=20)
            end_ts = int(end_block["timestamp"], 16)
        except Exception:
            end_ts = int(now_utc().timestamp())
        for log in result:
            bn = int(log["blockNumber"], 16)
            raw = int(log["data"], 16) if log.get("data") and log["data"] != "0x" else 0
            human = Decimal(raw) / (Decimal(10) ** decimals)
            ts = end_ts - (to - bn) * 3
            frm_addr = "0x" + log["topics"][1][-40:]
            to_addr = "0x" + log["topics"][2][-40:]
            # Skip the duplicate when a self-transfer matches both filters.
            if direction == "in" and frm_addr.lower() == wallet["address"].lower():
                continue
            counterparty = to_addr if direction == "out" else frm_addr
            rows.append({
                "t": datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "w": wallet["id"],
                "c": "bsc",
                "d": direction,
                "a": float(money_d(human)),
                "p": checksum_like(counterparty),
                "m": "",
                "h": log["transactionHash"],
                "i": int(log["logIndex"], 16),
                "pn": None,
                "ts_approx": True,
            })
        time.sleep(0.05)
    return rows


def checksum_like(addr: str) -> str:
    return "0x" + addr.lower().replace("0x", "")


def fill_bsc_methods(transfers, limit=250):
    """BSC log queries do not include the tx method. Read input selectors for outs."""
    pending = [tr for tr in transfers if tr.get("c") == "bsc" and tr.get("d") == "out" and not tr.get("m")]
    pending.sort(key=lambda tr: tr["t"], reverse=True)
    seen = set()
    filled = 0
    for tr in pending:
        if tr["h"] in seen:
            continue
        if filled >= limit:
            break
        seen.add(tr["h"])
        filled += 1
        try:
            tx, _ = rpc_call("bsc", "eth_getTransactionByHash", [tr["h"]], timeout=15)
            selector = ((tx or {}).get("input") or "0x")[:10].lower()
            method = "withdraw" if selector == "0xbd69a7ae" else selector
        except Exception:
            method = ""
            continue
        for other in pending:
            if other["h"] == tr["h"]:
                other["m"] = method
        time.sleep(0.04)
    return filled


def is_withdraw(tr):
    method = (tr.get("m") or "").lower()
    return method in {"withdraw", "0xbd69a7ae"}


def label_party(addr, names):
    a = (addr or "").lower()
    if a == COINBASE_PRIME.lower():
        return "Coinbase Prime 1"
    if a == COINBASE_HOT.lower():
        return "Coinbase hot wallet"
    if a == LIFI.lower():
        return "LI.FI diamond"
    if a == "0x267ebfbcbb7020f200c2749ee441e12574d60c6c":
        return "Treasury Safe"
    if a == ZERO:
        return "Mint / zero address"
    if a in names:
        return names[a]
    return None


def classify_topups(transfers, wallet_by_id):
    """Observed legs, plus inferred LI.FI completions matched by amount and time."""
    names = {}
    for w in WALLETS:
        names[w["address"].lower()] = w["role"]
    topups = []
    lifi_outs = []
    candidates = []
    for tr in transfers:
        if tr["d"] == "self":
            continue
        w = wallet_by_id[tr["w"]]
        party = tr["p"].lower()
        amount = Decimal(str(tr["a"]))
        if amount <= 0:
            continue
        kind = None
        inferred = False
        if w["id"].startswith("treasury_safe") and tr["d"] == "in" and party == COINBASE_PRIME.lower():
            kind = "coinbase_prime_to_safe"
        elif w["id"].startswith("treasury_safe") and tr["d"] == "out" and party == LIFI.lower():
            kind = "safe_to_lifi"
            lifi_outs.append(tr)
        elif w.get("kind") in {"current", "legacy"} and tr["d"] == "in" and party == COINBASE_HOT.lower():
            kind = "coinbase_hot_to_contract"
        elif w.get("kind") in {"current", "legacy"} and tr["d"] == "in" and party == "0x267ebfbcbb7020f200c2749ee441e12574d60c6c":
            kind = "safe_to_contract"
        elif w.get("kind") in {"current", "legacy"} and tr["d"] == "in" and (
            party == LIFI.lower() or party == ZERO or (tr.get("pn") or "").lower().find("lifi") >= 0
            or "minter" in (tr.get("pn") or "").lower() or "messenger" in (tr.get("pn") or "").lower()
        ):
            kind = "bridge_to_contract"
        if kind:
            topups.append(_topup_row(tr, w, kind, inferred, names))
        elif w.get("kind") in {"current", "legacy"} and tr["d"] == "in" and amount >= Decimal("1000"):
            if party not in names:
                candidates.append(tr)
    used = set()
    for src in sorted(lifi_outs, key=lambda r: r["t"]):
        src_amt = Decimal(str(src["a"]))
        src_t = parse_iso(src["t"])
        best = None
        best_score = None
        for cand in candidates:
            key = transfer_key(cand)
            if key in used:
                continue
            ct = parse_iso(cand["t"])
            delta = (ct - src_t).total_seconds()
            if delta < -300 or delta > 6 * 3600:
                continue
            camt = Decimal(str(cand["a"]))
            if camt < src_amt * Decimal("0.97") or camt > src_amt * Decimal("1.001"):
                continue
            score = abs(src_amt - camt)
            if best is None or score < best_score:
                best = cand
                best_score = score
        if best is not None:
            used.add(transfer_key(best))
            w = wallet_by_id[best["w"]]
            row = _topup_row(best, w, "inferred_lifi_to_contract", True, names)
            row["matched_tx"] = src["h"]
            row["note"] = (
                "Inference: amount and time match a Treasury Safe to LI.FI transfer "
                f"({src['h']}, {src['a']} USDC at {src['t']})."
            )
            topups.append(row)
    topups.sort(key=lambda r: r["t"], reverse=True)
    return topups


def _topup_row(tr, wallet, kind, inferred, names):
    return {
        "t": tr["t"],
        "amount": tr["a"],
        "chain": tr["c"],
        "wallet_id": tr["w"],
        "direction": tr["d"],
        "counterparty": tr["p"],
        "counterparty_label": label_party(tr["p"], names),
        "wallet_role": wallet["role"],
        "method": tr.get("m") or None,
        "tx": tr["h"],
        "tx_url": tx_url(tr["c"], tr["h"]),
        "kind": kind,
        "inferred": inferred,
    }


def flow_sums(topups, days, now):
    cut = now - timedelta(days=days)
    buckets = {
        "coinbase_prime_to_safe": Decimal("0"),
        "safe_to_lifi": Decimal("0"),
        "coinbase_hot_to_contract": Decimal("0"),
        "safe_to_contract": Decimal("0"),
        "bridge_to_contract": Decimal("0"),
        "inferred_lifi_to_contract": Decimal("0"),
    }
    by_chain = {k: {} for k in ("safe_to_contract", "bridge_to_contract", "inferred_lifi_to_contract", "coinbase_hot_to_contract")}
    for row in topups:
        if parse_iso(row["t"]) < cut:
            continue
        kind = row["kind"]
        if kind not in buckets:
            continue
        amt = Decimal(str(row["amount"]))
        buckets[kind] += amt
        if kind in by_chain:
            by_chain[kind][row["chain"]] = str(money_d(Decimal(by_chain[kind].get(row["chain"], "0")) + amt))
    return {
        "days": days,
        "amounts": {k: float(money_d(v)) for k, v in buckets.items()},
        "by_chain": by_chain,
    }


def withdrawal_stats(transfers, wallet_ids, now):
    out = {}
    for wid in wallet_ids:
        rows = [tr for tr in transfers if tr["w"] == wid and tr["d"] == "out" and is_withdraw(tr)]
        windows = {}
        for days in (7, 30, 90):
            cut = now - timedelta(days=days)
            picked = [tr for tr in rows if parse_iso(tr["t"]) >= cut]
            amounts = sorted(Decimal(str(tr["a"])) for tr in picked)
            daily = {}
            recipients = set()
            for tr in picked:
                day = tr["t"][:10]
                slot = daily.setdefault(day, {"date": day, "count": 0, "volume": Decimal("0")})
                slot["count"] += 1
                slot["volume"] += Decimal(str(tr["a"]))
                recipients.add(tr["p"].lower())
            if amounts:
                mid = len(amounts) // 2
                if len(amounts) % 2:
                    med = amounts[mid]
                else:
                    med = (amounts[mid - 1] + amounts[mid]) / 2
                med = float(money_d(med))
                mx = float(money_d(amounts[-1]))
            else:
                med = None
                mx = None
            windows[str(days)] = {
                "count": len(picked),
                "volume": float(money_d(sum(amounts, Decimal("0")))),
                "median": med,
                "max": mx,
                "unique_recipients": len(recipients),
                "daily": [
                    {"date": d, "count": daily[d]["count"], "volume": float(money_d(daily[d]["volume"]))}
                    for d in sorted(daily)
                ],
            }
        out[wid] = {"windows": windows, "method": "withdraw / 0xbd69a7ae"}
    return out


def large_moves(transfers, now, limit=80):
    cut = now - timedelta(days=30)
    rows = []
    names = {w["address"].lower(): w["role"] for w in WALLETS}
    for tr in transfers:
        if tr["d"] == "self":
            continue
        if Decimal(str(tr["a"])) < LARGE_USD:
            continue
        if parse_iso(tr["t"]) < cut:
            continue
        w = next(x for x in WALLETS if x["id"] == tr["w"])
        rows.append({
            "t": tr["t"],
            "chain": tr["c"],
            "wallet_id": tr["w"],
            "wallet_role": w["role"],
            "wallet_address": w["address"],
            "direction": tr["d"],
            "amount": tr["a"],
            "counterparty": tr["p"],
            "counterparty_label": label_party(tr["p"], names),
            "method": tr.get("m") or None,
            "tx": tr["h"],
            "tx_url": tx_url(tr["c"], tr["h"]),
        })
    rows.sort(key=lambda r: r["t"], reverse=True)
    return rows[:limit]


def load_seed_points():
    points = []
    if not os.path.exists(SEED):
        return points
    with open(SEED, encoding="utf-8") as f:
        header = f.readline().strip().split(",")
        keys = header[1:]
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split(",")
            raw_ts = parts[0].strip()
            # Seed stamps are Europe/Madrid CEST (UTC+2) in October.
            naive = datetime.strptime(raw_ts.replace(" CEST", "").replace(" CET", ""), "%Y-%m-%d %H:%M")
            offset = timedelta(hours=1 if raw_ts.endswith("CET") else 2)
            dt = (naive - offset).replace(tzinfo=timezone.utc)
            values = []
            for cell in parts[1:]:
                cell = cell.strip()
                if cell.upper() == "NA" or cell == "":
                    values.append(None)
                else:
                    values.append(float(money_d(cell)))
            if len(values) < len(CORE_KEYS):
                values.extend([None] * (len(CORE_KEYS) - len(values)))
            points.append([iso(dt)] + values[: len(CORE_KEYS)])
            if keys[: len(CORE_KEYS)] != CORE_KEYS:
                raise SystemExit(f"seed columns {keys} do not match {CORE_KEYS}")
    return points


def merge_history(previous, balances_by_id, now):
    hist = previous or {}
    points = hist.get("points") or []
    if not points:
        points = load_seed_points()
    values = []
    carried = []
    for key in CORE_KEYS:
        bal = balances_by_id.get(key)
        if bal and bal.get("read_ok"):
            values.append(bal["usdc"])
        elif bal and bal.get("usdc") is not None and bal.get("stale"):
            values.append(bal["usdc"])
            carried.append(key)
        else:
            values.append(None)
            carried.append(key)
    append = False
    if not points:
        append = True
    else:
        prev_vals = points[-1][1:]
        changed = prev_vals != values
        age = (now - parse_iso(points[-1][0])).total_seconds()
        # Hourly heartbeat keeps the line compact. Balance changes append immediately.
        append = changed or age >= 55 * 60
    if append and any(v is not None for v in values):
        points.append([iso(now)] + values)
    # Daily reconstruction is attached by the caller via hist["daily"].
    hist["keys"] = CORE_KEYS
    hist["points"] = points
    hist["last_carried"] = carried
    return hist, append


def mirror_internal(transfers):
    """Copy a transfer onto the other monitored wallet when Blockscout listed only one side."""
    addr_wallets = {}
    for wallet in WALLETS:
        addr_wallets.setdefault((wallet["chain"], wallet["address"].lower()), []).append(wallet)
    by_id = {w["id"]: w for w in WALLETS}
    seen = {(tr["w"], tr["h"], tr["i"]) for tr in transfers}
    extra = []
    for tr in transfers:
        if tr.get("d") not in {"in", "out"} or not tr.get("h"):
            continue
        for other in addr_wallets.get((tr["c"], tr["p"].lower()), []):
            key = (other["id"], tr["h"], tr["i"])
            if key in seen or other["id"] == tr["w"]:
                continue
            src = by_id[tr["w"]]
            extra.append({
                "t": tr["t"],
                "w": other["id"],
                "c": tr["c"],
                "d": "in" if tr["d"] == "out" else "out",
                "a": tr["a"],
                "p": src["address"],
                "m": tr.get("m") or "",
                "h": tr["h"],
                "i": tr["i"],
                "mirrored": True,
            })
            seen.add(key)
    return transfers + extra


def reconstruct_daily(transfers, balances_by_id, cutoff):
    """Balance at each UTC midnight = current balance minus net flows after that midnight.

    The series stops where walking backward would imply a negative balance, which
    means an earlier outbound transfer is missing. Wallets whose scan has not
    reached the lookback cutoff are skipped.
    """
    daily = {}
    today = now_utc().date()
    for wallet in WALLETS:
        key = wallet.get("history_key")
        if not key:
            continue
        bal = balances_by_id.get(key)
        if not bal or not bal.get("read_ok"):
            continue
        if bal.get("covered_from"):
            window_start = parse_iso(bal["covered_from"])
        elif bal.get("transfers_complete"):
            window_start = cutoff
        else:
            continue
        current = Decimal(bal["usdc_exact"])
        flows = []
        for tr in transfers:
            if tr["w"] != wallet["id"] or tr["d"] == "self":
                continue
            signed = Decimal(str(tr["a"])) if tr["d"] == "in" else -Decimal(str(tr["a"]))
            flows.append((parse_iso(tr["t"]), signed))
        flows.sort(reverse=True)
        running = current
        reliable_after = window_start
        for ts, signed in flows:
            previous = running - signed
            if previous < Decimal("-1"):
                reliable_after = ts
                break
            running = previous
        series = []
        day = reliable_after.date()
        if reliable_after != window_start:
            day = reliable_after.date() + timedelta(days=1)
        while day <= today:
            end = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(days=1)
            net_after = sum((amt for ts, amt in flows if ts >= end), Decimal("0"))
            value = current - net_after
            if value < Decimal("-1"):
                break
            if value < 0:
                value = Decimal("0")
            series.append([day.isoformat(), float(money_d(value))])
            day += timedelta(days=1)
        if series:
            daily[key] = series
    return daily


def point_at(points, target: datetime, max_gap_hours):
    best = None
    for row in points:
        ts = parse_iso(row[0])
        if ts <= target:
            best = row
        else:
            break
    if best is None:
        return None
    if (target - parse_iso(best[0])).total_seconds() > max_gap_hours * 3600:
        return None
    return best


def change_from_history(points, index, current, target, max_gap_hours):
    row = point_at(points, target, max_gap_hours)
    if row is None:
        return None
    prev = row[index]
    if prev is None or current is None:
        return None
    return float(money_d(Decimal(str(current)) - Decimal(str(prev))))


def has_role(chain, contract, account):
    data = "0x" + HAS_ROLE_SELECTOR + ROLE_HASH + account.lower().replace("0x", "").rjust(64, "0")
    result, source = rpc_call(chain, "eth_call", [{"to": contract, "data": data}, "latest"])
    return int(result, 16) == 1, source


def read_router(chain, contract):
    result, source = rpc_call(chain, "eth_call", [{"to": contract, "data": ROUTER_SELECTOR}, "latest"])
    if not result or int(result, 16) == 0:
        return None, source
    addr = "0x" + result[-40:]
    return addr, source


def zksync_latest(previous):
    addr = previous.get("address") if previous else None
    if not addr:
        addr = "0x919386306C47b2Fe1036e3B4F7C40D22D2461a23"
    url = f"https://block-explorer-api.mainnet.zksync.io/transactions?address={addr}&limit=1&page=1"
    try:
        data = http_json(url, timeout=30, retries=3)
        item = (data.get("items") or [None])[0]
        if not item:
            raise RuntimeError("no transactions returned")
        received = item.get("receivedAt")
        madrid = None
        if received:
            dt = parse_iso(received.replace("959Z", "Z") if received.endswith("959Z") else received)
            # Keep the raw explorer timestamp; Madrid label is formatted in the UI.
            madrid = received
        return {
            "address": item.get("to") or addr,
            "tx": item["hash"],
            "received_at": received,
            "status": item.get("status"),
            "explorer": f"https://explorer.zksync.io/tx/{item['hash']}",
            "source": "https://block-explorer-api.mainnet.zksync.io",
            "queried_at": iso(now_utc()),
            "note": (
                "Newest transaction returned by the zkSync Era block explorer API "
                "(newest-first, page 1). router() on the Ethereum proxy "
                "0xE80F92077131b9890599E418AE323de71cE1C35a returns this contract address."
            ),
        }
    except Exception as exc:
        if previous:
            prev = dict(previous)
            prev["stale"] = True
            prev["error"] = str(exc)
            return prev
        return {"error": str(exc), "address": addr}


def public_wallet(wallet, bal):
    return {
        "id": wallet["id"],
        "chain": wallet["chain"],
        "address": wallet["address"],
        "role": wallet["role"],
        "attribution": wallet["attribution"],
        "note": wallet["note"],
        "kind": wallet.get("kind"),
        "history_key": wallet.get("history_key"),
        "optional": bool(wallet.get("optional")),
        "usdc": bal.get("usdc"),
        "read_ok": bool(bal.get("read_ok")),
        "stale": bool(bal.get("stale")),
        "token": bal.get("token"),
        "token_symbol": bal.get("token_symbol"),
        "decimals": bal.get("decimals"),
        "explorer": addr_url(wallet["chain"], wallet["address"]),
        "change_24h": bal.get("change_24h"),
        "change_7d": bal.get("change_7d"),
    }


def main():
    started = now_utc()
    cutoff = started - timedelta(days=TRANSFER_LOOKBACK_DAYS)
    retain = started - timedelta(days=TRANSFER_RETAIN_DAYS)
    previous_latest = load_json("latest.json", {})
    previous_transfers = load_json("transfers.json", {"items": []})
    previous_history = load_json("history.json", {})
    previous_flows = load_json("flows.json", {})
    previous_sync = load_json("sync.json", {})
    prev_by_id = {w["id"]: w for w in previous_latest.get("wallets") or []}
    old_keys = {transfer_key(tr) for tr in previous_transfers.get("items") or []}

    errors = []
    balances = {}

    def do_balance(wallet):
        try:
            bal = read_balance(wallet)
            bal["stale"] = False
            return wallet["id"], bal, None
        except Exception as exc:
            prev = prev_by_id.get(wallet["id"]) or {}
            if prev.get("usdc") is not None:
                kept = {
                    "usdc": prev.get("usdc"),
                    "usdc_exact": str(prev.get("usdc")),
                    "token": prev.get("token") or USDC[wallet["chain"]]["address"],
                    "token_symbol": prev.get("token_symbol") or "USDC",
                    "decimals": prev.get("decimals") or USDC[wallet["chain"]]["decimals"],
                    "read_ok": False,
                    "stale": True,
                    "error": str(exc),
                }
                return wallet["id"], kept, f"{wallet['id']} balance kept previous value: {exc}"
            return wallet["id"], {
                "usdc": None,
                "read_ok": False,
                "stale": False,
                "error": str(exc),
                "token": USDC[wallet["chain"]]["address"],
                "token_symbol": "USDC",
                "decimals": USDC[wallet["chain"]]["decimals"],
            }, f"{wallet['id']} balance failed: {exc}"

    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = [pool.submit(do_balance, w) for w in WALLETS]
        for fut in as_completed(futs):
            wid, bal, err = fut.result()
            balances[wid] = bal
            if err:
                errors.append(err)

    fresh = sum(1 for b in balances.values() if b.get("read_ok"))
    if fresh == 0 and not previous_latest:
        print("All balance reads failed and no previous snapshot exists", file=sys.stderr)
        for err in errors:
            print(err, file=sys.stderr)
        return 1

    # Role / router reads. Failures are recorded, not fatal.
    onchain = {"operator": OPERATOR, "role": "BRIDGE_OPERATOR_ROLE", "checks": []}
    for wallet in WALLETS:
        if wallet["chain"] == "bsc":
            continue
        if wallet.get("kind") != "current" and not wallet.get("read_router"):
            continue
        try:
            holds, source = has_role(wallet["chain"], wallet["address"], OPERATOR)
            onchain["checks"].append({
                "wallet_id": wallet["id"],
                "chain": wallet["chain"],
                "address": wallet["address"],
                "has_bridge_operator_role": holds,
                "rpc": source,
            })
        except Exception as exc:
            onchain["checks"].append({
                "wallet_id": wallet["id"],
                "chain": wallet["chain"],
                "address": wallet["address"],
                "error": str(exc),
            })
    try:
        router, source = read_router("ethereum", "0xE80F92077131b9890599E418AE323de71cE1C35a")
        onchain["router"] = {"address": router, "rpc": source, "contract": "0xE80F92077131b9890599E418AE323de71cE1C35a"}
    except Exception as exc:
        onchain["router"] = previous_flows.get("onchain", {}).get("router") or {"error": str(exc)}

    zksync = zksync_latest((previous_flows.get("zksync_router") or {}))

    # Transfers.
    new_transfers = list(previous_transfers.get("items") or [])
    sync = previous_sync or {}
    coverage = {}

    def do_transfers(wallet):
        bal = balances.get(wallet["id"]) or {}
        if wallet.get("optional") and not (bal.get("usdc") or 0):
            return wallet["id"], [], {"skipped": "zero balance"}, None
        if not wallet.get("flows"):
            return wallet["id"], [], {"skipped": True}, None
        entry = (sync.get("wallets") or {}).get(wallet["id"]) or {}
        try:
            if wallet["chain"] == "bsc":
                rows, meta = fetch_bsc_logs(wallet, cutoff, entry, BSC_TIME_BUDGET_S / 2)
            else:
                head_pages = 6 if entry.get("reached_cutoff") else 3
                rows, meta = fetch_blockscout(
                    wallet,
                    cutoff,
                    old_keys,
                    None if entry.get("reached_cutoff") else entry.get("resume"),
                    head_pages=head_pages,
                )
                if entry.get("reached_cutoff"):
                    meta["reached_cutoff"] = True
                    meta["resume"] = None
            return wallet["id"], rows, meta, None
        except Exception as exc:
            return wallet["id"], [], {"error": str(exc), "reached_cutoff": entry.get("reached_cutoff", False)}, str(exc)

    transfer_workers = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = [pool.submit(do_transfers, w) for w in WALLETS if w["chain"] != "bsc"]
        for fut in as_completed(futs):
            wid, rows, meta, err = fut.result()
            coverage[wid] = meta
            if err:
                errors.append(f"{wid} transfers: {err}")
            transfer_workers.append((wid, rows))
    # BSC log scans are sequential so a public RPC is less likely to throttle them.
    for wallet in WALLETS:
        if wallet["chain"] != "bsc":
            continue
        wid, rows, meta, err = do_transfers(wallet)
        coverage[wid] = meta
        if err:
            errors.append(f"{wid} transfers: {err}")
        transfer_workers.append((wid, rows))

    by_key = {transfer_key(tr): tr for tr in new_transfers}
    delta = []
    for wid, rows in transfer_workers:
        for tr in rows:
            key = transfer_key(tr)
            if not tr.get("h"):
                continue
            if key not in by_key:
                delta.append(tr)
            by_key[key] = tr
        # Preserve prior reached_cutoff if this run failed.
        prev_entry = (sync.get("wallets") or {}).get(wid) or {}
        merged = dict(prev_entry)
        for key, value in coverage[wid].items():
            if value is None and key not in {"resume"}:
                continue
            merged[key] = value
        if coverage[wid].get("error") and prev_entry.get("reached_cutoff"):
            merged["reached_cutoff"] = True
            merged["resume"] = None
        sync.setdefault("wallets", {})[wid] = merged

    kept = []
    for tr in by_key.values():
        try:
            if parse_iso(tr["t"]) < retain:
                continue
        except Exception:
            continue
        # Drop bulky optional name field if empty.
        if not tr.get("pn"):
            tr.pop("pn", None)
        kept.append(tr)
    kept.sort(key=lambda tr: (tr["t"], tr["h"], tr["i"]))
    kept = mirror_internal(kept)

    wallet_by_id = {w["id"]: w for w in WALLETS}
    for wid, bal in balances.items():
        meta = sync.get("wallets", {}).get(wid) or {}
        bal["transfers_complete"] = bool(meta.get("reached_cutoff")) and bal.get("read_ok")
        if meta.get("covered_from"):
            bal["covered_from"] = meta["covered_from"]

    # Map history-key balances.
    balances_by_key = {}
    for wallet in WALLETS:
        key = wallet.get("history_key")
        if key:
            balances_by_key[key] = balances[wallet["id"]]

    history, _appended = merge_history(previous_history, balances_by_key, started)
    try:
        history["daily"] = reconstruct_daily(kept, balances_by_key, cutoff)
    except Exception as exc:
        errors.append(f"daily reconstruction: {exc}")
        history["daily"] = previous_history.get("daily") or {}

    # 24h / 7d from snapshot points, falling back to reconstructed daily balances.
    points = history.get("points") or []

    def series_rows(key):
        idx = CORE_KEYS.index(key)
        rows = []
        for day, value in (history.get("daily") or {}).get(key) or []:
            if value is not None:
                rows.append([f"{day}T00:00:00Z", value])
        for row in points:
            if row[idx + 1] is not None:
                rows.append([row[0], row[idx + 1]])
        rows.sort(key=lambda r: r[0])
        return rows

    for wallet in WALLETS:
        key = wallet.get("history_key")
        bal = balances[wallet["id"]]
        if not key or bal.get("usdc") is None:
            bal["change_24h"] = None
            bal["change_7d"] = None
            continue
        rows = series_rows(key)
        bal["change_24h"] = change_from_history(rows, 1, bal["usdc"], started - timedelta(hours=24), 36)
        bal["change_7d"] = change_from_history(rows, 1, bal["usdc"], started - timedelta(days=7), 72)

    visible = []
    for wallet in WALLETS:
        bal = balances[wallet["id"]]
        if wallet.get("optional") and not (bal.get("usdc") or 0):
            continue
        visible.append(public_wallet(wallet, bal))

    total = Decimal("0")
    total_ok = True
    for row in visible:
        if row["usdc"] is None:
            total_ok = False
            continue
        total += Decimal(str(row["usdc"]))

    def sum_change(field):
        acc = Decimal("0")
        for wallet in WALLETS:
            if not wallet.get("history_key"):
                continue
            bal = balances[wallet["id"]]
            if bal.get("usdc") is None or bal.get(field) is None:
                return None
            acc += Decimal(str(bal[field]))
        return float(money_d(acc))

    checked_zero = []
    for wallet in WALLETS:
        if not wallet.get("optional"):
            continue
        bal = balances[wallet["id"]]
        if bal.get("read_ok") and not (bal.get("usdc") or 0):
            checked_zero.append({
                "id": wallet["id"],
                "chain": wallet["chain"],
                "address": wallet["address"],
                "token": bal.get("token"),
                "token_symbol": bal.get("token_symbol"),
                "usdc": 0,
            })

    current_ids = [w["id"] for w in WALLETS if w.get("kind") == "current" and not w.get("optional")]
    # Include optional current contracts when they hold a balance.
    for w in WALLETS:
        if w.get("optional") and w.get("kind") == "current" and (balances[w["id"]].get("usdc") or 0) > 0:
            current_ids.append(w["id"])

    try:
        fill_bsc_methods(kept)
        topups = classify_topups(kept, wallet_by_id)
        withdrawals = withdrawal_stats(kept, current_ids, started)
        larges = large_moves(kept, started)
        flows_windows = {"7": flow_sums(topups, 7, started), "30": flow_sums(topups, 30, started)}
    except Exception as exc:
        errors.append(f"flow math: {exc}")
        topups = previous_flows.get("topups") or []
        withdrawals = previous_flows.get("withdrawals") or {}
        larges = previous_flows.get("large_moves") or []
        flows_windows = previous_flows.get("windows") or {}

    if not previous_latest:
        since = []
        cursor_note = "Cursor initialized on this run. transfers_since_last_run starts on the next successful run."
    else:
        since = delta
        cursor_note = None
    since_sorted = sorted(since, key=lambda tr: tr["t"], reverse=True)
    since_public = []
    for tr in since_sorted[:200]:
        w = wallet_by_id[tr["w"]]
        since_public.append({
            "t": tr["t"],
            "chain": tr["c"],
            "wallet_id": tr["w"],
            "wallet_role": w["role"],
            "direction": tr["d"],
            "amount": tr["a"],
            "counterparty": tr["p"],
            "method": tr.get("m") or None,
            "tx": tr["h"],
            "tx_url": tx_url(tr["c"], tr["h"]),
        })

    latest = {
        "schema_version": 1,
        "generated_at": iso(started),
        "cursor_note": cursor_note,
        "totals": {
            "usdc": float(money_d(total)),
            "change_24h": sum_change("change_24h"),
            "change_7d": sum_change("change_7d"),
            "wallets_included": len(visible),
            "wallets_read_ok": fresh,
            "partial": (not total_ok) or any(w.get("stale") for w in visible),
        },
        "wallets": visible,
        "checked_zero_usdc": checked_zero,
        "transfers_since_last_run": since_public,
        "transfers_since_last_run_truncated": len(since_sorted) > 200,
        "transfers_since_last_run_count": len(since_sorted),
        "errors": errors,
    }

    flows = {
        "schema_version": 1,
        "generated_at": iso(started),
        "windows": flows_windows,
        "topups": [r for r in topups if parse_iso(r["t"]) >= started - timedelta(days=30)][:300],
        "withdrawals": withdrawals,
        "large_moves": larges,
        "coverage": coverage,
        "onchain": onchain,
        "zksync_router": zksync,
        "counterparties": [
            {
                "id": "coinbase_prime",
                "address": COINBASE_PRIME,
                "label": "Coinbase Prime 1",
                "basis": "Address prefix 0xcd531ae9 in the source notes. Full address is the largest Ethereum USDC sender into the Treasury Safe on the Blockscout page reviewed while building this monitor.",
            },
            {
                "id": "coinbase_hot",
                "address": COINBASE_HOT,
                "label": "Coinbase hot wallet",
                "basis": "Source notes identify 0x4b5c7108…34f6 as a Coinbase hot wallet (Arkham entity label). Full address taken from inbound USDC transfers to the Base contract.",
            },
            {
                "id": "lifi",
                "address": LIFI,
                "label": "LI.FI diamond",
                "basis": "Recipient of Treasury Safe USDC transfers. Blockscout labels the address LiFiDiamond.",
            },
            {
                "id": "operator",
                "address": OPERATOR,
                "label": "Withdrawal operator",
                "basis": "Source notes. Live hasRole results are in onchain.checks. Recent Base withdraw transactions are sent by this address.",
            },
            {
                "id": "router",
                "address": (onchain.get("router") or {}).get("address"),
                "label": "router()",
                "basis": "eth_call router() on Ethereum proxy 0xE80F92077131b9890599E418AE323de71cE1C35a.",
            },
        ],
    }

    for tr in kept:
        tr.pop("pn", None)

    transfers_doc = {
        "schema_version": 1,
        "generated_at": iso(started),
        "retain_days": TRANSFER_RETAIN_DAYS,
        "lookback_days": TRANSFER_LOOKBACK_DAYS,
        "count": len(kept),
        "items": kept,
    }
    history["generated_at"] = iso(started)
    sync["generated_at"] = iso(started)

    # If every balance failed, do not replace a good snapshot's totals with empties.
    if fresh == 0 and previous_latest:
        print("All balance reads failed; leaving previous JSON in place")
        for err in errors:
            print(err)
        return 1

    write_json("latest.json", latest)
    write_json("history.json", history)
    write_json("flows.json", flows)
    write_json("transfers.json", transfers_doc)
    write_json("sync.json", sync)
    print(
        f"wrote snapshot {iso(started)} total={latest['totals']['usdc']} "
        f"wallets_ok={fresh} transfers={len(kept)} new={len(since_sorted)} errors={len(errors)}"
    )
    for err in errors:
        print("WARN", err)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
