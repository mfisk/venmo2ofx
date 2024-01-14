# venmo2ofx

Bookkeeping with Venmo is challenging as you basically need to treat it as another bank account to reconcile if you need to track transactions, such as if you are using Venmo to buy or sell for business-related purposes.  Venmo allows you to download monthly CSV [statements](https://account.venmo.com/statement) one at a time and uses an proprietary format that is difficult to import into bookkeeping software.  Enter *venmo2ofx*, a utility to convert one or more Venmo CSV files into the widely-supported OFX/QFX format.

### Motivation ###
Venmo is novel payments platform that defaults to zero privacy and sharing all of your transactions publicly by default.  Venmo is an e-money system and can keep a balance like a bank account.  When spending money in excess of your balance, a debit is typically made to an external debit account.  Venmo users can cash out positive balances with a credit to an external debit account.  Therefore, the transactions you see in your external account (e.g., your real bank account) are not 1-1 with your Venmo transactions.  Thus, the need to create your Venmo account as essentially another account in your bookkeeping software and import these transacitons.  

## Usage

`venmo2ofx [input files...] [> output.ofx]`

Provide an input CSV file on stdin or specify one or more named files, venmo2ofx produces a single ofx file on stdout.  

For a given invocation of *venmo2ofx*, all inputs must be from the same Venmo account.

## Features

* Auto-detects directionality to identify the other party
* Includes the notes field with all those emojis that Venmo users love
* Payments that generate a debit on your backing back account are expressed as two transactions:
   * A transfer of funds in from your backing bank account (which can be reconciled against that account)
   * A payment out (which can be reconciled against expense or revenue accounts)
* Supports multiple Venmo accounts
  * Includes venmo user `@handle` in account meta-data so that bookkeeping software can map to the correct account
* Supports Venmo consumer accounts and business accounts (download the Fees CSVs)
  * For business transactions, only the net proceeds are reported
* Tested with [Gnucash](https://www.gnucash.org/)

## Known-limitations

* Only fully supports USD transactions
* Have not tested with debit card transactions, unfullfilled requests

## References

* [OFX Banking Standard Version 2.3](https://financialdataexchange.org/FDX/About/OFX-Work-Group.aspx?a315d1c24e44=2#a315d1c24e44:~:text=Banking%20Version%202.3-,Download,-Download)
