# -*- coding: utf-8 -*-
"""QuickBooks Online → FRS CSV transforms.

Bridge tool for the manual FRS submission workflow documented in the
2020 "FRS Quick Reference Guide For QuickBooks - Online Version" and
the 2019/2024 FRS Introduction PDFs. Two report types supported:

  ACTUALS (Transaction Detail by Account)
    1. Treasurer runs QBO's "Transaction Detail by Account" report
    2. Columns = Account #, Date, Debit, Credit
    3. Date range = one month, a quarter, a fiscal year, whatever
    4. Upload → get FRS_Actuals_<lodge>_<period>.csv

  BALANCE SHEET (as-of snapshot — also feeds the FRS "Beginning Balance"
  one-time upload)
    1. Treasurer runs QBO's "Balance Sheet" report
    2. "As of" = 3/31 (year-end) or any snapshot date
    3. Upload → get FRS_BalanceSheet_<lodge>_<as-of>.csv

Both outputs use the same 4-column FRS Actuals shape per the 2019 spec:

    LodgeNumber, LodgeGLAccount, Date, Amount

For Actuals: Amount = Debit − Credit, one row per transaction (or
aggregated monthly / annually to fiscal year-end 3/31).

For Balance Sheet: Amount = closing balance, Date = the report's
as-of date, one row per balance-sheet account.

The core transforms are exposed as plain functions
(`transform_qbo_to_frs_actuals`, `transform_qbo_to_frs_balance_sheet`)
so other code and tests can call them without instantiating the wizard.
"""
import base64
import calendar
import csv
import io
import logging
import re
from collections import defaultdict
from datetime import date as date_cls, datetime
from decimal import Decimal, ROUND_HALF_UP

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


# Column header aliases QBO may emit depending on locale / version.
# The FRS guide tells treasurers to customize to Account # / Date /
# Debit / Credit — but we also accept QBO's default "Amount" (signed)
# column so the exporter doesn't fail on a vanilla report export.
_COL_ACCOUNT = {'account #', 'account number', 'account no', 'account'}
_COL_DATE    = {'date', 'txn date', 'transaction date'}
_COL_DEBIT   = {'debit'}
_COL_CREDIT  = {'credit'}
_COL_AMOUNT  = {'amount', 'subt nat amount', 'nat amount', 'net amount'}

# Account-number pattern: 5-digit code, optional 1-2 char subaccount.
# Matches both "10101" and "10104A" and "21200A" and "10104B1".
_ACCT_RE = re.compile(r'^\s*(\d{4,5})([A-Za-z0-9]{0,2})\s*$')

# QBO often puts the account header as "10101 Cash on Hand General"
# at the top of an account's transaction section. Capture the code.
_ACCT_HEADER_RE = re.compile(r'^\s*(\d{4,5})([A-Za-z0-9]{0,2})\s+\S')

# "Total for Account XXX", "Total 10101 Cash on Hand General",
# "Total September 2026", "TOTAL" — all skip.
_TOTAL_PATTERNS = (
    re.compile(r'^\s*total\b', re.I),
    re.compile(r'^\s*grand\s+total', re.I),
    re.compile(r'^\s*subtotal', re.I),
)


# ==================================================================
# Pure-Python transform (importable, testable without an Odoo env)
# ==================================================================
def transform_qbo_to_frs_actuals(
    raw_bytes,
    filename,
    lodge_number,
    aggregation='transaction',
    encoding='utf-8-sig',
    # backward-compat shim: callers passing aggregate_monthly=True still work
    aggregate_monthly=None,
):
    """Convert a QBO Transaction Detail export to an FRS Actuals CSV.

    :param bytes raw_bytes: the uploaded file content
    :param str filename: original filename (used to detect .xlsx vs .csv)
    :param str lodge_number: 4-digit lodge number for column 1
    :param str aggregation: one of 'transaction' (one row per txn,
        default), 'monthly' (one row per (account, month) at month-end),
        or 'annual' (one row per (account, Elks FYE 3/31))
    :param bool aggregate_monthly: deprecated — kept for backward
        compat; if True, forces aggregation='monthly'
    :param str encoding: text encoding for .csv inputs

    :returns: dict with keys:
        - 'csv_bytes': UTF-8 bytes of the FRS Actuals CSV
        - 'suggested_filename': FRS_Actuals_<lodge>_<YYYY-MM>.csv or
          FRS_Actuals_<lodge>_<YYYY-MM>_to_<YYYY-MM>.csv when the data
          spans multiple months
        - 'row_count': int, rows in the output CSV (excluding header)
        - 'min_date' / 'max_date': date objects of the data range
        - 'skipped': list of {row, reason} for diagnostic display
    """
    if not raw_bytes:
        raise ValueError("No file content provided.")
    if not lodge_number or not lodge_number.strip():
        raise ValueError("Lodge number is required.")

    lodge_number = lodge_number.strip()
    is_xlsx = (filename or '').lower().endswith(('.xlsx', '.xlsm', '.xls'))

    if is_xlsx:
        rows = _read_xlsx(raw_bytes)
    else:
        rows = _read_csv(raw_bytes, encoding)

    txns, skipped = _extract_transactions(rows)

    if not txns:
        raise ValueError(
            "No transactions found in the file. Make sure you exported "
            "the 'Transaction Detail by Account' report with columns "
            "Account #, Date, Debit, Credit.")

    # Backward-compat: honor the old boolean if the new selector wasn't set
    if aggregate_monthly:
        aggregation = 'monthly'
    if aggregation not in ('transaction', 'monthly', 'annual'):
        raise ValueError(
            f"aggregation must be 'transaction', 'monthly', or 'annual'; "
            f"got {aggregation!r}"
        )

    if aggregation == 'monthly':
        txns = _aggregate_monthly(txns)
    elif aggregation == 'annual':
        txns = _aggregate_annual(txns)

    # Sort for deterministic output: account, then date
    txns.sort(key=lambda t: (t['account'], t['date']))

    min_d = min(t['date'] for t in txns)
    max_d = max(t['date'] for t in txns)

    # Build CSV
    output = io.StringIO()
    writer = csv.writer(output, lineterminator='\n')
    writer.writerow(['LodgeNumber', 'LodgeGLAccount', 'Date', 'Amount'])
    for t in txns:
        amt = t['amount'].quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        if amt == 0:
            # FRS typically ignores zero-amount rows; skip to keep file clean.
            continue
        writer.writerow([
            lodge_number,
            t['account'],
            t['date'].strftime('%m/%d/%Y'),
            f"{amt:.2f}",
        ])

    csv_bytes = output.getvalue().encode('utf-8')

    # Filename convention matches the existing frs_export_wizard output.
    # Annual mode gets a fiscal-year-end tag for clarity.
    if aggregation == 'annual':
        unique_fyes = sorted({t['date'] for t in txns})
        if len(unique_fyes) == 1:
            period_tag = f"FY{unique_fyes[0].year}"  # e.g. FY2027 means 4/1/26-3/31/27
        else:
            period_tag = (
                f"FY{unique_fyes[0].year}_to_FY{unique_fyes[-1].year}"
            )
    elif min_d.year == max_d.year and min_d.month == max_d.month:
        period_tag = min_d.strftime('%Y-%m')
    else:
        period_tag = f"{min_d.strftime('%Y-%m')}_to_{max_d.strftime('%Y-%m')}"
    suggested_filename = f"FRS_Actuals_{lodge_number}_{period_tag}.csv"

    # Row count: subtract header line
    row_count = csv_bytes.count(b'\n') - 1

    return {
        'csv_bytes': csv_bytes,
        'suggested_filename': suggested_filename,
        'row_count': row_count,
        'min_date': min_d,
        'max_date': max_d,
        'skipped': skipped,
    }


# ==================================================================
# File readers — normalize to list[list[str|None]]
# ==================================================================
def _read_xlsx(raw_bytes):
    try:
        import openpyxl
    except ImportError as exc:
        raise ValueError(
            "openpyxl is required to read Excel files. Install it "
            "(`pip install openpyxl`), or export the QBO report as CSV "
            "instead.") from exc
    wb = openpyxl.load_workbook(
        io.BytesIO(raw_bytes), read_only=True, data_only=True)
    ws = wb.active
    out = []
    for row in ws.iter_rows(values_only=True):
        out.append([_cell_to_str(c) for c in row])
    wb.close()
    return out


def _read_csv(raw_bytes, encoding):
    text = _decode_text(raw_bytes, encoding)
    rdr = csv.reader(io.StringIO(text))
    return [list(row) for row in rdr]


def _decode_text(raw_bytes, encoding):
    for enc in (encoding, 'utf-8-sig', 'cp1252', 'latin-1'):
        try:
            return raw_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw_bytes.decode('utf-8', errors='replace')


def _cell_to_str(cell):
    if cell is None:
        return ''
    if isinstance(cell, (int, float, Decimal)):
        return str(cell)
    if isinstance(cell, (datetime, date_cls)):
        return cell.isoformat()
    return str(cell)


# ==================================================================
# Transaction extraction — the core parser
# ==================================================================
def _extract_transactions(rows):
    """Walk the QBO export and yield transaction dicts.

    Handles QBO's quirks: title rows, blank rows, account section
    headers, month sub-headers, total/subtotal rows, and the two shapes
    QBO uses for the account number (either in the Account # column on
    each transaction row, or only in a section-header row above).
    """
    # Find header row and column positions
    header_idx, cols = _find_header(rows)
    if header_idx is None:
        raise ValueError(
            "Could not find a header row with Account #, Date, Debit, "
            "and Credit columns. Did you customize the QBO report "
            "columns per the FRS guide?")

    txns = []
    skipped = []
    current_account = None  # set when we encounter a section header

    for i in range(header_idx + 1, len(rows)):
        row = rows[i]
        if _is_blank(row):
            continue

        # First column often contains the account header or total marker
        first_cell = (row[0] or '').strip() if row else ''

        # Total / subtotal row → skip
        if any(p.match(first_cell) for p in _TOTAL_PATTERNS):
            continue

        # Account section header like "10101 Cash on Hand General"
        m = _ACCT_HEADER_RE.match(first_cell)
        if m:
            current_account = f"{m.group(1)}{m.group(2).upper()}"
            continue

        # Try to extract transaction fields
        try:
            acct = _extract_account(row, cols, current_account)
            dt = _extract_date(row, cols)
            if dt is None:
                # Likely a QBO month header like "September 2026" — skip
                continue
            if cols.get('debit') is not None and cols.get('credit') is not None:
                debit = _extract_decimal(row, cols['debit'])
                credit = _extract_decimal(row, cols['credit'])
                amount = debit - credit
            elif cols.get('amount') is not None:
                # QBO's signed Amount column: positive = debit, negative = credit
                amount = _extract_decimal(row, cols['amount'])
            else:
                continue

            if not acct:
                skipped.append({
                    'row': i + 1,
                    'reason': 'no account number resolvable',
                })
                continue

            txns.append({
                'account': acct,
                'date': dt,
                'amount': amount,
            })
        except _SkipRow as sr:
            skipped.append({'row': i + 1, 'reason': str(sr)})
            continue
        except Exception as exc:  # noqa: BLE001
            skipped.append({'row': i + 1, 'reason': f"parse error: {exc}"})
            continue

    return txns, skipped


class _SkipRow(Exception):
    """Raise to skip a row with a specific reason."""


def _find_header(rows):
    """Locate the header row and return (row_index, {name: col_idx}).

    Accepts two column layouts:
      1. FRS-guide custom: Account # · Date · Debit · Credit
      2. QBO vanilla export: Date · ... · Amount (signed, negative = credit)
    Account # may live in its own column OR only on section-header rows.
    """
    for i, row in enumerate(rows[:30]):
        if not row:
            continue
        normalized = [(c or '').strip().lower() for c in row]
        cols = {}
        for j, cell in enumerate(normalized):
            if cell in _COL_ACCOUNT and 'account' not in cols:
                cols['account'] = j
            elif cell in _COL_DATE and 'date' not in cols:
                cols['date'] = j
            elif cell in _COL_DEBIT and 'debit' not in cols:
                cols['debit'] = j
            elif cell in _COL_CREDIT and 'credit' not in cols:
                cols['credit'] = j
            elif cell in _COL_AMOUNT and 'amount' not in cols:
                cols['amount'] = j
        has_date = 'date' in cols
        has_dr_cr = 'debit' in cols and 'credit' in cols
        has_amount = 'amount' in cols
        if has_date and (has_dr_cr or has_amount):
            # Fill in missing keys so downstream lookups are safe.
            cols.setdefault('account', None)
            cols.setdefault('debit', None)
            cols.setdefault('credit', None)
            cols.setdefault('amount', None)
            return i, cols
    return None, None


def _is_blank(row):
    return not row or all(
        (c is None or (isinstance(c, str) and not c.strip()))
        for c in row
    )


def _extract_account(row, cols, current_account):
    """Return the account code from the row, or fall back to context."""
    idx = cols.get('account')
    if idx is not None and idx < len(row):
        raw = (row[idx] or '').strip() if row[idx] else ''
        m = _ACCT_RE.match(raw)
        if m:
            return f"{m.group(1)}{m.group(2).upper()}"
    return current_account


def _extract_date(row, cols):
    idx = cols['date']
    if idx >= len(row):
        return None
    cell = row[idx]
    if not cell:
        return None
    s = str(cell).strip()
    if not s:
        return None
    # Try ISO (from openpyxl date cells), then US, then month-day-year
    for fmt in ('%Y-%m-%d', '%Y-%m-%dT%H:%M:%S',
                '%m/%d/%Y', '%m/%d/%y',
                '%m-%d-%Y', '%m-%d-%y'):
        try:
            return datetime.strptime(s[:19], fmt).date()
        except ValueError:
            continue
    # openpyxl may give us a bare 'YYYY-MM-DD 00:00:00' string
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        pass
    return None


def _extract_decimal(row, idx):
    if idx is None or idx >= len(row):
        return Decimal('0')
    cell = row[idx]
    if cell in (None, ''):
        return Decimal('0')
    s = str(cell).strip()
    if not s:
        return Decimal('0')
    # Strip $ and commas; handle parentheses as negatives
    neg = s.startswith('(') and s.endswith(')')
    s = s.strip('()').replace('$', '').replace(',', '').strip()
    if not s or s == '-':
        return Decimal('0')
    try:
        val = Decimal(s)
    except Exception:  # noqa: BLE001
        raise _SkipRow(f"not a number in debit/credit: {cell!r}")
    return -val if neg else val


def _aggregate_monthly(txns):
    """Collapse per-txn rows into one row per (account, month-end)."""
    totals = defaultdict(Decimal)
    for t in txns:
        key = (t['account'], t['date'].year, t['date'].month)
        totals[key] += t['amount']
    out = []
    for (acct, yy, mm), amt in totals.items():
        last_day = calendar.monthrange(yy, mm)[1]
        out.append({
            'account': acct,
            'date': date_cls(yy, mm, last_day),
            'amount': amt,
        })
    return out


def _fiscal_year_end(d):
    """Return the 3/31 that closes the Elks fiscal year containing date d.

    Lodge fiscal year is April 1 → March 31 per the AA Manual. A txn
    on 2026-04-05 belongs to FY ending 2027-03-31; a txn on 2026-03-15
    belongs to FY ending 2026-03-31.
    """
    if d.month >= 4:
        return date_cls(d.year + 1, 3, 31)
    return date_cls(d.year, 3, 31)


def _aggregate_annual(txns):
    """Collapse per-txn rows into one row per (account, fiscal-year-end)."""
    totals = defaultdict(Decimal)
    for t in txns:
        fye = _fiscal_year_end(t['date'])
        totals[(t['account'], fye)] += t['amount']
    return [
        {'account': acct, 'date': fye, 'amount': amt}
        for (acct, fye), amt in totals.items()
    ]


# ==================================================================
# Balance Sheet transform (QBO Balance Sheet export → FRS CSV)
# ==================================================================
# Section headers / total markers to skip in the Balance Sheet export.
# We match case-insensitively, substring at the start of column A.
_BALANCE_SHEET_SKIP_PREFIXES = (
    'total', 'grand total', 'subtotal',
    # Section headers QBO emits — treat the account rows that follow
    # as children and skip the header itself.
    'assets', 'current assets', 'bank accounts', 'accounts receivable',
    'other current assets', 'fixed assets', 'other assets',
    'liabilities and equity', 'liabilities', 'current liabilities',
    'credit cards', 'other current liabilities',
    'long-term liabilities', 'long term liabilities',
    'equity', 'retained earnings', 'net income',
)


def transform_qbo_to_frs_balance_sheet(
    raw_bytes,
    filename,
    lodge_number,
    as_of_date=None,
    encoding='utf-8-sig',
):
    """Convert a QBO Balance Sheet export to a 4-column FRS CSV.

    :param bytes raw_bytes: file content
    :param str filename: original filename (used to detect .xlsx vs .csv)
    :param str lodge_number: 4-digit lodge number for column 1
    :param date as_of_date: the balance-sheet "as of" date; if None,
        we try to detect it from the report title and fall back to
        today.
    :param str encoding: text encoding for .csv inputs

    :returns: dict with same keys as transform_qbo_to_frs_actuals.
    """
    if not raw_bytes:
        raise ValueError("No file content provided.")
    if not lodge_number or not lodge_number.strip():
        raise ValueError("Lodge number is required.")

    lodge_number = lodge_number.strip()
    is_xlsx = (filename or '').lower().endswith(('.xlsx', '.xlsm', '.xls'))

    if is_xlsx:
        rows = _read_xlsx(raw_bytes)
    else:
        rows = _read_csv(raw_bytes, encoding)

    detected_date = _detect_balance_sheet_date(rows)
    as_of = as_of_date or detected_date or date_cls.today()

    accounts, skipped = _extract_balance_sheet_rows(rows)
    if not accounts:
        raise ValueError(
            "No balance-sheet accounts found. Make sure you uploaded "
            "QBO's 'Balance Sheet' report (not the Trial Balance or "
            "P&L) and that account numbers are shown.")

    # Sort for deterministic output
    accounts.sort(key=lambda a: a['account'])

    output = io.StringIO()
    writer = csv.writer(output, lineterminator='\n')
    writer.writerow(['LodgeNumber', 'LodgeGLAccount', 'Date', 'Amount'])
    row_count = 0
    for a in accounts:
        amt = a['amount'].quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        if amt == 0:
            continue
        writer.writerow([
            lodge_number,
            a['account'],
            as_of.strftime('%m/%d/%Y'),
            f"{amt:.2f}",
        ])
        row_count += 1

    csv_bytes = output.getvalue().encode('utf-8')
    suggested_filename = (
        f"FRS_BalanceSheet_{lodge_number}_{as_of.strftime('%Y-%m-%d')}.csv"
    )
    return {
        'csv_bytes': csv_bytes,
        'suggested_filename': suggested_filename,
        'row_count': row_count,
        'min_date': as_of,
        'max_date': as_of,
        'skipped': skipped,
    }


def _detect_balance_sheet_date(rows):
    """Scan the top rows for an 'As of MM/DD/YYYY' or similar date."""
    pat = re.compile(
        r'as\s+of\s+(\w+\s+\d{1,2},?\s+\d{4}|\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2})',
        re.IGNORECASE,
    )
    for row in rows[:10]:
        for cell in row:
            if not cell:
                continue
            m = pat.search(str(cell))
            if m:
                return _parse_flexible_date(m.group(1))
    return None


def _parse_flexible_date(s):
    s = (s or '').strip().rstrip(',').strip()
    for fmt in ('%B %d, %Y', '%B %d %Y', '%b %d, %Y', '%b %d %Y',
                '%m/%d/%Y', '%m/%d/%y', '%Y-%m-%d'):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _extract_balance_sheet_rows(rows):
    """Find each balance-sheet account row and its closing balance.

    QBO Balance Sheet export typically has:
      col A — account name (sometimes with embedded account #), indented
              by nesting level; or "Total Assets" / "Total Current
              Liabilities" / section headers
      col N (last numeric column) — the balance as of the report date

    We look for rows whose col A contains a recognizable 5-digit account
    code (anywhere in the string — handles "10101 Cash on Hand" and
    "Cash on Hand (10101)") and take the right-most numeric cell as the
    balance.
    """
    out = []
    skipped = []
    for i, row in enumerate(rows):
        if _is_blank(row):
            continue

        first_cell = str((row[0] if row else '') or '').strip()
        first_lower = first_cell.lower()
        if any(first_lower.startswith(p) for p in _BALANCE_SHEET_SKIP_PREFIXES):
            continue

        acct = _find_account_in_text(first_cell)
        if not acct:
            # Maybe the account # is elsewhere in the row
            for cell in row[1:]:
                if cell:
                    acct = _find_account_in_text(str(cell))
                    if acct:
                        break
        if not acct:
            # Not an account row — section header we didn't match, etc.
            continue

        # Right-most numeric cell is the balance.
        balance = None
        for cell in reversed(row):
            if cell in (None, ''):
                continue
            s = str(cell).strip()
            if not s:
                continue
            # Skip cells that are obviously text (contain letters)
            if re.search(r'[A-Za-z]', s.replace('e', '').replace('E', '')):
                continue
            try:
                neg = s.startswith('(') and s.endswith(')')
                clean = s.strip('()').replace('$', '').replace(',', '').strip()
                if not clean or clean == '-':
                    balance = Decimal('0')
                    break
                balance = Decimal(clean)
                if neg:
                    balance = -balance
                break
            except Exception:  # noqa: BLE001
                continue
        if balance is None:
            skipped.append({
                'row': i + 1,
                'reason': f"found account {acct} but no numeric balance",
            })
            continue
        out.append({'account': acct, 'amount': balance})
    return out, skipped


def _find_account_in_text(s):
    """Search a string for a 5-digit account code + optional 1-2 char sub."""
    m = re.search(r'\b(\d{4,5})([A-Za-z0-9]{0,2})\b', s or '')
    if m:
        return f"{m.group(1)}{m.group(2).upper()}"
    return None


# ==================================================================
# Odoo wizard
# ==================================================================
class QboActualsTransformWizard(models.TransientModel):
    _name = 'qbo.actuals.transform.wizard'
    _description = 'QuickBooks Online → FRS CSV Transform'

    report_type = fields.Selection([
        ('actuals', 'Actuals (Transaction Detail by Account)'),
        ('balance_sheet', 'Balance Sheet (as-of snapshot)'),
    ], default='actuals', required=True, string='QBO Report Type',
       help="Which QBO report you're pulling.\n\n"
            "Actuals: Transaction Detail by Account. Can be any date "
            "range — a single month, YTD, or a full fiscal year.\n\n"
            "Balance Sheet: an as-of snapshot. One row per balance-sheet "
            "account showing its closing balance. Used for the FRS "
            "one-time Beginning Balance file and for year-end reports.",
    )

    source = fields.Selection([
        ('qbo_live', 'Pull live from QuickBooks Online'),
        ('upload',   'Upload Excel / CSV export from QBO'),
    ], string='Data Source', default='qbo_live', required=True,
       help="Pull live: Odoo calls QuickBooks' Reports API with your "
            "OAuth connection, gets the same data the Reports UI would "
            "show, and skips the Excel-export-and-upload step.\n\n"
            "Upload file: for when you want to tweak the export in "
            "Excel first or you're offline / disconnected.")

    connection_id = fields.Many2one(
        'elks.qbo.connection', string='QuickBooks Connection',
        default=lambda self: self.env['elks.qbo.connection'].search(
            [('connected', '=', True), ('active', '=', True)], limit=1),
        help="The QBO connection to pull from. Defaults to your active "
             "one if there's only a single connection.",
    )
    connection_environment = fields.Selection(
        related='connection_id.environment', readonly=True,
    )

    # Period controls (used for QBO-live Actuals pulls)
    period_preset = fields.Selection([
        ('fytd',         'Fiscal YTD (Apr 1 → last complete month)'),
        ('fytd_today',   'Fiscal YTD (Apr 1 → today)'),
        ('last_month',   'Prior Full Month'),
        ('this_month',   'This Month to Date'),
        ('current_fy',   'Current Fiscal Year (full)'),
        ('prior_fy',     'Prior Fiscal Year (full)'),
        ('custom',       'Custom Date Range'),
    ], default='fytd', string='Period',
       help="FRS Adaptive overwrites prior data on resubmission, so "
            "submitting Fiscal YTD each time is the safest mode — every "
            "backdated correction stays in sync.")
    period_start = fields.Date('Period Start')
    period_end = fields.Date('Period End')

    source_file = fields.Binary(
        'QBO Export (Excel or CSV)', attachment=False,
        help="The QBO report exported as .xlsx or .csv.",
    )
    source_filename = fields.Char('Source Filename')

    lodge_number = fields.Char(
        'Lodge Number', required=True,
        default=lambda self: self._default_lodge_number(),
        help="4-digit lodge number for column 1 of the FRS CSV. "
             "Pre-filled from Lodge Settings.",
    )

    # Actuals-only
    aggregation = fields.Selection([
        ('transaction', 'One row per transaction'),
        ('monthly',     'Monthly totals (1 row per account × month-end)'),
        ('annual',      'Annual totals (1 row per account × FYE 3/31)'),
    ], default='transaction', string='Aggregation',
       help="How to summarize the Actuals.\n\n"
            "Transaction: emit every QBO transaction line individually "
            "with its original date. Produces the largest file but "
            "preserves the audit trail.\n\n"
            "Monthly: roll up to one row per (account, month), dated at "
            "month-end. Smaller file; standard monthly FRS submission.\n\n"
            "Annual: roll up to one row per (account, fiscal year), "
            "dated at the Elks FYE of 3/31. Useful for year-end review "
            "and sanity-checking the sum of twelve monthly submissions.",
    )

    # Balance-sheet-only
    balance_sheet_as_of = fields.Date(
        'Balance Sheet As-Of Date',
        help="Override the as-of date for the Balance Sheet export. "
             "Leave blank to auto-detect from the report title; falls "
             "back to today if no 'As of ...' header is found.",
    )

    encoding = fields.Selection([
        ('utf-8-sig', 'UTF-8 (BOM)'),
        ('utf-8',     'UTF-8'),
        ('cp1252',    'Windows-1252'),
        ('latin-1',   'Latin-1'),
    ], default='utf-8-sig', string='CSV Encoding',
       help="Only used for .csv inputs; ignored for .xlsx.")

    # Results
    state = fields.Selection([
        ('draft', 'Ready'),
        ('done',  'Done'),
    ], default='draft')
    result_file = fields.Binary('FRS Actuals CSV', readonly=True, attachment=False)
    result_filename = fields.Char('Suggested Filename', readonly=True)
    result_row_count = fields.Integer('Rows (excl. header)', readonly=True)
    result_min_date = fields.Date('First Transaction Date', readonly=True)
    result_max_date = fields.Date('Last Transaction Date', readonly=True)
    report_html = fields.Html('Report', readonly=True, sanitize=False)

    @api.model
    def _default_lodge_number(self):
        settings = self.env['elks.lodge.settings'].sudo().search([], limit=1)
        return (settings.lodge_number or '').strip() if settings else ''

    # ==================================================================
    # Period preset resolution (mirrors frs_export_wizard)
    # ==================================================================
    @staticmethod
    def _fy_bounds(d):
        from datetime import date
        if d.month >= 4:
            return (date(d.year, 4, 1), date(d.year + 1, 3, 31))
        return (date(d.year - 1, 4, 1), date(d.year, 3, 31))

    @staticmethod
    def _last_complete_month_end(d):
        from datetime import timedelta
        return d.replace(day=1) - timedelta(days=1)

    def _apply_preset(self):
        from datetime import date
        today = fields.Date.context_today(self)
        fy_start, fy_end = self._fy_bounds(today)
        p = self.period_preset or 'fytd'
        if p == 'fytd':
            end = self._last_complete_month_end(today)
            self.period_start = fy_start
            self.period_end = end if end >= fy_start else today
        elif p == 'fytd_today':
            self.period_start = fy_start
            self.period_end = today
        elif p == 'this_month':
            self.period_start = today.replace(day=1)
            self.period_end = today
        elif p == 'last_month':
            end = self._last_complete_month_end(today)
            self.period_start = end.replace(day=1)
            self.period_end = end
        elif p == 'current_fy':
            self.period_start = fy_start
            self.period_end = fy_end
        elif p == 'prior_fy':
            self.period_start = date(fy_start.year - 1, 4, 1)
            self.period_end = date(fy_start.year, 3, 31)

    @api.onchange('period_preset', 'source', 'report_type')
    def _onchange_period(self):
        if self.source == 'qbo_live' and self.report_type == 'actuals' \
                and self.period_preset != 'custom':
            self._apply_preset()

    @api.model
    def default_get(self, fields_list):
        vals = super().default_get(fields_list)
        if (vals.get('source', 'qbo_live') == 'qbo_live'
                and vals.get('report_type', 'actuals') == 'actuals'
                and vals.get('period_preset', 'fytd') != 'custom'):
            tmp = self.new(vals)
            tmp._apply_preset()
            vals['period_start'] = tmp.period_start
            vals['period_end'] = tmp.period_end
        return vals

    # ==================================================================
    # Main action
    # ==================================================================
    def action_transform(self):
        self.ensure_one()
        if self.source == 'qbo_live':
            result = self._transform_from_qbo_live()
        else:
            result = self._transform_from_upload()
        # Rename to the FRS-canonical convention (matches the PDF
        # section titles so submissions are instantly recognizable).
        result['suggested_filename'] = self._frs_canonical_filename(result)

        self.write({
            'state': 'done',
            'result_file': base64.b64encode(result['csv_bytes']),
            'result_filename': result['suggested_filename'],
            'result_row_count': result['row_count'],
            'result_min_date': result['min_date'],
            'result_max_date': result['max_date'],
            'report_html': self._build_report_html(result),
        })

    # ------------------------------------------------------------------
    # Source branch: Pull live from QBO Reports API
    # ------------------------------------------------------------------
    def _transform_from_qbo_live(self):
        if not self.connection_id:
            raise UserError(_(
                "Pick a QuickBooks connection — or go to QuickBooks "
                "Online → Connections and Mapping and connect one first."))
        if (self.connection_id.environment or '').lower() == 'sandbox':
            raise UserError(_(
                "SANDBOX MODE — this connection is pointing at Intuit's "
                "sandbox test data, not your real books. Switch the "
                "connection's Environment to Production and reconnect "
                "before pulling an FRS submission."))
        if self.report_type == 'balance_sheet':
            return self._pull_balance_sheet()
        return self._pull_actuals()

    def _pull_actuals(self):
        from ..models.qbo import reports as _qbo_reports
        if not self.period_start or not self.period_end:
            raise UserError(_('Pick a period before pulling.'))
        if self.period_start > self.period_end:
            raise UserError(_('Period Start cannot be after Period End.'))
        json_data = self.connection_id._report('TransactionList', {
            'start_date': self.period_start.strftime('%Y-%m-%d'),
            'end_date':   self.period_end.strftime('%Y-%m-%d'),
            'columns':    'account_num,txn_date,debt_amt,credit_amt',
            'sort_by':    'account_num',
        })
        try:
            txns, skipped = _qbo_reports.parse_transaction_list(json_data)
        except _qbo_reports.ReportParseError as exc:
            raise UserError(str(exc))
        if not txns:
            raise UserError(_(
                "QuickBooks returned no transactions for %(start)s → "
                "%(end)s. Verify the period and that posted transactions "
                "exist in QBO for that range."
            ) % {'start': self.period_start, 'end': self.period_end})
        return self._build_actuals_csv(txns, skipped)

    def _pull_balance_sheet(self):
        from ..models.qbo import reports as _qbo_reports
        as_of = self.balance_sheet_as_of or fields.Date.context_today(self)
        json_data = self.connection_id._report('BalanceSheet', {
            'date_macro':        'Custom',
            'end_date':          as_of.strftime('%Y-%m-%d'),
            'accounting_method': 'Accrual',
            'summarize_column_by': 'Total',
        })
        try:
            detected_as_of, accounts, skipped = _qbo_reports.parse_balance_sheet(json_data)
        except _qbo_reports.ReportParseError as exc:
            raise UserError(str(exc))
        if not accounts:
            raise UserError(_(
                "QuickBooks returned no balance-sheet accounts as of %s. "
                "Make sure 'Show account numbers' is enabled in QBO's "
                "Account and Settings → Advanced.") % as_of)
        return self._build_balance_sheet_csv(
            accounts, skipped, detected_as_of or as_of)

    def _build_actuals_csv(self, txns, skipped):
        """Serialize already-parsed txn rows via the common path."""
        if self.aggregation == 'monthly':
            txns = _aggregate_monthly(txns)
        elif self.aggregation == 'annual':
            txns = _aggregate_annual(txns)
        txns.sort(key=lambda t: (t['account'], t['date']))
        min_d = min(t['date'] for t in txns)
        max_d = max(t['date'] for t in txns)
        output = io.StringIO()
        writer = csv.writer(output, lineterminator='\n')
        writer.writerow(['LodgeNumber', 'LodgeGLAccount', 'Date', 'Amount'])
        for t in txns:
            amt = t['amount'].quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            if amt == 0:
                continue
            writer.writerow([self.lodge_number, t['account'],
                             t['date'].strftime('%m/%d/%Y'), f"{amt:.2f}"])
        csv_bytes = output.getvalue().encode('utf-8')
        return {
            'csv_bytes': csv_bytes,
            'suggested_filename': '',  # replaced by _frs_canonical_filename
            'row_count': csv_bytes.count(b'\n') - 1,
            'min_date': min_d, 'max_date': max_d,
            'skipped': skipped,
        }

    def _build_balance_sheet_csv(self, accounts, skipped, as_of):
        from decimal import Decimal, ROUND_HALF_UP
        accounts.sort(key=lambda a: a['account'])
        output = io.StringIO()
        writer = csv.writer(output, lineterminator='\n')
        writer.writerow(['LodgeNumber', 'LodgeGLAccount', 'Date', 'Amount'])
        for a in accounts:
            amt = a['amount'].quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            if amt == 0:
                continue
            writer.writerow([self.lodge_number, a['account'],
                             as_of.strftime('%m/%d/%Y'), f"{amt:.2f}"])
        csv_bytes = output.getvalue().encode('utf-8')
        return {
            'csv_bytes': csv_bytes,
            'suggested_filename': '',
            'row_count': csv_bytes.count(b'\n') - 1,
            'min_date': as_of, 'max_date': as_of,
            'skipped': skipped,
        }

    # ------------------------------------------------------------------
    # Source branch: Upload (preserves original behavior)
    # ------------------------------------------------------------------
    def _transform_from_upload(self):
        if not self.source_file:
            raise UserError(_('Upload a QBO export file.'))
        raw = base64.b64decode(self.source_file)
        fn = self.source_filename or 'qbo_export.xlsx'
        try:
            if self.report_type == 'balance_sheet':
                return transform_qbo_to_frs_balance_sheet(
                    raw_bytes=raw, filename=fn,
                    lodge_number=self.lodge_number,
                    as_of_date=self.balance_sheet_as_of,
                    encoding=self.encoding)
            return transform_qbo_to_frs_actuals(
                raw_bytes=raw, filename=fn,
                lodge_number=self.lodge_number,
                aggregation=self.aggregation,
                encoding=self.encoding)
        except ValueError as exc:
            raise UserError(str(exc))

    # ------------------------------------------------------------------
    # Canonical FRS filename convention (matches PDF section titles)
    # ------------------------------------------------------------------
    def _frs_canonical_filename(self, result):
        """Match the FRS PDF section titles exactly.

        Per the 2019 FRS Introduction + the 2020 QBO Guide PDFs:
          * TransactionList submissions  → "Actual Monthly Activity File"
          * BalanceSheet one-time upload → "Actual Beginning Balance File"
          * Annual budget                → "Annual Budget File"
          * CoA translation              → "GL Account Mapping File"

        Filename shape:
            Lodge <num> - <PDF title> - <period or date>.csv
        """
        lodge = self.lodge_number
        if self.report_type == 'balance_sheet':
            tag = result['min_date'].strftime('%Y-%m-%d')
            return f"Lodge {lodge} - Actual Beginning Balance - {tag}.csv"
        # Actuals
        return (f"Lodge {lodge} - Actual Monthly Activity - "
                f"{self._period_tag(result)}.csv")

    def _period_tag(self, result):
        """Build the period portion of the FRS filename.

        Prefer the user's declared preset period over the pulled data's
        min/max dates — keeps the filename honest when the data range
        didn't quite line up with what the user asked for (e.g. picking
        Prior Fiscal Year but QBO only has mid-year data).
        """
        # Use declared preset period if available (QBO-live path),
        # otherwise fall back to the data's range (upload path, where
        # there's no preset).
        if self.source == 'qbo_live' and self.period_start and self.period_end:
            start, end = self.period_start, self.period_end
        else:
            start, end = result['min_date'], result['max_date']
        preset = self.period_preset
        fy_start, fy_end = self._fy_bounds(start)

        if preset in ('current_fy', 'prior_fy'):
            return f"FYE {fy_end.year}"
        if preset in ('fytd', 'fytd_today'):
            return f"YTD through {end.strftime('%b %Y')}"
        if preset == 'this_month':
            return f"{start.strftime('%b %Y')} (partial)"
        if preset == 'last_month' or (
                start.year == end.year and start.month == end.month):
            return start.strftime('%b %Y')
        # Custom / multi-month
        return f"{start.strftime('%b %Y')} to {end.strftime('%b %Y')}"
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_download(self):
        """Serve the generated CSV via the binary download URL."""
        self.ensure_one()
        if not self.result_file:
            raise UserError(_("Nothing to download — run Transform first."))
        return {
            'type': 'ir.actions.act_url',
            'url': (
                f"/web/content/{self._name}/{self.id}/result_file"
                f"?download=true&filename={self.result_filename}"
            ),
            'target': 'self',
        }

    def _build_report_html(self, result):
        skipped = result.get('skipped') or []
        html = [
            "<div style='margin-bottom:.5em;'>",
            f"<strong>Rows emitted:</strong> {result['row_count']}<br/>",
            f"<strong>Date range:</strong> "
            f"{result['min_date']} → {result['max_date']}<br/>",
            f"<strong>Suggested filename:</strong> "
            f"<code>{result['suggested_filename']}</code>",
            "</div>",
        ]
        if skipped:
            html.append(f"<details><summary>Skipped rows ({len(skipped)})</summary>")
            html.append("<table class='table table-sm' style='font-size:.85em;'>")
            html.append("<thead><tr><th>Source row</th><th>Reason</th></tr></thead><tbody>")
            for s in skipped[:200]:
                reason = (s['reason'] or '').replace('<', '&lt;').replace('>', '&gt;')
                html.append(f"<tr><td>{s['row']}</td><td>{reason}</td></tr>")
            if len(skipped) > 200:
                html.append(
                    f"<tr><td colspan='2'><em>... {len(skipped) - 200} more</em></td></tr>"
                )
            html.append("</tbody></table></details>")
        else:
            html.append("<div class='text-success'>No rows skipped — clean parse.</div>")
        return "\n".join(html)
