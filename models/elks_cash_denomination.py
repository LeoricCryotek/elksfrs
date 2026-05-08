# -*- coding: utf-8 -*-
"""Denomination-level counting for Cash On Hand.

Adds individual coin and bill quantity fields so the user enters how many
of each denomination they have.  Subtotals and the grand total are computed
automatically.  Replaces the old ``counted_cash`` / ``counted_checks`` /
``counted_other`` lump-sum approach.
"""
from odoo import api, fields, models


class ElksCashOnHandDenomination(models.Model):
    _inherit = 'elks.cash.on.hand'

    # ------------------------------------------------------------------
    # Extend count_type selection
    # ------------------------------------------------------------------
    count_type = fields.Selection(
        selection_add=[
            ('weekly_monday', 'Weekly Monday Count'),
            ('monthly', 'Monthly Count'),
            ('quarterly', 'Quarterly Count'),
        ],
        ondelete={
            'weekly_monday': 'set default',
            'monthly': 'set default',
            'quarterly': 'set default',
        },
    )

    # ------------------------------------------------------------------
    # COIN quantities (Integer — how many of each)
    # ------------------------------------------------------------------
    qty_pennies = fields.Integer('Pennies (1¢)', default=0)
    qty_nickels = fields.Integer('Nickels (5¢)', default=0)
    qty_dimes = fields.Integer('Dimes (10¢)', default=0)
    qty_quarters = fields.Integer('Quarters (25¢)', default=0)
    qty_half_dollars = fields.Integer('Half Dollars (50¢)', default=0)
    qty_dollar_coins = fields.Integer('Dollar Coins ($1)', default=0)

    # COIN subtotals (computed)
    sub_pennies = fields.Monetary(
        'Pennies Subtotal', compute='_compute_coin_subtotals',
        store=True, currency_field='currency_id')
    sub_nickels = fields.Monetary(
        'Nickels Subtotal', compute='_compute_coin_subtotals',
        store=True, currency_field='currency_id')
    sub_dimes = fields.Monetary(
        'Dimes Subtotal', compute='_compute_coin_subtotals',
        store=True, currency_field='currency_id')
    sub_quarters = fields.Monetary(
        'Quarters Subtotal', compute='_compute_coin_subtotals',
        store=True, currency_field='currency_id')
    sub_half_dollars = fields.Monetary(
        'Half Dollars Subtotal', compute='_compute_coin_subtotals',
        store=True, currency_field='currency_id')
    sub_dollar_coins = fields.Monetary(
        'Dollar Coins Subtotal', compute='_compute_coin_subtotals',
        store=True, currency_field='currency_id')
    total_coins = fields.Monetary(
        'Total Coins', compute='_compute_coin_subtotals',
        store=True, currency_field='currency_id')

    # ------------------------------------------------------------------
    # BILL quantities (Integer — how many of each)
    # ------------------------------------------------------------------
    qty_ones = fields.Integer('$1 Bills', default=0)
    qty_twos = fields.Integer('$2 Bills', default=0)
    qty_fives = fields.Integer('$5 Bills', default=0)
    qty_tens = fields.Integer('$10 Bills', default=0)
    qty_twenties = fields.Integer('$20 Bills', default=0)
    qty_fifties = fields.Integer('$50 Bills', default=0)
    qty_hundreds = fields.Integer('$100 Bills', default=0)

    # BILL subtotals (computed)
    sub_ones = fields.Monetary(
        '$1 Subtotal', compute='_compute_bill_subtotals',
        store=True, currency_field='currency_id')
    sub_twos = fields.Monetary(
        '$2 Subtotal', compute='_compute_bill_subtotals',
        store=True, currency_field='currency_id')
    sub_fives = fields.Monetary(
        '$5 Subtotal', compute='_compute_bill_subtotals',
        store=True, currency_field='currency_id')
    sub_tens = fields.Monetary(
        '$10 Subtotal', compute='_compute_bill_subtotals',
        store=True, currency_field='currency_id')
    sub_twenties = fields.Monetary(
        '$20 Subtotal', compute='_compute_bill_subtotals',
        store=True, currency_field='currency_id')
    sub_fifties = fields.Monetary(
        '$50 Subtotal', compute='_compute_bill_subtotals',
        store=True, currency_field='currency_id')
    sub_hundreds = fields.Monetary(
        '$100 Subtotal', compute='_compute_bill_subtotals',
        store=True, currency_field='currency_id')
    total_bills = fields.Monetary(
        'Total Bills', compute='_compute_bill_subtotals',
        store=True, currency_field='currency_id')

    # ------------------------------------------------------------------
    # Grand total from denominations
    # ------------------------------------------------------------------
    total_denomination = fields.Monetary(
        'Total Cash (Denominations)',
        compute='_compute_total_denomination',
        store=True, currency_field='currency_id',
        help="Sum of all coin and bill denomination counts.")

    # ------------------------------------------------------------------
    # Computations
    # ------------------------------------------------------------------
    @api.depends(
        'qty_pennies', 'qty_nickels', 'qty_dimes',
        'qty_quarters', 'qty_half_dollars', 'qty_dollar_coins')
    def _compute_coin_subtotals(self):
        for rec in self:
            rec.sub_pennies = rec.qty_pennies * 0.01
            rec.sub_nickels = rec.qty_nickels * 0.05
            rec.sub_dimes = rec.qty_dimes * 0.10
            rec.sub_quarters = rec.qty_quarters * 0.25
            rec.sub_half_dollars = rec.qty_half_dollars * 0.50
            rec.sub_dollar_coins = rec.qty_dollar_coins * 1.00
            rec.total_coins = (
                rec.sub_pennies + rec.sub_nickels + rec.sub_dimes
                + rec.sub_quarters + rec.sub_half_dollars
                + rec.sub_dollar_coins
            )

    @api.depends(
        'qty_ones', 'qty_twos', 'qty_fives', 'qty_tens',
        'qty_twenties', 'qty_fifties', 'qty_hundreds')
    def _compute_bill_subtotals(self):
        for rec in self:
            rec.sub_ones = rec.qty_ones * 1
            rec.sub_twos = rec.qty_twos * 2
            rec.sub_fives = rec.qty_fives * 5
            rec.sub_tens = rec.qty_tens * 10
            rec.sub_twenties = rec.qty_twenties * 20
            rec.sub_fifties = rec.qty_fifties * 50
            rec.sub_hundreds = rec.qty_hundreds * 100
            rec.total_bills = (
                rec.sub_ones + rec.sub_twos + rec.sub_fives
                + rec.sub_tens + rec.sub_twenties
                + rec.sub_fifties + rec.sub_hundreds
            )

    @api.depends('total_coins', 'total_bills')
    def _compute_total_denomination(self):
        for rec in self:
            rec.total_denomination = rec.total_coins + rec.total_bills

    @api.depends('total_denomination')
    def _compute_counted_total(self):
        """Override: counted_total now comes from denomination counts."""
        for rec in self:
            rec.counted_total = rec.total_denomination
