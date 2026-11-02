import secrets
from datetime import timedelta
from urllib.parse import urlencode

import requests
from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError

TOKEN_URL = 'https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer'


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
        self.env.cr.execute('SELECT id FROM elks_qbo_connection WHERE id=%s FOR UPDATE NOWAIT', [self.id])
        self.invalidate_recordset()

    def _query(self, entity, where=''):
        self.ensure_one()
        if entity not in ('Account', 'Vendor', 'Purchase'):
            raise ValueError('Unsupported entity')
        conn = self.sudo()
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
                rows = response.json().get('QueryResponse', {}).get(entity, [])
            except (requests.RequestException, ValueError):
                raise UserError(_('QuickBooks query is unavailable or returned invalid data.')) from None
            yield from rows
            if len(rows) < 1000:
                return
            start += 1000

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
            missing.write({'review_reason': 'No longer returned by QuickBooks in the configured date range. Review deletion, void, or date change.', 'source_missing': True})
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
    name = fields.Char(required=True)
    connection_id = fields.Many2one('elks.qbo.connection', required=True, ondelete='cascade')
    company_id = fields.Many2one(related='connection_id.company_id', store=True)
    qbo_id = fields.Char(required=True, readonly=True)
    source_type = fields.Char(readonly=True)
    account_id = fields.Many2one('elks.account', domain="[('is_header', '=', False)]")
    _unique = models.Constraint('UNIQUE(connection_id, qbo_id)', 'QuickBooks account already mapped.')

    def write(self, vals):
        if 'account_id' in vals:
            for rec in self:
                if rec.account_id.id != vals['account_id'] and self.env['elks.qbo.expense'].search_count([
                        ('connection_id', '=', rec.connection_id.id), ('journal_id', '!=', False)]):
                    raise UserError(_('Account mappings cannot change after FRS recording. Use an audited migration.'))
        return super().write(vals)



class VendorMap(models.Model):
    _name = 'elks.qbo.vendor.map'
    _description = 'QuickBooks vendor mapping'
    name = fields.Char(required=True)
    connection_id = fields.Many2one('elks.qbo.connection', required=True, ondelete='cascade')
    company_id = fields.Many2one(related='connection_id.company_id', store=True)
    qbo_id = fields.Char(required=True, readonly=True)
    partner_id = fields.Many2one('res.partner', check_company=True)
    _unique = models.Constraint('UNIQUE(connection_id, qbo_id)', 'QuickBooks vendor already mapped.')
