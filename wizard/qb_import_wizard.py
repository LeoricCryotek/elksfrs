# -*- coding: utf-8 -*-
"""QuickBooks Desktop Import Wizard.

Imports QuickBooks Desktop CSV/IIF exports into Elks journal entries.
Supports:
- QuickBooks P&L Detail export (CSV)
- QuickBooks Trial Balance export (CSV)
- QuickBooks General Journal export (IIF)

The wizard maps QB account numbers to the Elks Uniform COA and creates
journal entries for each transaction.
"""
import base64
import csv
import io

from odoo import api, fields, models, _
from odoo.exceptions import UserError

import logging

_logger = logging.getLogger(__name__)


class QbImportWizard(models.TransientModel):
    _name = "qb.import.wizard"
    _description = "QuickBooks Import Wizard"

    import_type = fields.Selection([
        ('journal', 'Transaction Detail / General Journal (CSV)'),
        ('trial_balance', 'Trial Balance (CSV)'),
        ('pnl', 'Profit & Loss Detail (CSV)'),
    ], string="Import Type", default='journal', required=True,
        help="Transaction Detail: individual transactions with dates, "
             "accounts, and amounts — one journal entry per date.\n"
             "Trial Balance: snapshot of account balances — one journal "
             "entry with all accounts.\n"
             "P&L Detail: same as Transaction Detail.",
    )

    file_data = fields.Binary("File", required=True)
    file_name = fields.Char("Filename")

    date_override = fields.Date(
        "Date Override",
        help="If set, all imported entries will use this date instead of "
             "dates in the file. Useful for trial balance imports.",
    )
    auto_post = fields.Boolean(
        "Auto-Post Entries", default=False,
        help="Automatically post journal entries after import.",
    )
    create_missing_accounts = fields.Boolean(
        "Create Missing Accounts", default=True,
        help="Automatically create Elks accounts for QB account numbers "
             "not yet in the Chart of Accounts.",
    )

    # Results
    state = fields.Selection([
        ('setup', 'Setup'),
        ('done', 'Done'),
    ], default='setup')
    result_message = fields.Text("Import Results", readonly=True)

    def action_import(self):
        self.ensure_one()
        if not self.file_data:
            raise UserError(_("Please upload a file."))

        raw = base64.b64decode(self.file_data)
        # Try UTF-8, then latin-1
        try:
            content = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            content = raw.decode('latin-1')

        if self.import_type == 'journal':
            result = self._import_journal_csv(content)
        elif self.import_type == 'trial_balance':
            result = self._import_trial_balance(content)
        elif self.import_type == 'pnl':
            result = self._import_pnl_detail(content)
        else:
            raise UserError(_("Unsupported import type."))

        self.write({
            'state': 'done',
            'result_message': result,
        })
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

    def _find_or_create_account(self, code, name=None):
        """Find an Elks account by code, optionally creating it.

        Uses the same code-range-based type detection as the budget
        import wizard and corrects mistyped existing accounts.
        """
        if not code:
            return False
        code = str(code).strip()

        # Reuse the code-range detection from the budget wizard
        from odoo.addons.elksfrs.wizard.budget_import_wizard import BudgetImportWizard
        guess_type = BudgetImportWizard._guess_account_type

        Account = self.env["elks.account"]

        # Try exact match
        acct = Account.search([("code", "=", code)], limit=1)

        # Try subaccount split (e.g. "30010A" → code=30010, sub=A)
        if not acct and len(code) > 5:
            base = code[:5]
            sub = code[5:]
            acct = Account.search([
                ('code', '=', base),
                ('subaccount', '=', sub),
            ], limit=1)

        if acct:
            updates = {}
            # Fix account type if code range says it should be different
            expected = guess_type(acct.code)
            if expected and acct.account_type != expected:
                _logger.info(
                    "Correcting account %s type: %s → %s",
                    acct.code, acct.account_type, expected,
                )
                updates['account_type'] = expected
            # Auto-assign department if missing
            if not acct.department_id:
                dept = self._guess_department(acct.code)
                if dept:
                    updates['department_id'] = dept.id
            if updates:
                acct.write(updates)
            return acct

        if not self.create_missing_accounts:
            return False

        # Determine base code and subaccount
        base_code = code
        sub = ''
        if len(code) > 5:
            base_code = code[:5]
            sub = code[5:]

        acct_type = guess_type(base_code)

        vals = {
            'code': base_code,
            'name': name or f"Imported Account {code}",
            'account_type': acct_type,
        }
        if sub:
            vals['subaccount'] = sub
            # Link to parent if it exists
            parent = Account.search([
                ('code', '=', base_code),
                ('subaccount', '=', False),
            ], limit=1)
            if parent:
                vals['parent_id'] = parent.id
                # Inherit department from parent
                if parent.department_id:
                    vals['department_id'] = parent.department_id.id

        # Auto-assign department from code range if not inherited
        if 'department_id' not in vals:
            dept = self._guess_department(base_code)
            if dept:
                vals['department_id'] = dept.id

        acct = Account.create(vals)
        _logger.info("Created Elks account %s: %s (%s)", code, name, acct_type)
        return acct

    def _guess_department(self, code):
        """Return the elks.department matching an account code range."""
        if not code:
            return False
        prefix = code[:2] if len(code) >= 2 else code
        # Map first 2 digits to department code
        dept_map = {
            '10': '10', '15': '10', '20': '10', '21': '10',
            '23': '10', '29': '10',  # balance sheet
            '30': '30',  # lodge operations
            '40': '40',  # bar / lounge
            '50': '50',  # food service
            '60': '60',  # entertainment
            '61': '61',  # fitness
            '62': '62',  # golf
            '63': '63',  # bowling
            '64': '64',  # rv park
            '65': '65',  # shooting
            '66': '66',  # rental activities
            '67': '67',  # other business
            '90': '90', '91': '90', '92': '90', '93': '90',
            '94': '90', '95': '90',  # restricted funds
        }
        dept_code = dept_map.get(prefix)
        if dept_code:
            Dept = self.env['elks.department']
            return Dept.search([('code', '=', dept_code)], limit=1)
        return False

    @staticmethod
    def _extract_account_code(raw_value):
        """Extract a numeric account code from a QB account field.

        QuickBooks exports accounts in various formats:
          "30205"                → 30205
          "30205 · Accounting"   → 30205
          "30205 Accounting"     → 30205
          "30010A"               → 30010A
          "Accounting"           → None (no code found)
        Returns (code, name) tuple.
        """
        if not raw_value:
            return None, ''
        raw_value = raw_value.strip()

        # Split on common QB separators: " · ", " - ", or first space
        for sep in (' · ', ' · ', ' - '):
            if sep in raw_value:
                parts = raw_value.split(sep, 1)
                code_part = parts[0].strip()
                name_part = parts[1].strip() if len(parts) > 1 else ''
                # Verify the first part looks like an account code (digits, maybe a letter suffix)
                if code_part and code_part[0].isdigit():
                    return code_part, name_part
                return None, raw_value

        # No separator — check if it starts with digits
        if raw_value[0].isdigit():
            # Split at first space if present
            parts = raw_value.split(None, 1)
            code_part = parts[0]
            name_part = parts[1] if len(parts) > 1 else ''
            return code_part, name_part

        return None, raw_value

    def _import_journal_csv(self, content):
        """Import a QuickBooks Transaction Detail or General Journal CSV.

        Handles common QB export formats with flexible column matching.
        Account codes can be in their own column or embedded in the account
        name (e.g. "30205 · Accounting").

        Expected columns (flexible matching):
        - Date, Trans Date, or Transaction Date
        - Account, Account No., or Account Number
        - Account Name (optional — extracted from Account if combined)
        - Debit / Credit (separate columns) or Amount (single column)
        - Memo, Description, or Name
        - Type, Trans Type, or Transaction Type (optional)
        - Num or Check No. (optional)
        """
        reader = csv.DictReader(io.StringIO(content))
        if not reader.fieldnames:
            raise UserError(_("Empty or invalid CSV file."))

        fn = reader.fieldnames
        fn_lower = [h.lower().strip() for h in fn]

        # Map column names with priority ordering
        date_col = next((fn[i] for i, h in enumerate(fn_lower) if 'date' in h), None)
        # Account column: prefer one without "name" in it
        acct_col = next(
            (fn[i] for i, h in enumerate(fn_lower)
             if 'account' in h and 'name' not in h),
            next((fn[i] for i, h in enumerate(fn_lower) if 'account' in h), None),
        )
        acct_name_col = next(
            (fn[i] for i, h in enumerate(fn_lower)
             if 'account' in h and 'name' in h),
            None,
        )
        debit_col = next((fn[i] for i, h in enumerate(fn_lower) if h == 'debit'), None)
        credit_col = next((fn[i] for i, h in enumerate(fn_lower) if h == 'credit'), None)
        amount_col = next(
            (fn[i] for i, h in enumerate(fn_lower) if h in ('amount', 'total')),
            next((fn[i] for i, h in enumerate(fn_lower) if 'amount' in h), None),
        )
        memo_col = next(
            (fn[i] for i, h in enumerate(fn_lower)
             if h in ('memo', 'description')),
            next((fn[i] for i, h in enumerate(fn_lower) if 'memo' in h or 'desc' in h), None),
        )
        type_col = next(
            (fn[i] for i, h in enumerate(fn_lower) if 'type' in h), None,
        )
        num_col = next(
            (fn[i] for i, h in enumerate(fn_lower)
             if h in ('num', 'check no.', 'check no', 'ref')),
            None,
        )

        if not acct_col:
            raise UserError(_(
                "Could not find an 'Account' column in the CSV. "
                "Found columns: %s"
            ) % ", ".join(fn))

        JournalEntry = self.env["elks.journal.entry"]
        entries_created = 0
        lines_created = 0
        skipped_no_code = 0
        skipped_no_account = []
        errors = []

        from collections import defaultdict
        by_date = defaultdict(list)

        for i, row in enumerate(reader, start=2):
            try:
                date_str = row.get(date_col, '').strip() if date_col else ''
                raw_acct = row.get(acct_col, '').strip()
                acct_name = row.get(acct_name_col, '').strip() if acct_name_col else ''
                memo = row.get(memo_col, '').strip() if memo_col else ''
                txn_type = row.get(type_col, '').strip() if type_col else ''
                txn_num = row.get(num_col, '').strip() if num_col else ''

                # Extract account code from QB's combined format
                acct_code, extracted_name = self._extract_account_code(raw_acct)
                if not acct_name and extracted_name:
                    acct_name = extracted_name

                if not acct_code:
                    # Skip total/header rows and rows with no account code
                    raw_lower = raw_acct.lower()
                    if raw_lower and not any(
                        w in raw_lower for w in ('total', 'net', 'profit', 'loss')
                    ):
                        skipped_no_code += 1
                    continue

                # Parse debit/credit
                debit = 0.0
                credit = 0.0
                if debit_col and credit_col:
                    debit = self._parse_amount(row.get(debit_col, ''))
                    credit = self._parse_amount(row.get(credit_col, ''))
                elif amount_col:
                    amt = self._parse_amount(row.get(amount_col, ''))
                    if amt >= 0:
                        debit = amt
                    else:
                        credit = abs(amt)

                # Skip zero-amount rows
                if abs(debit) < 0.005 and abs(credit) < 0.005:
                    continue

                # Build memo from available fields
                memo_parts = []
                if txn_type:
                    memo_parts.append(txn_type)
                if txn_num:
                    memo_parts.append(f"#{txn_num}")
                if memo:
                    memo_parts.append(memo)
                full_memo = " - ".join(memo_parts) if memo_parts else ''

                date_val = self.date_override or self._parse_date(date_str)
                key = str(date_val) if date_val else 'no_date'

                by_date[key].append({
                    'date': date_val,
                    'acct_code': acct_code,
                    'acct_name': acct_name,
                    'debit': debit,
                    'credit': credit,
                    'memo': full_memo,
                })
            except Exception as e:
                errors.append(f"Row {i}: {e}")

        # Create journal entries grouped by date
        for date_key, rows in sorted(by_date.items()):
            lines = []
            entry_date = rows[0]['date'] if rows[0]['date'] else fields.Date.context_today(self)
            for r in rows:
                acct = self._find_or_create_account(r['acct_code'], r['acct_name'])
                if acct:
                    lines.append((0, 0, {
                        'account_id': acct.id,
                        'debit': r['debit'],
                        'credit': r['credit'],
                        'memo': r['memo'],
                    }))
                    lines_created += 1
                else:
                    skipped_no_account.append(
                        f"  {r['acct_code']} — {r['acct_name']}"
                    )

            if lines:
                entry = JournalEntry.create({
                    'date': entry_date,
                    'memo': f"QuickBooks import — {date_key}",
                    'line_ids': lines,
                })
                if self.auto_post and entry.is_balanced:
                    entry.action_post()
                entries_created += 1

        # Build results message
        parts = [f"IMPORT RESULTS: {entries_created} journal entries, {lines_created} lines created."]
        if self.auto_post:
            parts.append("(Auto-post enabled — balanced entries were posted.)")
        if skipped_no_code:
            parts.append(f"\nSkipped {skipped_no_code} rows with no account code.")
        if skipped_no_account:
            unique = sorted(set(skipped_no_account))
            parts.append(f"\n--- SKIPPED: Account not found ({len(unique)}) ---")
            parts.append("Enable 'Create Missing Accounts' to auto-create these:")
            parts.extend(unique)
        if errors:
            parts.append(f"\n--- ERRORS ({len(errors)}) ---")
            parts.extend(errors)
        return "\n".join(parts)

    def _import_trial_balance(self, content):
        """Import a QuickBooks Trial Balance CSV.

        Creates a single journal entry with all account balances.
        Expected columns: Account, Account Name, Debit, Credit
        """
        reader = csv.DictReader(io.StringIO(content))
        acct_col = next(
            (h for h in reader.fieldnames
             if 'account' in h.lower() and 'name' not in h.lower()),
            next((h for h in reader.fieldnames if 'account' in h.lower()), None),
        )
        acct_name_col = next(
            (h for h in reader.fieldnames if 'name' in h.lower()),
            None,
        )
        debit_col = next((h for h in reader.fieldnames if 'debit' in h.lower()), None)
        credit_col = next((h for h in reader.fieldnames if 'credit' in h.lower()), None)

        if not acct_col:
            raise UserError(_("Could not find 'Account' column."))

        lines = []
        for row in reader:
            acct_code = row.get(acct_col, '').strip()
            acct_name = row.get(acct_name_col, '').strip() if acct_name_col else ''
            if not acct_code or acct_code.lower() in ('total', ''):
                continue

            debit = self._parse_amount(row.get(debit_col, '')) if debit_col else 0.0
            credit = self._parse_amount(row.get(credit_col, '')) if credit_col else 0.0

            if abs(debit) < 0.005 and abs(credit) < 0.005:
                continue

            acct = self._find_or_create_account(acct_code, acct_name)
            if acct:
                lines.append((0, 0, {
                    'account_id': acct.id,
                    'debit': debit,
                    'credit': credit,
                    'memo': f"TB: {acct_name}",
                }))

        if not lines:
            raise UserError(_("No valid account data found in the file."))

        date = self.date_override or fields.Date.context_today(self)
        entry = self.env["elks.journal.entry"].create({
            'date': date,
            'memo': f"QuickBooks Trial Balance Import - {date}",
            'line_ids': lines,
        })
        if self.auto_post and entry.is_balanced:
            entry.action_post()

        return f"Trial Balance imported: 1 entry with {len(lines)} lines."

    def _import_pnl_detail(self, content):
        """Import QuickBooks P&L Detail report CSV."""
        # P&L Detail is similar to journal import but may have different columns
        return self._import_journal_csv(content)

    @staticmethod
    def _parse_amount(val):
        """Parse a monetary amount string, handling QB formatting."""
        if not val:
            return 0.0
        val = str(val).strip()
        # Remove currency symbols, commas, spaces
        val = val.replace('$', '').replace(',', '').replace(' ', '')
        # Handle parentheses for negative
        if val.startswith('(') and val.endswith(')'):
            val = '-' + val[1:-1]
        try:
            return float(val)
        except ValueError:
            return 0.0

    @staticmethod
    def _parse_date(val):
        """Parse various date formats from QB exports."""
        import datetime
        if not val:
            return None
        val = val.strip()
        for fmt in ('%m/%d/%Y', '%m/%d/%y', '%Y-%m-%d', '%m-%d-%Y', '%m-%d-%y'):
            try:
                return datetime.datetime.strptime(val, fmt).date()
            except ValueError:
                continue
        return None


class CloverImportWizard(models.TransientModel):
    _name = "clover.import.wizard"
    _description = "Clover POS Transaction Import"

    file_data = fields.Binary("Clover CSV File", required=True)
    file_name = fields.Char("Filename")

    default_income_account_id = fields.Many2one(
        "elks.account", string="Default Income Account",
        help="Account to credit for Clover sales (e.g., Food Sales or Bar Sales).",
    )
    default_cash_account_id = fields.Many2one(
        "elks.account", string="Default Cash/Deposit Account",
        help="Account to debit for Clover deposits (e.g., Operating Checking).",
    )
    department_id = fields.Many2one(
        "elks.department", string="Department",
        help="Department for these transactions (Bar, Food, etc.).",
    )
    date_override = fields.Date("Date Override")
    auto_post = fields.Boolean("Auto-Post", default=False)

    state = fields.Selection([
        ('setup', 'Setup'),
        ('done', 'Done'),
    ], default='setup')
    result_message = fields.Text("Import Results", readonly=True)

    def action_import(self):
        """Import Clover transaction CSV.

        Standard Clover CSV columns typically include:
        - Date, Time, Order ID, Tender, Amount, Tax, Tip, Total
        - Or: Date, Category, Item, Qty, Revenue, Tax, Discounts
        """
        self.ensure_one()
        if not self.file_data:
            raise UserError(_("Please upload a Clover CSV file."))

        raw = base64.b64decode(self.file_data)
        try:
            content = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            content = raw.decode('latin-1')

        reader = csv.DictReader(io.StringIO(content))
        if not reader.fieldnames:
            raise UserError(_("Empty or invalid CSV file."))

        # Flexible column detection
        date_col = next(
            (h for h in reader.fieldnames if 'date' in h.lower()), None,
        )
        amount_col = next(
            (h for h in reader.fieldnames
             if h.lower() in ('total', 'amount', 'revenue', 'net')),
            next((h for h in reader.fieldnames if 'amount' in h.lower()), None),
        )
        tax_col = next(
            (h for h in reader.fieldnames if 'tax' in h.lower()), None,
        )
        tip_col = next(
            (h for h in reader.fieldnames if 'tip' in h.lower()), None,
        )
        category_col = next(
            (h for h in reader.fieldnames
             if h.lower() in ('category', 'department', 'type')),
            None,
        )

        if not amount_col:
            raise UserError(_(
                "Could not find an amount column. Found: %s"
            ) % ", ".join(reader.fieldnames))

        JournalEntry = self.env["elks.journal.entry"]
        from collections import defaultdict

        # Group by date
        by_date = defaultdict(lambda: {'sales': 0.0, 'tax': 0.0, 'tips': 0.0, 'rows': 0})

        for row in reader:
            date_str = row.get(date_col, '').strip() if date_col else ''
            date_val = self.date_override or QbImportWizard._parse_date(date_str)
            date_key = str(date_val) if date_val else 'unknown'

            amount = QbImportWizard._parse_amount(row.get(amount_col, ''))
            tax = QbImportWizard._parse_amount(row.get(tax_col, '')) if tax_col else 0.0
            tip = QbImportWizard._parse_amount(row.get(tip_col, '')) if tip_col else 0.0

            by_date[date_key]['sales'] += amount
            by_date[date_key]['tax'] += tax
            by_date[date_key]['tips'] += tip
            by_date[date_key]['rows'] += 1
            if date_val:
                by_date[date_key]['date'] = date_val

        entries = 0
        for date_key, data in by_date.items():
            lines = []
            total_deposit = data['sales'] + data['tax'] + data['tips']
            entry_date = data.get('date', fields.Date.context_today(self))

            # Debit cash account
            if self.default_cash_account_id and total_deposit > 0:
                lines.append((0, 0, {
                    'account_id': self.default_cash_account_id.id,
                    'debit': total_deposit,
                    'credit': 0.0,
                    'memo': f"Clover deposit {date_key} ({data['rows']} transactions)",
                }))

            # Credit income account
            if self.default_income_account_id and data['sales'] > 0:
                lines.append((0, 0, {
                    'account_id': self.default_income_account_id.id,
                    'debit': 0.0,
                    'credit': data['sales'],
                    'memo': f"Clover sales {date_key}",
                }))

            # Credit sales tax payable
            if data['tax'] > 0:
                tax_acct = self.env["elks.account"].search(
                    [("code", "=", "20200")], limit=1,
                )
                if tax_acct:
                    lines.append((0, 0, {
                        'account_id': tax_acct.id,
                        'debit': 0.0,
                        'credit': data['tax'],
                        'memo': f"Sales tax {date_key}",
                    }))

            # Credit tips (if applicable)
            if data['tips'] > 0:
                # Tips as a payroll liability
                tips_acct = self.env["elks.account"].search(
                    [("code", "=", "20100")], limit=1,
                )
                if tips_acct:
                    lines.append((0, 0, {
                        'account_id': tips_acct.id,
                        'debit': 0.0,
                        'credit': data['tips'],
                        'memo': f"Tips payable {date_key}",
                    }))

            if lines:
                entry = JournalEntry.create({
                    'date': entry_date,
                    'memo': f"Clover POS Import - {date_key}",
                    'line_ids': lines,
                })
                if self.auto_post and entry.is_balanced:
                    entry.action_post()
                entries += 1

        result = f"Clover import complete: {entries} daily summary entries created."
        self.write({
            'state': 'done',
            'result_message': result,
        })
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }
