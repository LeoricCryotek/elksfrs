import logging
import secrets
from psycopg2 import errors
from datetime import timedelta
from urllib.parse import urlencode

import requests
from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError

from . import reports as _qbo_reports

TOKEN_URL = 'https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer'
_logger = logging.getLogger(__name__)


class Connection(models.Model):
    _name = 'elks.qbo.connection'
    _description = 'QuickBooks Online connection'
    _check_company_auto = True

    name = fields.Char(required=True, default='QuickBooks Online')
    company_id = fields.Many2one('res.company', required=True, default=lambda s: s.env.company)
    environment = fields.Selection([('sandbox', 'Sandbox'), ('production', 'Production')], required=True, default='sandbox')
    client_id = fields.Char(groups='base.group_system', copy=False)
    client_secret = fields.Char(groups='base.group_system', copy=False)
    access_token = fields.Char(groups='base.group_system', copy=False)
    refresh_token = fields.Char(groups='base.group_system', copy=False)
    expires_at = fields.Datetime(readonly=True, copy=False)
    realm_id = fields.Char(readonly=True, copy=False)
    connected = fields.Boolean(readonly=True, copy=False)
    active = fields.Boolean(default=True)
    start_date = fields.Date(required=True, default=lambda s: fields.Date.context_today(s).replace(month=4, day=1) if fields.Date.context_today(s).month >= 4 else fields.Date.context_today(s).replace(year=fields.Date.context_today(s).year-1, month=4, day=1))
    auto_requisition = fields.Boolean(default=True)
    auto_record = fields.Boolean(default=False, help='After approval and officer signatures, record supported charges in FRS on the next pull.')
    auto_post_when_mapped = fields.Boolean(
        string='Auto-post fully-mapped charges to FRS',
        default=True,
        help="Treat QuickBooks as the authoritative ledger. When every "
             "account on a pulled QBO charge is mapped to an FRS "
             "account, post the journal entry straight to FRS without "
             "routing through a Purchase Order approval.\n\n"
             "Restricted-fund charges still land in Review so the "
             "treasurer can document the restricted purpose — those are "
             "the only charges this mode leaves for manual handling.\n\n"
             "Turn this off if you want every QBO charge to require "
             "retrospective PO approval and officer signatures in Odoo."
    )
    enforce_budget_on_auto_post = fields.Boolean(
        string='Block over-budget auto-posts',
        default=False,
        help="When auto-posting, refuse to record a charge that would "
             "push a department over its floor-approved budget. "
             "Off by default — if the treasurer already booked the "
             "charge in QBO, blocking it here just creates data drift. "
             "Turn this on if you want Odoo to enforce budgets as a "
             "second check."
    )
    pending_review_count = fields.Integer(
        string='Pending Review', compute='_compute_pending_review_count',
    )
    last_sync = fields.Datetime(readonly=True, copy=False)
    last_error = fields.Text(readonly=True, copy=False)
    mapping_ids = fields.One2many('elks.qbo.account.map', 'connection_id')
    vendor_ids = fields.One2many('elks.qbo.vendor.map', 'connection_id')
    purchase_workflow_available = fields.Boolean(compute='_compute_purchase_workflow_available')
    redirect_uri = fields.Char(compute='_compute_redirect')
    _company_unique = models.Constraint('UNIQUE(company_id)', 'Use one QuickBooks connection per company.')

    def _compute_purchase_workflow_available(self):
        available = 'x_approval_state' in self.env['purchase.order']._fields
        for conn in self:
            conn.purchase_workflow_available = available

    def _compute_pending_review_count(self):
        Expense = self.env['elks.qbo.expense']
        for conn in self:
            conn.pending_review_count = Expense.search_count([
                ('connection_id', '=', conn.id),
                ('journal_id', '=', False),
                ('source_missing', '=', False),
                ('source_changed', '=', False),
            ])

    def action_generate_frs_report(self):
        """Open the QBO → FRS transform wizard pre-loaded for live pull.

        One-click path from the connection to a downloaded FRS CSV:
        connection → wizard → period preset (FYTD default) → Generate.
        """
        self.ensure_one()
        self._manager()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Generate FRS Report from QuickBooks'),
            'res_model': 'qbo.actuals.transform.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_source': 'qbo_live',
                'default_connection_id': self.id,
                'default_report_type': 'actuals',
                'default_period_preset': 'fytd',
            },
        }

    def action_auto_post_all(self):
        """Walk the pending review queue and auto-post everything eligible.

        Called by the "Auto-Post Mapped Expenses" button on the
        connection. Expenses that still need human attention (restricted
        fund without a documented purpose, missing mapping, source
        changed, etc.) are left in review with a clear reason.
        """
        self._manager()
        self.ensure_one()
        if (self.environment or '').lower() == 'sandbox':
            raise UserError(_(
                "SANDBOX MODE — this connection is pointing at Intuit's "
                "sandbox test data, not your real books. Auto-post is "
                "disabled so sandbox charges can't land in the real FRS "
                "ledger.\n\n"
                "You can still pull data and configure mappings to "
                "preview the workflow. When you're ready, change "
                "Environment to Production, reconnect to QuickBooks, "
                "and run Auto-Post Mapped Expenses against live data."))
        if not self.auto_post_when_mapped:
            raise UserError(_(
                "Enable 'Auto-post fully-mapped charges to FRS' on this "
                "connection first."))
        if self.company_id != self.env.company:
            raise UserError(_('Switch to the connection company first.'))
        pending = self.env['elks.qbo.expense'].search([
            ('connection_id', '=', self.id),
            ('journal_id', '=', False),
            ('source_missing', '=', False),
            ('source_changed', '=', False),
        ])
        posted = skipped = 0
        for exp in pending:
            try:
                did_post = exp._auto_post_direct()
                if did_post:
                    posted += 1
                else:
                    skipped += 1
            except Exception as exc:  # noqa: BLE001
                skipped += 1
                exp._write_import({'review_reason': str(exc)})
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {
                    'title': _('Auto-post complete'),
                    'message': _('%(posted)s posted · %(skipped)s left in review') % {
                        'posted': posted, 'skipped': skipped,
                    },
                    'type': 'success' if posted else 'warning',
                    'sticky': False,
                }}

    def _compute_redirect(self):
        for conn in self:
            conn.redirect_uri = conn.get_base_url() + '/elks/qbo/callback'

    def _manager(self):
        if not self.env.user.has_group('elksfrs.group_elksfrs_manager'):
            raise AccessError(_('FRS manager access is required.'))

    def action_connect(self):
        self.ensure_one()
        if not self.env.user.has_group('base.group_system'):
            raise AccessError(_('Only administrators can connect QuickBooks.'))
        if not self.client_id or not self.client_secret:
            raise UserError(_('Enter the Intuit client ID and secret first.'))
        from odoo.http import request
        state = secrets.token_urlsafe(32)
        request.session['elks_qbo_oauth'] = {'state': state, 'connection': self.id, 'uid': self.env.uid,
                                           'expires': (fields.Datetime.now() + timedelta(minutes=10)).isoformat()}
        return {'type': 'ir.actions.act_url', 'target': 'self', 'url':
                'https://appcenter.intuit.com/connect/oauth2?' + urlencode({
                    'client_id': self.client_id, 'response_type': 'code',
                    'scope': 'com.intuit.quickbooks.accounting', 'redirect_uri': self.redirect_uri, 'state': state})}

    def write(self, vals):
        admin_fields = {'realm_id', 'connected', 'expires_at', 'client_id', 'client_secret',
                        'access_token', 'refresh_token', 'company_id', 'environment'}
        if admin_fields.intersection(vals) and not self.env.is_system():
            raise AccessError(_('Only administrators can change the QuickBooks connection identity or credentials.'))
        if 'start_date' in vals and any(rec.last_sync for rec in self):
            raise UserError(_('The import cutoff cannot change after the first successful pull.'))
        if set(vals) & {'company_id', 'environment'} and any(rec.realm_id for rec in self):
            raise UserError(_('Company and environment cannot change after connecting QuickBooks.'))
        return super().write(vals)

    def action_disconnect(self):
        self.ensure_one()
        if not self.env.user.has_group('base.group_system'):
            raise AccessError(_('Only administrators can disconnect QuickBooks.'))
        self._lock()
        if self.refresh_token:
            try:
                response = requests.post('https://developer.api.intuit.com/v2/oauth2/tokens/revoke',
                    auth=(self.client_id, self.client_secret), json={'token': self.refresh_token},
                    headers={'Accept': 'application/json'}, timeout=30)
                if response.status_code != 200:
                    raise UserError(_('QuickBooks token revocation failed. Retry disconnect.'))
            except requests.RequestException:
                raise UserError(_('QuickBooks revocation service is unavailable.')) from None
        self.write({'access_token': False, 'refresh_token': False, 'expires_at': False, 'connected': False})

    def _token(self, data):
        self.ensure_one()
        conn = self.sudo()
        try:
            response = requests.post(TOKEN_URL, auth=(conn.client_id, conn.client_secret), data=data,
                                     headers={'Accept': 'application/json'}, timeout=30)
            if response.status_code != 200:
                raise UserError(_('QuickBooks authorization failed (HTTP %s). Reconnect if necessary.') % response.status_code)
            token = response.json()
            conn.write({'access_token': token['access_token'], 'refresh_token': token['refresh_token'],
                        'expires_at': fields.Datetime.now() + timedelta(seconds=int(token['expires_in'])), 'connected': True})
        except (requests.RequestException, ValueError, KeyError):
            raise UserError(_('QuickBooks token service is unavailable or returned an invalid response.')) from None

    def _lock(self):
        self.ensure_one()
        self.check_access('write')
        self.flush_recordset()
        try:
            with self.env.cr.savepoint():
                self.env.cr.execute('SELECT id FROM elks_qbo_connection WHERE id=%s FOR UPDATE NOWAIT', [self.id])
        except errors.LockNotAvailable:
            raise UserError(_('This QuickBooks connection is already syncing. Try again when it finishes.')) from None
        self.invalidate_recordset()

    def _report(self, report_name, params=None):
        """Call a QBO Reports API endpoint and return the parsed JSON.

        :param report_name: 'TransactionList', 'BalanceSheet', 'ProfitAndLoss',
                            'GeneralLedger', 'BudgetSummary', etc.
        :param dict params: query-string parameters for the report

        Returns the raw JSON dict so callers can walk the nested
        Columns/Rows structure. Reuses the same OAuth refresh and
        host-routing (sandbox vs production) as `_query`.
        """
        self.ensure_one()
        _ALLOWED = {'TransactionList', 'BalanceSheet', 'ProfitAndLoss',
                    'GeneralLedger', 'BudgetSummary', 'TrialBalance'}
        if report_name not in _ALLOWED:
            raise ValueError(f'Unsupported report: {report_name!r}')
        conn = self.sudo()
        if not self.realm_id or not self.realm_id.isdigit():
            raise UserError(_('Reconnect QuickBooks: the company realm ID is invalid.'))
        if not conn.expires_at or conn.expires_at <= fields.Datetime.now() + timedelta(minutes=5):
            self._token({'grant_type': 'refresh_token', 'refresh_token': conn.refresh_token})
        host = ('sandbox-quickbooks.api.intuit.com'
                if self.environment == 'sandbox'
                else 'quickbooks.api.intuit.com')
        try:
            response = requests.get(
                f'https://{host}/v3/company/{self.realm_id}/reports/{report_name}',
                params=params or {},
                headers={'Accept': 'application/json',
                         'Authorization': 'Bearer ' + conn.access_token},
                timeout=90,  # reports can be slower than entity queries
            )
            if response.status_code != 200:
                raise UserError(_(
                    'QuickBooks report %(name)s failed (HTTP %(code)s). Retry.'
                ) % {'name': report_name, 'code': response.status_code})
            data = response.json()
        except (requests.RequestException, ValueError):
            raise UserError(_(
                'QuickBooks report %s is unavailable or returned invalid data.'
            ) % report_name) from None
        if not isinstance(data, dict) or 'Fault' in data:
            raise UserError(_('QuickBooks returned an invalid report response.'))
        return data

    def _query(self, entity, where=''):
        self.ensure_one()
        if entity not in ('Account', 'Vendor', 'Purchase'):
            raise ValueError('Unsupported entity')
        conn = self.sudo()
        if not self.realm_id or not self.realm_id.isdigit():
            raise UserError(_('Reconnect QuickBooks: the company realm ID is invalid.'))
        if not conn.expires_at or conn.expires_at <= fields.Datetime.now() + timedelta(minutes=5):
            self._token({'grant_type': 'refresh_token', 'refresh_token': conn.refresh_token})
        host = 'sandbox-quickbooks.api.intuit.com' if self.environment == 'sandbox' else 'quickbooks.api.intuit.com'
        start = 1
        while True:
            query = f'SELECT * FROM {entity} {where} STARTPOSITION {start} MAXRESULTS 1000'
            try:
                response = requests.get(f'https://{host}/v3/company/{self.realm_id}/query',
                                        params={'query': query}, headers={'Accept': 'application/json',
                                        'Authorization': 'Bearer ' + conn.access_token}, timeout=45)
                if response.status_code != 200:
                    raise UserError(_('QuickBooks query failed (HTTP %s). Retry the pull.') % response.status_code)
                data = response.json()
                if not isinstance(data, dict) or 'Fault' in data or not isinstance(data.get('QueryResponse'), dict):
                    raise UserError(_('QuickBooks returned an invalid query response. No missing records were inferred.'))
                rows = data['QueryResponse'].get(entity, [])
                if not isinstance(rows, list) or any(not isinstance(row, dict) or not row.get('Id') for row in rows):
                    raise UserError(_('QuickBooks returned invalid records. Retry the pull.'))
            except (requests.RequestException, ValueError):
                raise UserError(_('QuickBooks query is unavailable or returned invalid data.')) from None
            yield from rows
            if len(rows) < 1000:
                return
            start += 1000

    def _refresh_balances_from_qbo(self, as_of=None):
        """Call the QBO Balance Sheet report and cache each account's
        closing balance on its elks.qbo.account.map row.

        Powers the "Bank Accounts" tile on the FRS dashboard — the UI
        reads balance fields off the map rows rather than hitting
        Intuit on every page load.
        """
        self.ensure_one()
        as_of = as_of or fields.Date.context_today(self)
        json_data = self._report('BalanceSheet', {
            'date_macro':        'Custom',
            'end_date':          as_of.strftime('%Y-%m-%d'),
            'accounting_method': 'Accrual',
            'summarize_column_by': 'Total',
        })
        bs_as_of, rows, _skipped = _qbo_reports.parse_balance_sheet(json_data)
        # Index by local code so we can match rows back to map records.
        by_code = {r['account']: r['amount'] for r in rows}

        # The BalanceSheet report returns balances keyed by Elks-code
        # format, but our map table stores the raw QBO account id in
        # qbo_id and the canonical name in name (often with the code
        # embedded). Match via the account map's linked elks.account
        # when available, then fall back to extracting a code from name.
        now_dt = fields.Datetime.now()
        touched = 0
        for m in self.mapping_ids:
            # Preferred: match via the mapped elks.account code
            code = (m.account_id.code or '').strip() if m.account_id else ''
            if m.account_id and m.account_id.subaccount:
                code = f"{code}{m.account_id.subaccount}"
            # Fallback: extract a code from the QBO account name
            if not code or code not in by_code:
                code = _qbo_reports._find_account_in_text(m.name or '')
            if not code or code not in by_code:
                continue
            # Write via sudo to avoid the write() audit guard on the model.
            m.sudo().write({
                'last_known_balance': float(by_code[code]),
                'balance_as_of': now_dt,
            })
            touched += 1
        _logger.info(
            'Refreshed %s account balance(s) from QBO Balance Sheet '
            '(as of %s).', touched, bs_as_of or as_of)
        return touched

    def action_pull(self):
        self._manager()
        self.ensure_one()
        self._lock()
        if not self.connected:
            raise UserError(_('Connect QuickBooks first.'))
        # FRS base journals and COA are shared, so prevent cross-company posting.
        if self.company_id != self.env.company:
            raise UserError(_('Switch to the connection company before pulling.'))
        try:
            for entity, model in [('Account', 'elks.qbo.account.map'), ('Vendor', 'elks.qbo.vendor.map')]:
                for row in self._query(entity):
                    mapping = self.env[model].search([('connection_id', '=', self.id), ('qbo_id', '=', str(row['Id']))], limit=1)
                    vals = {'name': row.get('FullyQualifiedName') or row.get('DisplayName') or row.get('Name') or str(row['Id'])}
                    if entity == 'Account':
                        vals['source_type'] = row.get('AccountType', '')
                    if not mapping:
                        vals.update(connection_id=self.id, qbo_id=str(row['Id']))
                        self.env[model].create(vals)
                    else:
                        mapping.write(vals)
            # Full bounded pull catches edits and avoids CDC's 30-day gap limit.
            seen = set()
            for row in self._query('Purchase', "WHERE TxnDate >= '%s'" % self.start_date.isoformat()):
                seen.add(str(row['Id']))
                self.env['elks.qbo.expense']._upsert(self, row)
            missing = self.env['elks.qbo.expense'].search([('connection_id', '=', self.id), ('qbo_id', 'not in', list(seen))])
            missing._write_import({'review_reason': 'No longer returned by QuickBooks in the configured date range. Review deletion, void, or date change.', 'source_missing': True})
            # Refresh cached per-account balances from QBO Balance Sheet
            # so the dashboard "Bank Accounts" tile is always fresh after
            # a pull. Non-fatal — a BS-report failure doesn't kill the pull.
            try:
                self._refresh_balances_from_qbo()
            except Exception as exc:  # noqa: BLE001
                _logger.warning('QBO balance refresh failed: %s', exc)
            self.write({'last_sync': fields.Datetime.now(), 'last_error': False})
        except UserError as exc:
            # Retain rotated OAuth credentials and imported rows for an idempotent retry.
            self.last_error = str(exc)
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'title': _('QuickBooks pull'), 'message': self.last_error or _('Pull completed. Review expenses and mappings.'),
            'type': 'warning' if self.last_error else 'success', 'sticky': bool(self.last_error)}}

    @api.model
    def _cron_pull(self):
        for conn in self.search([('connected', '=', True), ('active', '=', True)]):
            conn.with_company(conn.company_id).action_pull()


class AccountMap(models.Model):
    _name = 'elks.qbo.account.map'
    _description = 'QuickBooks to FRS account mapping'
    _order = 'is_bank_or_cc desc, last_known_balance desc, name'
    name = fields.Char(required=True)
    connection_id = fields.Many2one('elks.qbo.connection', required=True, ondelete='cascade')
    company_id = fields.Many2one(related='connection_id.company_id', store=True)
    currency_id = fields.Many2one(related='company_id.currency_id', store=False)
    qbo_id = fields.Char(required=True, readonly=True)
    source_type = fields.Char(readonly=True)
    account_id = fields.Many2one('elks.account', domain="[('is_header', '=', False)]")

    # Cached balance last reported by the QBO Balance Sheet API, so the
    # dashboard "Bank Accounts" tile doesn't need to re-hit Intuit on
    # every page load. Refreshed at the end of every connection pull.
    last_known_balance = fields.Monetary(
        'Balance (as of last pull)', currency_field='currency_id',
        readonly=True, copy=False,
    )
    balance_as_of = fields.Datetime(
        'Balance As Of', readonly=True, copy=False,
    )
    is_bank_or_cc = fields.Boolean(
        'Bank or Credit Card', compute='_compute_is_bank_or_cc', store=True,
        help="True for Bank, Credit Card, Savings, and other account "
             "types that belong on the 'Business at a Glance' dashboard.",
    )
    _unique = models.Constraint('UNIQUE(connection_id, qbo_id)', 'QuickBooks account already mapped.')

    @api.depends('source_type')
    def _compute_is_bank_or_cc(self):
        for rec in self:
            st = (rec.source_type or '').strip()
            rec.is_bank_or_cc = st in ('Bank', 'Credit Card')

    def write(self, vals):
        if 'account_id' in vals:
            for rec in self:
                if rec.account_id.id == vals['account_id']:
                    continue
                recorded = self.env['elks.qbo.expense'].search([
                    ('connection_id', '=', rec.connection_id.id), ('journal_id', '!=', False)])
                for expense in recorded:
                    source = expense.payload or {}
                    refs = {str(source.get('AccountRef', {}).get('value', ''))}
                    refs.update(str(line.get('AccountBasedExpenseLineDetail', {}).get('AccountRef', {}).get('value', ''))
                                for line in source.get('Line', []))
                    if rec.qbo_id in refs:
                        raise UserError(_('This account mapping is used by recorded expenses. Use an audited migration.'))
        result = super().write(vals)
        # After a mapping gains an account_id, drain any pending expenses
        # that referenced this QBO account. One edit = many charges cleared.
        if 'account_id' in vals and vals.get('account_id'):
            self._reprocess_dependents()
        return result

    def _reprocess_dependents(self):
        """Re-run _process() on pending expenses that reference this mapping."""
        Expense = self.env['elks.qbo.expense']
        for rec in self:
            pending = Expense.search([
                ('connection_id', '=', rec.connection_id.id),
                ('journal_id', '=', False),
                ('source_missing', '=', False),
                ('source_changed', '=', False),
            ])
            for exp in pending:
                p = exp.payload or {}
                refs = {str((p.get('AccountRef') or {}).get('value') or '')}
                for ln in (p.get('Line') or []):
                    acct_ref = (ln.get('AccountBasedExpenseLineDetail') or {}).get('AccountRef') or {}
                    refs.add(str(acct_ref.get('value') or ''))
                if rec.qbo_id in refs:
                    exp._write_import({'review_reason': False})
                    try:
                        exp._process()
                    except Exception:  # noqa: BLE001
                        pass  # _process logs its own review_reason



class VendorMap(models.Model):
    _name = 'elks.qbo.vendor.map'
    _description = 'QuickBooks vendor mapping'
    name = fields.Char(required=True)
    connection_id = fields.Many2one('elks.qbo.connection', required=True, ondelete='cascade')
    company_id = fields.Many2one(related='connection_id.company_id', store=True)
    qbo_id = fields.Char(required=True, readonly=True)
    partner_id = fields.Many2one('res.partner', check_company=True)
    _unique = models.Constraint('UNIQUE(connection_id, qbo_id)', 'QuickBooks vendor already mapped.')

    def write(self, vals):
        if 'partner_id' in vals:
            for rec in self:
                if rec.partner_id.id != vals['partner_id'] and self.env['elks.qbo.expense'].search_count([
                        ('connection_id', '=', rec.connection_id.id), ('vendor_qbo_id', '=', rec.qbo_id),
                        '|', ('purchase_id', '!=', False), ('journal_id', '!=', False)]):
                    raise UserError(_('This vendor mapping is used by matched or recorded expenses. Review the existing links before changing it.'))
        result = super().write(vals)
        # After a vendor gains a partner, drain any pending expenses from
        # that QBO vendor.
        if 'partner_id' in vals and vals.get('partner_id'):
            Expense = self.env['elks.qbo.expense']
            for rec in self:
                pending = Expense.search([
                    ('connection_id', '=', rec.connection_id.id),
                    ('vendor_qbo_id', '=', rec.qbo_id),
                    ('journal_id', '=', False),
                    ('source_missing', '=', False),
                    ('source_changed', '=', False),
                ])
                for exp in pending:
                    exp._write_import({'review_reason': False})
                    try:
                        exp._process()
                    except Exception:  # noqa: BLE001
                        pass
        return result
        return super().write(vals)
