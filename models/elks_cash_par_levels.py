# -*- coding: utf-8 -*-
"""Par-level (bank-on-hand) settings and deposit / change-order analysis.

Adds target quantities per denomination to Lodge Settings so the lodge
can define how much of each coin and bill to keep in the safe.

Extends the Cash On Hand model with computed over/under analysis that
compares counted quantities against the par levels and recommends:
  • Deposit amounts for denominations over par
  • Change-order amounts for denominations under par
"""
from odoo import api, fields, models


# ======================================================================
# Lodge Settings — par-level targets per denomination
# ======================================================================
class ElksLodgeSettingsPar(models.Model):
    _inherit = 'elks.lodge.settings'

    # --- Coin par levels (how many to keep) ---
    par_pennies = fields.Integer('Pennies (1¢)', default=0,
        help="Target number of pennies to keep on hand.")
    par_nickels = fields.Integer('Nickels (5¢)', default=0,
        help="Target number of nickels to keep on hand.")
    par_dimes = fields.Integer('Dimes (10¢)', default=0,
        help="Target number of dimes to keep on hand.")
    par_quarters = fields.Integer('Quarters (25¢)', default=0,
        help="Target number of quarters to keep on hand.")
    par_half_dollars = fields.Integer('Half Dollars (50¢)', default=0,
        help="Target number of half dollars to keep on hand.")
    par_dollar_coins = fields.Integer('Dollar Coins ($1)', default=0,
        help="Target number of dollar coins to keep on hand.")

    # --- Bill par levels ---
    par_ones = fields.Integer('$1 Bills', default=0,
        help="Target number of $1 bills to keep on hand.")
    par_twos = fields.Integer('$2 Bills', default=0,
        help="Target number of $2 bills to keep on hand.")
    par_fives = fields.Integer('$5 Bills', default=0,
        help="Target number of $5 bills to keep on hand.")
    par_tens = fields.Integer('$10 Bills', default=0,
        help="Target number of $10 bills to keep on hand.")
    par_twenties = fields.Integer('$20 Bills', default=0,
        help="Target number of $20 bills to keep on hand.")
    par_fifties = fields.Integer('$50 Bills', default=0,
        help="Target number of $50 bills to keep on hand.")
    par_hundreds = fields.Integer('$100 Bills', default=0,
        help="Target number of $100 bills to keep on hand.")


# ======================================================================
# Cash On Hand — over/under analysis vs. par levels
# ======================================================================
class ElksCashOnHandParAnalysis(models.Model):
    _inherit = 'elks.cash.on.hand'

    # --- Over / under quantities (counted - par) ---
    over_pennies = fields.Integer('Pennies +/−', compute='_compute_par_analysis', store=True)
    over_nickels = fields.Integer('Nickels +/−', compute='_compute_par_analysis', store=True)
    over_dimes = fields.Integer('Dimes +/−', compute='_compute_par_analysis', store=True)
    over_quarters = fields.Integer('Quarters +/−', compute='_compute_par_analysis', store=True)
    over_half_dollars = fields.Integer('Half $ +/−', compute='_compute_par_analysis', store=True)
    over_dollar_coins = fields.Integer('$1 Coins +/−', compute='_compute_par_analysis', store=True)
    over_ones = fields.Integer('$1 Bills +/−', compute='_compute_par_analysis', store=True)
    over_twos = fields.Integer('$2 Bills +/−', compute='_compute_par_analysis', store=True)
    over_fives = fields.Integer('$5 Bills +/−', compute='_compute_par_analysis', store=True)
    over_tens = fields.Integer('$10 Bills +/−', compute='_compute_par_analysis', store=True)
    over_twenties = fields.Integer('$20 Bills +/−', compute='_compute_par_analysis', store=True)
    over_fifties = fields.Integer('$50 Bills +/−', compute='_compute_par_analysis', store=True)
    over_hundreds = fields.Integer('$100 Bills +/−', compute='_compute_par_analysis', store=True)

    # --- Monetary totals ---
    total_to_deposit = fields.Monetary(
        'Total to Deposit', compute='_compute_par_analysis',
        store=True, currency_field='currency_id',
        help="Total dollar value of denominations OVER par level — send to bank.")
    total_change_order = fields.Monetary(
        'Change Order Needed', compute='_compute_par_analysis',
        store=True, currency_field='currency_id',
        help="Total dollar value of denominations UNDER par level — request from bank.")

    # denomination → (par field, qty field, face value)
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
        'qty_pennies', 'qty_nickels', 'qty_dimes', 'qty_quarters',
        'qty_half_dollars', 'qty_dollar_coins',
        'qty_ones', 'qty_twos', 'qty_fives', 'qty_tens',
        'qty_twenties', 'qty_fifties', 'qty_hundreds',
    )
    def _compute_par_analysis(self):
        settings = self.env['elks.lodge.settings'].sudo().search([], limit=1)
        for rec in self:
            deposit = 0.0
            change_order = 0.0
            for denom, face in self._DENOM_MAP:
                par_qty = getattr(settings, f'par_{denom}', 0) if settings else 0
                counted_qty = getattr(rec, f'qty_{denom}', 0)
                diff = counted_qty - par_qty
                setattr(rec, f'over_{denom}', diff)
                if diff > 0:
                    deposit += diff * face
                elif diff < 0:
                    change_order += abs(diff) * face
            rec.total_to_deposit = deposit
            rec.total_change_order = change_order
