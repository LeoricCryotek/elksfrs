# -*- coding: utf-8 -*-
"""Budget Amendments and Transfers.

Budget Transfers move money between existing budget lines (same total).
Budget Amendments increase (or decrease) the overall budget and require
approval via a floor vote before taking effect.

Both are only allowed on approved (locked) budgets.
"""
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError


class ElksBudgetTransfer(models.Model):
    """Move budgeted dollars from one line to another within the same budget."""

    _name = "elks.budget.transfer"
    _description = "Budget Line Transfer"
    _order = "transfer_date desc, id desc"
    _inherit = ["mail.thread"]

    name = fields.Char(
        "Reference", readonly=True, default=lambda self: _("New"),
        copy=False,
    )
    budget_id = fields.Many2one(
        "elks.budget", string="Budget", required=True,
        domain="[('state', 'in', ('board_approved', 'floor_approved'))]",
        tracking=True,
    )
    transfer_date = fields.Date(
        "Transfer Date", required=True,
        default=fields.Date.context_today,
    )
    state = fields.Selection([
        ('draft', 'Draft'),
        ('done', 'Completed'),
        ('cancelled', 'Cancelled'),
    ], default='draft', tracking=True)

    from_line_id = fields.Many2one(
        "elks.budget.line", string="From Budget Line", required=True,
        domain="[('budget_id', '=', budget_id)]",
        help="The budget line to take money from.",
    )
    to_line_id = fields.Many2one(
        "elks.budget.line", string="To Budget Line", required=True,
        domain="[('budget_id', '=', budget_id)]",
        help="The budget line to receive the transferred amount.",
    )
    amount = fields.Monetary(
        "Transfer Amount", required=True,
        currency_field='currency_id',
    )
    currency_id = fields.Many2one(
        related="budget_id.currency_id",
        store=True,
    )
    reason = fields.Text(
        "Reason for Transfer", required=True,
        help="Explain why this transfer is needed.",
    )
    requested_by = fields.Many2one(
        "res.users", string="Requested By",
        default=lambda self: self.env.user,
    )

    @api.constrains("from_line_id", "to_line_id")
    def _check_different_lines(self):
        for rec in self:
            if rec.from_line_id and rec.to_line_id and rec.from_line_id == rec.to_line_id:
                raise ValidationError(_(
                    "Source and destination budget lines must be different."
                ))

    @api.constrains("amount")
    def _check_positive_amount(self):
        for rec in self:
            if rec.amount <= 0:
                raise ValidationError(_("Transfer amount must be positive."))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'elks.budget.transfer'
                ) or _('New')
        return super().create(vals_list)

    def action_confirm(self):
        """Execute the transfer: decrease source line, increase dest line."""
        for rec in self:
            if rec.state != 'draft':
                raise UserError(_("Only draft transfers can be confirmed."))
            if rec.budget_id.state not in ('board_approved', 'floor_approved'):
                raise UserError(_(
                    "Transfers are only allowed on approved budgets."
                ))
            if rec.amount > rec.from_line_id.amount:
                raise UserError(_(
                    "Cannot transfer %(amt)s from %(acct)s — "
                    "only %(avail)s is budgeted.",
                    amt=rec.amount,
                    acct=rec.from_line_id.account_id.display_name,
                    avail=rec.from_line_id.amount,
                ))

            rec.from_line_id.sudo().write({
                'amount': rec.from_line_id.amount - rec.amount,
            })
            rec.to_line_id.sudo().write({
                'amount': rec.to_line_id.amount + rec.amount,
            })
            rec.state = 'done'
            rec.budget_id.message_post(
                body=_(
                    "Budget transfer %(ref)s: %(amt)s moved from "
                    "%(from_acct)s to %(to_acct)s. Reason: %(reason)s",
                    ref=rec.name,
                    amt=rec.amount,
                    from_acct=rec.from_line_id.account_id.display_name,
                    to_acct=rec.to_line_id.account_id.display_name,
                    reason=rec.reason,
                ),
            )

    def action_cancel(self):
        for rec in self:
            if rec.state == 'done':
                raise UserError(_(
                    "Cannot cancel a completed transfer.  "
                    "Create a reverse transfer instead."
                ))
            rec.state = 'cancelled'


class ElksBudgetAmendment(models.Model):
    """Propose a change to the overall budget that requires floor vote approval.

    Amendments increase (or decrease) one or more budget lines beyond what
    was originally approved.  They go through: Draft → Proposed → Voted →
    Approved/Rejected.  Only approved amendments modify the budget.
    """

    _name = "elks.budget.amendment"
    _description = "Budget Amendment"
    _order = "create_date desc"
    _inherit = ["mail.thread"]

    name = fields.Char(
        "Reference", readonly=True, default=lambda self: _("New"),
        copy=False,
    )
    budget_id = fields.Many2one(
        "elks.budget", string="Budget", required=True,
        domain="[('state', 'in', ('board_approved', 'floor_approved'))]",
        tracking=True,
    )
    state = fields.Selection([
        ('draft', 'Draft'),
        ('proposed', 'Proposed to Floor'),
        ('approved', 'Approved (Voted)'),
        ('rejected', 'Rejected'),
        ('cancelled', 'Cancelled'),
    ], default='draft', tracking=True)

    title = fields.Char(
        "Amendment Title", required=True,
        help="Short description of what this amendment does.",
    )
    justification = fields.Text(
        "Justification", required=True,
        help="Detailed explanation for why this amendment is needed "
             "and what it will fund.",
    )
    proposed_by = fields.Many2one(
        "res.users", string="Proposed By",
        default=lambda self: self.env.user,
    )
    proposed_date = fields.Date("Date Proposed")
    vote_date = fields.Date("Floor Vote Date")
    vote_result = fields.Selection([
        ('passed', 'Passed'),
        ('failed', 'Failed'),
    ], string="Vote Result", tracking=True)
    vote_notes = fields.Text(
        "Vote Notes",
        help="Minutes / notes from the floor vote (e.g. vote count).",
    )

    line_ids = fields.One2many(
        "elks.budget.amendment.line", "amendment_id",
        string="Amendment Lines",
    )

    total_change = fields.Monetary(
        "Total Budget Change", compute="_compute_total_change",
        store=True, currency_field='currency_id',
        help="Net change to the budget (positive = increase spending).",
    )
    currency_id = fields.Many2one(
        related="budget_id.currency_id",
        store=True,
    )

    @api.depends("line_ids.change_amount")
    def _compute_total_change(self):
        for rec in self:
            rec.total_change = sum(rec.line_ids.mapped('change_amount'))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'elks.budget.amendment'
                ) or _('New')
        return super().create(vals_list)

    def action_propose(self):
        """Submit the amendment for floor vote consideration."""
        for rec in self:
            if rec.state != 'draft':
                raise UserError(_("Only draft amendments can be proposed."))
            if not rec.line_ids:
                raise UserError(_("Add at least one amendment line."))
            rec.write({
                'state': 'proposed',
                'proposed_date': fields.Date.context_today(self),
            })
            rec.budget_id.message_post(
                body=_(
                    "Budget amendment %(ref)s proposed: %(title)s "
                    "(%(amt)s net change). Awaiting floor vote.",
                    ref=rec.name,
                    title=rec.title,
                    amt=rec.total_change,
                ),
            )

    def action_record_vote_passed(self):
        """Record that the floor vote passed and apply the amendment."""
        for rec in self:
            if rec.state != 'proposed':
                raise UserError(_("Only proposed amendments can be voted on."))

            rec.write({
                'state': 'approved',
                'vote_date': fields.Date.context_today(self),
                'vote_result': 'passed',
            })

            # Apply changes to budget lines
            for aline in rec.line_ids:
                budget_line = self.env['elks.budget.line'].search([
                    ('budget_id', '=', rec.budget_id.id),
                    ('account_id', '=', aline.account_id.id),
                ], limit=1)

                if budget_line:
                    budget_line.sudo().write({
                        'amount': budget_line.amount + aline.change_amount,
                        'note': (budget_line.note or '') +
                                f" [Amended {rec.name}: {aline.change_amount:+.2f}]",
                    })
                else:
                    # Create new budget line if account didn't have one
                    self.env['elks.budget.line'].sudo().create({
                        'budget_id': rec.budget_id.id,
                        'account_id': aline.account_id.id,
                        'amount': aline.change_amount,
                        'note': f"Added by amendment {rec.name}",
                    })

            rec.budget_id.message_post(
                body=_(
                    "Budget amendment %(ref)s APPROVED by floor vote. "
                    "%(count)d budget lines updated, net change: %(amt)s.",
                    ref=rec.name,
                    count=len(rec.line_ids),
                    amt=rec.total_change,
                ),
            )

    def action_record_vote_failed(self):
        """Record that the floor vote rejected the amendment."""
        for rec in self:
            if rec.state != 'proposed':
                raise UserError(_("Only proposed amendments can be voted on."))
            rec.write({
                'state': 'rejected',
                'vote_date': fields.Date.context_today(self),
                'vote_result': 'failed',
            })
            rec.budget_id.message_post(
                body=_(
                    "Budget amendment %(ref)s REJECTED by floor vote.",
                    ref=rec.name,
                ),
            )

    def action_cancel(self):
        for rec in self:
            if rec.state == 'approved':
                raise UserError(_(
                    "Cannot cancel an approved amendment.  "
                    "Create a reversing amendment instead."
                ))
            rec.state = 'cancelled'


class ElksBudgetAmendmentLine(models.Model):
    """Individual line within a budget amendment."""

    _name = "elks.budget.amendment.line"
    _description = "Budget Amendment Line"

    amendment_id = fields.Many2one(
        "elks.budget.amendment", string="Amendment",
        required=True, ondelete="cascade",
    )
    account_id = fields.Many2one(
        "elks.account", string="Account", required=True,
        domain="[('is_header', '=', False)]",
    )
    current_budget = fields.Monetary(
        "Current Budget", compute="_compute_current_budget",
        currency_field='currency_id',
        help="Current budgeted amount for this account.",
    )
    change_amount = fields.Monetary(
        "Change Amount", required=True,
        currency_field='currency_id',
        help="Amount to add (positive) or remove (negative) from this line.",
    )
    new_budget = fields.Monetary(
        "New Budget", compute="_compute_new_budget",
        currency_field='currency_id',
    )
    currency_id = fields.Many2one(
        related="amendment_id.currency_id",
        store=True,
    )
    note = fields.Char("Note")

    @api.depends("amendment_id.budget_id", "account_id")
    def _compute_current_budget(self):
        for rec in self:
            if rec.amendment_id.budget_id and rec.account_id:
                bl = self.env['elks.budget.line'].search([
                    ('budget_id', '=', rec.amendment_id.budget_id.id),
                    ('account_id', '=', rec.account_id.id),
                ], limit=1)
                rec.current_budget = bl.amount if bl else 0.0
            else:
                rec.current_budget = 0.0

    @api.depends("current_budget", "change_amount")
    def _compute_new_budget(self):
        for rec in self:
            rec.new_budget = rec.current_budget + rec.change_amount
