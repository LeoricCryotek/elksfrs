# -*- coding: utf-8 -*-
"""Wizard to match an Elks Journal Entry to a purchase.order.

Lets the user filter POs by vendor, total, and date range, then link one
to the journal entry for audit-trail purposes. The amount filter is
tolerance-based (default $0.01) since QBO totals occasionally include
sales tax or rounding not present on the PO.
"""
from odoo import api, fields, models, _
from odoo.exceptions import UserError


class JournalPoMatchWizard(models.TransientModel):
    _name = "elks.journal.po.match.wizard"
    _description = "Match Journal Entry to Purchase Order"

    entry_id = fields.Many2one(
        "elks.journal.entry", string="Journal Entry",
        required=True, readonly=True,
    )
    entry_date = fields.Date(related="entry_id.date", readonly=True)
    entry_memo = fields.Text(related="entry_id.memo", readonly=True)
    amount = fields.Float("Target Amount", digits=(16, 2))
    tolerance = fields.Float(
        "Tolerance ($)", default=5.00, digits=(16, 2),
        help="How close the PO total must be to the target amount. "
             "Default $5 absorbs sales tax, delivery fees, or rounding "
             "the PO didn't account for. Tighten to $0.01 for exact "
             "matches.",
    )
    partner_id = fields.Many2one(
        "res.partner", string="Vendor",
        domain=[('supplier_rank', '>', 0)],
    )
    date_from = fields.Date("PO Date From")
    date_to = fields.Date("PO Date To")
    only_open = fields.Boolean(
        "Hide already-matched POs", default=True,
        help="Exclude POs that are already linked to another journal entry.",
    )

    candidate_ids = fields.Many2many(
        "purchase.order", string="Candidate POs",
        compute="_compute_candidate_ids",
    )
    purchase_id = fields.Many2one(
        "purchase.order", string="Select PO to Link",
    )

    @api.depends("amount", "tolerance", "partner_id",
                 "date_from", "date_to", "only_open")
    def _compute_candidate_ids(self):
        PO = self.env["purchase.order"]
        for rec in self:
            domain = [('state', 'in', ('purchase', 'done', 'draft', 'sent'))]
            if rec.partner_id:
                domain.append(('partner_id', '=', rec.partner_id.id))
            if rec.date_from:
                domain.append(('date_order', '>=', rec.date_from))
            if rec.date_to:
                domain.append(('date_order', '<=', rec.date_to))
            if rec.amount:
                tol = rec.tolerance or 0.01
                domain += [
                    ('amount_total', '>=', rec.amount - tol),
                    ('amount_total', '<=', rec.amount + tol),
                ]
            pos = PO.search(domain, limit=50, order="date_order desc")
            if rec.only_open:
                linked = self.env["elks.journal.entry"].search([
                    ('purchase_id', 'in', pos.ids),
                    ('id', '!=', rec.entry_id.id),
                ]).mapped('purchase_id').ids
                pos = pos.filtered(lambda p: p.id not in linked)
            rec.candidate_ids = pos

    def action_link(self):
        self.ensure_one()
        if not self.purchase_id:
            raise UserError(_("Pick a PO from the candidates below first."))
        self.entry_id.purchase_id = self.purchase_id
        self.entry_id.message_post(body=_(
            "Linked to PO %s (%s, $%.2f) via match wizard."
        ) % (self.purchase_id.name,
             self.purchase_id.partner_id.display_name or '',
             self.purchase_id.amount_total or 0.0))
        return {'type': 'ir.actions.act_window_close'}

    def action_refresh(self):
        # Form reloads automatically on save of a transient; this just
        # gives the user a visible button to re-run the search.
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
            'context': self.env.context,
        }
