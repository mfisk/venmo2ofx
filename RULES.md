# Rules Format

Rules use `if:`/`then:` structure. Filters (nouns) go under `if:`, side-effects
(verbs) go under `then:`.

## Semantics

- **AND:** All top-level terms in `if:` are ANDed together.
- **OR:** For OR, use separate rules. Within a single filter type, a list of
  values is ORed (e.g. `desc: [A, B]` matches A or B).
- **First-match:** Rules are evaluated in order; the first matching rule wins.

## `if:` filters

`if:` accepts a bare string (shorthand for `desc`) or a dict of filters:

| Filter | Type | Description |
|--------|------|-------------|
| `desc` | string or list | Substring match against payee+memo (case-insensitive). List = OR. |
| `mcc` | string or list | Merchant category code from Plaid. List = OR. |
| `pfc` | string or list | Plaid personal finance category. List = OR. |
| `amount` | number or `">N"`/`"<N"` | Signed amount: negative = debit/out, positive = credit/in. |
| `checknum` | string/list/dict | Check number(s). Dict format: `{Payee: [nums]}` (also sets payee). |
| `fitid` | string or list | Exact transaction ID. Cannot be combined with other filters. |
| `memo_regex` | string | Regex matched against memo (used in offsets; extracts amount from group 1). |
| `no_mcc` | bool | Require no MCC present. |
| `no_pfc` | bool | Require no PFC present. |
| `source` | string | Source account path. Usually set at parent level for grouping. |

`fitid` cannot be combined with other filters in the same `if:` (AND would be
meaningless for an exact ID). Use separate rules for OR.

## `then:` actions

`then:` accepts a bare account string (shorthand) or a dict:

```yaml
# Shorthand
- if: PAYROLL
  then: Expenses:Payroll

# Dict with account and offsets
- if: PAYROLL
  then:
    account: Expenses:Payroll
    offsets:
      - if:
          memo_regex: 'Bonus\s+\$([\d,]+\.?\d*)'
        debit: Assets:Bonus
        credit: Expenses:Payroll:Bonus
        describe_as: 'Bonus for {payee}'
```

### Offsets

When a transaction matches the rule's `if:`, each offset's own `if:` is tested.
Matching offsets generate double-entry transactions:

- `-amount` from the credit account (as the `!Account` source)
- `+amount` to the debit account (as the `L` transfer)

Amount is extracted from regex group 1. `describe_as` supports `{payee}` and
`{amount}` templates.

## `source:` grouping

`source:` at the parent level groups rules; children inherit it:

```yaml
- source: Assets:Checking
  rules:
    - if: PAYROLL
      then: Expenses:Payroll
    - if:
        desc: GROCERY
        mcc: ['5411']
      then: Expenses:Groceries
```

Children can override with their own `source:` if needed.

## Fallback rules

A rule marked with `fallback: true` is only tried after all regular rules
have been checked. This ensures a specific rule always beats a catch-all,
regardless of sort order.

```yaml
- source: Assets:Green Dot
  rules:
    - if:
        desc: VENMO-CASHOUT
      then: Assets:Gina Venmo
    - if:
        amount: '>0'
      then: Zia Revenue
      fallback: true
```

Fallback-matched transactions are flagged for review — they indicate a
transaction not covered by a specific rule. For transfer pairing, fallback-
matched transactions are treated as uncategorized (available as mirrors).

## Account placeholders

- `YYYY` → transaction year (e.g. `2026`)
- `YYYY-MM` → transaction year-month (e.g. `2026-10`)

## Checknum formats

Two supported formats (format 2, num→payee dict, was dropped):

```yaml
# List (no payee replacement)
checks: ['101', '102']

# Payee -> nums dict (also sets payee from check image)
checks:
  'Acme Corp': ['101', '102']
```

The payee dict populates payee replacement: the generic "Check Paid #NNN"
payee is replaced with the handwritten name after matching.
