# Bookkeeping Utilities

| Utility | Description |
|---|---|
| `reconcile.py` | Merges statements from multiple sources, reconciles transfers, applies bookkeeping rules, emits a single reconciled multi-account QIF |
| `venmo2ofx` | Standalone Venmo CSV → OFX converter (original utility) |
| `plaid-pull.py` | Fetches transactions via Plaid's transactions-sync API |

---

# reconcile.py

Merges statement files from multiple sources, reconciles transfers across accounts, applies bookkeeping rules from `reconcile.yaml`, and emits a single reconciled multi-account QIF.

## Usage

```bash
# Reconcile a month's statements to QIF
./reconcile.py --period 2026-09 -o out.qif \
  --rules reconcile.yaml \
  inbox/plaid-*.json inbox/*.ofx inbox/*.csv

# Or a full year
./reconcile.py --period 2026 -o out-2026.qif \
  --rules reconcile.yaml \
  inbox/plaid-*.json inbox/*.ofx inbox/*.csv
```

Copy `reconcile.yaml.example` to `reconcile.yaml` and fill in your account mappings and categorization rules.

## Arguments

#### `--period PERIOD` (required)

* Period to include: `YYYY-MM` for a month, `YYYY` for a full year.

#### `-o, --output FILE`

* Output QIF path (default: `<period>.qif`, e.g. `2026-09.qif`). Aborts if the file already exists — delete it or specify a different path.

#### `--rules FILE`

* Rules file (default: `reconcile.yaml` in the current directory).

#### `--allow-unpaired`

* List transfers with no mirror side to stderr instead of aborting (they are still emitted, flagged).

#### `--unknowns-out FILE`

* Write uncategorized transactions as JSON for research.

#### `--register FILE`

* QuickBooks register xls for enriching INTUIT/PAYROLL bank transactions with payee/memo detail (one-to-one match by date+amount; register entries are enrichment-only, never emitted).

#### `inputs` (required)

* One or more statement files (any supported format, mixed).

## Input formats

#### Plaid JSON

* Output of `plaid-pull.py` (transactions-sync, with MCC/PFC metadata)
* Account ID comes from the filename: `plaid-1234.json` → `1234` (the account mask)

#### Venmo CSV

* Monthly statements from venmo.com (detected by `Account Statement` header row or business-format columns)
* Account ID comes from the `Account Statement - (@user)` header row → `@user`

#### OFX/QFX

* Bank and card exports
* Account ID comes from the account number inside the file

#### Schwab brokerage JSON

* Web-export transaction history (detected by `BrokerageTransactions` structure)
* Account ID comes from the filename: `..._XXX1234_...` → `1234`

#### QuickBooks banking CSV

* Online banking transaction exports (detected by `Bank description` / `Transaction Posted` columns)
* QuickBooks names its download `Intuit.csv` with no account identifier. Rename it so the account ID can be extracted: `plaid-<id>` (e.g. `plaid-1234.csv`) or containing `XXX<id>` (e.g. `checking_XXX1234.csv`). Then map that ID in `accounts:`. The program aborts if it can't identify the source account for an input file.

#### QuickBooks register

* `.xls`/`.xlsx` bank account report exports, used via `--register` to enrich INTUIT/PAYROLL transactions with payee/memo detail (enrichment-only, never creates transactions)

## How it works

1. **Parse** — every input becomes the same transaction dict (date, amount, payee, memo, account id)
2. **Enrich** (optional `--register`) — fills in missing payee/memo detail from a QuickBooks register export via one-to-one date+amount matching; each bank transaction matches at most one register entry, so two same-amount same-day payments can't collapse onto one person. Only INTUIT/PAYROLL transactions are eligible (the register is a payroll detail source). **The register never creates transactions** — it only adds detail to transactions already imported from bank sources (Plaid, banking CSV, etc.). This is deliberate: the register is QuickBooks' internal view, which includes manual entries, QB-specific categorizations, and items that may not reflect actual bank activity 1:1. The bank inputs are the source of truth for money movement; the register's value is its rich payee/memo detail that bank exports lack.
3. **Classify** — each transaction is categorized via `reconcile.yaml`. See `RULES.md` for the full rule format. Rules are tried in order; the first match wins. Order is: hand-written rules without `source:` (in YAML order) first, then the rest sorted by pattern length (longer patterns first). Amount-only rules (no pattern) sort last as catch-alls.
4. **Reconcile** — pairs transfer legs across accounts; strict by default, `--allow-unpaired` lists instead of aborting
5. **Emit** — a single reconciled multi-account QIF (one file, all accounts, deterministic)

## The rules file (`reconcile.yaml`)

Copy `reconcile.yaml.example` to `reconcile.yaml`. See `RULES.md` for the full
rule format documentation.

Two top-level keys: `source_accounts:` (mapping of account IDs to paths) and
`rules:` (a list of rule dicts using `if:`/`then:` structure).

---

# Import mechanisms

## venmo2ofx

Standalone Venmo CSV → OFX converter (the original utility this repo is named for).

**Note:** `reconcile.py` subsumes this functionality via its modular Venmo input parser. To recreate `venmo2ofx` with `reconcile.py`:

```bash
./reconcile.py --period 2026-09 -o out.qif venmo-statement.csv
```

The Venmo parsing logic lives in `reconcile.py`'s `parse_venmo_csv` — Venmo is one of several supported input formats, not a special case. (Output is QIF rather than OFX; use the standalone `venmo2ofx` if you specifically need OFX.)

Bookkeeping with Venmo is challenging as you basically need to treat it as another bank account to reconcile if you need to track transactions, such as if you are using Venmo to buy or sell for business-related purposes. Venmo allows you to download monthly CSV [statements](https://account.venmo.com/statement) one at a time and uses an proprietary format that is difficult to import into bookkeeping software.

## Usage (standalone)

`venmo2ofx [input files...] [> output.ofx]`

## plaid-pull.py

Fetches transactions via Plaid's `transactions-sync` API.

### Arguments

#### `--days-requested N` (default: 730)

* History hint for the *first* sync only (no saved cursor yet). Max 730. Ignored once a cursor exists — subsequent pulls are purely cursor-based.

```bash
./plaid-pull.py                        # incremental pull, resumes from saved cursor
./plaid-pull.py --days-requested 90    # shorter history on first run (default is 730)
```

**Requires:** a `plaid` CLI on PATH that supports `plaid transactions-sync`. Without it, feed `reconcile.py` with OFX/QFX or CSV exports instead.

### Log output

All progress goes to stderr. Each institution is tagged with its display name in brackets:

```text
plaid-pull: syncing 3 institution(s)
[Example Bank] resuming from saved cursor
[Example Bank] page 1: +12 ~0 -0
[Example Bank] delta: plaid/incremental/<id>/20261004T123456-000000Z.json (+12 ~0 -0)
[Example Bank] plaid-1234.json: 1240 transactions (3 pending)
[Example Bank] materialized: +12 new, ~0 modified, -0 removed
[Example Bank] cursor saved -> plaid/incremental/<id>/cursor.json
plaid-pull: FAILED [Other Bank]: ITEM_LOGIN_REQUIRED ...
```

A failed institution is logged and skipped; the rest continue. The process exits non-zero if any institution failed, so callers know the sync is incomplete.

### Directory structure

Everything lives under `plaid/`, with each linked institution (bank connection) syncing independently — a failure in one (needs re-auth, locked, etc.) does not block the others:

```
plaid/
  plaid-<last4>.json         # materialized current state, one per account
                             # {"institution": ..., "account_name": ...,
                             #  "account_type": ..., "mask": ..., "transactions": [...]}
  incremental/
    <credential_id>/
      <timestamp>.json       # immutable per-fetch deltas, this institution only
      cursor.json            # opaque cursor for this institution only
```

`<last4>` is the last four digits of the account number (Plaid's `mask` field) — e.g. `plaid-1234.json`.

Each run first lists institutions via `plaid accounts`, then syncs each one separately with `plaid transactions-sync --credential-id <id>`. Each institution gets one timestamped delta file per run holding exactly what Plaid returned for that institution — added / modified / removed transactions plus the before/after cursor. The `plaid-<last4>.json` files are derived by folding deltas in order — `reconcile.py` reads these, not the deltas directly. Each institution's cursor only advances after its delta is written *and* materialization succeeded, so a failed institution changes nothing and re-running resumes it from its saved cursor while the others stay current.

### Why incrementals: the cost of a full dump

Plaid's `/transactions/sync` is designed for cursor-based incrementals, not full re-pulls. A full non-cursor dump is expensive:

* **Several connected institutions**, each paginated separately (cursors are per-Item, not global).
* **Thousands of transactions** materialized — and growing every month. A 2-year history pull (`--days-requested 730`) fetches all of them, at 500 per page.
* That's **dozens of paginated API requests** across all institutions, every single run — re-downloading the entire history to get the handful of new transactions.
* A rate limit (HTTP 429) or pagination mutation mid-pull discards all progress. Large pulls are more likely to trigger both.

An incremental pull fetches only what changed since the last cursor — typically 1–2 pages total. The 429 handler persists partial progress as a delta file and advances the cursor past it, so the next run continues instead of re-fetching. Pending transactions are kept in the fetch (not filtered at pull time): a pending txn arrives in `added` with `pending=true`, and when it posts Plaid sends `removed: [<pending_id>]` + `added: [<posted>]` — the materializer promotes in place via `pending_transaction_id`, so no duplicate remains. `reconcile.py` drops pending transactions at merge time.
