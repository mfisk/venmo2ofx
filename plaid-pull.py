#!/usr/bin/env python3
"""Incremental Plaid transaction pull with per-institution cursor persistence.

Each linked institution (bank connection) syncs independently. A failure in
one institution (needs re-auth, locked, rate-limited, etc.) does not block
the others; each institution has its own incremental directory and cursor,
so they can be on different dates.

For each institution, every run appends ONE new immutable file to
plaid/incremental/<credential_id>/ holding the raw delta Plaid returned for
that fetch (added / modified / removed + cursor). The plaid/plaid-<last4>.json
files are MATERIALIZED current state, derived by folding deltas in order;
reconcile.py reads them exactly as before. (<last4> is the last four digits
of the account number, Plaid's `mask` field.)

Directory layout:
    plaid/
      plaid-<last4>.json          # materialized, one per account
      incremental/
        <credential_id>/
          <timestamp>.json       # immutable per-fetch deltas, this institution only
          cursor.json            # opaque cursor for this institution only

Cursor file: plaid/incremental/<credential_id>/cursor.json holds the opaque
next_cursor string the CLI returned, stored verbatim and never parsed. The
saved cursor advances only after the delta file is written AND materialization
succeeded. A failed run changes nothing on disk for that institution, so
re-running is always safe: it resumes from the saved cursor and skips nothing.

Cursor semantics (Plaid /transactions/sync):
- next_cursor is an opaque per-Item position in Plaid's change log, NOT a date
  watermark. Backdated transactions (late-arriving history, old posting dates)
  simply appear in `added` with their old dates; no date arithmetic is needed
  on our side and none is done.
- Pending lifecycle: a pending transaction first arrives in `added` with
  pending=true. When it posts, Plaid does NOT modify it in place; it sends
  `removed: [<pending transaction_id>]` plus `added: [<posted transaction>]`,
  where the posted row carries `pending_transaction_id` pointing back at the
  pending id (this back-reference is NOT guaranteed). A pending transaction
  that never settles (released hold, cancelled authorization) arrives as
  `removed` with no matching `added`. The materializer pairs removed+added via
  pending_transaction_id (promotion in place); unmatched removals are plain
  deletes. Applying the same delta twice is a no-op.

Failure behavior (per institution, loud, never silent):
- One institution failing does NOT abort the others. Each failure is logged
  to stderr and the run continues; the process exits non-zero at the end if
  any institution failed, so callers know the sync is incomplete.
- HTTP 429 / connector rate limit: persist the pages fetched so far as a
  partial delta, advance the saved cursor past them, then record the abort.
  Re-running continues where this run stopped instead of re-fetching.
- TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION: discard this attempt's pages
  and restart pagination from the run's starting cursor (up to 3 attempts).
- has_more=true with no next_cursor, or has_more=false with no next_cursor:
  record the failure for this institution, change nothing.
- An account_id whose mask changed since the previous run, or two account_ids
  sharing one mask: record the failure instead of merging the wrong accounts
  together.

A saved cursor never advances past data that was not both written to a
delta file and folded into the materialized JSONs.
"""

import argparse
import json
import os
import subprocess
import sys
import traceback
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
PLAID_DIR = os.path.join(BASE, "plaid")
DELTA_DIR = os.path.join(PLAID_DIR, "incremental")


def die(msg):
    print(f"plaid-pull: FATAL: {msg}", file=sys.stderr)
    sys.exit(1)


def warn(msg):
    print(f"plaid-pull: WARNING: {msg}", file=sys.stderr)


def atomic_write_json(path, obj):
    """Write JSON atomically: temp file in the same directory, then rename."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
        f.write("\n")
    os.rename(tmp, path)


def load_json(path):
    with open(path) as f:
        return json.load(f)


class SyncError(Exception):
    pass


class MutationDuringPagination(SyncError):
    pass


class RateLimited(SyncError):
    pass


def get_institutions():
    """Run `plaid accounts`; return [{credential_id, display_name}, ...].

    Raises SyncError if the CLI fails or the output is unparseable.
    """
    raw = subprocess.run(["plaid", "accounts"], capture_output=True, text=True)
    if raw.returncode != 0:
        raise SyncError("plaid accounts failed:\n" + raw.stderr.strip())
    try:
        body = json.loads(raw.stdout).get("body", {})
    except json.JSONDecodeError:
        raise SyncError("could not parse plaid accounts output as JSON:\n"
                        + raw.stdout[:2000])
    institutions = []
    for inst in body.get("institutions", []) or []:
        cid = inst.get("credential_id")
        if not cid:
            continue
        institutions.append({
            "credential_id": cid,
            "display_name": inst.get("display_name") or cid,
        })
    return institutions


def run_sync(credential_id, cursor, days_requested):
    """One CLI call = one page for one institution."""
    cmd = ["plaid", "transactions-sync", "--count", "500",
           "--credential-id", credential_id]
    if cursor is None:
        cmd += ["--days-requested", str(days_requested)]
    else:
        cmd += ["--cursor", cursor]
    raw = subprocess.run(cmd, capture_output=True, text=True)
    if raw.returncode != 0:
        err = raw.stderr.strip()
        if "429" in err or "request limit exceeded" in err.lower():
            raise RateLimited(err)
        if "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION" in err:
            raise MutationDuringPagination(err)
        raise SyncError("plaid transactions-sync failed:\n" + err)
    try:
        body = json.loads(raw.stdout).get("body", {})
    except json.JSONDecodeError:
        raise SyncError("could not parse plaid transactions-sync output as "
                        "JSON:\n" + raw.stdout[:2000])
    return body


def body_list(body, *names):
    for n in names:
        v = body.get(n)
        if isinstance(v, list):
            return v
    txns = body.get("transactions")
    if isinstance(txns, dict):
        for n in names:
            v = txns.get(n)
            if isinstance(v, list):
                return v
    return []


def collect_accounts(body):
    """account_id -> full account metadata. Walk the whole body; the CLI nests
    per-institution payloads."""
    accounts = {}

    def walk(o):
        if isinstance(o, dict):
            for a in o.get("accounts", []) or []:
                if isinstance(a, dict) and a.get("account_id"):
                    accounts[a["account_id"]] = {
                        "name": a.get("name", ""),
                        "official_name": a.get("official_name", ""),
                        "mask": a.get("mask", "") or a["account_id"][-4:],
                        "type": a.get("type", ""),
                        "subtype": a.get("subtype", ""),
                    }
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(body)
    return accounts


def removed_ids(removed):
    ids = []
    for r in removed:
        if isinstance(r, dict):
            ids.append(r.get("transaction_id"))
        elif isinstance(r, str):
            ids.append(r)
    return [i for i in ids if i]


def institution_dirs(credential_id):
    """Return (inst_dir, cursor_path) for one institution."""
    inst_dir = os.path.join(DELTA_DIR, credential_id)
    return inst_dir, os.path.join(inst_dir, "cursor.json")


def load_cursor(cursor_path):
    """Return the saved cursor string, or None for a first sync.

    Raises SyncError if the file exists but is unreadable.
    """
    if not os.path.exists(cursor_path):
        return None
    try:
        saved = load_json(cursor_path)
    except (json.JSONDecodeError, OSError) as e:
        raise SyncError(
            f"cursor file {cursor_path} is unreadable ({e}); refusing to "
            "guess. Restore it from backup or delete it to start a fresh "
            "initial sync.")
    return saved.get("cursor")


def sync_institution(credential_id, display_name, days_requested):
    """Sync one institution: paginate, write delta, materialize, save cursor.

    Raises SyncError on any failure; the caller isolates failures per
    institution. Nothing is written for this institution unless the sync
    produced at least one good page with a usable cursor.
    """
    inst_dir, cursor_path = institution_dirs(credential_id)
    os.makedirs(inst_dir, exist_ok=True)

    start_cursor = load_cursor(cursor_path)
    initial = start_cursor is None
    tag = f"[{display_name}]"
    if initial:
        print(f"{tag} no saved cursor: initial sync "
              f"(days_requested={days_requested})", file=sys.stderr)
    else:
        print(f"{tag} resuming from saved cursor", file=sys.stderr)

    # Paginate. One "attempt" runs from start_cursor to has_more=false.
    # Mutation-during-pagination discards the attempt's pages and restarts
    # (up to 3 attempts). A 429 (or other sync error) after at least one good
    # page persists those pages as a partial delta and advances the cursor
    # past them, so the next run continues instead of re-fetching.
    added, modified, removed = [], [], []
    accounts = {}
    last_cursor = None
    pages = 0
    abort_reason = None
    abort_detail = ""
    attempts = 0
    while True:
        attempts += 1
        if attempts > 3:
            raise SyncError("pagination mutated 3 times in a row; aborting "
                            "this institution. Nothing was written; re-run "
                            "to resume from the saved cursor.")
        added, modified, removed = [], [], []
        accounts = {}
        last_cursor = None
        pages = 0
        cursor = start_cursor
        try:
            while True:
                pages += 1
                body = run_sync(credential_id, cursor, days_requested)
                accounts.update(collect_accounts(body))
                a = body_list(body, "added")
                m = body_list(body, "modified")
                r = body_list(body, "removed")
                added.extend(a)
                modified.extend(m)
                removed.extend(r)
                print(f"{tag} page {pages}: +{len(a)} ~{len(m)} -{len(r)}",
                      file=sys.stderr)
                nxt = body.get("next_cursor")
                if body.get("has_more"):
                    if not nxt:
                        raise SyncError(
                            "has_more=true but no next_cursor returned; "
                            "refusing to continue a partial pagination.")
                    last_cursor = nxt
                    cursor = nxt
                    continue
                last_cursor = nxt
                break
        except MutationDuringPagination:
            warn(f"{tag} TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION: "
                 "restarting pagination from the run's starting cursor")
            continue
        except RateLimited as e:
            abort_reason = "rate_limited"
            abort_detail = str(e)
            break
        except SyncError as e:
            abort_reason = "sync_error"
            abort_detail = str(e)
            break
        break

    if abort_reason and pages == 0:
        # Nothing was fetched; there is no cursor to advance to.
        raise SyncError(
            f"aborted ({abort_reason}) before any page succeeded. Nothing "
            f"was written and the saved cursor is unchanged.\n" + abort_detail)

    if last_cursor is None:
        # Either Plaid returned a final page with no cursor (refuse to advance
        # blind), or we aborted on the first page after a cursorless initial
        # request. Either way there is no safe cursor to persist.
        reason = (f"aborted ({abort_reason}) after {pages} page(s) with no "
                  f"usable cursor.\n{abort_detail}" if abort_reason else
                  "sync finished with has_more=false but no next_cursor; "
                  "refusing to advance without a cursor.")
        raise SyncError(reason + " Nothing was written.")

    # Mask sanity: one mask per account_id, one account_id per mask.
    mask_of_account = {}
    account_of_mask = {}
    for aid, info in accounts.items():
        mask = info["mask"]
        if mask in account_of_mask and account_of_mask[mask] != aid:
            raise SyncError(
                f"two account_ids ({account_of_mask[mask]}, {aid}) share mask "
                f"{mask}; refusing to merge them into one file.")
        account_of_mask[mask] = aid
        mask_of_account[aid] = mask

    # Write the immutable delta file: exactly what Plaid gave us this fetch.
    # Microsecond stamp keeps filenames chronologically sortable.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S-%fZ")
    delta_path = os.path.join(inst_dir, f"{stamp}.json")
    n = 2
    while os.path.exists(delta_path):
        delta_path = os.path.join(inst_dir, f"{stamp}-{n}.json")
        n += 1
    delta = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "credential_id": credential_id,
        "display_name": display_name,
        "initial_sync": initial,
        "cursor_before": start_cursor,
        "cursor_after": last_cursor,
        "partial": abort_reason is not None,
        "abort_reason": abort_reason,
        "pages": pages,
        "accounts": accounts,
        "added": added,
        "modified": modified,
        "removed": removed,
    }
    atomic_write_json(delta_path, delta)
    print(f"{tag} delta: {delta_path} "
          f"(+{len(added)} ~{len(modified)} -{len(removed)}"
          f"{' PARTIAL' if abort_reason else ''}",
          file=sys.stderr)

    # Materialize: fold the delta into per-account current-state JSONs.
    # Keyed by (mask, transaction_id); every operation is idempotent so a
    # crash between delta-write and cursor-advance can be recovered by simply
    # re-running (the same delta re-applies as a no-op). Accounts never
    # overlap between institutions, so per-institution materialization is safe.
    store = {}  # (mask, txn_id) -> txn
    for mask in account_of_mask:
        path = os.path.join(PLAID_DIR, f"plaid-{mask}.json")
        if os.path.exists(path):
            data = load_json(path)
            # New format: {"transactions": [...], ...}; old format: [...]
            txns = data.get("transactions") if isinstance(data, dict) else data
            for t in txns or []:
                store[(mask, t["transaction_id"])] = t

    def mask_for(txn):
        aid = txn.get("account_id")
        mask = mask_of_account.get(aid)
        if mask is None:
            raise SyncError(
                f"transaction {txn.get('transaction_id')} references unknown "
                f"account_id {aid}; refusing to guess its file.")
        return mask

    # 1. Pending -> posted promotions: added row carries pending_transaction_id
    #    pointing at the pending row already in the store. Promote in place.
    promoted_pending_ids = set()
    pending_added = []
    for t in added:
        pid = t.get("pending_transaction_id")
        if not pid:
            pending_added.append(t)
            continue
        mask = mask_for(t)
        if (mask, pid) in store:
            store[(mask, t["transaction_id"])] = t
            del store[(mask, pid)]
            promoted_pending_ids.add(pid)
            print(f"{tag} promoted pending {pid} -> posted "
                  f"{t['transaction_id']}", file=sys.stderr)
        else:
            # Back-reference not guaranteed by Plaid; the posted row still
            # stands on its own and the pending row (if any) is removed below.
            pending_added.append(t)

    # 2. Remaining added rows: insert, skip if already present (idempotent).
    n_new = 0
    for t in pending_added:
        mask = mask_for(t)
        key = (mask, t["transaction_id"])
        if key not in store:
            store[key] = t
            n_new += 1

    # 3. Modified rows: update in place; insert if we never saw the original
    #    (a missed add must not become a silently dropped transaction).
    n_mod = 0
    for t in modified:
        mask = mask_for(t)
        key = (mask, t["transaction_id"])
        if key not in store:
            warn(f"{tag} modified transaction {t['transaction_id']} was not "
                 f"in the store; inserting it rather than dropping it")
        store[key] = t
        n_mod += 1

    # 4. Removed rows: delete; skip ids already promoted above.
    n_del = 0
    for rid in removed_ids(removed):
        if rid in promoted_pending_ids:
            continue
        for mask in account_of_mask:
            if (mask, rid) in store:
                del store[(mask, rid)]
                n_del += 1
                break

    # Write materialized files, one per mask, sorted by date.
    by_mask = {}
    for (mask, _tid), t in store.items():
        by_mask.setdefault(mask, []).append(t)
    # Build mask -> account info for metadata
    mask_info = {}
    for aid, info in accounts.items():
        mask = info.get("mask")
        if mask and mask not in mask_info:
            mask_info[mask] = info
    for mask in sorted(account_of_mask):
        txns = sorted(by_mask.get(mask, []), key=lambda x: x.get("date", ""))
        path = os.path.join(PLAID_DIR, f"plaid-{mask}.json")
        if not txns:
            continue
        info = mask_info.get(mask, {})
        output = {
            "institution": display_name,
            "account_name": info.get("name", ""),
            "official_name": info.get("official_name", ""),
            "account_type": info.get("type", ""),
            "account_subtype": info.get("subtype", ""),
            "mask": mask,
            "transactions": txns,
        }
        atomic_write_json(path, output)
        n_pend = sum(1 for t in txns if t.get("pending"))
        print(f"{tag} {path}: {len(txns)} transactions ({n_pend} pending)",
              file=sys.stderr)
    print(f"{tag} materialized: +{n_new} new, ~{n_mod} modified, "
          f"-{n_del} removed", file=sys.stderr)

    # Advance the cursor only now that everything above succeeded.
    atomic_write_json(cursor_path, {
        "cursor": last_cursor,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "last_delta": os.path.basename(delta_path),
    })
    print(f"{tag} cursor saved -> {cursor_path}", file=sys.stderr)

    if abort_reason:
        # Partial progress was persisted above; the next run continues from
        # the saved cursor. Raise so the caller records this institution as
        # failed (other institutions still sync).
        raise SyncError(
            f"ABORTED ({abort_reason}) after {pages} pages. Partial progress "
            f"was SAVED to {delta_path} and the cursor advanced past it. "
            f"Re-run to continue.\n{abort_detail}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days-requested", type=int, default=730,
                    help="history hint for the FIRST sync only (no saved "
                         "cursor yet); default 730 (max). Ignored once a cursor "
                         "exists. Max 730.")
    args = ap.parse_args()

    os.makedirs(DELTA_DIR, exist_ok=True)

    try:
        institutions = get_institutions()
    except SyncError as e:
        die(f"could not list institutions: {e}")
    if not institutions:
        die("plaid accounts returned no institutions; nothing to sync.")
    print(f"plaid-pull: syncing {len(institutions)} institution(s)",
          file=sys.stderr)

    failures = []
    for inst in institutions:
        cid = inst["credential_id"]
        name = inst["display_name"]
        try:
            sync_institution(cid, name, args.days_requested)
        except SyncError as e:
            print(f"plaid-pull: FAILED [{name}]: {e}", file=sys.stderr)
            failures.append(name)
        except Exception:
            # Never let one institution's unexpected failure abort the rest.
            # Log the traceback so the bug is visible, then continue.
            print(f"plaid-pull: FAILED [{name}] with unexpected error:",
                  file=sys.stderr)
            traceback.print_exc()
            failures.append(name)

    if failures:
        print(f"plaid-pull: {len(failures)} institution(s) failed "
              f"({', '.join(failures)}); others synced successfully.",
              file=sys.stderr)
        sys.exit(1)
    print("plaid-pull: all institutions synced successfully", file=sys.stderr)


if __name__ == "__main__":
    main()
