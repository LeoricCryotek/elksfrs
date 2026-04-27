# -*- coding: utf-8 -*-
"""QuickBooks P&L Reconciliation Wizard.

Imports a QuickBooks Profit & Loss report (CSV/Excel or PDF), compares
each account's amount against the FRS budget actuals, and auto-creates
draft adjustment journal entries for any discrepancies.

Workflow:
1. Upload QB P&L export file
2. Wizard parses the file and matches QB accounts to FRS budget lines
3. For each discrepancy, a draft journal entry is created
   (memo: "Balance Adjustment from QB Import")
4. A reconciliation summary is generated showing all adjustments
"""
import base64
import csv
import datetime
import io
import logging

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class QbPnlReconcileWizard(models.TransientModel):
    _name = "qb.pnl.reconcile.wizard"
    _description = "QuickBooks P&L Reconciliation Import"

    budget_id = fields.Many2one(
        "elks.budget", string="Budget", required=True,
        default=lambda self: self._default_budget(),
    )
    file_data = fields.Binary("QB P&L File", required=True)
    file_name = fields.Char("Filename")
    file_format = fields.Selection([
        ('xlsx', 'Excel (.xlsx)'),
        ('csv', 'CSV Export'),
        ('pdf', 'PDF Export'),
    ], string="File Format", default='xlsx', required=True)
    date_override = fields.Date(
        "Adjustment Date",
        default=fields.Date.context_today,
        help="Date for the adjustment journal entries.",
    )
    create_missing_accounts = fields.Boolean(
        "Create Missing Accounts", default=True,
    )

    # Adjustment account for balancing entries
    adjustment_account_id = fields.Many2one(
        "elks.account", string="Adjustment Offset Account",
        help="Account to use as the offsetting entry for adjustments. "
             "Typically an equity or suspense account.",
        domain="[('is_header', '=', False)]",
    )

    # Results
    state = fields.Selection([
        ('setup', 'Setup'),
        ('preview', 'Preview'),
        ('done', 'Done'),
    ], default='setup')
    result_message = fields.Text("Import Results", readonly=True)
    reconciliation_html = fields.Html(
        "Reconciliation Report", readonly=True, sanitize=False,
    )
    adjustment_count = fields.Integer("Adjustments Created", readonly=True)
    adjustment_entry_ids = fields.Many2many(
        "elks.journal.entry", string="Adjustment Entries",
    )

    @api.model
    def _default_budget(self):
        """Find the current fiscal year budget."""
        today = fields.Date.context_today(self)
        if today.month <= 3:
            fye = today.replace(month=3, day=31)
        else:
            fye = today.replace(year=today.year + 1, month=3, day=31)
        budget = self.env['elks.budget'].search(
            [('fiscal_year_end', '=', fye)], limit=1,
        )
        if not budget:
            budget = self.env['elks.budget'].search(
                [], order='fiscal_year_end desc', limit=1,
            )
        return budget

    def action_import_and_reconcile(self):
        """Parse the QB P&L file and generate reconciliation."""
        self.ensure_one()
        if not self.file_data:
            raise UserError(_("Please upload a QuickBooks P&L file."))
        if not self.budget_id:
            raise UserError(_("Please select a budget."))

        raw = base64.b64decode(self.file_data)

        if self.file_format == 'pdf':
            qb_data = self._parse_pdf_pnl(raw)
        elif self.file_format == 'xlsx':
            qb_data = self._parse_xlsx_pnl(raw)
        else:
            # CSV
            try:
                content = raw.decode('utf-8-sig')
            except UnicodeDecodeError:
                content = raw.decode('latin-1')
            qb_data = self._parse_csv_pnl(content)

        if not qb_data:
            raise UserError(_(
                "No account data could be extracted from the file. "
                "Please check the file format."
            ))

        # Compare and create adjustments
        result = self._reconcile_and_adjust(qb_data)

        self.write({
            'state': 'done',
            'result_message': result['summary'],
            'reconciliation_html': result['html'],
            'adjustment_count': result['count'],
            'adjustment_entry_ids': [(6, 0, result['entry_ids'])],
        })
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

    def _parse_xlsx_pnl(self, raw_data):
        """Parse a QuickBooks P&L Excel (.xlsx) export.

        QB Excel P&L has a specific structure:
        - Account lines like "30010 · Members' Dues Regular" in columns D-G
        - Amounts in the last populated column (typically H)
        - "Total" rows are subtotals and should be skipped
        - Hierarchy indicated by column indentation
        """
        try:
            import openpyxl
        except ImportError:
            raise UserError(_(
                "openpyxl is required for Excel imports. "
                "Please install: pip install openpyxl"
            ))

        wb = openpyxl.load_workbook(
            io.BytesIO(raw_data), data_only=True, read_only=True,
        )
        ws = wb.active
        results = []

        for row in ws.iter_rows(values_only=False):
            # Find the text cell (account description) and amount cell
            text_val = None
            amount_val = None

            for cell in row:
                v = cell.value
                if v is None:
                    continue
                if isinstance(v, str) and v.strip():
                    text_val = v.strip()
                elif isinstance(v, (int, float)):
                    amount_val = v

            if not text_val or amount_val is None:
                continue

            # Skip header, total, and summary rows
            lower = text_val.lower()
            if any(w in lower for w in (
                'total ', 'ordinary income', 'gross profit',
                'net income', 'net loss', 'cost of goods sold',
                'income/expense', 'income', 'expense',
            )):
                # But allow rows that START with a digit (account code)
                if not (text_val and text_val[0].isdigit()):
                    continue

            # Extract account code
            code, name = self._extract_account_code(text_val)
            if not code:
                continue

            amount = abs(float(amount_val))
            if amount < 0.005:
                continue

            results.append({
                'code': code,
                'name': name,
                'amount': amount,
            })

        wb.close()
        return results

    def _parse_csv_pnl(self, content):
        """Parse a QuickBooks P&L CSV export.

        QB P&L exports have various formats. Common patterns:
        - Standard: columns like Account, Total/Amount
        - Detail: columns with individual transactions
        - Summary: just account names and totals

        We look for account codes and their totals.
        Returns: list of dicts with 'code', 'name', 'amount'
        """
        # Try to parse as a standard CSV with headers
        lines = content.strip().split('\n')
        if not lines:
            return []

        # QuickBooks P&L CSV often has a header section before the data
        # Try to find where the actual data starts
        reader = csv.reader(io.StringIO(content))
        all_rows = list(reader)

        if not all_rows:
            return []

        # Strategy 1: Look for a row with "Account" or similar header
        header_idx = None
        for i, row in enumerate(all_rows):
            row_lower = [c.lower().strip() for c in row if c.strip()]
            if any('account' in c for c in row_lower):
                header_idx = i
                break

        results = []

        if header_idx is not None:
            headers = all_rows[header_idx]
            fn_lower = [h.lower().strip() for h in headers]

            # Find account and amount columns
            acct_col = None
            amount_col = None
            for i, h in enumerate(fn_lower):
                if 'account' in h and 'name' not in h:
                    acct_col = i
                elif h in ('total', 'amount', 'balance', 'net'):
                    amount_col = i
                elif 'total' in h or 'amount' in h or 'balance' in h:
                    if amount_col is None:
                        amount_col = i

            if acct_col is None:
                # Try first column
                acct_col = 0
            if amount_col is None:
                # Try last column
                amount_col = len(headers) - 1

            for row in all_rows[header_idx + 1:]:
                if len(row) <= max(acct_col, amount_col):
                    continue
                raw_acct = row[acct_col].strip()
                raw_amount = row[amount_col].strip() if amount_col < len(row) else ''

                if not raw_acct or not raw_amount:
                    continue

                # Skip total/header rows
                lower = raw_acct.lower()
                if any(w in lower for w in (
                    'total', 'net income', 'net loss', 'profit',
                    'gross profit', 'net ordinary',
                )):
                    continue

                code, name = self._extract_account_code(raw_acct)
                if not code:
                    continue

                amount = self._parse_amount(raw_amount)
                if abs(amount) < 0.005:
                    continue

                results.append({
                    'code': code,
                    'name': name,
                    'amount': abs(amount),  # P&L shows positive amounts
                })
        else:
            # Strategy 2: No clear header — try parsing each row
            for row in all_rows:
                if not row:
                    continue
                first_cell = row[0].strip()
                code, name = self._extract_account_code(first_cell)
                if not code:
                    continue
                # Find the last numeric value in the row
                amount = 0.0
                for cell in reversed(row[1:]):
                    val = self._parse_amount(cell)
                    if abs(val) > 0.005:
                        amount = abs(val)
                        break
                if amount > 0:
                    results.append({
                        'code': code,
                        'name': name,
                        'amount': amount,
                    })

        return results

    def _parse_pdf_pnl(self, raw_data):
        """Extract account data from a QuickBooks P&L PDF.

        Uses pdfplumber or PyPDF2 to extract text, then parses
        account codes and amounts from the text lines.
        """
        import re

        text = ''
        try:
            import pdfplumber
            pdf_stream = io.BytesIO(raw_data)
            with pdfplumber.open(pdf_stream) as pdf:
                for page in pdf.pages:
                    text += page.extract_text() or ''
                    text += '\n'
        except ImportError:
            try:
                from PyPDF2 import PdfReader
                pdf_stream = io.BytesIO(raw_data)
                reader = PdfReader(pdf_stream)
                for page in reader.pages:
                    text += page.extract_text() or ''
                    text += '\n'
            except ImportError:
                raise UserError(_(
                    "PDF parsing requires pdfplumber or PyPDF2. "
                    "Please install: pip install pdfplumber\n"
                    "Or use the CSV export format instead."
                ))

        if not text.strip():
            raise UserError(_("Could not extract text from the PDF."))

        # Parse text lines looking for account code patterns
        results = []
        # Pattern: account code (5+ digits, possibly with letter suffix),
        # followed by text, followed by dollar amount
        pattern = re.compile(
            r'(\d{5,7}[A-Za-z]?)\s+'  # account code
            r'(.+?)\s+'                 # account name
            r'[\$]?\s*([\d,]+\.?\d*)\s*$'  # amount at end of line
        )

        for line in text.split('\n'):
            line = line.strip()
            if not line:
                continue

            # Skip total/header lines
            lower = line.lower()
            if any(w in lower for w in (
                'total', 'net income', 'net loss', 'profit & loss',
                'profit and loss', 'gross profit', 'page',
            )):
                continue

            m = pattern.search(line)
            if m:
                code = m.group(1)
                name = m.group(2).strip()
                amount = self._parse_amount(m.group(3))
                if abs(amount) > 0.005:
                    results.append({
                        'code': code,
                        'name': name,
                        'amount': abs(amount),
                    })

        return results

    def _reconcile_and_adjust(self, qb_data):
        """Compare QB data to FRS actuals and create adjustment entries.

        For each account:
        - If QB amount > FRS actual: create adjustment for the difference
        - If QB amount < FRS actual: create adjustment for the difference
        - If amounts match: no adjustment needed

        Returns dict with 'summary', 'html', 'count', 'entry_ids'.
        """
        budget = self.budget_id
        JournalEntry = self.env['elks.journal.entry']
        adj_date = self.date_override or fields.Date.context_today(self)

        # Build lookup of current FRS actuals from budget lines
        frs_by_code = {}
        for line in budget.line_ids:
            code = line.account_code or ''
            if line.account_id.subaccount:
                code = f"{code}{line.account_id.subaccount}"
            frs_by_code[code] = {
                'account_id': line.account_id,
                'budget_line': line,
                'actual': line.actual_amount or 0.0,
                'budgeted': line.amount or 0.0,
                'type': line.account_type,
            }

        # Process each QB line
        matches = []
        unmatched = []
        adjustments = []
        entry_ids = []

        for qb in qb_data:
            code = qb['code']
            qb_amount = qb['amount']

            # Try to find matching FRS line
            frs = frs_by_code.get(code)
            if not frs:
                # Try without subaccount suffix
                base = code[:5] if len(code) > 5 else code
                frs = frs_by_code.get(base)

            if not frs:
                # Try to find account directly
                acct = self._find_account(code)
                if acct:
                    frs = {
                        'account_id': acct,
                        'budget_line': None,
                        'actual': 0.0,
                        'budgeted': 0.0,
                        'type': acct.account_type,
                    }
                else:
                    unmatched.append(qb)
                    continue

            frs_actual = frs['actual']
            diff = qb_amount - frs_actual

            match = {
                'code': code,
                'name': qb['name'],
                'qb_amount': qb_amount,
                'frs_actual': frs_actual,
                'frs_budgeted': frs['budgeted'],
                'difference': diff,
                'account_id': frs['account_id'],
                'type': frs['type'],
            }
            matches.append(match)

            # Create adjustment if there's a discrepancy
            if abs(diff) >= 0.01:
                adjustments.append(match)

                # Build the journal entry
                acct = frs['account_id']
                lines = []

                if frs['type'] == 'income':
                    # Income: QB higher means we need to credit more
                    if diff > 0:
                        lines.append((0, 0, {
                            'account_id': acct.id,
                            'debit': 0.0,
                            'credit': abs(diff),
                            'memo': f"QB Adj: {acct.display_name}",
                        }))
                    else:
                        lines.append((0, 0, {
                            'account_id': acct.id,
                            'debit': abs(diff),
                            'credit': 0.0,
                            'memo': f"QB Adj: {acct.display_name}",
                        }))
                else:
                    # Expense/COGS: QB higher means we need to debit more
                    if diff > 0:
                        lines.append((0, 0, {
                            'account_id': acct.id,
                            'debit': abs(diff),
                            'credit': 0.0,
                            'memo': f"QB Adj: {acct.display_name}",
                        }))
                    else:
                        lines.append((0, 0, {
                            'account_id': acct.id,
                            'debit': 0.0,
                            'credit': abs(diff),
                            'memo': f"QB Adj: {acct.display_name}",
                        }))

                # Offsetting entry
                if self.adjustment_account_id:
                    offset_debit = lines[0][2]['credit']
                    offset_credit = lines[0][2]['debit']
                    lines.append((0, 0, {
                        'account_id': self.adjustment_account_id.id,
                        'debit': offset_debit,
                        'credit': offset_credit,
                        'memo': f"QB Adj offset: {acct.display_name}",
                    }))

                entry = JournalEntry.create({
                    'date': adj_date,
                    'memo': f"Balance Adjustment from QB Import — "
                            f"{acct.display_name} "
                            f"(QB: ${qb_amount:,.2f} vs FRS: ${frs_actual:,.2f})",
                    'line_ids': lines,
                })
                entry_ids.append(entry.id)

        # Build reconciliation report HTML
        html = self._build_reconciliation_html(matches, unmatched, adjustments)
        summary = (
            f"Reconciliation complete:\n"
            f"  {len(matches)} accounts matched\n"
            f"  {len(adjustments)} adjustments created (as draft journal entries)\n"
            f"  {len(unmatched)} QB accounts not matched to FRS\n\n"
            f"All adjustment entries are in DRAFT status — review and approve "
            f"each one to apply the changes to your budget actuals."
        )

        return {
            'summary': summary,
            'html': html,
            'count': len(adjustments),
            'entry_ids': entry_ids,
        }

    def _build_reconciliation_html(self, matches, unmatched, adjustments):
        """Build a formatted HTML reconciliation report."""
        css = '''
        <style>
            .recon-table { width:100%; border-collapse:collapse; font-size:13px; }
            .recon-table th { background:#2E4057; color:#fff; padding:8px 10px;
                              text-align:right; }
            .recon-table th:first-child, .recon-table th:nth-child(2) { text-align:left; }
            .recon-table td { padding:5px 10px; border-bottom:1px solid #eee; }
            .recon-table td:nth-child(n+3) { text-align:right; font-family:monospace; }
            .recon-match { background:#E8F5E9; }
            .recon-adj { background:#FFF3E0; }
            .recon-neg { color:#dc3545; font-weight:600; }
            .recon-pos { color:#28a745; font-weight:600; }
            .recon-zero { color:#888; }
            .recon-summary { background:#f5f7fa; padding:15px; border-radius:8px;
                             margin-bottom:15px; }
        </style>
        '''

        def fmt(val):
            if abs(val) < 0.005:
                return '<span class="recon-zero">-</span>'
            if val < 0:
                return f'<span class="recon-neg">({abs(val):,.2f})</span>'
            return f'{val:,.2f}'

        def fmt_diff(val):
            if abs(val) < 0.005:
                return '<span class="recon-zero">-</span>'
            cls = 'recon-neg' if val != 0 else 'recon-zero'
            if val > 0:
                return f'<span class="{cls}">+{val:,.2f}</span>'
            return f'<span class="{cls}">{val:,.2f}</span>'

        # Summary box
        adj_total = sum(abs(a['difference']) for a in adjustments)
        summary = (
            f'<div class="recon-summary">'
            f'<h4>Reconciliation Summary</h4>'
            f'<p><strong>{len(matches)}</strong> accounts compared | '
            f'<strong>{len(adjustments)}</strong> adjustments needed | '
            f'<strong>{len(matches) - len(adjustments)}</strong> matched exactly</p>'
            f'<p>Total adjustment amount: <strong>${adj_total:,.2f}</strong></p>'
            f'</div>'
        )

        # Main table
        rows = []
        for m in sorted(matches, key=lambda x: x['code']):
            cls = 'recon-match' if abs(m['difference']) < 0.01 else 'recon-adj'
            status = ('&#10004;' if abs(m['difference']) < 0.01
                      else f'Adj: {fmt_diff(m["difference"])}')
            rows.append(
                f'<tr class="{cls}">'
                f'<td>{m["code"]}</td>'
                f'<td>{m["name"]}</td>'
                f'<td>{fmt(m["frs_budgeted"])}</td>'
                f'<td>{fmt(m["qb_amount"])}</td>'
                f'<td>{fmt(m["frs_actual"])}</td>'
                f'<td>{fmt_diff(m["difference"])}</td>'
                f'<td>{status}</td></tr>'
            )

        # Unmatched section
        if unmatched:
            rows.append(
                '<tr><td colspan="7" style="background:#FDECEA;'
                'font-weight:600;padding:10px;">Unmatched QB Accounts '
                f'({len(unmatched)})</td></tr>'
            )
            for u in unmatched:
                rows.append(
                    f'<tr style="background:#FFF5F5;">'
                    f'<td>{u["code"]}</td>'
                    f'<td>{u["name"]}</td>'
                    f'<td>-</td>'
                    f'<td>{fmt(u["amount"])}</td>'
                    f'<td>-</td><td>-</td>'
                    f'<td>No FRS match</td></tr>'
                )

        html = (
            f'{css}{summary}'
            f'<table class="recon-table">'
            f'<thead><tr>'
            f'<th>Code</th><th>Account</th><th>Budget</th>'
            f'<th>QB Amount</th><th>FRS Actual</th>'
            f'<th>Difference</th><th>Status</th>'
            f'</tr></thead><tbody>'
            + ''.join(rows)
            + '</tbody></table>'
        )
        return html

    def action_view_adjustments(self):
        """Open the created adjustment journal entries."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('QB Adjustment Entries'),
            'res_model': 'elks.journal.entry',
            'view_mode': 'list,form',
            'domain': [('id', 'in', self.adjustment_entry_ids.ids)],
        }

    def _find_account(self, code):
        """Find an elks.account by code."""
        Account = self.env['elks.account']
        acct = Account.search([('code', '=', code)], limit=1)
        if not acct and len(code) > 5:
            base = code[:5]
            sub = code[5:]
            acct = Account.search([
                ('code', '=', base),
                ('subaccount', '=', sub),
            ], limit=1)
        return acct

    @staticmethod
    def _extract_account_code(raw_value):
        """Extract account code from QB formatted string."""
        if not raw_value:
            return None, ''
        raw_value = raw_value.strip()
        for sep in (' \u00b7 ', ' · ', ' - '):
            if sep in raw_value:
                parts = raw_value.split(sep, 1)
                code_part = parts[0].strip()
                name_part = parts[1].strip() if len(parts) > 1 else ''
                if code_part and code_part[0].isdigit():
                    return code_part, name_part
                return None, raw_value
        if raw_value[0].isdigit():
            parts = raw_value.split(None, 1)
            return parts[0], (parts[1] if len(parts) > 1 else '')
        return None, raw_value

    @staticmethod
    def _parse_amount(val):
        """Parse a monetary amount string."""
        if not val:
            return 0.0
        val = str(val).strip()
        val = val.replace('$', '').replace(',', '').replace(' ', '')
        if val.startswith('(') and val.endswith(')'):
            val = '-' + val[1:-1]
        try:
            return float(val)
        except ValueError:
            return 0.0
