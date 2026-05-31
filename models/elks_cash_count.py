# -*- coding: utf-8 -*-
"""Cash Management — Counts.

A count is a denomination-level snapshot of the physical cash in a
single location (Bank, Till, Bag, or Event Till) at a point in time.

The picture-perfect simple form: a column of bill quantities, a column
of coin quantities, subtotals, and a grand total.  No checks, no
variance-against-expected magic, no aggregation from sub-lines — just
what's physically there right now.

Counts are the *only* required digital record in this cash-management
workflow.  Everything else (change requests, change orders, till
deposits) is paper-first; counts get typed in from the printed slip.
"""
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from .elks_cash_location import DENOM_NAMES, DENOM_FACE, DENOM_LABEL


class ElksCashCount(models.Model):
    _name = 'elks.cash.count'
    _description = 'Cash Count'
    _order = 'count_date desc, id desc'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------
    name = fields.Char(
        'Reference', compute='_compute_name', store=True,
    )
    location_id = fields.Many2one(
        'elks.cash.location', string='Location',
        required=True, ondelete='restrict', tracking=True, index=True,
    )
    location_type = fields.Selection(
        related='location_id.location_type', store=True, readonly=True,
    )
    is_bank = fields.Boolean(
        related='location_id.is_bank', store=True, readonly=True,
    )

    count_date = fields.Datetime(
        'Count Date', required=True, default=fields.Datetime.now,
        tracking=True, index=True,
    )
    count_type = fields.Selection([
        ('weekly_bank', 'Weekly Bank Count'),
        ('shift_close', 'Shift Close'),
        ('event_close', 'Event Close'),
        ('audit', 'Audit'),
        ('other', 'Other'),
    ], string='Count Type', required=True, default='shift_close',
       tracking=True, index=True,
    )

    state = fields.Selection([
        ('draft', 'Draft'),
        ('done', 'Done'),
        ('cancelled', 'Cancelled'),
    ], string='Status', default='draft', tracking=True, index=True, copy=False)

    # ------------------------------------------------------------------
    # People
    # ------------------------------------------------------------------
    counted_by_id = fields.Many2one(
        'res.users', string='Counted By', required=True,
        default=lambda self: self.env.user, tracking=True,
    )
    witnessed_by_id = fields.Many2one(
        'res.users', string='Witnessed By', tracking=True,
        help="Optional second-party witness for audit hygiene.",
    )

    # ------------------------------------------------------------------
    # Pre-printed slip serial (from ir.sequence)
    # ------------------------------------------------------------------
    slip_number = fields.Char(
        'Slip Serial', copy=False, index=True, tracking=True,
        help="Pre-printed serial on the paper count slip. "
             "Auto-generated on create; can be overridden to match a "
             "hand-printed slip.",
    )

    # ------------------------------------------------------------------
    # Denomination quantities (direct entry)
    # ------------------------------------------------------------------
    qty_hundreds = fields.Integer('$100 Bills', default=0, tracking=True)
    qty_fifties = fields.Integer('$50 Bills', default=0, tracking=True)
    qty_twenties = fields.Integer('$20 Bills', default=0, tracking=True)
    qty_tens = fields.Integer('$10 Bills', default=0, tracking=True)
    qty_fives = fields.Integer('$5 Bills', default=0, tracking=True)
    qty_twos = fields.Integer('$2 Bills', default=0, tracking=True)
    qty_ones = fields.Integer('$1 Bills', default=0, tracking=True)
    qty_dollar_coins = fields.Integer('Dollar Coins ($1)', default=0, tracking=True)
    qty_half_dollars = fields.Integer('Half Dollars (50¢)', default=0, tracking=True)
    qty_quarters = fields.Integer('Quarters (25¢)', default=0, tracking=True)
    qty_dimes = fields.Integer('Dimes (10¢)', default=0, tracking=True)
    qty_nickels = fields.Integer('Nickels (5¢)', default=0, tracking=True)
    qty_pennies = fields.Integer('Pennies (1¢)', default=0, tracking=True)

    # ------------------------------------------------------------------
    # Subtotals (computed)
    # ------------------------------------------------------------------
    sub_hundreds = fields.Monetary(compute='_compute_subtotals', store=True,
                                   currency_field='currency_id')
    sub_fifties = fields.Monetary(compute='_compute_subtotals', store=True,
                                  currency_field='currency_id')
    sub_twenties = fields.Monetary(compute='_compute_subtotals', store=True,
                                   currency_field='currency_id')
    sub_tens = fields.Monetary(compute='_compute_subtotals', store=True,
                               currency_field='currency_id')
    sub_fives = fields.Monetary(compute='_compute_subtotals', store=True,
                                currency_field='currency_id')
    sub_twos = fields.Monetary(compute='_compute_subtotals', store=True,
                               currency_field='currency_id')
    sub_ones = fields.Monetary(compute='_compute_subtotals', store=True,
                               currency_field='currency_id')
    sub_dollar_coins = fields.Monetary(compute='_compute_subtotals', store=True,
                                       currency_field='currency_id')
    sub_half_dollars = fields.Monetary(compute='_compute_subtotals', store=True,
                                       currency_field='currency_id')
    sub_quarters = fields.Monetary(compute='_compute_subtotals', store=True,
                                   currency_field='currency_id')
    sub_dimes = fields.Monetary(compute='_compute_subtotals', store=True,
                                currency_field='currency_id')
    sub_nickels = fields.Monetary(compute='_compute_subtotals', store=True,
                                  currency_field='currency_id')
    sub_pennies = fields.Monetary(compute='_compute_subtotals', store=True,
                                  currency_field='currency_id')

    total_bills = fields.Monetary(
        'Total Bills', compute='_compute_subtotals', store=True,
        currency_field='currency_id',
    )
    total_coins = fields.Monetary(
        'Total Coins', compute='_compute_subtotals', store=True,
        currency_field='currency_id',
    )
    total_cash = fields.Monetary(
        'Total Cash', compute='_compute_subtotals', store=True,
        currency_field='currency_id', tracking=True,
    )

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------
    notes = fields.Text('Notes')
    currency_id = fields.Many2one(
        'res.currency', default=lambda self: self.env.company.currency_id,
    )

    # ==================================================================
    # Defaults / lifecycle
    # ==================================================================
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('slip_number'):
                vals['slip_number'] = self.env['ir.sequence'].next_by_code(
                    'elks.cash.count.slip'
                ) or '/'
        return super().create(vals_list)

    @api.depends('location_id', 'count_date', 'slip_number')
    def _compute_name(self):
        for rec in self:
            parts = []
            if rec.location_id:
                parts.append(rec.location_id.code or rec.location_id.name)
            if rec.count_date:
                parts.append(fields.Datetime.to_string(rec.count_date)[:16])
            label = " ".join(parts) if parts else (rec.slip_number or "New")
            rec.name = label

    _BILL_DENOMS = ('hundreds', 'fifties', 'twenties', 'tens', 'fives',
                    'twos', 'ones')
    _COIN_DENOMS = ('dollar_coins', 'half_dollars', 'quarters',
                    'dimes', 'nickels', 'pennies')

    @api.depends(*[f'qty_{d}' for d in DENOM_NAMES])
    def _compute_subtotals(self):
        for rec in self:
            bills_total = 0.0
            coins_total = 0.0
            for denom in DENOM_NAMES:
                qty = getattr(rec, f'qty_{denom}', 0) or 0
                sub = qty * DENOM_FACE[denom]
                setattr(rec, f'sub_{denom}', sub)
                if denom in rec._BILL_DENOMS:
                    bills_total += sub
                else:
                    coins_total += sub
            rec.total_bills = bills_total
            rec.total_coins = coins_total
            rec.total_cash = bills_total + coins_total

    # ==================================================================
    # State transitions
    # ==================================================================
    def action_done(self):
        for rec in self:
            if rec.state == 'cancelled':
                raise ValidationError(_(
                    "Cannot finalize a cancelled count. "
                    "Reset to draft first."
                ))
            rec.state = 'done'
        return True

    def action_draft(self):
        for rec in self:
            rec.state = 'draft'
        return True

    def action_cancel(self):
        for rec in self:
            rec.state = 'cancelled'
        return True

    # ==================================================================
    # Reports
    # ==================================================================
    def action_print_count_slip(self):
        """Print the count slip (filled if data exists, blank if not)."""
        self.ensure_one()
        return self.env.ref(
            'elksfrs.action_report_cash_count_slip'
        ).report_action(self)

    @api.model
    def action_print_blank_count_slip(self):
        """Create a draft count with zero quantities, print, then discard.

        For printing a stack of blanks to leave at tills/bags.
        """
        # Caller passes default_location_id in context for a specific location.
        location_id = self.env.context.get('default_location_id')
        draft = self.create({
            'location_id': location_id or self.env['elks.cash.location'].search(
                [('location_type', '=', 'bank')], limit=1).id,
            'count_type': 'other',
            'notes': "Blank slip — placeholder, do not finalize.",
        })
        action = draft.action_print_count_slip()
        # Mark as cancelled so it doesn't pollute the active list.
        draft.action_cancel()
        return action
