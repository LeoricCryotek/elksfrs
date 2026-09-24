# -*- coding: utf-8 -*-
"""QuickBooks Chart of Accounts import wizard.

Reads a QuickBooks-style Chart of Accounts CSV export and creates or
updates ``elks.account`` records to match.  Handles:

* Letter subaccounts (10104A, 21200A, 30150J, 91051F, etc.) — split
  into base 5-digit code + up to 2-character subaccount.
* QB parent:child naming (``Cash Bank Bar:Bar Till #1``) — the last
  colon-separated segment becomes the display name; the full path is
  preserved in the account's Description.
* Rows with blank Account number (QB carries "un-numbered" bookkeeping
  rows for things like `Uncategorized Expense`) — skipped with a note
  in the report.
* Preview / apply two-phase run — the user can see exactly what will
  be created / updated / skipped before committing.

Designed to work equally on a fresh install (where the module's seed
CoA is present and this wizard adds the lodge's local accounts on top)
and on a live upgrade (where the wizard finds existing accounts and
updates their names / department mappings without disturbing linked
journal entries).
"""
import base64
import csv
import io
import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


# ==================================================================
# QB Account Type / Detail Type → elks.account.account_type mapping
# ==================================================================
# QuickBooks exports 4 columns; the two type columns don't map 1:1 to
# the elks.account selection.  We do a two-stage classification:
#   1. QB Detail Type wins when it's specific (Accounts Receivable, AP,
#      Bank, Credit Card, etc.).
#   2. Fall back to QB Account Type family.
#   3. Ultimately fall back to code-prefix inference if the CSV is silent.
_QB_DETAIL_MAP = {
    'accounts receivable (a/r)': 'receivable',
    'accounts payable (a/p)':    'payable',
    'checking':                  'bank',
    'savings':                   'bank',
    'credit card':               'liability',
    'undeposited funds':         'asset',
    'allowance for bad debts':   'asset',
    'sales tax payable':         'liability',
    'global tax payable':        'liability',
    'payroll tax payable':       'liability',
    'deferred revenue':          'liability',
    'unapplied cash payment income':          'income',
    'unapplied cash bill payment expense':    'expense',
    'other current liabilities': 'liability',
    'other long term liabilities': 'long_term_liability',
    'other fixed assets':        'fixed_asset',
    'other long-term assets':    'other_asset',
    'other current assets':      'asset',
    'opening balance equity':    'equity',
    'retained earnings':         'equity',
    'paid-in capital or surplus': 'equity',
    'accumulated adjustment':    'equity',
    'service/fee income':        'income',
    'supplies & materials - cogs': 'cogs',
    'other miscellaneous service cost': 'expense',
}
_QB_TYPE_MAP = {
    'bank':                        'bank',
    'accounts receivable (a/r)':   'receivable',
    'accounts payable (a/p)':      'payable',
    'other current assets':        'asset',
    'fixed assets':                'fixed_asset',
    'other assets':                'other_asset',
    'credit card':                 'liability',
    'other current liabilities':   'liability',
    'long term liabilities':       'long_term_liability',
    'equity':                      'equity',
    'income':                      'income',
    'cost of goods sold':          'cogs',
    'expenses':                    'expense',
}

# Code prefix → department xml_id (elksfrs.dept_*)
_DEPT_PREFIX_MAP = [
    ('99', 'dept_balance_sheet'),   # year-end closing accounts
    ('90', 'dept_restricted'),
    ('91', 'dept_restricted'),
    ('92', 'dept_restricted'),
    ('93', 'dept_restricted'),
    ('94', 'dept_restricted'),
    ('95', 'dept_restricted'),
    ('96', 'dept_restricted'),
    ('97', 'dept_restricted'),
    ('67', 'dept_other'),
    ('66', 'dept_rental'),
    ('65', 'dept_shooting'),
    ('64', 'dept_rv'),
    ('63', 'dept_bowling'),
    ('62', 'dept_golf'),
    ('61', 'dept_fitness'),
    ('60', 'dept_entertainment'),
    ('50', 'dept_food'),
    ('40', 'dept_bar'),
    ('30', 'dept_lodge'),
    ('29', 'dept_balance_sheet'),
    ('23', 'dept_balance_sheet'),
    ('21', 'dept_balance_sheet'),
    ('20', 'dept_balance_sheet'),
    ('15', 'dept_balance_sheet'),
    ('10', 'dept_balance_sheet'),
]

# Code prefix → account_type fallback if CSV type columns are missing/blank
_PREFIX_TYPE_FALLBACK = [
    ('10', 'asset'),
    ('15', 'fixed_asset'),
    ('20', 'payable'),
    ('21', 'liability'),
    ('23', 'long_term_liability'),
    ('29', 'equity'),
    ('30', 'income'),
    ('40', 'income'),
    ('50', 'income'),
    ('60', 'income'),
    ('66', 'income'),
    ('90', 'income'),
    ('99', 'expense'),
]

# Regex: split "10104A" or "10104A1" or plain "10104" into (code, sub).
# Uniform CoA subaccounts are 1-2 characters (letters or digits after
# the leading 5-digit code).
_CODE_SPLIT_RE = re.compile(r'^\s*(\d{4,5})([A-Za-z0-9]{0,2})\s*$')


class QbCoaImportWizard(models.TransientModel):
    _name = 'qb.coa.import.wizard'
    _description = 'QuickBooks Chart of Accounts Import Wizard'

    csv_file = fields.Binary(
        'CSV File', required=True, attachment=False,
        help="QuickBooks-exported Chart of Accounts CSV. Expected "
             "columns (with header row): Account number, Account name, "
             "Account type, Detail type.",
    )
    filename = fields.Char('Filename')

    update_existing_names = fields.Boolean(
        'Update existing account names', default=False,
        help="If checked, existing elks.account records will have their "
             "Name overwritten from the CSV. Off by default so treasurer "
             "customizations to the seed CoA aren't clobbered.",
    )
    infer_missing_departments = fields.Boolean(
        'Auto-assign departments by code prefix', default=True,
        help="Assign each account a department based on its 5-digit code "
             "prefix (10xxx=Balance Sheet, 40xxx=Bar, 50xxx=Food, etc.).",
    )
    encoding = fields.Selection([
        ('utf-8-sig', 'UTF-8 (BOM)'),
        ('utf-8',     'UTF-8'),
        ('cp1252',    'Windows-1252 (typical QB Desktop)'),
        ('latin-1',   'Latin-1'),
    ], string='CSV Encoding', default='utf-8-sig', required=True)

    state = fields.Selection([
        ('draft',   'Ready'),
        ('preview', 'Preview'),
        ('done',    'Done'),
    ], default='draft')

    report_html = fields.Html(
        'Import Report', readonly=True, sanitize=False,
    )
    stats_created = fields.Integer('Accounts Created', readonly=True)
    stats_updated = fields.Integer('Accounts Updated', readonly=True)
    stats_skipped = fields.Integer('Accounts Skipped', readonly=True)
    stats_errors  = fields.Integer('Rows with Errors', readonly=True)

    # ==================================================================
    # Actions
    # ==================================================================
    def action_preview(self):
        """Parse the CSV and show what would happen without writing."""
        self.ensure_one()
        summary = self._run(commit=False)
        self.write({
            'state': 'preview',
            'report_html': summary['html'],
            'stats_created': summary['created'],
            'stats_updated': summary['updated'],
            'stats_skipped': summary['skipped'],
            'stats_errors':  summary['errors'],
        })
        return self._reopen()

    def action_apply(self):
        """Parse the CSV and actually create/update elks.account rows."""
        self.ensure_one()
        summary = self._run(commit=True)
        self.write({
            'state': 'done',
            'report_html': summary['html'],
            'stats_created': summary['created'],
            'stats_updated': summary['updated'],
            'stats_skipped': summary['skipped'],
            'stats_errors':  summary['errors'],
        })
        return self._reopen()

    def _reopen(self):
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    # ==================================================================
    # Core parsing / apply
    # ==================================================================
    def _decode_csv(self):
        """Decode the uploaded file with the selected encoding, tolerating
        common QB output quirks."""
        raw = base64.b64decode(self.csv_file or b'')
        try:
            text = raw.decode(self.encoding)
        except UnicodeDecodeError as exc:
            # Fall back to a permissive decode so users don't lose their
            # whole import over one bad byte.
            _logger.warning(
                "QB CoA import: %s decode failed (%s); retrying with "
                "errors='replace'", self.encoding, exc)
            text = raw.decode(self.encoding, errors='replace')
        # Strip BOM if the encoding didn't already handle it.
        if text.startswith('﻿'):
            text = text[1:]
        return text

    def _run(self, commit):
        Account = self.env['elks.account']
        Department = self.env['elks.department']

        # Pre-resolve department xml_ids so the per-row lookup is O(1).
        dept_cache = {}
        for _prefix, xmlid in _DEPT_PREFIX_MAP:
            if xmlid in dept_cache:
                continue
            try:
                dept_cache[xmlid] = self.env.ref(f'elksfrs.{xmlid}').id
            except ValueError:
                dept_cache[xmlid] = False

        # Pre-load existing accounts keyed by (code, subaccount) for fast lookup.
        existing = {}
        for acc in Account.sudo().search([]):
            existing[(acc.code, acc.subaccount or '')] = acc

        text = self._decode_csv()
        reader = csv.DictReader(io.StringIO(text))
        expected = {'Account number', 'Account name'}
        if not expected.issubset(set(reader.fieldnames or [])):
            raise UserError(_(
                "CSV is missing required columns. Expected 'Account number' "
                "and 'Account name' (also 'Account type' and 'Detail type' "
                "if available). Got: %s") % (reader.fieldnames or []))

        created = updated = skipped = errors = 0
        report_rows = []

        savepoint = commit and self.env.cr.savepoint(flush=False)
        try:
            for i, row in enumerate(reader, start=2):  # header is line 1
                try:
                    result = self._process_row(
                        row, existing, dept_cache, commit,
                    )
                except Exception as exc:  # noqa: BLE001
                    errors += 1
                    report_rows.append({
                        'code': (row.get('Account number') or '').strip(),
                        'name': (row.get('Account name') or '').strip(),
                        'action': 'ERROR',
                        'note': str(exc),
                    })
                    _logger.warning(
                        "QB CoA import row %s: %s", i, exc)
                    continue

                action = result['action']
                if action == 'created':
                    created += 1
                elif action == 'updated':
                    updated += 1
                elif action == 'skipped':
                    skipped += 1
                report_rows.append(result)
        except Exception:
            if savepoint:
                savepoint.rollback()
                savepoint.close()
            raise
        else:
            if savepoint:
                if commit:
                    savepoint.close()
                else:
                    savepoint.rollback()
                    savepoint.close()

        return {
            'created': created,
            'updated': updated,
            'skipped': skipped,
            'errors': errors,
            'html': self._build_report_html(
                report_rows, created, updated, skipped, errors, commit),
        }

    def _process_row(self, row, existing, dept_cache, commit):
        raw_code = (row.get('Account number') or '').strip()
        raw_name = (row.get('Account name') or '').strip()
        qb_type = (row.get('Account type') or '').strip().lower()
        qb_detail = (row.get('Detail type') or '').strip().lower()

        if not raw_code:
            return {
                'code': '', 'name': raw_name, 'action': 'skipped',
                'note': _("no account number (QB bookkeeping-only row)"),
            }

        match = _CODE_SPLIT_RE.match(raw_code)
        if not match:
            return {
                'code': raw_code, 'name': raw_name, 'action': 'skipped',
                'note': _("code doesn't match Uniform CoA pattern "
                          "(5-digit + optional 2-char sub)"),
            }
        base_code, sub = match.group(1), (match.group(2) or '').upper()

        # QB parent:child hierarchy — display name is the last segment;
        # keep the full path in the description note.
        if ':' in raw_name:
            parts = [p.strip() for p in raw_name.split(':')]
            display_name = parts[-1]
            note = _("QB path: %s") % raw_name
        else:
            display_name = raw_name
            note = ''

        # Resolve account_type: detail → type → prefix fallback.
        account_type = (
            _QB_DETAIL_MAP.get(qb_detail)
            or _QB_TYPE_MAP.get(qb_type)
            or self._prefix_type_fallback(base_code)
        )
        if not account_type:
            return {
                'code': raw_code, 'name': raw_name, 'action': 'skipped',
                'note': _("couldn't determine account type (QB type=%s, "
                          "detail=%s)") % (qb_type, qb_detail),
            }

        # Department by code prefix.
        dept_id = False
        if self.infer_missing_departments:
            for prefix, xmlid in _DEPT_PREFIX_MAP:
                if base_code.startswith(prefix):
                    dept_id = dept_cache.get(xmlid) or False
                    break

        key = (base_code, sub)
        existing_rec = existing.get(key)

        vals = {
            'code': base_code,
            'subaccount': sub or False,
            'name': display_name or _("(unnamed)"),
            'account_type': account_type,
        }
        if dept_id:
            vals['department_id'] = dept_id
        if base_code.startswith(('9', '99')):
            vals['is_restricted'] = base_code.startswith('9') and not base_code.startswith('99')
        if note:
            vals['note'] = note

        if existing_rec:
            change_vals = {}
            if self.update_existing_names and existing_rec.name != vals['name']:
                change_vals['name'] = vals['name']
            if not existing_rec.account_type:
                change_vals['account_type'] = vals['account_type']
            if not existing_rec.department_id and vals.get('department_id'):
                change_vals['department_id'] = vals['department_id']
            if not existing_rec.note and vals.get('note'):
                change_vals['note'] = vals['note']
            if change_vals:
                if commit:
                    existing_rec.sudo().write(change_vals)
                return {
                    'code': f"{base_code}{sub}", 'name': vals['name'],
                    'action': 'updated',
                    'note': ", ".join(f"{k}={v!r}" for k, v in change_vals.items()),
                }
            return {
                'code': f"{base_code}{sub}", 'name': vals['name'],
                'action': 'skipped',
                'note': _("already up to date"),
            }

        if commit:
            new_rec = self.env['elks.account'].sudo().create(vals)
            existing[key] = new_rec
        return {
            'code': f"{base_code}{sub}", 'name': vals['name'],
            'action': 'created',
            'note': _("type=%s, dept=%s") % (
                account_type, dept_id or "unassigned"),
        }

    @staticmethod
    def _prefix_type_fallback(base_code):
        for prefix, atype in _PREFIX_TYPE_FALLBACK:
            if base_code.startswith(prefix):
                return atype
        return None

    # ==================================================================
    # Report rendering
    # ==================================================================
    def _build_report_html(self, rows, created, updated, skipped, errors, commit):
        header = _("Applied") if commit else _("Preview (nothing written)")
        html = [
            f"<h3>{header}</h3>",
            "<div style='display:flex; gap:1em; margin-bottom:.5em;'>",
            f"<span><strong>Created:</strong> {created}</span>",
            f"<span><strong>Updated:</strong> {updated}</span>",
            f"<span><strong>Skipped:</strong> {skipped}</span>",
            f"<span style='color:#b00'><strong>Errors:</strong> {errors}</span>",
            "</div>",
            "<table class='table table-sm' style='width:100%; font-size:.9em;'>",
            "<thead><tr><th>Code</th><th>Name</th><th>Action</th><th>Note</th></tr></thead>",
            "<tbody>",
        ]
        color = {
            'created': '#0a0', 'updated': '#06c',
            'skipped': '#888', 'ERROR':   '#b00',
        }
        for r in rows[:500]:  # truncate to keep the widget responsive
            c = color.get(r['action'], '#000')
            # Escape HTML in user-supplied fields to keep sanitize=False safe.
            code = (r['code'] or '').replace('<', '&lt;').replace('>', '&gt;')
            name = (r['name'] or '').replace('<', '&lt;').replace('>', '&gt;')
            note = (r['note'] or '').replace('<', '&lt;').replace('>', '&gt;')
            html.append(
                f"<tr><td>{code}</td><td>{name}</td>"
                f"<td style='color:{c}'>{r['action']}</td>"
                f"<td>{note}</td></tr>"
            )
        if len(rows) > 500:
            html.append(
                f"<tr><td colspan='4'><em>... {len(rows) - 500} more rows "
                "omitted; check the server log for full detail.</em></td></tr>"
            )
        html.append("</tbody></table>")
        return "\n".join(html)
