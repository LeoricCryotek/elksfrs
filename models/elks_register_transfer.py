# -*- coding: utf-8 -*-
"""Register-to-register cash transfers.

After counting each register bag, cash may need to move between
registers to bring each back to its starter-bank level.  This model
records those transfers with denomination-level detail.
"""
from odoo import api, fields, models
from odoo.exceptions import ValidationError


class ElksRegisterTransfer(models.Model):
    _name = 'elks.register.transfer'
    _description = 'Register Cash Transfer'
    _order = 'sequence, id'

    cash_on_hand_id = fields.Many2one(
        'elks.cash.on.hand', string='Cash Count',
        required=True, ondelete='cascade', index=True)
    from_register_id = fields.Many2one(
        'elks.register', string='From Register',
        required=True, ondelete='restrict')
    to_register_id = fields.Many2one(
        'elks.register', string='To Register',
        required=True, ondelete='restrict')
    sequence = fields.Integer(default=10)

    # Denomination quantities transferred
    qty_pennies = fields.Integer('Pennies', default=0)
    qty_nickels = fields.Integer('Nickels', default=0)
    qty_dimes = fields.Integer('Dimes', default=0)
    qty_quarters = fields.Integer('Quarters', default=0)
    qty_half_dollars = fields.Integer('Half Dollars', default=0)
    qty_dollar_coins = fields.Integer('Dollar Coins', default=0)
    qty_ones = fields.Integer('$1 Bills', default=0)
    qty_twos = fields.Integer('$2 Bills', default=0)
    qty_fives = fields.Integer('$5 Bills', default=0)
    qty_tens = fields.Integer('$10 Bills', default=0)
    qty_twenties = fields.Integer('$20 Bills', default=0)
    qty_fifties = fields.Integer('$50 Bills', default=0)
    qty_hundreds = fields.Integer('$100 Bills', default=0)

    transfer_total = fields.Monetary(
        'Transfer Amount', compute='_compute_transfer_total',
        store=True, currency_field='currency_id')
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

    @api.constrains('from_register_id', 'to_register_id')
    def _check_different_registers(self):
        for rec in self:
            if rec.from_register_id == rec.to_register_id:
                raise ValidationError(
                    "Cannot transfer cash from a register to itself.")

    @api.depends(*[f'qty_{d}' for d, _ in [
        ('pennies',0),('nickels',0),('dimes',0),('quarters',0),
        ('half_dollars',0),('dollar_coins',0),('ones',0),('twos',0),
        ('fives',0),('tens',0),('twenties',0),('fifties',0),('hundreds',0),
    ]])
    def _compute_transfer_total(self):
        for rec in self:
            total = 0.0
            for denom, face in self._DENOM_MAP:
                total += getattr(rec, f'qty_{denom}', 0) * face
            rec.transfer_total = total
