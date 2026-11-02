from odoo import fields, models, _
from odoo.exceptions import UserError


class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'
    qbo_expense_ids = fields.One2many('elks.qbo.expense', 'purchase_id', string='QuickBooks Charges')

    qbo_workflow_available = fields.Boolean(compute='_compute_qbo_workflow_available')

    def _compute_qbo_workflow_available(self):
        available = 'x_approval_state' in self._fields
        for order in self:
            order.qbo_workflow_available = available

    def _check_qbo_ordering(self):
        if self.env['elks.qbo.expense'].sudo().search_count([('purchase_id', 'in', self.ids)]):
            raise UserError(_('This PO has a QuickBooks charge. Record or reconcile it from the charge to avoid duplicate FRS expenses.'))

    def action_match_qbo_charge(self):
        self.ensure_one()
        if not self.qbo_workflow_available:
            raise UserError(_('Install Elks Lodge Purchase Requests to enable approval and charge matching.'))
        return {'type': 'ir.actions.act_window', 'name': _('Match QuickBooks Charge'),
                'res_model': 'elks.qbo.match', 'view_mode': 'form', 'target': 'new',
                'context': {'default_purchase_id': self.id}}

