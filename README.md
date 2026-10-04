# Vest USDC monitor

Public dashboard of on-chain USDC balances and transfers for a monitored set of wallets. The site is static. A scheduled GitHub Action reads public RPCs and Blockscout, writes JSON under `data/`, and commits only when those files change.

The page reports wallet balances and transfers. It does not report Vest’s virtual or funded-account balances. Vest’s own terms, quoted on the page, describe those balances as virtual, notional, or simulated. Where this repository infers a link to Vest, the inference is labeled.

Data from public blockchains. Not financial advice. Not affiliated with Vest Markets.

## Live data

The page is static files at the repository root. Paths to `data/` are relative so the same page works from GitHub Pages or from a raw file host. GitHub Pages is not enabled on this repository yet. Until it is, the page is viewed from the default branch with relative assets.

The workflow is **Refresh on-chain data** (`.github/workflows/refresh.yml`). The schedule is `9,29,49 * * * *`, plus `workflow_dispatch`. GitHub has registered that cron and it does start some runs, but most 20-minute slots are dropped. After a successful run the job waits 12 minutes and, if nothing is already queued, dispatches the same workflow on `main` with `GITHUB_TOKEN`. That dispatch is allowed from `GITHUB_TOKEN`; ordinary token pushes are not. The cron remains as a backup.

JSON for other tools, on the default branch:

- `https://raw.githubusercontent.com/diatle-glitch/vest-watch/main/data/latest.json`
- `https://raw.githubusercontent.com/diatle-glitch/vest-watch/main/data/history.json`
- `https://raw.githubusercontent.com/diatle-glitch/vest-watch/main/data/flows.json`

`data/latest.json` is the alert payload: `generated_at`, `totals` (USDC, 24h change, 7d change), `wallets[]` (chain, role, balance, attribution, stale flag), and `transfers_since_last_run` (USDC transfers seen for the first time on that run). The first successful run initializes the cursor and leaves `transfers_since_last_run` empty. Later runs list the new transfers.

## What is updated

| File | Contents |
| --- | --- |
| `data/latest.json` | Current balances, totals, transfers since the previous run, read errors |
| `data/history.json` | Append-only balance points for the seven seed wallets, plus daily reconstruction when transfer history is complete |
| `data/flows.json` | 7- and 30-day top-up sums, 1/7/30-day contract flow summary, custody moves, withdrawal stats, large moves, role checks, zkSync router check |
| `data/transfers.json` | Rolling transfer log (about 120 days) used to rebuild the aggregates |
| `data/sync.json` | Per-wallet scan cursor, so a later run continues a 90-day backfill instead of starting over |
| `data/seed-history.csv` | Hourly seed from 2026-10-02 21:35 Europe/Madrid (CEST). `NA` is a failed read and is not treated as zero |

A snapshot point is appended when a core balance changes, or when the last point is at least 55 minutes old. A failed balance read does not overwrite the last good value.

## Wallets

USDC contracts: Ethereum `0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48`, Base `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`, Arbitrum `0xaf88d065e77c8cC2239327C5EDb3A432268e5831` (6 decimals), BSC `0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d` (18 decimals). Optimism and Polygon use native USDC, and bridged USDC.e if native USDC is zero.

| Id | Chain | Address | Attribution |
| --- | --- | --- | --- |
| Treasury Safe | Ethereum, and the same address on Base and Arbitrum | `0x267EbfbcBb7020F200c2749Ee441E12574d60C6c` | Inferred. Funded by `0xCD531Ae9EFCCE479654c4926dec5F6209531Ca7b` (Coinbase Prime 1 in the source notes) and sends USDC to the LI.FI diamond. |
| Second Safe | Ethereum | `0x0e66f5443Db92423b692866CFDd9D9F6AcDe8597` | Inferred. Not named in the Vest pages reviewed for this site. |
| Current Base contract | Base | `0x55133c825603E6A5b9E911ABAb23e75Dc3Bb07aF` | Observed payout/deposit contract (`withdraw`). |
| Current Arbitrum contract | Arbitrum | `0xB2F86eAE1197032Fa85389cc6c0F3b06B58Dd1Ea` | Observed. |
| Current Ethereum contract | Ethereum | `0xE80F92077131b9890599E418AE323de71cE1C35a` | Proxy. Verified implementation [SrcBridge](https://eth.blockscout.com/address/0xEcD91C77B98d507E3C20Bac86D2541ECbDc881E3) contains `NAME = "VestRouterV2"`. |
| Same address | Optimism, Polygon, Base | `0xE80F92077131b9890599E418AE323de71cE1C35a` | Included in the total only when it holds USDC. |
| Current BNB contract | BSC | `0x74dD5d9ca86CE7FAec8403210dd37ea9122F0879` | Observed. |
| Older contracts | Arbitrum `0x80C526d1c2fddADB3Cd39810cd7A79E07b0EDa00`, Base `0x32d95F243F9E2c1344E4BAa91a8D32711527ef7e`, BSC `0xef14da66876476C1A75dC057343B97b6Bd372c41` | | Observed historical contracts. |

Counterparties, not added into the balance total:

- Coinbase hot wallet `0x4B5c71082d027D16d2A146465d66f9EEC11634F6`. Source notes cite an Arkham Coinbase label for `0x4b5c7108…34f6`. The full address is the sender of large inbound USDC transfers to the Base contract.
- Coinbase Prime deposit forwarder (inferred) `0x18F0Ddbab74A4BF7f4EF5c5469334CAc0DdC5b77`. Not a tracked balance. Outbound USDC from this address goes to Coinbase Prime 1 in the same minute. On 2026-10-03 at about 01:10 UTC the Treasury Safe sent it 400,000 USDC, forwarded to Coinbase Prime 1 in the transaction starting `0xd71f29e5`. Treasury Safe outflows to this address, or directly to Coinbase or Coinbase Prime, are classified as moved to custody (inferred). They are not payouts.
- LI.FI diamond `0x1231DEB6f5749EF6cE6943a275A1D3E7486F4EaE` (Blockscout name LiFiDiamond).
- Withdrawal operator `0xBbBACC96be3d045b9Ca71bd422c78AFb80CF5970`. Each run calls `hasRole(BRIDGE_OPERATOR_ROLE)` on the current contracts.
- `router()` on the Ethereum proxy. The returned address is checked against the zkSync Era explorer.

## How a number is produced

Balances: RPC `eth_call` `balanceOf(address)` against the USDC contract, via public RPCs, with retries and fallbacks. Blockscout `/token-balances` can lag that read. On 4 Oct 2026 it still showed $378.7k for the Base contract when the live `balanceOf` was $216.9k. Transfers: Blockscout v2 where a keyless instance responds (Ethereum, Base, Arbitrum, Optimism, Polygon). BSC uses `eth_getLogs` because the BSC Blockscout hosts tried here returned 404. BSC log timestamps inside a chunk are estimated from the chunk end block.

Inference: the on-chain wallets appear to act as a payout float topped up in batches from Coinbase and Coinbase Prime. Custody and exchange balances are not visible on-chain, so the tracked total is not total firm reserves.

Daily history is the current balance minus later transfers, and it stops where an earlier day would imply a negative balance. A transfer between two monitored wallets is copied to the other wallet when the explorer returned only one side.

Top-up legs are classified from counterparty addresses. A contract inflow is marked as an inferred LI.FI completion only when it is within 3% of a Treasury Safe → LI.FI amount and within 6 hours after that transfer. The UI labels that row as an inference.

The headline flow table covers 1, 7, and 30 days for the payout and deposit contracts. Treasury top-ups are inflows from the Treasury Safe, Coinbase, Coinbase Prime, LI.FI, or a mint/bridge sender, including inferred LI.FI completions. User deposits are other external inflows. Transfers between monitored wallets are omitted. Filtered outflows are `withdraw` / `0xbd69a7ae` outs to an address outside that set. Net is top-ups plus user deposits minus those outflows. Custody moves are excluded from the outflows.

Payout stats use outbound transfers whose method is `withdraw` or selector `0xbd69a7ae`. Count, volume, median, max, and unique recipients are computed for 7, 30, and 90 days. Large moves are transfers of at least 10,000 USDC. Treasury Safe outs to the deposit forwarder, Coinbase, or Coinbase Prime are labeled moved to custody (inferred) there as well.

The verified SrcBridge `withdraw` path releases tokens after an operator check or a validator ECDSA signature (`BRIDGE_OPERATOR_ROLE`), plus a request signature and a replay check. The verified source has no on-chain balance variable and no Merkle or state-root check. `signatureProof` in that function is a signer-delegation check. See the methodology section on the site for the quotes and the contract link.

Evaluation and program purchases are described in Vest’s privacy policy as Stripe Checkout payments. Those card payments are off-chain and are not included in these USDC totals.

## Local refresh

```bash
python3 scripts/fetch.py
```

Requires Python 3.11+ and network access. No third-party packages. `VEST_MAX_PAGES` (default 160) is the Blockscout backfill page budget per wallet per run. `VEST_BSC_BUDGET` (default 240) is the BSC log time budget in seconds, split across the BSC wallets.

## Pages

The site is `index.html`, `styles.css`, and `app.js`. Chart.js is loaded from a CDN. There is no build step. GitHub Pages should publish the root of the `main` branch.
