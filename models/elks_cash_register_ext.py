# -*- coding: utf-8 -*-
"""Extend Cash On Hand with register-based counting.

Adds One2many relationships to register count lines and transfers,
overrides the denomination qty fields as computed aggregates from
register lines, and provides the deposit / change-order denomination
breakdown for the Treasurer Report.
"""
from odoo import api, fields, models

# Shared denomination map
_DENOM_MAP = [
    ('pennies',      0.01),
    ('nickels',      0.05),
    ('dimes',        0.10),
    ('quarters',     0.25),
    ('half_dollars', 0.50),
    ('dollar_coins', 1.00),
    ('ones',         1),
    ('twos',         2),
    ('fives',        5),
    ('tens',         10),
    ('twenties',     20),
    ('fifties',      50),
    ('hundreds',     100),
]
_DENOM_NAMES = [d for d, _ in _DENOM_MAP]


class ElksCashOnHandRegister(models.Model):
    _inherit = 'elks.cash.on.hand'

    # ------------------------------------------------------------------
    # One2many to register lines and transfers
    # ------------------------------------------------------------------
    register_line_ids = fields.One2many(
        'elks.register.count.line', 'cash_on_hand_id',
        string='Register Counts',
        help="Count each register bag separately.")
    transfer_ids = fields.One2many(
        'elks.register.transfer', 'cash_on_hand_id',
        string='Register Transfers',
        help="Record cash movements between registers to rebalance tills.")

    register_count = fields.Integer(
        'Registers Counted', compute='_compute_register_count', store=True)
    total_starter_banks = fields.Monetary(
        'Total Starter Banks', compute='_compute_register_count',
        store=True, currency_field='currency_id',
        help="Combined starter-bank value across all registers.")
    total_excess = fields.Monetary(
        'Total Excess (Revenue)', compute='_compute_register_count',
        store=True, currency_field='currency_id',
        help="Combined excess across all registers — the revenue collected.")

    # ------------------------------------------------------------------
    # Override qty_* fields as computed aggregates from register lines
    # ------------------------------------------------------------------
    qty_pennies = fields.Integer(
        'Pennies (1¢)', compute='_compute_aggregated_qty', store=True)
    qty_nickels = fields.Integer(
        'Nickels (5¢)', compute='_compute_aggregated_qty', store=True)
    qty_dimes = fields.Integer(
        'Dimes (10¢)', compute='_compute_aggregated_qty', store=True)
    qty_quarters = fields.Integer(
        'Quarters (25¢)', compute='_compute_aggregated_qty', store=True)
    qty_half_dollars = fields.Integer(
        'Half Dollars (50¢)', compute='_compute_aggregated_qty', store=True)
    qty_dollar_coins = fields.Integer(
        'Dollar Coins ($1)', compute='_compute_aggregated_qty', store=True)
    qty_ones = fields.Integer(
        '$1 Bills', compute='_compute_aggregated_qty', store=True)
    qty_twos = fields.Integer(
        '$2 Bills', compute='_compute_aggregated_qty', store=True)
    qty_fives = fields.Integer(
        '$5 Bills', compute='_compute_aggregated_qty', store=True)
    qty_tens = fields.Integer(
        '$10 Bills', compute='_compute_aggregated_qty', store=True)
    qty_twenties = fields.Integer(
        '$20 Bills', compute='_compute_aggregated_qty', store=True)
    qty_fifties = fields.Integer(
        '$50 Bills', compute='_compute_aggregated_qty', store=True)
    qty_hundreds = fields.Integer(
        '$100 Bills', compute='_compute_aggregated_qty', store=True)

    # ------------------------------------------------------------------
    # Deposit denomination breakdown (excess above lodge par → bank)
    # ------------------------------------------------------------------
    deposit_pennies = fields.Integer(compute='_compute_deposit_breakdown', store=True)
    deposit_nickels = fields.Integer(compute='_compute_deposit_breakdown', store=True)
    deposit_dimes = fields.Integer(compute='_compute_deposit_breakdown', store=True)
    deposit_quarters = fields.Integer(compute='_compute_deposit_breakdown', store=True)
    deposit_half_dollars = fields.Integer(compute='_compute_deposit_breakdown', store=True)
    deposit_dollar_coins = fields.Integer(compute='_compute_deposit_breakdown', store=True)
    deposit_ones = fields.Integer(compute='_compute_deposit_breakdown', store=True)
    deposit_twos = fields.Integer(compute='_compute_deposit_breakdown', store=True)
    deposit_fives = fields.Integer(compute='_compute_deposit_breakdown', store=True)
    deposit_tens = fields.Integer(compute='_compute_deposit_breakdown', store=True)
    deposit_twenties = fields.Integer(compute='_compute_deposit_breakdown', store=True)
    deposit_fifties = fields.Integer(compute='_compute_deposit_breakdown', store=True)
    deposit_hundreds = fields.Integer(compute='_compute_deposit_breakdown', store=True)

    deposit_total_denom = fields.Monetary(
        'Deposit Total', compute='_compute_deposit_breakdown',
        store=True, currency_field='currency_id',
        help="Total dollar value to hand to the treasurer for bank deposit.")

    # ------------------------------------------------------------------
    # Change-order denomination breakdown (below lodge par → request)
    # ------------------------------------------------------------------
    change_pennies = fields.Integer(compute='_compute_deposit_breakdown', store=True)
    change_nickels = fields.Integer(compute='_compute_deposit_breakdown', store=True)
    change_dimes = fields.Integer(compute='_compute_deposit_breakdown', store=True)
    change_quarters = fields.Integer(compute='_compute_deposit_breakdown', store=True)
    change_half_dollars = fields.Integer(compute='_compute_deposit_breakdown', store=True)
    change_dollar_coins = fields.Integer(compute='_compute_deposit_breakdown', store=True)
    change_ones = fields.Integer(compute='_compute_deposit_breakdown', store=True)
    change_twos = fields.Integer(compute='_compute_deposit_breakdown', store=True)
    change_fives = fields.Integer(compute='_compute_deposit_breakdown', store=True)
    change_tens = fields.Integer(compute='_compute_deposit_breakdown', store=True)
    change_twenties = fields.Integer(compute='_compute_deposit_breakdown', store=True)
    change_fifties = fields.Integer(compute='_compute_deposit_breakdown', store=True)
    change_hundreds = fields.Integer(compute='_compute_deposit_breakdown', store=True)

    change_total_denom = fields.Monetary(
        'Change Order Total', compute='_compute_deposit_breakdown',
        store=True, currency_field='currency_id',
        help="Total dollar value to request back from the bank as change.")

    # ------------------------------------------------------------------
    # Computations
    # ------------------------------------------------------------------
    @api.depends('register_line_ids', 'register_line_ids.counted_total',
                 'register_line_ids.starter_total',
                 'register_line_ids.excess_total')
    def _compute_register_count(self):
        for rec in self:
            lines = rec.register_line_ids
            rec.register_count = len(lines)
            rec.total_starter_banks = sum(lines.mapped('starter_total'))
            rec.total_excess = sum(lines.mapped('excess_total'))

    @api.depends(
        *[f'register_line_ids.qty_{d}' for d in _DENOM_NAMES],
    )
    def _compute_aggregated_qty(self):
        for rec in self:
            for denom in _DENOM_NAMES:
                total = sum(rec.register_line_ids.mapped(f'qty_{denom}'))
                setattr(rec, f'qty_{denom}', total)

    @api.depends(
        *[f'register_line_ids.excess_{d}' for d in _DENOM_NAMES],
    )
    def _compute_deposit_breakdown(self):
        """Compare combined excess per denomination against lodge par levels.

        The lodge par represents total cash the lodge wants to retain.
        The combined register starter banks are kept in the tills.
        The excess above starter banks is revenue; compare that against
        lodge par to decide deposit vs change-order per denomination.

        For each denomination:
          excess > 0 → deposit that many
          excess < 0 → need that many in a change order
        But we also factor in the lodge par: the lodge may want to keep
        some of the excess on hand.

        Logic per denomination:
          total_excess = sum of all register excess_X
          keep_on_hand = lodge par_X  (how many the lodge wants in the safe)
          to_deposit = max(0, total_excess - keep_on_hand)
          change_needed = max(0, keep_on_hand - total_excess) if total_excess < keep_on_hand
        """
        settings = self.env['elks.lodge.settings'].sudo().search([], limit=1)
        for rec in self:
            deposit_total = 0.0
            change_total = 0.0
            for denom, face in _DENOM_MAP:
                total_excess = sum(rec.register_line_ids.mapped(f'excess_{denom}'))
                par = getattr(settings, f'par_{denom}', 0) if settings else 0
                # Excess above what the lodge wants to keep → deposit
                net = total_excess - par
                if net > 0:
                    setattr(rec, f'deposit_{denom}', net)
                    setattr(rec, f'change_{denom}', 0)
                    deposit_total += net * face
                elif net < 0:
                    setattr(rec, f'deposit_{denom}', 0)
                    setattr(rec, f'change_{denom}', abs(net))
                    change_total += abs(net) * face
                else:
                    setattr(rec, f'deposit_{denom}', 0)
                    setattr(rec, f'change_{denom}', 0)
            rec.deposit_total_denom = deposit_total
            rec.change_total_denom = change_total

    # ------------------------------------------------------------------
    # Report action
    # ------------------------------------------------------------------
    def action_print_treasurer_report(self):
        """Print the Treasurer Cash Report PDF."""
        return self.env.ref(
            'elksfrs.action_report_treasurer_cash'
        ).report_action(self)
