from odoo import fields, models, _
from odoo.exceptions import UserError


class MatchCharge(models.TransientModel):
    _name = 'elks.qbo.match'
    _description = 'Match an existing QuickBooks charge to a PO'
    purchase_id = fields.Many2one('purchase.order', required=True, readonly=True)
    company_id = fields.Many2one(related='purchase_id.company_id')
    partner_id = fields.Many2one(related='purchase_id.partner_id')
    expense_id = fields.Many2one('elks.qbo.expense', required=True,
                                domain="[('company_id', '=', company_id), ('partner_id', '=', partner_id), ('journal_id', '=', False)]")

    def action_match(self):
        self.ensure_one()
        self.expense_id._manager()
        if self.expense_id.journal_id:
            raise UserError(_('This charge is already recorded.'))
        self.expense_id.purchase_id = self.purchase_id
        self.expense_id.action_recheck()
        return {'type': 'ir.actions.act_window_close'}
