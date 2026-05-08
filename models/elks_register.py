# -*- coding: utf-8 -*-
"""Register / Till master data.

Defines the registers (tills) available at the lodge.  Each register
carries a default starter-bank — the denomination quantities it should
start each session with.  The starter bank is snapshotted onto each
Register Count Line so the count is historically accurate even if the
default changes later.
"""
from odoo import api, fields, models


class ElksRegister(models.Model):
    _name = 'elks.register'
    _description = 'Register / Till'
    _order = 'sequence, name'

    name = fields.Char('Register Name', required=True,
        help="e.g. Main Bar, Dining Room, Event Bar")
    location = fields.Char('Location / Description')
    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)

    # --- Default starter-bank quantities per denomination ---
    starter_pennies = fields.Integer('Pennies (1¢)', default=0)
    starter_nickels = fields.Integer('Nickels (5¢)', default=0)
    starter_dimes = fields.Integer('Dimes (10¢)', default=0)
    starter_quarters = fields.Integer('Quarters (25¢)', default=0)
    starter_half_dollars = fields.Integer('Half Dollars (50¢)', default=0)
    starter_dollar_coins = fields.Integer('Dollar Coins ($1)', default=0)
    starter_ones = fields.Integer('$1 Bills', default=0)
    starter_twos = fields.Integer('$2 Bills', default=0)
    starter_fives = fields.Integer('$5 Bills', default=0)
    starter_tens = fields.Integer('$10 Bills', default=0)
    starter_twenties = fields.Integer('$20 Bills', default=0)
    starter_fifties = fields.Integer('$50 Bills', default=0)
    starter_hundreds = fields.Integer('$100 Bills', default=0)

    starter_total = fields.Monetary(
        'Starter Bank Total', compute='_compute_starter_total',
        store=True, currency_field='currency_id',
        help="Dollar value of the default starter bank.")
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

    @api.depends(
        'starter_pennies', 'starter_nickels', 'starter_dimes',
        'starter_quarters', 'starter_half_dollars', 'starter_dollar_coins',
        'starter_ones', 'starter_twos', 'starter_fives', 'starter_tens',
        'starter_twenties', 'starter_fifties', 'starter_hundreds',
    )
    def _compute_starter_total(self):
        for rec in self:
            total = 0.0
            for denom, face in self._DENOM_MAP:
                total += getattr(rec, f'starter_{denom}', 0) * face
            rec.starter_total = total
