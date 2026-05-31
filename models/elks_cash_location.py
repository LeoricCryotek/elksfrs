# -*- coding: utf-8 -*-
"""Cash Management — Locations.

A cash location is anywhere physical cash lives:

  * The Bank — the lodge's central safe / vault (singleton).
  * Tills — permanent point-of-sale drawers (Main Bar, Dining, etc.).
  * Bags — personal till bags, typically assigned to one server.
  * Event Tills — one-off cash drawers for specific events.

Locations get *counted* (see ``elks.cash.count``) and cash flows between
them via ``elks.cash.movement`` records.  The Bank fills change requests
from Tills/Bags; Tills/Bags drop revenue into the Bank at shift close;
the Bank deposits accumulated cash to the Operating Checking account on
Monday.

This module only enforces structural constraints (one Bank singleton,
unique codes).  It does NOT enforce that running balances stay
non-negative — the running balance is informational, since most
movements are paper-first and digitised after the fact.
"""
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


# Shared denomination map.  Order matters for displays.
_DENOM_MAP = [
    ('hundreds',     100.00, '$100 Bills'),
    ('fifties',       50.00, '$50 Bills'),
    ('twenties',      20.00, '$20 Bills'),
    ('tens',          10.00, '$10 Bills'),
    ('fives',          5.00, '$5 Bills'),
    ('twos',           2.00, '$2 Bills'),
    ('ones',           1.00, '$1 Bills'),
    ('dollar_coins',   1.00, 'Dollar Coins ($1)'),
    ('half_dollars',   0.50, 'Half Dollars (50¢)'),
    ('quarters',       0.25, 'Quarters (25¢)'),
    ('dimes',          0.10, 'Dimes (10¢)'),
    ('nickels',        0.05, 'Nickels (5¢)'),
    ('pennies',        0.01, 'Pennies (1¢)'),
]
DENOM_NAMES = [d for d, _f, _l in _DENOM_MAP]
DENOM_FACE = {d: f for d, f, _l in _DENOM_MAP}
DENOM_LABEL = {d: lbl for d, _f, lbl in _DENOM_MAP}


class ElksCashLocation(models.Model):
    _name = 'elks.cash.location'
    _description = 'Cash Location (Bank / Till / Bag)'
    _order = 'location_type, sequence, name'
    _inherit = ['mail.thread']

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------
    name = fields.Char(
        'Location Name', required=True, tracking=True,
        help="Display name. Examples: 'The Bank', 'Main Bar Till', "
             "'Susan Dining Bag', 'Event Bar 2026-06-15'.",
    )
    code = fields.Char(
        'Code', required=True, copy=False, tracking=True,
        help=(
            "A short, unique identifier for this location. Appears on every "
            "Cash Count Slip, Change Slip, Deposit Slip, and Bag Tag printed "
            "from the system, so make it brief and recognizable at a glance.\n\n"
            "Conventions:\n"
            "  • UPPERCASE letters, numbers, and hyphens only\n"
            "  • Keep it short — 3 to 12 characters is ideal\n"
            "  • Tie it to the physical place, not the person currently using it\n\n"
            "Examples:\n"
            "  • BANK            — the safe / vault (the singleton bank)\n"
            "  • MAIN            — the main bar till\n"
            "  • DINING          — the dining-room register\n"
            "  • DINING-SUSAN    — Susan's personal dining bag\n"
            "  • EV-20260615     — Event till for the 2026-06-15 event\n\n"
            "The code must be unique across all locations. It's used as a "
            "lookup key in reports and filters, so changing it later will "
            "rename it everywhere it appears."
        ),
    )
    location_type = fields.Selection([
        ('bank', 'Bank (Safe)'),
        ('till', 'Till / Register'),
        ('bag', 'Personal Till Bag'),
        ('event_till', 'Event Till'),
    ], string='Type', required=True, default='till', tracking=True, index=True,
       help="Bank is the singleton central reserve. Tills and Bags are "
            "point-of-sale locations. Event Tills are temporary and "
            "auto-archive after their event date.",
    )
    is_bank = fields.Boolean(
        'Is The Bank', compute='_compute_is_bank', store=True, index=True,
    )

    # ------------------------------------------------------------------
    # Optional assignment (for bags) and event date
    # ------------------------------------------------------------------
    assigned_to_id = fields.Many2one(
        'res.partner', string='Assigned To',
        help="The person who carries this bag (for bag locations). "
             "Bags should normally be assigned to a partner so deposits "
             "and counts can be attributed.",
    )
    event_date = fields.Date(
        'Event Date',
        help="For event tills — the date the event occurs. Used to "
             "auto-archive the location N days after the event.",
    )

    # ------------------------------------------------------------------
    # Suggested starter levels per denomination (informational only)
    # ------------------------------------------------------------------
    starter_hundreds = fields.Integer('Starter $100 Bills', default=0)
    starter_fifties = fields.Integer('Starter $50 Bills', default=0)
    starter_twenties = fields.Integer('Starter $20 Bills', default=0)
    starter_tens = fields.Integer('Starter $10 Bills', default=0)
    starter_fives = fields.Integer('Starter $5 Bills', default=0)
    starter_twos = fields.Integer('Starter $2 Bills', default=0)
    starter_ones = fields.Integer('Starter $1 Bills', default=0)
    starter_dollar_coins = fields.Integer('Starter $1 Coins', default=0)
    starter_half_dollars = fields.Integer('Starter Half Dollars', default=0)
    starter_quarters = fields.Integer('Starter Quarters', default=0)
    starter_dimes = fields.Integer('Starter Dimes', default=0)
    starter_nickels = fields.Integer('Starter Nickels', default=0)
    starter_pennies = fields.Integer('Starter Pennies', default=0)

    starter_total = fields.Monetary(
        'Suggested Starter Total', compute='_compute_starter_total',
        store=True, currency_field='currency_id',
        help="Dollar value of the suggested starter bank. Not enforced — "
             "actual fills can vary.",
    )

    # ------------------------------------------------------------------
    # Running balance (informational)
    # ------------------------------------------------------------------
    last_count_id = fields.Many2one(
        'elks.cash.count', string='Last Count',
        compute='_compute_last_count', store=False,
    )
    last_count_date = fields.Datetime(
        'Last Count Date', compute='_compute_last_count', store=False,
    )
    last_count_total = fields.Monetary(
        'Last Count Total', compute='_compute_last_count', store=False,
        currency_field='currency_id',
    )
    estimated_balance = fields.Monetary(
        'Estimated Balance', compute='_compute_estimated_balance', store=False,
        currency_field='currency_id',
        help="Approximate current cash: last count + recorded movements "
             "since. Only accurate if movements are kept up to date.",
    )

    # ------------------------------------------------------------------
    # Bookkeeping
    # ------------------------------------------------------------------
    sequence = fields.Integer('Display Order', default=10)
    active = fields.Boolean(default=True, tracking=True)
    notes = fields.Text('Notes')
    currency_id = fields.Many2one(
        'res.currency', default=lambda self: self.env.company.currency_id,
    )

    # Odoo 19 — _sql_constraints is removed; use models.Constraint instead.
    _code_unique = models.Constraint(
        'UNIQUE(code)',
        "Location code must be unique.",
    )

    # ==================================================================
    # Computes
    # ==================================================================
    @api.depends('location_type')
    def _compute_is_bank(self):
        for rec in self:
            rec.is_bank = (rec.location_type == 'bank')

    @api.depends(*[f'starter_{d}' for d in DENOM_NAMES])
    def _compute_starter_total(self):
        for rec in self:
            total = 0.0
            for denom in DENOM_NAMES:
                total += getattr(rec, f'starter_{denom}', 0) * DENOM_FACE[denom]
            rec.starter_total = total

    def _compute_last_count(self):
        Count = self.env['elks.cash.count']
        for rec in self:
            last = Count.search(
                [('location_id', '=', rec.id), ('state', '=', 'done')],
                order='count_date desc, id desc', limit=1,
            )
            rec.last_count_id = last.id if last else False
            rec.last_count_date = last.count_date if last else False
            rec.last_count_total = last.total_cash if last else 0.0

    def _compute_estimated_balance(self):
        Movement = self.env['elks.cash.movement']
        for rec in self:
            last_count = rec.last_count_id
            base = rec.last_count_total or 0.0
            base_date = last_count.count_date if last_count else False
            domain = [('state', '=', 'posted')]
            if base_date:
                domain.append(('move_date', '>', base_date))
            inflow = Movement.search(domain + [
                ('to_location_id', '=', rec.id),
            ])
            outflow = Movement.search(domain + [
                ('from_location_id', '=', rec.id),
            ])
            rec.estimated_balance = (
                base
                + sum(inflow.mapped('total_amount'))
                - sum(outflow.mapped('total_amount'))
            )

    # ==================================================================
    # Constraints
    # ==================================================================
    @api.constrains('location_type')
    def _check_single_bank(self):
        for rec in self:
            if rec.location_type == 'bank':
                others = self.search([
                    ('location_type', '=', 'bank'),
                    ('id', '!=', rec.id),
                ])
                if others:
                    raise ValidationError(_(
                        "There can be only one Bank location. "
                        "'%(name)s' is already the Bank.",
                        name=others[0].display_name,
                    ))

    @api.constrains('location_type', 'event_date')
    def _check_event_date(self):
        for rec in self:
            if rec.location_type == 'event_till' and not rec.event_date:
                raise ValidationError(_(
                    "Event tills must have an Event Date so they "
                    "can be auto-archived after the event."
                ))

    # ==================================================================
    # Actions
    # ==================================================================
    def action_print_bag_tag(self):
        """Print a small tag for a personal till bag."""
        self.ensure_one()
        return self.env.ref(
            'elksfrs.action_report_cash_bag_tag'
        ).report_action(self)

    def action_view_counts(self):
        """Open the count list filtered to this location."""
        self.ensure_one()
        return {
            'name': _("Counts — %s", self.name),
            'type': 'ir.actions.act_window',
            'res_model': 'elks.cash.count',
            'view_mode': 'list,form,graph',
            'domain': [('location_id', '=', self.id)],
            'context': {'default_location_id': self.id},
        }

    def action_view_movements(self):
        """Open the movement list filtered to this location."""
        self.ensure_one()
        return {
            'name': _("Movements — %s", self.name),
            'type': 'ir.actions.act_window',
            'res_model': 'elks.cash.movement',
            'view_mode': 'list,form,graph,pivot',
            'domain': [
                '|',
                ('from_location_id', '=', self.id),
                ('to_location_id', '=', self.id),
            ],
            'context': {},
        }

    # ==================================================================
    # Cron: archive expired event tills
    # ==================================================================
    @api.model
    def _cron_archive_old_event_tills(self, days_after_event=30):
        """Archive event_till locations whose event_date is older than N days."""
        from datetime import timedelta
        cutoff = fields.Date.today() - timedelta(days=days_after_event)
        old = self.search([
            ('location_type', '=', 'event_till'),
            ('active', '=', True),
            ('event_date', '<=', cutoff),
        ])
        if old:
            old.write({'active': False})
        return len(old)
