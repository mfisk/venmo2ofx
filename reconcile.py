#!/usr/bin/env python3
"""Merge 1+ statement files of any format into GnuCash-ready QIF with balanced double-entry transactions.

Usage:
    reconcile.py --period 2026-09 -o out/2026-09.qif [--allow-unpaired]
               [--unknowns-out unknowns.json] inputs...
    reconcile.py --period 2026 -o out/2026.qif inputs...

Inputs may be mixed freely:
    .json  Plaid-schema transaction arrays (Plaid fetch output, or any
           producer using the Plaid schema), or Schwab brokerage web-export
           JSON (detected by content: a dict with 'BrokerageTransactions')
    .csv   Venmo statement CSVs (detected by their "Account Statement"
           header row) or generic amount/date/description CSVs
    .ofx/.qfx  bank OFX/QFX downloads

Every transaction gets its GnuCash transfer account pre-assigned via
reconcile.yaml (first match wins). Symmetric transfers seen from both sides
are merged into one balanced transaction with both legs.

By default the run ABORTS if a transfer between two import accounts has
no mirror side -- half-recording is the worst possible outcome.
--allow-unpaired downgrades that to a stderr listing (the transactions
are still emitted with their categorized transfer account).

Anything unmatched lands in Expenses:Unknown and is listed in the review
report; --unknowns-out writes those transactions as JSON (with MCC,
PFC, counterparties, and Venmo metadata) for offline research.

The program is stateless: output is deterministic, so re-running a month
regenerates byte-identical QIF.

GnuCash import: File -> Import -> Import QIF. QIF categories that exactly
match an existing account name map automatically.
"""

import argparse, csv, hashlib, html, io, json, os, re, sys
from datetime import datetime

import yaml

try:
    from ofxparse import OfxParser
    HAVE_OFX = True
except ImportError:
    HAVE_OFX = False

try:
    import xlrd
    HAVE_XLRD = True
except ImportError:
    HAVE_XLRD = False

try:
    import openpyxl
    HAVE_OPENPYXL = True
except ImportError:
    HAVE_OPENPYXL = False

BASE = os.path.dirname(os.path.abspath(__file__))
UNKNOWN = "Root Account:Expenses:Unknown"


def check(cond, msg):
    if not cond:
        raise SystemExit(f"INVARIANT FAILED: {msg}")


def fold(text):
    # Unescape HTML entities first (Plaid raw names contain &amp; etc.).
    # Must come before digit folding so numeric entities like &#38; survive.
    d = html.unescape(text or "")
    d = d.upper().strip()
    d = re.sub(r"\d+", "#", d)
    d = re.sub(r"\s+", " ", d)
    # Strip trailing whitespace/punctuation but KEEP # (digit placeholder).
    # This lets patterns use # to match digits (e.g., "PILOT #" matches "PILOT 4569").
    # A # in the pattern matches # in the folded text, which comes from either
    # digits or literal # in the original description.
    d = re.sub(r"[\s.\-*~]+$", "", d)
    return d.strip()


class Rules:
    def __init__(self, path):
        with open(path) as f:
            cfg = yaml.safe_load(f) or {}
        cfg_accounts = cfg.get("source_accounts") or {}
        # Two formats:
        #   Old (dict): {'AAAA': 'Assets:Checking'} — mask only
        #   New (list): [{'mask': 'AAAA', 'institution': 'Example Bank',
        #                 'account_type': 'checking', 'account': 'Assets:Checking'}]
        self.source_accounts = []  # list of dicts with mask/institution/type/account
        if isinstance(cfg_accounts, dict):
            for mask, acct in cfg_accounts.items():
                self.source_accounts.append({
                    "mask": str(mask),
                    "account": acct,
                })
        elif isinstance(cfg_accounts, list):
            for entry in cfg_accounts:
                check(isinstance(entry, dict) and "mask" in entry and "account" in entry,
                      f"source_accounts entry needs mask and account: {entry!r}")
                self.source_accounts.append({
                    "mask": str(entry["mask"]),
                    "institution": entry.get("institution"),
                    "account_type": entry.get("account_type"),
                    "account": entry["account"],
                })
        # For backward-compat lookups by mask only
        self._mask_to_account = {}
        for e in self.source_accounts:
            # If multiple entries share a mask, mask-only lookup is ambiguous;
            # don't populate it (caller must use full metadata).
            if e["mask"] not in self._mask_to_account:
                self._mask_to_account[e["mask"]] = e["account"]
            else:
                self._mask_to_account[e["mask"]] = None  # ambiguous
        # Rules are a recursive tree. Any node can have a nested `rules:` list;
        # children inherit all parent keys (source, account, etc.) unless they
        # override them. A node with an `account` and matching criteria
        # (pattern, mcc, pfc, or checknum) is a leaf rule.
        #
        # Example:
        #   rules:
        #     - source: Assets:Checking
        #       rules:
        #         - account: Expenses:Fuel
        #           mcc: [5541, 5542]
        #         - account: Expenses:Fuel
        #           pattern: MURPHY EXPRESS #
        #           no_mcc: true
        flat = []
        hand_count = 0
        self.offset_rules = []  # collected by flatten() from memo_regex nodes
        # Matching criteria keys (a leaf rule needs account + at least one).
        # Amount conditions use signed amounts: negative = debit (money out),
        # positive = credit (money in). The amount value can be an exact
        # number (amount: 100), or a string with >/< prefix
        # (amount: ">0", amount: "<0"). An amount-only rule is a catch-all
        # for that flow on its source (e.g. amount: ">0" catches all
        # credits/deposits).
        CRITERIA = ("desc", "mcc", "pfc", "checks", "checknum", "fitid",
                    "amount")
        # All valid keys on a rule node.
        VALID = {"source", "account", "desc", "mcc", "pfc",
                 "no_mcc", "no_pfc", "checks", "checknum", "fitid",
                 "amount", "rules", "memo_regex", "debit_account",
                 "credit_account", "description", "if", "then",
                 "debit", "credit", "describe_as"}
        def flatten(nodes, inherited, where):
            nonlocal hand_count
            for node in nodes:
                check(isinstance(node, dict),
                      f"rule must be a dict at {where}: {node!r}")
                unknown = set(node) - VALID
                check(not unknown,
                      f"unknown rule keys {sorted(unknown)} at {where}: {node!r}")
                # Merge: node's own keys override inherited ones.
                ctx = dict(inherited)
                for k, v in node.items():
                    if k != "rules":
                        ctx[k] = v
                # Normalize if:/then: structure to flat format.
                # if: {filters} -> filters at top level
                # then: <account string> -> account: <string>
                # then: {debit, credit, describe_as} -> offset rule keys
                # then: {account: ...} -> account at top level
                if "if" in ctx:
                    m = ctx.pop("if")
                    check(isinstance(m, dict),
                          f"if must be a dict at {where}: {m!r}")
                    for k, v in m.items():
                        ctx[k] = v
                if "then" in ctx:
                    t = ctx.pop("then")
                    if isinstance(t, str):
                        # Bare account shorthand
                        ctx["account"] = t
                    elif isinstance(t, dict):
                        # Full stanza: map verb keys to internal keys
                        if "debit" in t:
                            ctx["debit_account"] = t["debit"]
                        if "credit" in t:
                            ctx["credit_account"] = t["credit"]
                        if "describe_as" in t:
                            ctx["description"] = t["describe_as"]
                        if "account" in t:
                            ctx["account"] = t["account"]
                    else:
                        check(False, f"then must be a string or dict at {where}: {t!r}")
                # Leaf rule: has account + at least one criterion.
                if "account" in ctx and any(k in ctx for k in CRITERIA):
                    pats = ctx.get("desc")  # None = match any description
                    if pats is not None:
                        if isinstance(pats, str):
                            pats = [pats]
                        check(isinstance(pats, list) and all(
                            isinstance(p, str) and p for p in pats),
                            f"pattern must be a string or list of strings at {where}")
                    else:
                        pats = [None]  # pattern-less: one entry, no pattern
                    cond = {}
                    for ck in ("mcc", "pfc"):
                        if ck in ctx:
                            v = ctx[ck] if isinstance(ctx[ck], list) else [ctx[ck]]
                            cond[ck] = [str(x) for x in v]
                    for ck in ("no_mcc", "no_pfc"):
                        if ck in ctx:
                            check(isinstance(ctx[ck], bool),
                                  f"{ck} must be true/false at {where}")
                            cond[ck] = ctx[ck]
                    for ck in ("amount",):
                        if ck in ctx:
                            v = ctx[ck]
                            # amount can be a number (exact match) or a string
                            # like ">0", "<0"
                            if isinstance(v, (int, float)) and not isinstance(v, bool):
                                cond[ck] = ("==", float(v))
                            elif isinstance(v, str):
                                s = v.strip()
                                op = None
                                for prefix in (">", "<"):
                                    if s.startswith(prefix):
                                        op = prefix
                                        num_str = s[len(prefix):].strip()
                                        break
                                check(op is not None,
                                      f"amount string must start with > or < at {where}: {v!r}")
                                try:
                                    num = float(num_str)
                                except ValueError:
                                    check(False,
                                          f"amount value must be a number at {where}: {v!r}")
                                cond[ck] = (op, num)
                            else:
                                check(False,
                                      f"amount must be a number or >/​< string at {where}: {v!r}")
                    check(not ("mcc" in cond and cond.get("no_mcc")),
                          f"mcc + no_mcc can never fire at {where}")
                    check(not ("pfc" in cond and cond.get("no_pfc")),
                          f"pfc + no_pfc can never fire at {where}")
                    src = ctx.get("source")  # None = matches any source
                    for pat in pats:
                        flat.append((src, ctx["account"], pat, cond or None))
                        if src is None:
                            hand_count += 1
                # Offset rule: has memo_regex + debit_account + credit_account.
                # Collected separately, not added to flat.
                elif "memo_regex" in ctx:
                    import re as _re
                    check("debit_account" in ctx and "credit_account" in ctx,
                          f"offset rule needs debit_account and credit_account at {where}: {node!r}")
                    self.offset_rules.append({
                        "regex": _re.compile(ctx["memo_regex"], _re.IGNORECASE),
                        "debit_account": ctx["debit_account"],
                        "credit_account": ctx["credit_account"],
                        "description": ctx.get("description", "Offset for {payee}"),
                        "source": ctx.get("source"),  # None = any source
                    })
                # Recurse into nested rules, if any.
                if "rules" in node:
                    children = node["rules"]
                    check(isinstance(children, list),
                          f"rules must be a list at {where}")
                    flatten(children, ctx, where)
                # A node with neither criteria nor children is an error.
                elif "account" not in ctx or not any(k in ctx for k in CRITERIA):
                    # Pure grouping node without children: error unless it's
                    # just inherited context (e.g. top-level source grouping).
                    if "account" in node or any(k in node for k in CRITERIA):
                        check(False,
                              f"rule has account but no matching criteria at {where}: {node!r}")
        flatten(cfg.get("rules") or [], {}, "rules")
        # keep hand patterns first, sort the rest by length (stable: preserves YAML order on ties)
        # None patterns (conditions-only) sort as length 0 = lowest priority
        hand_flat = flat[:hand_count]
        gen_flat = sorted(flat[hand_count:], key=lambda x: -len(x[2] or ""))
        self.rules_flat = hand_flat + gen_flat
        # Check-number rules: (source, checknum) -> account. Built from leaf
        # rules with a checknum criterion. Plaid folds "Check Paid #583" to
        # "CHECK PAID ##", so description patterns cannot tell checks apart;
        # check numbers are exact per-account identifiers instead.
        # Checknum can be a list [1042, 1043] or a dict {1042: "Payee Name"}
        # where the value overrides the transaction's description.
        self.checknum_index = {}
        self.check_payees = {}  # (source, checknum) -> handwritten payee
        self.fitid_index = {}  # (source, fitid) -> account
        self.fitid_memos = {}  # (source, fitid) -> memo override
        # We need to re-walk the tree to get checknum rules with their source.
        # For now, build from a second flatten pass that captures checknum.
        def collect_checknum(nodes, inherited):
            for node in nodes:
                ctx = dict(inherited)
                for k, v in node.items():
                    if k != "rules":
                        ctx[k] = v
                # Accept both 'checks' (preferred) and 'checknum' (legacy).
                cn_key = "checks" if "checks" in ctx else "checknum"
                if cn_key in ctx and "account" in ctx:
                    src = ctx.get("source")
                    acct = ctx["account"]
                    cn = ctx[cn_key]
                    # Three formats:
                    #   checknum: ['517', '521']                    (list)
                    #   checknum: {'517': 'Dan Kurtz', ...}         (num -> payee)
                    #   checknum: {'Dan Kurtz': ['517', '521']}     (payee -> nums)
                    if isinstance(cn, dict):
                        # Detect payee->nums format: keys are non-numeric, values are lists
                        sample_key = next(iter(cn.keys()), None)
                        sample_val = cn.get(sample_key)
                        if sample_key is not None and not str(sample_key).strip("'\"").isdigit() \
                                and isinstance(sample_val, list):
                            # Payee -> [check numbers]
                            for payee, nums in cn.items():
                                for num in nums:
                                    self.checknum_index[(src, str(num))] = acct
                                    if payee:
                                        self.check_payees[(src, str(num))] = str(payee)
                        else:
                            # Check number -> payee
                            for num, payee in cn.items():
                                self.checknum_index[(src, str(num))] = acct
                                if payee:
                                    self.check_payees[(src, str(num))] = str(payee)
                    else:
                        nums = [cn] if isinstance(cn, (str, int)) else cn
                        for num in nums:
                            self.checknum_index[(src, str(num))] = acct
                if "fitid" in ctx and "account" in ctx:
                    src = ctx.get("source")
                    acct = ctx["account"]
                    fids = ctx["fitid"]
                    if isinstance(fids, dict):
                        for fid, memo in fids.items():
                            self.fitid_index[(src, str(fid))] = acct
                            if memo:
                                self.fitid_memos[(src, str(fid))] = str(memo)
                    else:
                        if isinstance(fids, (str, int)):
                            fids = [fids]
                        for fid in fids:
                            self.fitid_index[(src, str(fid))] = acct
                if "rules" in node:
                    collect_checknum(node["rules"], ctx)
        collect_checknum(cfg.get("rules") or [], {})
        # MCC/PFC-only rules (no pattern) are code rules: last resort after
        # all pattern-based rules. They're already in rules_flat with pat=None;
        # the categorize() method checks them after pattern rules by trying
        # the fallback indexes. Build those indexes here.
        self.mcc_index = {}  # (source, mcc) -> account
        self.pfc_index = {}  # (source, pfc_detailed) -> account
        for src, acct, pat, cond in self.rules_flat:
            if pat is None and cond:
                # Pattern-less rule: its conditions go to the fallback index.
                for mcc in cond.get("mcc") or []:
                    self.mcc_index[(src, str(mcc))] = acct
                for pfc in cond.get("pfc") or []:
                    self.pfc_index[(src, pfc)] = acct

    def check_payee(self, source, checknum):
        """Handwritten payee for a check number, from checknum: dict values.

        Returns None when the number has no recorded payee. Falls back to
        source-agnostic entries, mirroring categorize()'s checknum lookup.
        """
        if not checknum:
            return None
        hit = self.check_payees.get((source, str(checknum)))
        if hit is None:
            hit = self.check_payees.get((None, str(checknum)))
        return hit

    def fitid_memo(self, source, fitid):
        """Memo override for a FITID rule, from memo: values.

        Returns None when the FITID has no recorded memo. Falls back to
        source-agnostic entries, mirroring categorize()'s fitid lookup.
        """
        if not fitid:
            return None
        hit = self.fitid_memos.get((source, str(fitid)))
        if hit is None:
            hit = self.fitid_memos.get((None, str(fitid)))
        return hit

    @staticmethod
    def _cond_ok(cond, plaid, amount=None):
        """Check a hand rule's compound conditions against the txn's codes."""
        plaid = plaid or {}
        mcc = plaid.get("mcc")
        pfc = plaid.get("pfc_detailed")
        if "mcc" in cond and str(mcc) not in cond["mcc"]:
            return False
        if "pfc" in cond and pfc not in cond["pfc"]:
            return False
        if cond.get("no_mcc") and mcc:
            return False
        if cond.get("no_pfc") and pfc:
            return False
        # Amount conditions: signed amount, negative = debit (out),
        # positive = credit (in). Fail closed when amount is unknown.
        if amount is None and "amount" in cond:
            return False
        if "amount" in cond:
            op, threshold = cond["amount"]
            if op == "==" and not amount == threshold:
                return False
            if op == ">" and not amount > threshold:
                return False
            if op == "<" and not amount < threshold:
                return False
        return True

    def source_account(self, acct_id, hint=None, metadata=None):
        """Map a statement account id to a GnuCash account path.

        metadata: optional dict with institution, account_type, mask for
        disambiguation when multiple accounts share a mask.
        """
        meta = metadata or {}
        # Try full metadata match first (institution + type + mask)
        if meta:
            for e in self.source_accounts:
                if e["mask"] != str(acct_id):
                    continue
                # All specified fields must match; unspecified fields are wildcards
                if e.get("institution") and e["institution"] != meta.get("institution"):
                    continue
                if e.get("account_type") and e["account_type"] != meta.get("account_type"):
                    continue
                return e["account"]
        # Fall back to mask-only (None if ambiguous)
        if acct_id and str(acct_id) in self._mask_to_account:
            acct = self._mask_to_account[str(acct_id)]
            if acct:
                return acct
        if hint and hint in [e["account"] for e in self.source_accounts]:
            return hint
        # GnuCash convention: unmapped accounts go to "Unspecified"
        return "Unspecified"

    def offset_splits(self, memo, payee, source=None):
        """Check memo against offset_rules. Returns list of
        (debit_account, credit_account, amount, description) for each match.
        Amount is always positive; debit gets +amount, credit gets -amount.
        The regex must have one group: the amount (group 1).
        Description template supports {payee}, {amount}.
        If a rule has a source, it only matches that source.
        """
        results = []
        memo = memo or ""
        for cr in self.offset_rules:
            if cr.get("source") is not None and cr["source"] != source:
                continue
            for m in cr["regex"].finditer(memo):
                try:
                    amt = float(m.group(1).replace(",", ""))
                except (IndexError, ValueError):
                    continue
                if amt <= 0:
                    continue
                desc = cr["description"].format(payee=payee or "", amount=f"{amt:,.2f}")
                results.append((cr["debit_account"], cr["credit_account"], amt, desc))
        return results

    def categorize(self, source, text, date, plaid=None, checknum=None,
                     fitid=None, amount=None):
        """Categorize a transaction.

        Args:
            source: GnuCash source account path
            text: transaction description
            date: transaction date
            plaid: optional dict with 'mcc' and/or 'pfc_detailed' from Plaid API
            fitid: optional unique transaction ID for one-off mappings
            amount: signed amount (negative = debit/out, positive = credit/in);
                enables amount rules
        """
        folded = fold(text)
        if hasattr(date, "strftime"):
            year, year_month = date.strftime("%Y"), date.strftime("%Y-%m")
        else:
            year, year_month = str(date)[:4], str(date)[:7]
        def resolve(acct):
            # YYYY-MM first: it contains YYYY as a prefix
            return acct.replace("YYYY-MM", year_month).replace("YYYY", year)
        # Exact check-number match: beats description patterns.
        if checknum:
            hit = self.checknum_index.get((source, str(checknum)))
            if hit is None:
                hit = self.checknum_index.get((None, str(checknum)))
            if hit is not None:
                return resolve(hit), f'checknum:"{checknum}"'
        # Exact FITID match: beats description patterns (one-off Venmo etc.).
        if fitid:
            hit = self.fitid_index.get((source, str(fitid)))
            if hit is None:
                hit = self.fitid_index.get((None, str(fitid)))
            if hit is not None:
                return resolve(hit), f'fitid:"{fitid}"'
        for rule_src, account, m, cond in self.rules_flat:
            # If rule has a source, it must match; None means any source
            if rule_src is not None and rule_src != source:
                continue
            # Pattern-less rules (m=None) are code rules: skip here, they're
            # checked via the fallback indexes below. Amount-only rules are
            # the exception — they match any description for their amount
            # range (e.g. all credits on a source).
            if m is None and not (cond and "amount" in cond):
                continue
            if m is not None and fold(m) not in folded:
                continue
            if cond is not None and not self._cond_ok(cond, plaid, amount):
                continue
            if m is None:
                op, threshold = cond["amount"]
                _desc = f"{op}{threshold:g}"
                return resolve(account), f'amount:{_desc}'
            return resolve(account), f'rule:"{m}"'
        # Code-based rules (MCC, then PFC) are the last resort.
        if plaid:
            mcc = plaid.get("mcc")
            if mcc:
                # Try source-specific, then global (None source)
                acct = self.mcc_index.get((source, str(mcc))) or self.mcc_index.get((None, str(mcc)))
                if acct:
                    return resolve(acct), f'mcc:{mcc}'
            pfc = plaid.get("pfc_detailed")
            if pfc:
                acct = self.pfc_index.get((source, pfc)) or self.pfc_index.get((None, pfc))
                if acct:
                    return resolve(acct), f'pfc:{pfc}'
        return UNKNOWN, "unmatched"


# ---------------------------------------------------------------------------
# Parsers. Each produces the same txn dict:
#   acct_id, date, amount (negative = outflow), payee, memo, fitid, checknum,
#   plaid {mcc, pfc_detailed} | None, meta {counterparties, location, ...}
# ---------------------------------------------------------------------------

def parse_ofx(path):
    if not HAVE_OFX:
        raise SystemExit("ofxparse not installed; cannot read " + path)
    # Read as UTF-8 text (not bytes): ofxparse wraps the stream in an ASCII
    # decoder when the OFX header lacks ENCODING, crashing on non-ASCII bytes.
    raw = open(path, "rb").read().decode("utf-8", errors="replace")
    # venmo2ofx copies the CSV's formatted balance verbatim, e.g.
    # <BALAMT>$98.38</BALAMT>; ofxparse float()s it and dies with
    # "Empty ledger balance". Strip the currency symbol.
    raw = re.sub(r"(<BALAMT>\s*)\$", r"\1", raw)
    if "ENCODING:" not in raw[:raw.find("<")].upper():
        # OFX v2 has no ENCODING header, so ofxparse falls back to ASCII and
        # crashes on emoji in memos. Declare UTF-8 so the full unicode text
        # passes through intact instead of being stripped or crashing.
        raw = "ENCODING:UTF-8\n" + raw
    ofx = OfxParser.parse(io.BytesIO(raw.encode("utf-8")))
    out = []
    for acct in ofx.accounts:
        acct_id = getattr(acct, "number", "") or getattr(acct, "account_id", "")
        for t in acct.statement.transactions:
            out.append({
                "acct_id": str(acct_id),
                "date": t.date.date() if hasattr(t.date, "date") else t.date,
                "amount": float(t.amount),
                "payee": t.payee or "",
                "memo": t.memo or "",
                "fitid": t.id or "",
                "checknum": getattr(t, "checknum", "") or "",
                "plaid": None,
                "meta": {},
            })
    return out


def parse_csv(path, account_hint=None):
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        sample = f.read(4096)
        f.seek(0)
        dialect = csv.Sniffer().sniff(sample) if sample.strip() else csv.excel
        reader = csv.DictReader(f, dialect=dialect)
        cols = {c.lower().strip(): c for c in (reader.fieldnames or [])}

        def col(*names):
            for n in names:
                if n in cols:
                    return cols[n]
            return None

        c_date = col("date", "posted date", "transaction date")
        c_desc = col("description", "bank description", "payee", "merchant", "name")
        c_memo = col("memo", "notes", "reference", "transaction posted")
        c_amt = col("amount", "amt")
        c_debit = col("debit", "withdrawal", "withdrawals")
        c_credit = col("credit", "deposit", "deposits")
        c_check = col("check number", "checknumber", "check num", "check #",
                      "check no", "check no.", "chk num", "checknum")
        c_fromto = col("from/to", "from to")
        for row in reader:
            try:
                if c_amt:
                    amount = float(str(row[c_amt]).replace(",", "").replace("$", ""))
                else:
                    debit = float(str(row[c_debit] or 0).replace(",", "")) if c_debit else 0
                    credit = float(str(row[c_credit] or 0).replace(",", "")) if c_credit else 0
                    amount = credit - debit
            except (ValueError, TypeError):
                raise SystemExit(
                    f"ERROR: {path} line {reader.line_num}: cannot parse amount "
                    f"from row {dict(row)}; refusing to silently skip transactions.")
            date = parse_date(row[c_date]) if c_date else None
            if date is None:
                raise SystemExit(
                    f"ERROR: {path} line {reader.line_num}: cannot parse date "
                    f"from row {dict(row)}; refusing to silently skip transactions.")
            desc = row[c_desc] if c_desc else ""
            memo = row[c_memo] if c_memo else ""
            checknum = str(row[c_check]).strip() if c_check and row.get(c_check) else ""
            fromto = str(row[c_fromto]).strip() if c_fromto and row.get(c_fromto) else ""
            key_src = f"{account_hint}|{date}|{amount:.2f}|{desc}"
            rows.append({
                "acct_id": "",
                "account_hint": account_hint,
                "date": date,
                "amount": amount,
                "payee": desc,
                "memo": memo,
                "fitid": "csv-" + hashlib.sha1(key_src.encode()).hexdigest()[:16],
                "checknum": checknum,
                "plaid": None,
                "meta": {"from_to": fromto} if fromto else {},
            })
    return rows


def parse_qb_xls(path, account_hint=None):
    """Parse a QuickBooks bank account register export (.xls/.xlsx).

    Layout: row 0 is a title row, the header row follows (Date, Ref No.,
    Payee, Memo, Payment, Deposit, ...), then one row per transaction.
    Amount = Deposit - Payment (payments are outflows, negative).
    """
    ext = os.path.splitext(path)[1].lower()
    if ext == ".xls":
        if not HAVE_XLRD:
            raise SystemExit(
                f"ERROR: {path}: reading .xls requires the xlrd package "
                f"(apt install python3-xlrd); refusing to silently skip it.")
        wb = xlrd.open_workbook(path)
        sh = wb.sheet_by_index(0)
        nrows, ncols = sh.nrows, sh.ncols

        def cell(r, c):
            v = sh.cell(r, c)
            if v.ctype == xlrd.XL_CELL_DATE:
                return datetime(*xlrd.xldate_as_tuple(v.value, wb.datemode)[:3]).date()
            if v.ctype == xlrd.XL_CELL_NUMBER:
                return v.value
            return str(v.value).strip()

        def get(r, c):
            return cell(r, c) if c is not None and c < ncols else ""
    elif ext == ".xlsx":
        if not HAVE_OPENPYXL:
            raise SystemExit(
                f"ERROR: {path}: reading .xlsx requires the openpyxl package "
                f"(apt install python3-openpyxl); refusing to silently skip it.")
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        sh = wb.active
        nrows, ncols = sh.max_row, sh.max_column

        def get(r, c):
            if c is None or c >= ncols:
                return ""
            v = sh.cell(row=r + 1, column=c + 1).value
            if v is None:
                return ""
            if hasattr(v, "date"):  # datetime
                return v.date()
            return str(v).strip()
    else:
        raise SystemExit(f"ERROR: {path}: not an Excel file; refusing to silently skip it.")

    # Find the header row: first row whose cells include Date and Payee.
    header_row, headers = None, None
    for r in range(min(10, nrows)):
        row_vals = [str(get(r, c)).lower().strip() for c in range(ncols)]
        if "date" in row_vals and "payee" in row_vals:
            header_row, headers = r, row_vals
            break
    if header_row is None:
        raise SystemExit(
            f"ERROR: {path}: no QuickBooks header row (Date/Payee) found in "
            f"first 10 rows; refusing to silently skip it.")

    def col(*names):
        for n in names:
            if n in headers:
                return headers.index(n)
        return None

    c_date = col("date")
    c_desc = col("payee", "description", "name")
    c_memo = col("memo", "notes")
    c_pay = col("payment", "withdrawal", "debit")
    c_dep = col("deposit", "credit")
    c_ref = col("ref no.", "ref no", "ref", "check no.", "check #")

    rows = []
    for r in range(header_row + 1, nrows):
        raw_date = get(r, c_date)
        if isinstance(raw_date, str):
            date = parse_date(raw_date)
        else:
            date = raw_date  # already a date object from Excel
        if not date:
            # Skip blank/trailing rows, but abort loudly on a row that
            # has amounts with an unparseable date.
            pay = get(r, c_pay) if c_pay is not None else ""
            dep = get(r, c_dep) if c_dep is not None else ""
            if str(pay).strip() or str(dep).strip():
                raise SystemExit(
                    f"ERROR: {path} row {r + 1}: cannot parse date from "
                    f"{raw_date!r}; refusing to silently skip transactions.")
            continue
        try:
            payment = float(str(get(r, c_pay)).replace(",", "").replace("$", "") or 0) if c_pay is not None else 0
            deposit = float(str(get(r, c_dep)).replace(",", "").replace("$", "") or 0) if c_dep is not None else 0
        except (ValueError, TypeError):
            raise SystemExit(
                f"ERROR: {path} row {r + 1}: cannot parse Payment/Deposit "
                f"amounts; refusing to silently skip transactions.")
        amount = deposit - payment
        if amount == 0:
            continue  # blank or zero-value row
        desc = str(get(r, c_desc)) if c_desc is not None else ""
        memo = str(get(r, c_memo)) if c_memo is not None else ""
        ref = str(get(r, c_ref)).strip() if c_ref is not None else ""
        key_src = f"{account_hint}|{date}|{amount:.2f}|{desc}"
        rows.append({
            "acct_id": account_from_filename(path) or "",
            "account_hint": account_hint,
            "date": date,
            "amount": amount,
            "payee": desc,
            "memo": memo,
            "fitid": "qbxls-" + hashlib.sha1(key_src.encode()).hexdigest()[:16],
            "checknum": ref,
            "plaid": None,
            "meta": {"qb_type": str(get(r, col("type"))) if col("type") is not None else ""},
        })
    if not rows:
        raise SystemExit(f"ERROR: {path}: no transactions parsed; refusing to silently skip it.")
    return rows


def enrich_from_register(bank_txns, register_entries):
    """Enrich bank transactions with register detail (payee, memo, type).

    Matches one-to-one by (date, amount): each bank txn gets at most one
    register entry, and each register entry is used at most once. This
    prevents double-matching when e.g. two people are paid the same amount
    on the same day — the two bank txns get two distinct register entries.

    Ambiguous groups (N bank txns, M register entries, N>1 or M>1) are still
    matched one-to-one in order, but flagged 'register-ambiguous' in meta
    so the uncertainty is visible. Unmatched bank txns keep their bank detail.

    Only INTUIT/PAYROLL transactions are eligible: the register is a payroll
    detail source, and every register match in practice is for a payroll
    debit. Restricting to these descriptions eliminates false matches on
    non-payroll transactions at zero cost.
    """
    from collections import defaultdict
    reg_by_key = defaultdict(list)
    for r in register_entries:
        key = (r["date"], round(r["amount"], 2))
        reg_by_key[key].append(r)

    def is_payroll_txn(b):
        desc = f"{b.get('payee', '')} {b.get('memo', '')}".upper()
        return "INTUIT" in desc or "PAYROLL" in desc

    used = set()
    enriched, ambiguous, unmatched = 0, 0, 0
    from datetime import timedelta
    for b in bank_txns:
        if not is_payroll_txn(b):
            unmatched += 1
            continue
        # Try exact date first, then ±1 day (bank vs register posting dates differ)
        candidates = []
        for delta in [0, -1, 1]:
            key = (b["date"] + timedelta(days=delta), round(b["amount"], 2))
            candidates = [r for r in reg_by_key.get(key, []) if id(r) not in used]
            if candidates:
                break
        if not candidates:
            unmatched += 1
            continue
        reg = candidates[0]
        used.add(id(reg))
        # Fill in missing detail from the register entry
        # Preserve the transaction type: "Intuit Payroll to <name>",
        # "Intuit Deposit from <name>", etc.
        payee = reg["payee"]
        qb_type = reg["meta"].get("qb_type")
        bank_desc = b.get("payee", "").upper()
        if payee:
            if qb_type == "Paycheck":
                payee = f"Intuit Payroll to {payee}"
            elif qb_type == "Deposit" and "INTUIT" in bank_desc:
                payee = f"Intuit Deposit from {payee}"
            elif "TRAN FEE" in bank_desc:
                # Keep the INTUIT TRAN FEE description
                payee = b.get("payee", payee)
        if payee and not b["payee"]:
            b["payee"] = payee
        elif payee:
            # Bank has generic payee; register has the real name — prefer it
            # but keep the bank description in meta for traceability
            b["meta"]["bank_payee"] = b["payee"]
            b["payee"] = payee
        if reg["memo"]:
            b["memo"] = f"{b['memo']} | {reg['memo']}" if b["memo"] else reg["memo"]
        if reg["meta"].get("qb_type"):
            b["meta"]["qb_type"] = reg["meta"]["qb_type"]
        # Recompute fitid since payee/memo changed
        key_src = f"{b.get('account_hint')}|{b['date']}|{b['amount']:.2f}|{b['payee']}"
        b["fitid"] = "qbm-" + hashlib.sha1(key_src.encode()).hexdigest()[:16]
        if len(candidates) > 1 or len(reg_by_key[key]) > 1:
            b["meta"]["enriched"] = "register-ambiguous"
            ambiguous += 1
        else:
            b["meta"]["enriched"] = "register"
            enriched += 1
    print(f"Register enrichment: {enriched} clean, {ambiguous} ambiguous, "
          f"{unmatched} unmatched", file=sys.stderr)
    return bank_txns


def parse_date(s):
    s = str(s).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%Y/%m/%d",
                "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(s.split()[0] if fmt.startswith("%b") else s, fmt).date()
        except ValueError:
            pass
    # last resort: strip time portion
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Plaid-schema input. The Plaid transaction schema is the lingua franca:
# Plaid fetch output uses it natively, and Venmo CSVs are converted to it
# below (same field mapping venmo2json used). One converter serves both.
# ---------------------------------------------------------------------------

def plaid_to_txn(p, acct_id, ctx):
    """Convert one Plaid-schema dict to the internal txn dict.

    Returns None for pending transactions (counted by the caller, never
    silently dropped). Raises on anything malformed.
    """
    if p.get("pending"):
        return None
    try:
        # Plaid: positive = outflow. Internally (and in QIF): negative = outflow.
        amount = -float(p["amount"])
    except (KeyError, TypeError, ValueError):
        raise SystemExit(f"ERROR: {ctx}: bad amount in {str(p)[:200]}")
    try:
        date = datetime.strptime(str(p["date"]), "%Y-%m-%d").date()
    except (KeyError, TypeError, ValueError):
        raise SystemExit(f"ERROR: {ctx}: bad date in {str(p)[:200]}")
    for k in ("transaction_id", "name"):
        if k not in p or p[k] is None:
            raise SystemExit(f"ERROR: {ctx}: missing {k} in {str(p)[:200]}")
    pfc = (p.get("personal_finance_category") or {}).get("detailed")
    return {
        "acct_id": acct_id,
        "date": date,
        "amount": amount,
        "payee": str(p["name"]),
        "memo": str(p.get("merchant_name") or ""),
        "fitid": str(p["transaction_id"]),
        "checknum": str(p.get("check_number") or ""),
        "plaid": {"mcc": p.get("merchant_category_code"), "pfc_detailed": pfc},
        "meta": {
            "counterparties": p.get("counterparties") or [],
            "location": p.get("location") or {},
            "datetime": p.get("datetime"),
            "venmo": p.get("venmo"),
        },
    }


def parse_plaid_json(path, acct_id):
    with open(path) as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            raise SystemExit(f"ERROR: {path}: invalid JSON: {e}")
    # New format: {"institution": ..., "transactions": [...]}
    # Old format: [...] (bare transaction list)
    metadata = {}
    if isinstance(data, dict):
        metadata = {
            "institution": data.get("institution"),
            "account_type": data.get("account_type"),
            "mask": data.get("mask"),
        }
        data = data.get("transactions", [])
    if not isinstance(data, list):
        raise SystemExit(f"ERROR: {path}: expected a JSON array of transactions")
    txns, pending = [], 0
    for i, p in enumerate(data):
        t = plaid_to_txn(p, acct_id, f"{path} txn #{i}")
        if t is None:
            pending += 1
        else:
            t["metadata"] = metadata
            txns.append(t)
    return txns, pending


def is_schwab_brokerage_json(path):
    """Detect a Schwab web-export brokerage JSON by content, not filename.

    Schwab's export is a dict with a 'BrokerageTransactions' list of rows
    (Date/Action/Symbol/Description/Quantity/Price/Fees & Comm/Amount);
    Plaid-schema inputs are JSON arrays. Content decides.
    """
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return False
    return (isinstance(data, dict)
            and isinstance(data.get("BrokerageTransactions"), list))


def _schwab_parse_amount(s, ctx):
    # Already signed cash-flow: negative = money out (unlike Plaid's
    # positive=outflow), so no sign flip.
    t = (s or "").replace(",", "").replace("$", "").strip()
    if t in ("", "-"):
        # Non-cash entries (e.g. position Journals) carry no amount. Kept as
        # $0.00 with full metadata rather than dropped: never silently skip.
        return 0.0
    try:
        return float(t)
    except ValueError:
        raise SystemExit(f"ERROR: {ctx}: cannot parse amount from {s!r}; "
                         f"refusing to silently skip transactions.")


def parse_schwab_brokerage_json(path, acct_id):
    """Parse a Schwab brokerage web-export JSON. Returns (txns, 0)."""
    with open(path) as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            raise SystemExit(f"ERROR: {path}: invalid JSON: {e}")
    rows = data.get("BrokerageTransactions")
    if not isinstance(rows, list) or not rows:
        raise SystemExit(f"ERROR: {path}: no BrokerageTransactions found")
    txns = []
    for i, r in enumerate(rows):
        ctx = f"{path} row #{i}"
        if not isinstance(r, dict):
            raise SystemExit(f"ERROR: {ctx}: not an object: {str(r)[:200]}")
        for k in ("Date", "Action", "Description", "Amount"):
            if k not in r:
                raise SystemExit(f"ERROR: {ctx}: missing {k!r} in {str(r)[:200]}")
        # Schwab sometimes posts "MM/DD/YYYY as of MM/DD/YYYY" (accrual date);
        # the transaction date is the first one.
        date = parse_date(str(r["Date"]).split(" as of ")[0].strip())
        if date is None:
            raise SystemExit(f"ERROR: {ctx}: cannot parse date from {r['Date']!r}; "
                             f"refusing to silently skip transactions.")
        amount = _schwab_parse_amount(r["Amount"], ctx)
        payee = str(r["Description"] or "").strip()
        memo = str(r["Action"] or "").strip()
        if r.get("Symbol"):
            memo = f"{memo} {r['Symbol']}".strip()
        if not payee:
            raise SystemExit(f"ERROR: {ctx}: empty Description in {str(r)[:200]}")
        # Source-qualified identity: account id + date + amount + payee.
        key_src = f"{acct_id}|{date.isoformat()}|{amount:.2f}|{payee}"
        txns.append({
            "acct_id": acct_id,
            "date": date,
            "amount": amount,
            "payee": payee,
            "memo": memo,
            "fitid": "schwab-" + hashlib.sha1(key_src.encode()).hexdigest()[:16],
            "checknum": "",
            "plaid": None,
            "meta": {"schwab": {k: r.get(k) for k in
                                ("Date", "Action", "Symbol", "Quantity", "Price",
                                 "Fees & Comm", "AcctgRuleCd")}},
        })
    return txns, 0


def account_from_filename(path):
    """Derive the accounts-map key from the filename.

    Handles both 'plaid-1234.json' and archive names like
    '2026-08 plaid 1234 Checking.json', and the same
    with a 'venmo-' prefix for Venmo usernames.
    Falls back to the full basename (e.g. 'greendot.csv' -> 'greendot').
    """
    base = os.path.splitext(os.path.basename(path))[0]
    m = re.search(r"plaid[ _-](\S+)", base)
    if m:
        return m.group(1)
    # Schwab web exports mask the account as XXX1234, e.g.
    # Joint_Brokerage_XXX333_Transactions_20260927-212403.json -> '333'.
    # The id->account mapping itself lives in reconcile.yaml, not here.
    m = re.search(r"XXX(\d+)", base)
    if m:
        return m.group(1)
    m = re.search(r"venmo[ _-](\S+)", base)
    if m:
        return m.group(1)
    # Fall back to the basename itself (e.g. 'greendot' for Green Dot).
    # Strip trailing suffixes like '-pending' (e.g. 'greendot-pending' -> 'greendot')
    if "-" in base:
        prefix = base.split("-")[0]
        # Only strip if the prefix is a plausible mask (not empty)
        if prefix:
            return prefix
    return base


# ---------------------------------------------------------------------------
# Venmo CSV -> Plaid schema. Same mapping venmo2json used: counterparty
# logic and the synthetic bank-funding leg are identical to venmo2ofx so
# existing transfer rules and pairing keep working.
# ---------------------------------------------------------------------------

_VENMO_NULL_LOCATION = {
    "address": None, "city": None, "country": None, "lat": None,
    "lon": None, "postal_code": None, "region": None, "store_number": None,
}

_VENMO_NULL_PAYMENT_META = {
    "by_order_of": None, "payee": None, "payer": None,
    "payment_method": None, "payment_processor": None, "ppd_id": None,
    "reason": None, "reference_number": None,
}


def _venmo_plaid_dict(tx_id, account_id, plaid_amount, dt, name, pending,
                      pfc_primary, pfc_detailed, counterparties, venmo_ext):
    return {
        "account_id": account_id,
        "account_owner": None,
        "amount": round(plaid_amount, 2),
        "authorized_date": None,
        "authorized_datetime": None,
        "category": None,
        "category_id": None,
        "check_number": None,
        "counterparties": counterparties,
        "date": dt.strftime("%Y-%m-%d"),
        "datetime": dt.isoformat(),
        "iso_currency_code": "USD",
        "location": dict(_VENMO_NULL_LOCATION),
        "logo_url": None,
        "merchant_category_code": None,
        "merchant_entity_id": None,
        "merchant_name": None,
        "name": name,
        "payment_channel": "online",
        "payment_meta": dict(_VENMO_NULL_PAYMENT_META),
        "pending": pending,
        "pending_transaction_id": None,
        "personal_finance_category": {
            # Inferred from Venmo's Type column, not by Plaid:
            # confidence LOW is honest about that.
            "confidence_level": "LOW",
            "detailed": pfc_detailed,
            "primary": pfc_primary,
            "version": "v2",
        },
        "personal_finance_category_icon_url": None,
        "transaction_code": None,
        "transaction_id": tx_id,
        "transaction_type": "digital",
        "unofficial_currency_code": None,
        "website": None,
        "venmo": venmo_ext,
    }


def _venmo_other_party(val):
    """Counterparty logic, identical to venmo2ofx."""
    party = val["To"]
    if val["Type"] == "Standard Transfer" and val["From"] == "(None)":
        party = "VENMO-CASHOUT to " + val["Destination"]
    elif val["Type"] == "Payment":
        if not val["Amount (total)"].startswith("-"):
            party = val["From"]
    elif val["Type"] == "Charge":
        if val["Amount (total)"].startswith("-"):
            party = val["From"]
    return party


def _venmo_parse_amount(s, ctx):
    """Parse '+ $1,245.00' / '- $50.00' -> float, negative = money out
    (OFX TRNAMT convention, matching venmo2ofx)."""
    t = (s or "").replace(",", "").replace("$", "").replace(" ", "").replace("+", "")
    try:
        return float(t)
    except ValueError:
        raise SystemExit(f"ERROR: {ctx}: cannot parse amount from {s!r}")


def parse_venmo_csv(path, venmo_id=None):
    """Parse a Venmo statement CSV via the Plaid schema. Returns (txns, pending).

    venmo_id is the fallback account identity for business-format statements,
    which have no 'Account Statement - (@user)' header row. A present header
    always wins; a fallback that conflicts with the header aborts. With
    neither, parsing aborts rather than guessing the account.
    """
    txns, pending = [], 0
    with open(path, newline="") as f:
        rows = csv.reader(f)
        pos, is_biz = {}, False
        for lineno, row in enumerate(rows, start=1):
            ctx = f"{path} line {lineno}"
            if not row:
                continue
            if row[0].startswith("Account Statement - (@"):
                new_id = "@" + row[0].split("(@", 1)[1].split(")", 1)[0]
                if venmo_id and venmo_id != new_id:
                    raise SystemExit(
                        f"ERROR: cannot mix Venmo IDs {venmo_id} and {new_id} "
                        f"(file '{path}')")
                venmo_id = new_id
                continue
            if row[0] == "Account Activity":
                continue
            if not pos:
                for i, col in enumerate(row):
                    pos[col] = i
                is_biz = "Amount (net)" in pos
                for need in ("Type", "Status", "Note", "From", "To",
                             "Amount (total)", "Funding Source", "Destination"):
                    if need not in pos:
                        raise SystemExit(f"ERROR: {ctx}: missing required column {need!r}")
                continue
            if venmo_id is None:
                raise SystemExit(f"ERROR: {ctx}: no 'Account Statement' header found")

            val = {k: row[pos[k]] if pos[k] < len(row) else "" for k in pos}

            # Summary rows carry balances but no date; not transactions.
            if not (val.get("Datetime") or val.get("Date")):
                continue

            try:
                if is_biz:
                    dt = datetime.strptime(
                        val["Date"] + " " + val["Time (UTC)"], "%m/%d/%Y %H:%M:%S")
                    tx_id = val["Transaction ID"].replace('"', "")
                    amount_s = val["Amount (net)"]
                    if amount_s == "0" or amount_s == "":
                        amount_s = val["Amount (total)"]
                else:
                    dt = datetime.fromisoformat(val["Datetime"])
                    tx_id = val["ID"]
                    amount_s = val["Amount (total)"]
            except (ValueError, KeyError) as e:
                raise SystemExit(f"ERROR: {ctx}: cannot parse row {row!r} ({e})")
            if not tx_id:
                raise SystemExit(f"ERROR: {ctx}: empty transaction id in row {row!r}")

            ofx_amt = _venmo_parse_amount(amount_s, ctx)  # negative = outflow
            plaid_amt = -ofx_amt                          # Plaid: positive = outflow
            pending_flag = val["Status"] != "Complete"
            party = _venmo_other_party(val)
            note = val["Note"]
            name = party if note in ("", "(None)") else f'{party} "{note}"'

            if plaid_amt >= 0:
                pfc = ("TRANSFER_OUT", "TRANSFER_OUT_ACCOUNT_TRANSFER")
            else:
                pfc = ("TRANSFER_IN", "TRANSFER_IN_ACCOUNT_TRANSFER")

            venmo_ext = {
                "id": tx_id,
                "type": val["Type"],
                "status": val["Status"],
                "note": None if note == "(None)" else note,
                "from": val["From"],
                "to": val["To"],
                "amount_total": val["Amount (total)"],
                "amount_tip": val.get("Amount (tip)") or None,
                "amount_tax": val.get("Amount (tax)") or None,
                "amount_fee": val.get("Amount (fee)") or None,
                "amount_net": val.get("Amount (net)") or None,
                "tax_rate": val.get("Tax Rate") or None,
                "tax_exempt": val.get("Tax Exempt") or None,
                "funding_source": val["Funding Source"],
                "destination": val["Destination"],
                "beginning_balance": val.get("Beginning Balance") or None,
                "ending_balance": val.get("Ending Balance") or None,
            }

            plaid_dicts = [_venmo_plaid_dict(
                tx_id, venmo_id, plaid_amt, dt, name, pending_flag,
                pfc[0], pfc[1],
                [{"name": party, "type": "merchant"}],
                venmo_ext)]

            # Synthetic bank-funding leg, identical in purpose to venmo2ofx's:
            # when a real bank account (not the Venmo balance) funded or
            # received the money, emit its side so transfer pairing against
            # the checking account keeps working.
            dest = val["Destination"].replace("Venmo balance", "").replace("(None)", "")
            src = val["Funding Source"].replace("Venmo balance", "").replace("(None)", "")
            if dest and not src:
                account = dest
            elif src and not dest:
                account = src
            elif src and dest:
                raise SystemExit(
                    f"ERROR: {ctx}: unexpected source ({src}) and destination ({dest})")
            else:
                account = None

            if account and val["Type"] != "Standard Transfer":
                leg_name = "VENMO PAYMENT from " + account
                leg_ext = dict(venmo_ext)
                leg_ext["synthetic_bank_leg"] = True
                leg_ext["bank_account"] = account
                plaid_dicts.append(_venmo_plaid_dict(
                    tx_id + ".1", venmo_id, -plaid_amt, dt, leg_name,
                    pending_flag,
                    "TRANSFER_IN" if plaid_amt > 0 else "TRANSFER_OUT",
                    "TRANSFER_IN_ACCOUNT_TRANSFER" if plaid_amt > 0
                    else "TRANSFER_OUT_ACCOUNT_TRANSFER",
                    [{"name": account, "type": "financial_institution"}],
                    leg_ext))

            for i, pd in enumerate(plaid_dicts):
                t = plaid_to_txn(pd, venmo_id, f"{ctx} venmo-txn #{i}")
                if t is None:
                    pending += 1
                else:
                    txns.append(t)
    if not txns and not pending:
        raise SystemExit(f"ERROR: {path}: no transactions found")
    return txns, pending


def is_venmo_csv(path):
    """Detect a Venmo statement CSV by its content, not its filename.

    Personal format starts with an 'Account Statement - (@user)' row;
    business format starts with its column header row. Either signature
    is Venmo's file format, not our archive naming convention.
    """
    with open(path, newline="") as f:
        rows = [row for _, row in zip(range(3), csv.reader(f))]
    if not rows:
        return False
    if rows[0] and rows[0][0].startswith("Account Statement"):
        return True
    return "Transaction ID" in rows[0] and "Time (UTC)" in rows[0]


def venmo_id_for_hint(hint, accounts):
    """Reverse-resolve a GnuCash account hint to a Venmo acct_id (@handle)."""
    if not hint:
        return None
    for acct_id, path in accounts.items():
        if path == hint and acct_id.startswith("@"):
            return acct_id
    return None


# ---------------------------------------------------------------------------
# Transfer merging
# ---------------------------------------------------------------------------

def pair_transfers(txns, rules, window_days=7, verbose=False):
    """Merge symmetric transfers seen from both sides.

    When we pull statements from two accounts and money moves between them,
    we get two transactions (one per statement) for the same economic event.
    Emitting both would double-count in GnuCash. This finds mirror pairs and
    keeps only one side.

    A transfer is any transaction whose source and transfer accounts are both
    import accounts (the accounts defined in the yaml's accounts: map -- we
    pull both statements). A transfer to an account outside the yaml has no
    mirror to find.

    The transfer rule may exist in only one direction (e.g. Chase payments
    are identified from the card side only). The mirror needs no rule: it is
    the transaction on the transfer-target account with the opposite amount
    within window_days. The mirror must be uncategorized or the symmetric
    leg -- never another transfer's leg.

    There is no special case for credit accounts; every transfer pairs the
    same way. The earlier-dated side is kept so the transfer lands in the
    right month; the kept side's transfer account is set to the true
    counterparty account, which also corrects fallback guesses (e.g. the
    funding account guessing the wrong Venmo or Capital One card).

    Pairs are matched closest-date-first so recurring same-amount transfers
    pair with their true mirror, not a neighbor's. Every pairing decision
    (including how many candidates were considered) is logged to stderr.

    Returns (kept, dropped_fitids, pair_count, unpaired, pair_log).
    `unpaired` are transfers with no mirror -- the caller decides whether
    to abort (default) or list them (--allow-unpaired).
    """
    input_accounts = set(e["account"] for e in rules.source_accounts)

    def cents(x):
        return int(round(float(x) * 100))

    def is_transfer(t):
        return (t["source"] in input_accounts
                and t["transfer"] in input_accounts
                and t["source"] != t["transfer"])

    def is_mirror(P, M):
        if M["fitid"] == P["fitid"]:
            return False
        if M["source"] != P["transfer"]:
            return False
        if cents(M["amount"]) != -cents(P["amount"]):
            return False
        if abs((M["date"] - P["date"]).days) > window_days:
            return False
        # The mirror is the uncategorized counter-entry or the symmetric
        # leg; never a leg of some other transfer.
        if M["transfer"] != P["source"] and M["transfer"] != UNKNOWN:
            return False
        return True

    transfers = [t for t in txns if is_transfer(t)]
    triples = []
    cand_count = {}
    for P in transfers:
        cands = [M for M in txns if is_mirror(P, M)]
        cand_count[id(P)] = len(cands)
        for M in cands:
            triples.append((abs((M["date"] - P["date"]).days), P["date"], id(P), P, M))
    # Closest date first, so recurring same-amount transfers pair with
    # their true mirror instead of a neighbor's.
    triples.sort(key=lambda t: (t[0], t[1]))

    dropped, used = set(), set()
    pair_count, pair_log = 0, []
    for gap, _, pid, P, M in triples:
        if P["fitid"] in used or M["fitid"] in used:
            continue
        # Keep the earlier-dated side so the transfer lands in the right
        # month; on a tie keep the rule-identified side (P). The kept
        # side's transfer account becomes the true counterparty account.
        keep, drop = (M, P) if M["date"] < P["date"] else (P, M)
        keep["transfer"] = drop["source"]
        dropped.add(drop["fitid"])
        used.add(P["fitid"])
        used.add(M["fitid"])
        pair_count += 1
        pair_log.append({"kept": keep["fitid"], "dropped": drop["fitid"],
                         "gap_days": gap, "candidates": cand_count[pid]})
        if verbose:
            print(f"paired: {keep['date']} {keep['source'].split(':')[-1]} -> "
                  f"{keep['transfer'].split(':')[-1]} ${abs(keep['amount']):.2f} "
                  f"(gap {gap}d, {cand_count[pid]} candidate(s), "
                  f"dropped {drop['source'].split(':')[-1]} side {drop['date']})",
                  file=sys.stderr)

    kept = [t for t in txns if t["fitid"] not in dropped]
    unpaired = [P for P in transfers if P["fitid"] not in used]
    return kept, dropped, pair_count, unpaired, pair_log


def qif_date(d):
    return d.strftime("%m/%d/%Y")


def clean(s):
    return re.sub(r"[\r\n]+", " ", s or "").strip()


def load_ticket_details(path):
    """Parse airline-tickets-2026.md's Ticket Details table.

    Returns {ticket_digits: compact_detail_string}. Only tickets with at
    least one known field (traveler, confirmation, flights) are included.
    """
    details = {}
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return details
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 10:
            continue
        ticket, traveler, conf, flights, depart, route = cells[:6]
        if not re.fullmatch(r"\d{10,}", ticket):
            continue
        def known(v):
            v = (v or "").strip().lower()
            return v and not (v == "unknown" or v.startswith("unknown ")
                              or v == "n/a" or v.startswith("n/a "))
        parts = []
        if known(traveler):
            parts.append(traveler)
        if known(conf):
            parts.append("conf " + conf)
        if known(flights):
            parts.append(flights)
        if known(depart) or known(route):
            parts.append(" ".join(x for x in (depart, route) if known(x)))
        if parts:
            details[ticket] = "; ".join(parts)
    return details


def unknown_record(t):
    """Full research record for one uncategorized transaction."""
    plaid = t.get("plaid") or {}
    meta = t.get("meta") or {}
    return {
        "fitid": t["fitid"],
        "date": t["date"].isoformat(),
        "amount": t["amount"],
        "source": t["source"],
        "payee": t["payee"],
        "memo": t["memo"],
        "checknum": t.get("checknum") or "",
        "mcc": plaid.get("mcc"),
        "pfc_detailed": plaid.get("pfc_detailed"),
        "counterparties": meta.get("counterparties") or [],
        "location": meta.get("location") or {},
        "datetime": meta.get("datetime"),
        "venmo": meta.get("venmo"),
        "schwab": meta.get("schwab"),
        "file": t.get("file"),
        "how": t.get("how"),
    }


def main():
    ap = argparse.ArgumentParser(
        description="Merge 1+ statement files (any format) into GnuCash-ready "
                    "QIF with balanced double-entry transactions.")
    ap.add_argument("--period", required=True,
                    help="period to include: YYYY-MM for a month, YYYY for a full year")
    ap.add_argument("-o", "--output", default=None,
                    help="output QIF path (default: <period>.qif)")
    ap.add_argument("--rules", default="reconcile.yaml")
    ap.add_argument("--verbose", action="store_true",
                    help="print every transfer pairing decision (default: summary only)")
    ap.add_argument("--allow-unpaired", action="store_true",
                    help="list transfers with no mirror side to stderr instead "
                         "of aborting (they are still emitted, flagged)")
    ap.add_argument("--unknowns-out", default=None, metavar="FILE",
                    help="write uncategorized transactions as JSON for research")
    ap.add_argument("--register", default=None, metavar="FILE",
                    help="QuickBooks register xls for enriching INTUIT/PAYROLL "
                         "bank transactions with payee/memo detail (one-to-one "
                         "match by date+amount; register entries are "
                         "enrichment-only, never emitted)")
    ap.add_argument("inputs", nargs="+")
    args = ap.parse_args()
    if args.output is None:
        args.output = f"{args.period}.qif"
    if os.path.exists(args.output):
        raise SystemExit(f"ERROR: output file exists: {args.output} "
                         f"(delete it or specify a different -o path)")

    rules = Rules(args.rules)
    input_accounts = set(e["account"] for e in rules.source_accounts)

    # ---- parse: every input becomes the same txn dict ---------------------
    txns, pending_total = [], 0
    pending_files = {}
    for inp in args.inputs:
        ext = os.path.splitext(inp)[1].lower()
        if ext == ".json":
            # JSON inputs are detected by content: Plaid-schema arrays vs
            # Schwab brokerage web exports (dict w/ BrokerageTransactions).
            if is_schwab_brokerage_json(inp):
                acct_id = account_from_filename(inp) or ""
                items, pend = parse_schwab_brokerage_json(inp, acct_id)
            else:
                acct_id = account_from_filename(inp) or ""
                items, pend = parse_plaid_json(inp, acct_id)
            pending_total += pend
            if pend:
                pending_files[inp] = pend
        elif ext == ".csv" and is_venmo_csv(inp):
            items, pend = parse_venmo_csv(
                inp, venmo_id=venmo_id_for_hint(None, [e["account"] for e in rules.source_accounts]))
            pending_total += pend
            if pend:
                pending_files[inp] = pend
        elif ext == ".csv":
            items = parse_csv(inp, account_hint=None)
            # Tag QuickBooks banking exports with their account id from the filename
            qbid = account_from_filename(inp)
            if qbid:
                for t in items:
                    t["acct_id"] = qbid
        elif ext in (".xls", ".xlsx"):
            items = parse_qb_xls(inp, account_hint=None)
        elif ext in (".ofx", ".qfx"):
            items = parse_ofx(inp)
        else:
            raise SystemExit(
                f"ERROR: {inp}: unknown extension; refusing to silently skip it.")
        for t in items:
            t["file"] = inp
        # Every input file must resolve to a source account. An empty
        # acct_id means we couldn't identify it (e.g. QuickBooks names its
        # download "Intuit.csv" with no bank name). Abort rather than
        # silently dumping everything into Unspecified.
        if items and not any(t.get("acct_id") for t in items):
            raise SystemExit(
                f"ERROR: {inp}: could not identify source account.\n"
                f"  Rename the file to include the account ID (e.g. the last 4\n"
                f"  digits: plaid-1234.json), then map that ID in the\n"
                f"  'accounts:' section of your rules file.")
        txns.extend(items)
    n_in = len(txns)
    check(n_in > 0, "no transactions parsed from inputs")
    # Prune to the requested period at read time, retaining ~3 days of
    # fuzzy boundary data so transfers near Jan 1 / Dec 31 can still pair.
    # Boundary transactions are flagged and excluded from output below.
    from datetime import timedelta
    BOUNDARY_DAYS = 3
    period = args.period
    if re.fullmatch(r"\d{4}", period):
        y = int(period)
        p_start = datetime(y, 1, 1).date()
        p_end = datetime(y, 12, 31).date()
    elif re.fullmatch(r"\d{4}-\d{2}", period):
        y, m = map(int, period.split("-"))
        p_start = datetime(y, m, 1).date()
        p_end = (datetime(y, m + 1, 1) if m < 12 else datetime(y + 1, 1, 1)).date() - timedelta(days=1)
    else:
        raise SystemExit(f"ERROR: --period must be YYYY or YYYY-MM, got '{period}'")
    r_start = p_start - timedelta(days=BOUNDARY_DAYS)
    r_end = p_end + timedelta(days=BOUNDARY_DAYS)
    for t in txns:
        t["in_period"] = p_start <= t["date"] <= p_end
    txns = [t for t in txns if r_start <= t["date"] <= r_end]
    check(any(t["in_period"] for t in txns), f"no transactions in period {period}")
    if pending_total:
        print(f"WARNING: {pending_total} pending transaction(s) skipped "
              f"(not yet posted, will appear once Plaid reports them as posted):",
              file=sys.stderr)
        for f, n in sorted(pending_files.items()):
            print(f"  {n:4d}  {f}", file=sys.stderr)

    # ---- register enrichment ------------------------------------------------
    # If --register is given, use the QuickBooks register xls to fill in
    # missing payee/memo detail on the bank transactions (one-to-one by
    # date+amount; register entries are enrichment-only, never emitted).
    if args.register:
        reg_entries = parse_qb_xls(args.register)
        txns = enrich_from_register(txns, reg_entries)

    # ---- classify ----------------------------------------------------------
    for t in txns:
        source = rules.source_account(t["acct_id"], t.get("account_hint"),
                                      t.get("metadata"))
        text = f"{t['payee']} {t['payee']} {t['memo']}".strip()
        acct, how = rules.categorize(source, text, t["date"], plaid=t.get("plaid"),
                                     checknum=t.get("checknum"),
                                     fitid=t.get("fitid"),
                                     amount=t.get("amount"))
        t["source"], t["transfer"], t["how"] = source, acct, how
        # Paper checks: replace the generic "Check Paid #NNN" payee with the
        # handwritten payee from the check image (checknum: dict values in
        # reconcile.yaml). Applied after categorize so rule matching still sees
        # the original statement text.
        payee = rules.check_payee(source, t.get("checknum"))
        if payee:
            t["payee"] = payee
        # FITID memo override: replace the transaction's memo with the
        # memo: value from the matching fitid rule (rules.yaml).
        memo = rules.fitid_memo(source, t.get("fitid"))
        if memo:
            t["memo"] = memo

    # ---- offsets: memo_regex -> double-entry offset transactions ------------
    # For each transaction whose memo matches an offset rule, emit an
    # additional transaction out of the credit account: -amount from
    # credit_account (as the !Account source), +amount to debit_account
    # (as the L transfer). Uses standard Header+L format, no splits needed.
    offset_txns = []
    for t in txns:
        if not t.get("in_period", True):
            continue
        for debit_acct, credit_acct, amt, desc in rules.offset_splits(t.get("memo"), t.get("payee"), t.get("source")):
            offset_txns.append({
                "date": t["date"],
                "amount": -amt,  # out of the credit account
                "payee": t.get("payee", ""),
                "memo": desc,
                "source": credit_acct,  # groups under its own !Account stanza
                "transfer": debit_acct,  # L line
                "how": f'offset:"{desc}"',
                "in_period": True,
                "acct_id": t.get("acct_id"),
                "fitid": f"offset-{t.get('fitid', '')}-{amt:.2f}",
            })
    if offset_txns:
        print(f"generated {len(offset_txns)} offset transactions", file=sys.stderr)
    txns.extend(offset_txns)

    # ---- annotate: airline ticket flight details -> memo -------------------
    # Airline ticket transactions carry flight details (traveler, flights,
    # route, dates, confirmation) in airline-tickets-2026.md. Append them
    # to the memo so they land in GnuCash on import. Applied after
    # categorize so rule matching still sees the original statement text.
    ticket_details = load_ticket_details(
        os.path.join(BASE, "airline-tickets-2026.md"))
    if ticket_details:
        for t in txns:
            digits = re.sub(r"\D", "", t.get("payee") or "")
            for ticket, detail in ticket_details.items():
                if ticket in digits:
                    t["memo"] = ((t["memo"] + " | ") if t.get("memo") else "") + detail
                    break

    # ---- merge: pair symmetric transfers -----------------------------------
    # Runs before the month filter so transfers straddling a month boundary
    # still find their mirrors.
    kept, dropped, pair_count, unpaired, pair_log = pair_transfers(
        txns, rules, verbose=args.verbose)
    check({t["fitid"] for t in kept} | dropped == {t["fitid"] for t in txns}
          and not ({t["fitid"] for t in kept} & dropped),
          "merge lost or duplicated transactions")
    if unpaired and not args.allow_unpaired:
        detail = "\n".join(
            f"  {t['date']} {t['amount']:10.2f} {t['source']} -> {t['transfer']} :: "
            f"{clean(t['payee'])} (file {t['file']})"
            for t in sorted(unpaired, key=lambda t: (str(t["date"]), t["fitid"])))
        raise SystemExit(
            f"ERROR: {len(unpaired)} transfer(s) between import accounts with no "
            f"mirror found. Refusing to half-record; extend the pull or add "
            f"the missing statement.\n{detail}")
    for t in sorted(unpaired, key=lambda t: (str(t["date"]), t["fitid"])):
        print(f"unpaired transfer (no mirror, kept as categorized): {t['date']} "
              f"{t['amount']:10.2f} {t['source']} -> {t['transfer']} :: "
              f"{clean(t['payee'])}", file=sys.stderr)
    # Suggest rules for unpaired transfers: look for matching uncategorized
    # transactions in OTHER source accounts (opposite amount, within 7 days).
    # Logged as suggestions only -- never auto-paired.
    input_accounts = set(e["account"] for e in rules.source_accounts)
    for t in sorted(unpaired, key=lambda t: (str(t["date"]), t["fitid"])):
        for M in txns:
            if M["fitid"] == t["fitid"]:
                continue
            if M["source"] not in input_accounts or M["source"] == t["source"]:
                continue
            if M["transfer"] != UNKNOWN:
                continue
            if int(round(float(M["amount"]) * 100)) != -int(round(float(t["amount"]) * 100)):
                continue
            if abs((M["date"] - t["date"]).days) > 7:
                continue
            print(f"suggested rule: {t['date']} {t['amount']:10.2f} {t['source']} "
                  f"-> {M['source']} ? mirror: {M['date']} {M['amount']:10.2f} "
                  f":: {clean(M['payee'])[:50]}", file=sys.stderr)

    # Drop boundary transactions from output (kept for pairing only).
    kept = [t for t in kept if t.get("in_period", True)]
    unknown = [t for t in kept if t["transfer"] == UNKNOWN]

    # ---- emit: deterministic multi-account QIF ------------------------------
    per_account = {}
    for t in kept:
        per_account.setdefault(t["source"], []).append(t)
    buf = io.StringIO()
    for source in sorted(per_account):
        qif_type = "CCard" if source.startswith("Root Account:Credit:") else "Bank"
        buf.write("!Account\n")
        buf.write(f"N{source}\nT{qif_type}\n^\n")
        buf.write(f"!Type:{qif_type}\n")
        for t in sorted(per_account[source], key=lambda x: (x["date"], x["amount"])):
            buf.write(f"D{qif_date(t['date'])}\n")
            buf.write(f"T{t['amount']:.2f}\n")
            if t.get("checknum"):
                buf.write(f"N{clean(t['checknum'])}\n")
            if clean(t["payee"]):
                buf.write(f"P{clean(t['payee'])}\n")
            if clean(t["memo"]):
                buf.write(f"M{clean(t['memo'])}\n")
            if t.get("splits"):
                # Split transaction (e.g. commission offsets): S/E/$ lines,
                # no L line. Splits must sum to the T amount.
                for s in t["splits"]:
                    buf.write(f"S{s['account']}\n")
                    if clean(s.get("memo")):
                        buf.write(f"E{clean(s['memo'])}\n")
                    buf.write(f"${s['amount']:.2f}\n")
            else:
                buf.write(f"L{t['transfer']}\n")
            buf.write("^\n")
    qif_bytes = buf.getvalue().encode("utf-8")
    sha256 = hashlib.sha256(qif_bytes).hexdigest()
    with open(args.output, "wb") as q:
        q.write(qif_bytes)
    check(qif_bytes.count(b"^\n") ==
          sum(len(v) for v in per_account.values()) + len(per_account),
          "QIF transaction count mismatch")

    # ---- review report -------------------------------------------------------
    rep_path = os.path.splitext(args.output)[0] + "-review.txt"
    dist = {}
    for t in kept:
        dist[t["transfer"]] = dist.get(t["transfer"], 0) + 1
    with open(rep_path, "w") as r:
        r.write(f"Import {args.period}: {len(kept)} transactions, "
                f"{pair_count} transfer pairs merged, {len(unpaired)} unpaired, "
                f"{pending_total} pending skipped, {len(unknown)} need rules\n")
        r.write(f"Inputs: {', '.join(args.inputs)}\n")
        r.write(f"QIF SHA-256: {sha256}\n\n")
        r.write("Transactions per transfer account:\n")
        for acct, n in sorted(dist.items(), key=lambda x: -x[1]):
            r.write(f"  {n:5d}  {acct}\n")
        if unpaired:
            r.write("\nUNPAIRED TRANSFERS (no mirror found):\n")
            for t in sorted(unpaired, key=lambda t: (str(t["date"]), t["fitid"])):
                r.write(f"  {t['date']} {t['amount']:10.2f}  {t['source'].split(':')[-1]:22s} -> "
                        f"{t['transfer'].split(':')[-1]:22s}  {clean(t['payee'])}\n")
        if unknown:
            r.write("\nNEEDS RULE (went to Expenses:Unknown):\n")
            for t in sorted(unknown, key=lambda x: x["date"]):
                r.write(f"  {t['date']} {t['amount']:10.2f}  {t['source'].split(':')[-1]:22s}  "
                        f"{clean(t['payee'])} | {clean(t['memo'])}\n")

    if args.unknowns_out:
        with open(args.unknowns_out, "w") as f:
            json.dump([unknown_record(t) for t in sorted(unknown, key=lambda x: x["date"])],
                      f, indent=1)
        print(f"wrote {args.unknowns_out}: {len(unknown)} uncategorized transactions",
              file=sys.stderr)

    print(f"wrote {args.output}: {len(kept)} txns across {len(per_account)} accounts, "
          f"{len(unknown)} unmatched (see {rep_path})")


if __name__ == "__main__":
    main()
