import hashlib
import json
from collections import defaultdict

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError
from .payload import normalize_purchase
from .standard_codes import STANDARD_CODES


class Expense(models.Model):
    _name = 'elks.qbo.expense'
    _description = 'QuickBooks bank or card expense'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'date desc, id desc'
    _check_company_auto = True

    name = fields.Char(required=True, readonly=True)
    connection_id = fields.Many2one('elks.qbo.connection', required=True, readonly=True, ondelete='restrict')
    company_id = fields.Many2one(related='connection_id.company_id', store=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    qbo_id = fields.Char(required=True, readonly=True, copy=False, index=True)
    source_hash = fields.Char(readonly=True, copy=False)
    payload = fields.Json(readonly=True, copy=False)
    date = fields.Date(readonly=True)
    amount = fields.Monetary(readonly=True)
    vendor_qbo_id = fields.Char(readonly=True)
    partner_id = fields.Many2one('res.partner', compute='_compute_partner', store=True)
    purchase_workflow_available = fields.Boolean(related='connection_id.purchase_workflow_available')
    purchase_id = fields.Many2one('purchase.order', string='Purchase Order / Requisition', check_company=True,
                                  tracking=True, copy=False, domain="[('company_id', '=', company_id), ('state', '!=', 'cancel')]")
    journal_id = fields.Many2one('elks.journal.entry', readonly=True, copy=False, ondelete='restrict')
    existing_journal_id = fields.Many2one('elks.journal.entry', string='Reconcile Existing FRS Entry', copy=False, help='Select an existing entry instead of posting again. Date, currency and every debit/credit allocation must match exactly.')
    _journal_unique = models.Constraint('UNIQUE(journal_id)', 'An FRS journal can be linked to only one QuickBooks charge.')
    review_reason = fields.Text(readonly=True)
    source_missing = fields.Boolean(readonly=True)
    source_changed = fields.Boolean(readonly=True)
    restricted_purpose = fields.Text(tracking=True, help='Document how this expense serves the restricted purpose; requires human review.')
    state = fields.Selection([('review', 'Needs Review'), ('approval', 'Awaiting Purchase Approval'),
                              ('ready', 'Ready to Record'), ('recorded', 'Recorded in FRS')], compute='_compute_state')
    _unique = models.Constraint('UNIQUE(connection_id, qbo_id)', 'This QuickBooks expense is already imported.')

    def _manager(self):
        if not self.env.user.has_group('elksfrs.group_elksfrs_manager'):
            raise AccessError(_('FRS manager access is required.'))

    def _lock(self):
        for rec in self.sorted('id'):
            self.env.cr.execute('SELECT id FROM elks_qbo_expense WHERE id=%s FOR UPDATE', [rec.id])
        self.invalidate_recordset()

    @api.depends('vendor_qbo_id', 'connection_id.vendor_ids.partner_id')
    def _compute_partner(self):
        for rec in self:
            rec.partner_id = self.env['elks.qbo.vendor.map'].search([
                ('connection_id', '=', rec.connection_id.id), ('qbo_id', '=', rec.vendor_qbo_id)], limit=1).partner_id

    @api.depends('journal_id', 'review_reason', 'purchase_id', 'purchase_id.state', 'source_changed', 'source_missing', 'purchase_workflow_available')
    def _compute_state(self):
        for rec in self:
            if rec.review_reason or rec.source_changed or rec.source_missing:
                rec.state = 'review'
            elif rec.journal_id:
                rec.state = 'recorded'
            elif not rec.purchase_workflow_available:
                rec.state = 'review'
            elif rec.purchase_id.x_approval_state == 'approved' and rec.purchase_id.x_all_officers_signed:
                rec.state = 'ready'
            else:
                rec.state = 'approval'

    def _require_purchase_workflow(self):
        if 'x_approval_state' not in self.env['purchase.order']._fields:
            raise UserError(_('Install Elks Lodge Purchase Requests (elkspurchase) to enable approval and PO matching. Imported QuickBooks data is retained.'))

    @api.constrains('purchase_id')
    def _check_purchase(self):
        for rec in self.filtered('purchase_id'):
            rec._require_purchase_workflow()
            po = rec.purchase_id
            if po.state == 'cancel' or po.x_approval_state == 'rejected':
                raise ValidationError(_('Select an active requisition or PO.'))
            if not rec.partner_id or po.partner_id != rec.partner_id:
                raise ValidationError(_('The PO vendor must match the mapped QuickBooks vendor.'))
            if po.currency_id != rec.currency_id:
                raise ValidationError(_('The PO and expense must use the company currency.'))

    def write(self, vals):
        if 'purchase_id' in vals:
            self._manager()
            self._lock()
            if any(rec.journal_id and rec.purchase_id.id != vals['purchase_id'] for rec in self):
                raise UserError(_('A recorded expense cannot be reassigned.'))
        return super().write(vals)

    @api.model
    def _upsert(self, conn, row):
        fingerprint = hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
        expense = self.search([('connection_id', '=', conn.id), ('qbo_id', '=', str(row['Id']))], limit=1)
        if expense and expense.source_hash == fingerprint:
            if expense.source_missing:
                expense.write({'source_missing': False, 'review_reason': False})
            expense._process()
            return expense
        if expense and (expense.journal_id or expense.purchase_id):
            expense.write({'source_changed': True, 'review_reason': 'QuickBooks changed after PO matching or FRS recording. Review the source and correct with an audited adjustment; the original accounting is retained.'})
            return expense
        vals = {'name': 'QBO Purchase ' + str(row['Id']), 'payload': row, 'source_hash': fingerprint,
                'vendor_qbo_id': str(row.get('EntityRef', {}).get('value', '')),
                'date': row.get('TxnDate'), 'amount': row.get('TotalAmt', 0), 'source_missing': False,
                'review_reason': False}
        if expense:
            expense.write(vals)
        else:
            expense = self.create(dict(vals, connection_id=conn.id, qbo_id=str(row['Id'])))
        expense._process()
        return expense

    def _process(self):
        self.ensure_one()
        if self.journal_id or self.source_changed or self.source_missing:
            return
        try:
            with self.env.cr.savepoint():
                self._accounting_lines()
                if not self.partner_id:
                    raise UserError(_('Map the QuickBooks vendor to an Odoo contact.'))
                if self.connection_id.auto_requisition or self.connection_id.auto_record:
                    self._require_purchase_workflow()
                if self.connection_id.auto_requisition:
                    self.action_prepare_purchase()
                if self.connection_id.auto_record and self.purchase_id.x_approval_state == 'approved' and self.purchase_id.x_all_officers_signed:
                    self.action_record_frs()
        except (UserError, ValueError) as exc:
            self.review_reason = str(exc)

    def _accounting_lines(self):
        self.ensure_one()
        try:
            normalized = normalize_purchase(self.payload, self.currency_id.name)
        except ValueError as exc:
            raise UserError(str(exc)) from None
        maps = self.connection_id.mapping_ids
        result = []
        for line in normalized:
            account = maps.filtered(lambda m: m.qbo_id == line['account']).account_id
            if not account or account.is_header or not account.active:
                raise UserError(_('Map each source expense account to an active FRS posting account.'))
            if account.account_type not in ('expense', 'cogs', 'fixed_asset'):
                raise UserError(_('Non-expense allocations require manual review.'))
            result.append(dict(line, account=account))
        payment_map = maps.filtered(lambda m: m.qbo_id == str(self.payload['AccountRef']['value']))
        payment = payment_map.account_id
        allowed = ('bank', 'asset') if payment_map.source_type == 'Bank' else ('liability',) if payment_map.source_type == 'Credit Card' else ()
        if not payment or not payment.active or payment.is_header or payment.account_type not in allowed:
            raise UserError(_('Map the source bank or credit card account to an active FRS bank or liability account.'))
        for account in [payment] + [line['account'] for line in result]:
            standard = account.elks_standard_code or account.code
            if standard not in STANDARD_CODES or len(account.code or '') != 5 or not account.code.isdigit() or len(account.subaccount or '') > 2:
                raise UserError(_('Each FRS account needs a five-digit code, at most a two-character suffix, and an authorized Uniform COA mapping.'))
        return result, payment

    def action_recheck(self):
        self._manager()
        self._lock()
        for rec in self:
            if rec.source_changed or rec.source_missing:
                raise UserError(_('Resolve the QuickBooks source change with the treasurer before processing.'))
            rec._accounting_lines()
            if not rec.partner_id:
                raise UserError(_('Map the vendor first.'))
            rec.review_reason = False
            if rec.connection_id.auto_requisition:
                rec.action_prepare_purchase()

    def _po_compatible(self, po, lines):
        """Only match full, untaxed, unallocated POs; partial charges remain manual."""
        if po.partner_id != self.partner_id or po.currency_id != self.currency_id or po.company_id != self.company_id:
            return False
        if any(line.tax_ids for line in po.order_line) or self.currency_id.compare_amounts(po.amount_total, self.amount):
            return False
        actual, expected = defaultdict(float), defaultdict(float)
        for line in po.order_line.filtered(lambda l: not l.display_type):
            if not line.x_elks_account_id:
                return False
            actual[line.x_elks_account_id.id] += line.price_subtotal
        for line in lines:
            expected[line['account'].id] += line['amount']
        return set(actual) == set(expected) and all(not self.currency_id.compare_amounts(actual[k], expected[k]) for k in actual)

    def action_prepare_purchase(self):
        self._manager()
        self._require_purchase_workflow()
        self._lock()
        for rec in self:
            if rec.source_changed or rec.source_missing:
                raise UserError(_('Resolve the source exception first.'))
            if rec.purchase_id:
                continue
            lines, _payment = rec._accounting_lines()
            if not rec.partner_id:
                raise UserError(_('Map the vendor before creating a requisition.'))
            candidates = self.env['purchase.order'].search([
                ('company_id', '=', rec.company_id.id), ('partner_id', '=', rec.partner_id.id),
                ('state', '!=', 'cancel'), ('x_approval_state', '!=', 'rejected'),
                ('qbo_expense_ids', '=', False)])
            compatible = candidates.filtered(lambda po: rec._po_compatible(po, lines))
            if len(compatible) == 1:
                rec.purchase_id = compatible.id
                rec.message_post(body=_('Matched an existing PO by vendor, currency, total, and FRS account allocation.'))
            elif candidates:
                rec.review_reason = _('Existing vendor requisitions or POs need review. Select the matching PO or explicitly create a new requisition.')
            else:
                rec.action_create_requisition()

    def action_create_requisition(self):
        self._manager()
        self._require_purchase_workflow()
        self._lock()
        for rec in self:
            if rec.purchase_id:
                continue
            if rec.source_changed or rec.source_missing:
                raise UserError(_('Resolve the source exception first.'))
            lines, _payment = rec._accounting_lines()
            if not rec.partner_id:
                raise UserError(_('Map the vendor first.'))
            product = self.env.ref('elksfrs.qbo_expense_product')
            po = self.env['purchase.order'].create({
                'partner_id': rec.partner_id.id, 'company_id': rec.company_id.id,
                'currency_id': rec.currency_id.id, 'origin': rec.name, 'date_order': fields.Datetime.to_datetime(rec.date),
                'x_requisition_title': _('Retrospective approval: %s') % rec.name,
                'order_line': [(0, 0, {'name': line['memo'], 'product_qty': 1, 'product_id': product.id, 'product_uom_id': product.uom_id.id,
                    'price_unit': line['amount'], 'date_planned': fields.Datetime.to_datetime(rec.date),
                    'tax_ids': [(5, 0, 0)], 'x_elks_account_id': line['account'].id}) for line in lines]})
            rec.purchase_id = po
            po.message_post(body=_('This charge has already been recorded in QuickBooks. Retrospective approval is required; do not pay it again.'))
            po.action_submit()
            rec.review_reason = False

    def action_record_frs(self):
        self._manager()
        self._require_purchase_workflow()
        self._lock()
        for rec in self:
            if rec.journal_id:
                continue
            if rec.source_changed or rec.source_missing:
                raise UserError(_('Resolve the source exception first.'))
            # Base FRS journals/COA lack company isolation. Fail closed for multi-company databases.
            if self.env['res.company'].sudo().search_count([]) != 1:
                raise UserError(_('FRS posting is limited to single-company databases until the base FRS ledger supports company isolation.'))
            lines, payment = rec._accounting_lines()
            po = rec.purchase_id
            if not po or po.x_approval_state != 'approved' or not po.x_all_officers_signed or po.state == 'cancel':
                raise UserError(_('Board/Floor approval and all officer signatures are required.'))
            # Serialize different charges attempting to consume the same PO.
            self.env.cr.execute('SELECT id FROM purchase_order WHERE id=%s FOR UPDATE', [po.id])
            if not rec._po_compatible(po, lines):
                raise UserError(_('The PO total and FRS allocations must exactly match this expense. Partial or combined payments require manual review.'))
            if self.search_count([('purchase_id', '=', po.id), ('id', '!=', rec.id), ('journal_id', '!=', False)]):
                raise UserError(_('Another recorded charge already consumes this PO.'))
            if po.order_line.filtered(lambda l: (l.x_ordered or l.x_journal_entry_id) and (not rec.existing_journal_id or l.x_journal_entry_id != rec.existing_journal_id)):
                raise UserError(_('This PO already has ordered lines or FRS journals. Reconcile the existing entry manually to avoid a duplicate expense.'))
            if (payment.is_restricted or any(line['account'].is_restricted for line in lines)) and not rec.restricted_purpose:
                raise UserError(_('Document the restricted fund purpose before recording.'))
            # Historical charges must use their historical approved budget.
            fye = rec.date.replace(year=rec.date.year + (rec.date.month >= 4), month=3, day=31)
            for account in {line['account'] for line in lines}:
                budgets = self.env['elks.budget.line'].search([
                    ('account_id', '=', account.id), ('budget_id.fiscal_year_end', '=', fye),
                    ('budget_id.state', 'in', ['floor_approved', 'submitted'])])
                if len(budgets) != 1:
                    raise UserError(_('A unique floor-approved budget line is required for every allocation.'))
                self.env.cr.execute('SELECT id FROM elks_budget_line WHERE id=%s FOR UPDATE', [budgets.id])
                budgets.invalidate_recordset()
                start = fye.replace(year=fye.year-1, month=4, day=1)
                actuals = self.env['elks.journal.entry.line'].search([
                    ('account_id', '=', account.id), ('entry_state', '=', 'posted'),
                    ('date', '>=', start), ('date', '<=', fye)])
                actual = sum(actuals.mapped('debit')) - sum(actuals.mapped('credit'))
                commitments = self.env['purchase.order.line'].search([
                    ('x_elks_account_id', '=', account.id), ('order_id.x_approval_state', '=', 'approved'),
                    ('order_id.state', '!=', 'cancel'), ('order_id.date_order', '>=', str(start)),
                    ('order_id.date_order', '<', str(fye.replace(month=4, day=1))),
                    ('x_ordered', '=', False), ('order_id', '!=', po.id)])
                pending = sum(commitments.mapped('price_subtotal'))
                charge = 0 if rec.existing_journal_id else sum(line['amount'] for line in lines if line['account'] == account)
                if rec.currency_id.compare_amounts(actual + pending + charge, budgets.amount) > 0:
                    raise UserError(_('Approve the budget amendment or transfer before recording this expense.'))
            entries = [(0, 0, {'account_id': line['account'].id, 'debit': line['amount'], 'memo': line['memo']}) for line in lines]
            entries.append((0, 0, {'account_id': payment.id, 'credit': rec.amount, 'memo': rec.name}))
            expected = defaultdict(lambda: [0.0, 0.0])
            for command in entries:
                vals = command[2]
                expected[vals['account_id']][0] += vals.get('debit', 0)
                expected[vals['account_id']][1] += vals.get('credit', 0)
            def matches(entry):
                if entry.state != 'posted' or entry.date != rec.date or entry.currency_id != rec.currency_id:
                    return False
                actual = defaultdict(lambda: [0.0, 0.0])
                for line in entry.line_ids:
                    actual[line.account_id.id][0] += line.debit
                    actual[line.account_id.id][1] += line.credit
                return set(actual) == set(expected) and all(
                    not rec.currency_id.compare_amounts(actual[k][i], expected[k][i]) for k in expected for i in (0, 1))
            if rec.existing_journal_id:
                journal = rec.existing_journal_id
                self.env.cr.execute('SELECT id FROM elks_journal_entry WHERE id=%s FOR UPDATE', [journal.id])
                journal.invalidate_recordset()
                if not matches(journal):
                    raise UserError(_('The existing posted FRS entry must match the charge date, currency, and all debit/credit allocations.'))
                if self.search_count([('journal_id', '=', journal.id), ('id', '!=', rec.id)]):
                    raise UserError(_('That FRS entry is already linked to another charge.'))
            else:
                possible = self.env['elks.journal.entry'].search([('date', '=', rec.date), ('state', '=', 'posted')])
                if any(matches(entry) for entry in possible):
                    raise UserError(_('A matching FRS entry already exists. Select it under Reconcile Existing FRS Entry.'))
                journal = self.env['elks.journal.entry'].create({'entry_number': f'QBO-{rec.connection_id.id}-Purchase-{rec.qbo_id}',
                    'date': rec.date, 'memo': rec.name + ' / ' + po.name, 'currency_id': rec.currency_id.id, 'line_ids': entries})
                journal.action_post()
            rec.journal_id = journal
            po.order_line.filtered(lambda l: not l.display_type).write({
                'x_ordered': True, 'x_ordered_date': rec.date, 'x_journal_entry_id': journal.id})
            rec.review_reason = False
            rec.message_post(body=_('FRS entry recorded; PO lines now reference the same entry. No new payment was issued.'))
