#!/usr/bin/env python3
"""Tests for reconcile.py YAML parsing and rule matching."""

import sys
import os
import tempfile
import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reconcile import Rules


def write_yaml(content):
    fd, path = tempfile.mkstemp(suffix='.yaml')
    with os.fdopen(fd, 'w') as f:
        f.write(content)
    return path


def test_pattern_rule():
    """Basic pattern rule."""
    path = write_yaml('''
rules:
  - account: Expenses:Food
    desc: PIZZA
''')
    r = Rules(path)
    assert len(r.rules_flat) == 1
    os.unlink(path)
    print("PASS: pattern rule")


def test_nested_inheritance():
    """Children inherit parent keys (source, account)."""
    path = write_yaml('''
rules:
  - source: Assets:Checking
    rules:
      - account: Expenses:Food
        desc: PIZZA
      - account: Expenses:Food
        desc: BURGER
''')
    r = Rules(path)
    assert len(r.rules_flat) == 2
    # Both inherit source
    for src, acct, pat, cond, _, _ in r.rules_flat:
        assert src == 'Assets:Checking', f"got {src}"
    # Child can override
    path2 = write_yaml('''
rules:
  - source: Assets:Checking
    account: Expenses:Food
    rules:
      - desc: PIZZA
      - desc: BURGER
        account: Expenses:Snacks
''')
    r2 = Rules(path2)
    accts = {pat: acct for _, acct, pat, _, _, _ in r2.rules_flat}
    assert accts['PIZZA'] == 'Expenses:Food'
    assert accts['BURGER'] == 'Expenses:Snacks'
    os.unlink(path)
    os.unlink(path2)
    print("PASS: nested inheritance")


def test_code_rules():
    """mcc:/pfc: without pattern build the fallback indexes."""
    path = write_yaml('''
rules:
  - source: Assets:Checking
    account: Expenses:Groceries
    mcc: [5411]
  - account: Expenses:Dining
    pfc: [FOOD_AND_DRINK_RESTAURANT]
''')
    r = Rules(path)
    assert r.mcc_index[('Assets:Checking', '5411')] == 'Expenses:Groceries'
    assert r.pfc_index[(None, 'FOOD_AND_DRINK_RESTAURANT')] == 'Expenses:Dining'
    # Pattern-less rules are in rules_flat with pat=None (skipped in main loop,
    # checked via fallback indexes)
    assert len(r.rules_flat) == 2
    assert all(pat is None for _, _, pat, _, _, _ in r.rules_flat)
    os.unlink(path)
    print("PASS: code rules")


def test_no_code_rules_key():
    """Top-level code_rules: is no longer read."""
    path = write_yaml('''
rules: []
code_rules:
  - account: Expenses:X
    mcc: [9999]
''')
    r = Rules(path)
    assert len(r.mcc_index) == 0
    os.unlink(path)
    print("PASS: code_rules ignored")


def test_not_mcc_removed():
    """not_mcc:/not_pfc: are rejected as unknown."""
    path = write_yaml('''
rules:
  - account: Expenses:Food
    desc: PIZZA
    not_mcc: [1234]
''')
    try:
        Rules(path)
        assert False, "not_mcc should be rejected"
    except SystemExit:
        pass
    os.unlink(path)
    print("PASS: not_mcc removed")


def test_compound_conditions():
    """pattern + mcc/pfc/no_mcc/no_pfc all must pass."""
    path = write_yaml('''
rules:
  - source: Assets:Checking
    account: Expenses:Fuel
    rules:
      - desc: QUICK STOP
        mcc: [5541, 5542]
      - desc: CASH FUEL
        no_mcc: true
''')
    r = Rules(path)
    # With matching MCC
    acct, _, _, _ = r.categorize('Assets:Checking', 'QUICK STOP 123',
                           None, plaid={'mcc': '5541', 'pfc_detailed': None})
    assert acct == 'Expenses:Fuel', f"got {acct}"
    # With wrong MCC
    acct, _, _, _ = r.categorize('Assets:Checking', 'QUICK STOP 123',
                           None, plaid={'mcc': '5411', 'pfc_detailed': None})
    assert acct != 'Expenses:Fuel', f"should not desc: {acct}"
    # no_mcc with no MCC
    acct, _, _, _ = r.categorize('Assets:Checking', 'CASH FUEL',
                           None, plaid={'mcc': None, 'pfc_detailed': None})
    assert acct == 'Expenses:Fuel', f"got {acct}"
    # no_mcc with MCC present
    acct, _, _, _ = r.categorize('Assets:Checking', 'CASH FUEL',
                           None, plaid={'mcc': '5541', 'pfc_detailed': None})
    assert acct != 'Expenses:Fuel', f"should not desc: {acct}"
    os.unlink(path)
    print("PASS: compound conditions")


def test_checknum():
    """checknum: list and dict forms. Now a regular if: filter."""
    path = write_yaml('''
rules:
  - source: Assets:Checking
    rules:
      - if:
          desc: LANDLORD
          checknum: [1001, 1002]
        then: Expenses:Rent
      - if:
          desc: GARAGE
          checks:
            CITY GARAGE: [2001]
        then: Expenses:Parking
''')
    r = Rules(path)
    # check_payees still built for payee replacement
    assert r.check_payees[('Assets:Checking', '2001')] == 'CITY GARAGE'
    assert ('Assets:Checking', '1001') not in r.check_payees
    # checknum is now a regular filter in cond
    acct, _, _, _ = r.categorize('Assets:Checking', 'LANDLORD', None, checknum='1001')
    assert acct == 'Expenses:Rent', f"got {acct}"
    acct, _, _, _ = r.categorize('Assets:Checking', 'LANDLORD', None, checknum='9999')
    assert acct != 'Expenses:Rent', f"checknum filter not applied"
    os.unlink(path)
    print("PASS: checknum")


def test_source_scoping():
    """Rules without source match any; with source must match."""
    path = write_yaml('''
rules:
  - account: Expenses:Any
    desc: GENERIC
  - source: Assets:Checking
    account: Expenses:Scoped
    desc: SPECIFIC
''')
    r = Rules(path)
    acct, _, _, _ = r.categorize('Assets:Savings', 'GENERIC', None, plaid=None)
    assert acct == 'Expenses:Any', f"got {acct}"
    acct, _, _, _ = r.categorize('Assets:Savings', 'SPECIFIC', None, plaid=None)
    assert acct != 'Expenses:Scoped', f"should not match scoped: {acct}"
    acct, _, _, _ = r.categorize('Assets:Checking', 'SPECIFIC', None, plaid=None)
    assert acct == 'Expenses:Scoped', f"got {acct}"
    os.unlink(path)
    print("PASS: source scoping")


def test_transfer_as_rule():
    """Transfers are just rules between source accounts."""
    path = write_yaml('''
source_accounts:
  'AAAA': Assets:Checking
  'BBBB': Liabilities:Card
rules:
  - source: Assets:Checking
    account: Liabilities:Card
    desc: CARD PAYMENT
''')
    r = Rules(path)
    acct, how, _, _ = r.categorize('Assets:Checking', 'CARD PAYMENT THANK YOU',
                             None, plaid=None)
    assert acct == 'Liabilities:Card', f"got {acct}"
    os.unlink(path)
    print("PASS: transfer as rule")



def test_amount_conditions():
    import tempfile, os
    rules = '''
rules:
- source: Assets:Checking
  rules:
  - account: Expenses:Fees
    desc: INTUIT
    amount: "<0"
  - account: Income:Revenue
    desc: INTUIT
    amount: ">0"
  - account: Expenses:Exact
    desc: EXACT
    amount: 100
  - account: Expenses:Credits
    amount: ">0"
'''
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(rules)
        path = f.name
    r = Rules(path)
    # Debit with INTUIT -> Fees
    acct, _, _, _ = r.categorize('Assets:Checking', 'INTUIT *QBOOKS', None, amount=-50.0)
    assert acct == 'Expenses:Fees', f"debit got {acct}"
    # Credit with INTUIT -> Revenue
    acct, _, _, _ = r.categorize('Assets:Checking', 'INTUIT *QBOOKS', None, amount=1000.0)
    assert acct == 'Income:Revenue', f"credit got {acct}"
    # Exact amount match
    acct, _, _, _ = r.categorize('Assets:Checking', 'EXACT MATCH', None, amount=100.0)
    assert acct == 'Expenses:Exact', f"exact got {acct}"
    # Exact amount non-match -> falls to amount-only credit catch-all
    acct, _, _, _ = r.categorize('Assets:Checking', 'EXACT MATCH', None, amount=99.0)
    assert acct == 'Expenses:Credits', f"non-exact got {acct}"
    # Amount-only catch-all for credits
    acct, _, _, _ = r.categorize('Assets:Checking', 'RANDOM DESC', None, amount=5.0)
    assert acct == 'Expenses:Credits', f"catch-all got {acct}"
    # Debit with random desc -> no match (no debit catch-all)
    acct, _, _, _ = r.categorize('Assets:Checking', 'RANDOM DESC', None, amount=-5.0)
    assert acct is None or "Unknown" in str(acct), f"debit catch-all got {acct}"
    os.unlink(path)
    print("PASS: amount conditions")

if __name__ == '__main__':
    test_pattern_rule()
    test_nested_inheritance()
    test_code_rules()
    test_no_code_rules_key()
    test_not_mcc_removed()
    test_compound_conditions()
    test_checknum()
    test_source_scoping()
    test_transfer_as_rule()
    test_amount_conditions()
    print("\nAll tests passed.")

