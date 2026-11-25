# -*- coding: utf-8 -*-
"""QuickBooks Online Reports API — parsers.

Thin JSON→dict normalizers that convert QBO's nested Report response
shape into the flat `[{'account', 'date', 'amount'}, ...]` format our
existing transform functions expect.

QBO Reports JSON shape (abbreviated):
    {
      "Header":  {"StartPeriod":"...", "EndPeriod":"...", ...},
      "Columns": {"Column":[{"ColTitle":"Account #","ColType":"String"},...]},
      "Rows":    {"Row":[  <recursive row tree>  ]}
    }

Each Row is one of:
    * Section: has "Header" (section title), nested "Rows", optional "Summary"
    * Data:    has "ColData" = flat array of {value: str, id?: str}

For TransactionList: mostly flat Data rows.
For BalanceSheet:    deeply nested Section tree with Data leaves and Summary totals.

Design notes:
  * We never trust the JSON blindly — every row path goes through
    defensive .get() chains so malformed responses raise a specific
    error rather than KeyErroring somewhere obscure.
  * We skip Summary rows (QBO's "Total X" subtotals) — the transform
    layer either recomputes them or doesn't need them.
  * Account codes are re-parsed from either Account # column or from
    the account name (which often contains the number embedded).
"""
import re
from datetime import date as date_cls, datetime
from decimal import Decimal, InvalidOperation


# Matches 5-digit account codes, with optional 1-2 char letter/number sub.
_ACCT_IN_TEXT_RE = re.compile(r'\b(\d{4,5})([A-Za-z0-9]{0,2})\b')


class ReportParseError(ValueError):
    """Raised when a QBO report response can't be parsed."""


# ==================================================================
# Column index resolution
# ==================================================================
def _column_index(columns_block, titles):
    """Return the column index whose ColTitle matches any of the given
    titles (case-insensitive). Returns None if not found.

    `titles` is a tuple of acceptable header strings.
    """
    cols = ((columns_block or {}).get('Column') or [])
    for i, col in enumerate(cols):
        if (col.get('ColTitle') or '').strip().lower() in {t.lower() for t in titles}:
            return i
    return None


# ==================================================================
# TransactionList — the "Transaction Detail by Account" equivalent
# ==================================================================
def parse_transaction_list(json_data, lodge_timezone_offset_hours=0):
    """Return a list of {'account', 'date', 'amount'} dicts.

    Mirrors the shape produced by the xlsx/csv transform so the
    downstream FRS writer is source-agnostic.
    """
    if not isinstance(json_data, dict):
        raise ReportParseError('Expected a JSON object at report root.')
    columns = json_data.get('Columns')
    rows_block = (json_data.get('Rows') or {}).get('Row') or []
    acct_num_idx = _column_index(columns, ('Account #', 'Account number', 'Account No'))
    acct_name_idx = _column_index(columns, ('Account', 'Account name'))
    date_idx = _column_index(columns, ('Date', 'Txn Date', 'Transaction Date'))
    debit_idx = _column_index(columns, ('Debit', 'Debt Amt', 'Debit Amount'))
    credit_idx = _column_index(columns, ('Credit', 'Credit Amount'))
    amount_idx = _column_index(columns, ('Amount', 'Subt Nat Amount',
                                         'Nat Amount', 'Subt Amount'))
    if date_idx is None:
        raise ReportParseError(
            "Report is missing a Date column. Add Date to the "
            "TransactionList report columns.")
    if acct_num_idx is None and acct_name_idx is None:
        raise ReportParseError(
            "Report has no Account # or Account column. Enable Account "
            "numbers in the QBO report customization.")
    if debit_idx is None and credit_idx is None and amount_idx is None:
        raise ReportParseError(
            "Report is missing Debit/Credit and signed Amount columns.")

    out = []
    skipped = []
    current_account = None
    _walk_transaction_rows(
        rows_block, out, skipped,
        acct_num_idx, acct_name_idx, date_idx, debit_idx, credit_idx, amount_idx,
        current_account,
    )
    return out, skipped


def _walk_transaction_rows(rows, out, skipped, acct_num_idx, acct_name_idx,
                           date_idx, debit_idx, credit_idx, amount_idx,
                           current_account):
    for row in rows or []:
        rtype = row.get('type')
        if rtype == 'Section':
            # Section headers in TransactionList often carry the
            # account for every data row below them.
            header = (row.get('Header') or {})
            section_acct = _extract_account_from_section_header(header)
            child_account = section_acct or current_account
            _walk_transaction_rows(
                (row.get('Rows') or {}).get('Row') or [],
                out, skipped,
                acct_num_idx, acct_name_idx, date_idx,
                debit_idx, credit_idx, amount_idx,
                child_account,
            )
            # Summary rows exist but we don't want them in output.
            continue
        # Data row
        cols = row.get('ColData') or []
        try:
            acct = _resolve_account_from_coldata(
                cols, acct_num_idx, acct_name_idx, current_account)
            dt = _parse_date_cell(
                cols[date_idx].get('value')
                if date_idx < len(cols) else None
            )
            if dt is None:
                continue
            amount = _resolve_amount(cols, debit_idx, credit_idx, amount_idx)
            if amount is None:
                continue
            if not acct:
                skipped.append({'row': row, 'reason': 'no account resolvable'})
                continue
            out.append({'account': acct, 'date': dt, 'amount': amount})
        except Exception as exc:  # noqa: BLE001
            skipped.append({'row': row, 'reason': str(exc)})
            continue


def _extract_account_from_section_header(header):
    """QBO section headers look like 'ColData: [{value: "10101 Cash on Hand"}]'."""
    for cell in (header.get('ColData') or []):
        v = (cell.get('value') or '')
        acct = _find_account_in_text(v)
        if acct:
            return acct
    # Some headers carry the account in the top-level 'value' field.
    v = header.get('value') or ''
    return _find_account_in_text(v)


def _resolve_account_from_coldata(cols, num_idx, name_idx, fallback):
    if num_idx is not None and num_idx < len(cols):
        raw = (cols[num_idx].get('value') or '').strip()
        if raw:
            acct = _find_account_in_text(raw)
            if acct:
                return acct
    if name_idx is not None and name_idx < len(cols):
        raw = (cols[name_idx].get('value') or '').strip()
        acct = _find_account_in_text(raw)
        if acct:
            return acct
    return fallback


def _find_account_in_text(s):
    if not s:
        return None
    m = _ACCT_IN_TEXT_RE.search(s)
    if m:
        return f"{m.group(1)}{m.group(2).upper()}"
    return None


def _parse_date_cell(raw):
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    for fmt in ('%Y-%m-%d', '%m/%d/%Y', '%m/%d/%y',
                '%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M:%S%z'):
        try:
            return datetime.strptime(s[:19], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        return None


def _parse_number_cell(raw):
    if raw in (None, ''):
        return Decimal('0')
    s = str(raw).strip()
    if not s:
        return Decimal('0')
    neg = s.startswith('(') and s.endswith(')')
    s = s.strip('()').replace('$', '').replace(',', '').strip()
    if not s or s == '-':
        return Decimal('0')
    try:
        val = Decimal(s)
    except (InvalidOperation, ValueError):
        raise ValueError(f'invalid number: {raw!r}')
    return -val if neg else val


def _resolve_amount(cols, debit_idx, credit_idx, amount_idx):
    """Prefer explicit Debit/Credit columns; fall back to signed Amount."""
    if debit_idx is not None and credit_idx is not None:
        d = _parse_number_cell(
            cols[debit_idx].get('value') if debit_idx < len(cols) else None)
        c = _parse_number_cell(
            cols[credit_idx].get('value') if credit_idx < len(cols) else None)
        return d - c
    if amount_idx is not None and amount_idx < len(cols):
        return _parse_number_cell(cols[amount_idx].get('value'))
    return None


# ==================================================================
# BalanceSheet — as-of snapshot
# ==================================================================
def parse_balance_sheet(json_data):
    """Return (as_of_date, [{'account', 'amount'}, ...], skipped).

    Walks the nested Section tree, emitting one row per account with
    its closing balance as of the report's end_date. Section totals
    (Total Assets, etc.) are discarded; we keep only leaf account rows.
    """
    if not isinstance(json_data, dict):
        raise ReportParseError('Expected a JSON object at report root.')

    header = json_data.get('Header') or {}
    as_of_raw = header.get('EndPeriod') or header.get('end_date')
    as_of = _parse_date_cell(as_of_raw) or date_cls.today()

    columns = json_data.get('Columns')
    rows_block = (json_data.get('Rows') or {}).get('Row') or []

    # Balance Sheet usually has one data column (the balance), sometimes
    # multiple for comparison reports. We want the LAST non-label column,
    # which holds the as-of balance.
    cols = (columns or {}).get('Column') or []
    balance_idx = None
    for i, col in enumerate(cols):
        if (col.get('ColType') or '').lower() == 'money':
            balance_idx = i
    if balance_idx is None:
        balance_idx = len(cols) - 1  # last column; still often works

    out = []
    skipped = []
    _walk_balance_sheet_rows(rows_block, out, skipped, balance_idx)
    return as_of, out, skipped


def _walk_balance_sheet_rows(rows, out, skipped, balance_idx):
    for row in rows or []:
        rtype = row.get('type')
        if rtype == 'Section':
            _walk_balance_sheet_rows(
                (row.get('Rows') or {}).get('Row') or [],
                out, skipped, balance_idx,
            )
            # Skip the Summary subtotal
            continue
        cols = row.get('ColData') or []
        if not cols:
            continue
        label = (cols[0].get('value') or '').strip()
        if not label:
            continue
        acct = _find_account_in_text(label)
        if not acct:
            # QBO may embed the account ID in ColData[0].id
            raw_id = (cols[0].get('id') or '').strip()
            acct = _find_account_in_text(raw_id)
        if not acct:
            # Section-header row that isn't actually an account (e.g. "Bank Accounts")
            continue
        try:
            balance = _parse_number_cell(
                cols[balance_idx].get('value')
                if balance_idx < len(cols) else None
            )
        except ValueError as exc:
            skipped.append({'row': row, 'reason': str(exc)})
            continue
        out.append({'account': acct, 'amount': balance})
