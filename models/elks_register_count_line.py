# -*- coding: utf-8 -*-
"""Register Count Line — individual till count within a Cash On Hand session.

Each line represents one register bag being counted.  Denomination
quantities are entered, subtotals computed, and the excess over the
register's starter bank is calculated automatically.
"""
from odoo import api, fields, models


class ElksRegisterCountLine(models.Model):
    _name = 'elks.register.count.line'
    _description = 'Register Count Line'
    _order = 'sequence, id'

    cash_on_hand_id = fields.Many2one(
        'elks.cash.on.hand', string='Cash Count',
        required=True, ondelete='cascade', index=True)
    register_id = fields.Many2one(
        'elks.register', string='Register / Till',
        required=True, ondelete='restrict')
    sequence = fields.Integer(default=10)

    # ------------------------------------------------------------------
    # Counted denomination quantities
    # ------------------------------------------------------------------
    qty_pennies = fields.Integer('Pennies (1¢)', default=0)
    qty_nickels = fields.Integer('Nickels (5¢)', default=0)
    qty_dimes = fields.Integer('Dimes (10¢)', default=0)
    qty_quarters = fields.Integer('Quarters (25¢)', default=0)
    qty_half_dollars = fields.Integer('Half Dollars (50¢)', default=0)
    qty_dollar_coins = fields.Integer('Dollar Coins ($1)', default=0)
    qty_ones = fields.Integer('$1 Bills', default=0)
    qty_twos = fields.Integer('$2 Bills', default=0)
    qty_fives = fields.Integer('$5 Bills', default=0)
    qty_tens = fields.Integer('$10 Bills', default=0)
    qty_twenties = fields.Integer('$20 Bills', default=0)
    qty_fifties = fields.Integer('$50 Bills', default=0)
    qty_hundreds = fields.Integer('$100 Bills', default=0)

    # ------------------------------------------------------------------
    # Counted totals (computed)
    # ------------------------------------------------------------------
    counted_total = fields.Monetary(
        'Counted Total', compute='_compute_counted_total',
        store=True, currency_field='currency_id')

    # ------------------------------------------------------------------
    # Starter-bank snapshot (copied from register on selection)
    # ------------------------------------------------------------------
    starter_pennies = fields.Integer('Starter Pennies', default=0)
    starter_nickels = fields.Integer('Starter Nickels', default=0)
    starter_dimes = fields.Integer('Starter Dimes', default=0)
    starter_quarters = fields.Integer('Starter Quarters', default=0)
    starter_half_dollars = fields.Integer('Starter Half $', default=0)
    starter_dollar_coins = fields.Integer('Starter $1 Coins', default=0)
    starter_ones = fields.Integer('Starter $1 Bills', default=0)
    starter_twos = fields.Integer('Starter $2 Bills', default=0)
    starter_fives = fields.Integer('Starter $5 Bills', default=0)
    starter_tens = fields.Integer('Starter $10 Bills', default=0)
    starter_twenties = fields.Integer('Starter $20 Bills', default=0)
    starter_fifties = fields.Integer('Starter $50 Bills', default=0)
    starter_hundreds = fields.Integer('Starter $100 Bills', default=0)

    starter_total = fields.Monetary(
        'Starter Bank', compute='_compute_starter_total',
        store=True, currency_field='currency_id')

    # ------------------------------------------------------------------
    # Excess per denomination (counted − starter)
    # ------------------------------------------------------------------
    excess_pennies = fields.Integer('Excess Pennies', compute='_compute_excess', store=True)
    excess_nickels = fields.Integer('Excess Nickels', compute='_compute_excess', store=True)
    excess_dimes = fields.Integer('Excess Dimes', compute='_compute_excess', store=True)
    excess_quarters = fields.Integer('Excess Quarters', compute='_compute_excess', store=True)
    excess_half_dollars = fields.Integer('Excess Half $', compute='_compute_excess', store=True)
    excess_dollar_coins = fields.Integer('Excess $1 Coins', compute='_compute_excess', store=True)
    excess_ones = fields.Integer('Excess $1', compute='_compute_excess', store=True)
    excess_twos = fields.Integer('Excess $2', compute='_compute_excess', store=True)
    excess_fives = fields.Integer('Excess $5', compute='_compute_excess', store=True)
    excess_tens = fields.Integer('Excess $10', compute='_compute_excess', store=True)
    excess_twenties = fields.Integer('Excess $20', compute='_compute_excess', store=True)
    excess_fifties = fields.Integer('Excess $50', compute='_compute_excess', store=True)
    excess_hundreds = fields.Integer('Excess $100', compute='_compute_excess', store=True)

    excess_total = fields.Monetary(
        'Excess / (Short)', compute='_compute_excess',
        store=True, currency_field='currency_id',
        help="Positive = revenue taken in; negative = register is short.")

    currency_id = fields.Many2one(
        'res.currency', default=lambda self: self.env.company.currency_id)

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

    # ------------------------------------------------------------------
    # Onchange: populate starter bank from register defaults
    # ------------------------------------------------------------------
    @api.onchange('register_id')
    def _onchange_register_id(self):
        if self.register_id:
            for denom, _face in self._DENOM_MAP:
                setattr(self, f'starter_{denom}',
                        getattr(self.register_id, f'starter_{denom}', 0))

    @api.model_create_multi
    def create(self, vals_list):
        """Snapshot starter bank from register if not explicitly provided."""
        for vals in vals_list:
            if vals.get('register_id') and not any(
                    f'starter_{d}' in vals for d, _ in self._DENOM_MAP):
                reg = self.env['elks.register'].browse(vals['register_id'])
                for denom, _face in self._DENOM_MAP:
                    vals.setdefault(f'starter_{denom}',
                                   getattr(reg, f'starter_{denom}', 0))
        return super().create(vals_list)

    # ------------------------------------------------------------------
    # Computed fields
    # ------------------------------------------------------------------
    @api.depends(*[f'qty_{d}' for d, _ in [
        ('pennies',0),('nickels',0),('dimes',0),('quarters',0),
        ('half_dollars',0),('dollar_coins',0),('ones',0),('twos',0),
        ('fives',0),('tens',0),('twenties',0),('fifties',0),('hundreds',0),
    ]])
    def _compute_counted_total(self):
        for rec in self:
            total = 0.0
            for denom, face in self._DENOM_MAP:
                total += getattr(rec, f'qty_{denom}', 0) * face
            rec.counted_total = total

    @api.depends(*[f'starter_{d}' for d, _ in [
        ('pennies',0),('nickels',0),('dimes',0),('quarters',0),
        ('half_dollars',0),('dollar_coins',0),('ones',0),('twos',0),
        ('fives',0),('tens',0),('twenties',0),('fifties',0),('hundreds',0),
    ]])
    def _compute_starter_total(self):
        for rec in self:
            total = 0.0
            for denom, face in self._DENOM_MAP:
                total += getattr(rec, f'starter_{denom}', 0) * face
            rec.starter_total = total

    @api.depends(
        *[f'qty_{d}' for d, _ in [
            ('pennies',0),('nickels',0),('dimes',0),('quarters',0),
            ('half_dollars',0),('dollar_coins',0),('ones',0),('twos',0),
            ('fives',0),('tens',0),('twenties',0),('fifties',0),('hundreds',0),
        ]],
        *[f'starter_{d}' for d, _ in [
            ('pennies',0),('nickels',0),('dimes',0),('quarters',0),
            ('half_dollars',0),('dollar_coins',0),('ones',0),('twos',0),
            ('fives',0),('tens',0),('twenties',0),('fifties',0),('hundreds',0),
        ]],
    )
    def _compute_excess(self):
        for rec in self:
            excess_total = 0.0
            for denom, face in self._DENOM_MAP:
                diff = getattr(rec, f'qty_{denom}', 0) - getattr(rec, f'starter_{denom}', 0)
                setattr(rec, f'excess_{denom}', diff)
                excess_total += diff * face
            rec.excess_total = excess_total
