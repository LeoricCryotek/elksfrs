# -*- coding: utf-8 -*-
"""Elks General Journal Entries.

A simplified journal entry system tied to the Elks Uniform COA.
Each journal entry has header info (date, memo, entry number) and
one or more lines that must balance (total debits = total credits).
"""
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

import logging

_logger = logging.getLogger(__name__)


class ElksJournalEntry(models.Model):
    _name = "elks.journal.entry"
    _description = "Elks General Journal Entry"
    _order = "date desc, entry_number desc"
    _inherit = ["mail.thread", "mail.activity.mixin"]

    name = fields.Char(
        "Reference", compute="_compute_name", store=True,
    )
    entry_number = fields.Char(
        "Entry No.", required=True, copy=False, index=True,
        default=lambda self: _("New"),
        help="Custom entry number, e.g. YECLOSE2026 for year-end closing.",
    )
    date = fields.Date(
        "Date", required=True, default=fields.Date.context_today,
        tracking=True, index=True,
    )
    memo = fields.Text(
        "Memo", tracking=True,
        help="Short explanation of why this entry is needed.",
    )
    state = fields.Selection([
        ('draft', 'Draft'),
        ('posted', 'Posted'),
        ('cancelled', 'Cancelled'),
    ], string="Status", default='draft', tracking=True, index=True)

    line_ids = fields.One2many(
        "elks.journal.entry.line", "entry_id", string="Journal Lines",
        copy=True,
    )

    # Computed balance check
    total_debit = fields.Monetary(
        "Total Debit", compute="_compute_totals", store=True,
        currency_field='currency_id',
    )
    total_credit = fields.Monetary(
        "Total Credit", compute="_compute_totals", store=True,
        currency_field='currency_id',
    )
    is_balanced = fields.Boolean(
        "Balanced", compute="_compute_totals", store=True,
    )
    currency_id = fields.Many2one(
        "res.currency", default=lambda self: self.env.company.currency_id,
    )

    # Lodge fiscal year helper
    lodge_year = fields.Char(
        "Lodge Year", compute="_compute_lodge_year", store=True, index=True,
        help="Elks fiscal year, e.g. 2025-2026 for April 2025 – March 2026.",
    )

    @api.depends("entry_number", "date")
    def _compute_name(self):
        for rec in self:
            parts = [rec.entry_number or "New"]
            if rec.date:
                parts.append(str(rec.date))
            rec.name = " / ".join(parts)

    @api.depends("date")
    def _compute_lodge_year(self):
        for rec in self:
            if rec.date:
                if rec.date.month >= 4:
                    start = rec.date.year
                else:
                    start = rec.date.year - 1
                rec.lodge_year = f"{start}-{start + 1}"
            else:
                rec.lodge_year = False

    @api.depends("line_ids.debit", "line_ids.credit")
    def _compute_totals(self):
        for rec in self:
            rec.total_debit = sum(rec.line_ids.mapped("debit"))
            rec.total_credit = sum(rec.line_ids.mapped("credit"))
            rec.is_balanced = abs(rec.total_debit - rec.total_credit) < 0.005

    def action_post(self):
        for rec in self:
            if not rec.is_balanced:
                raise ValidationError(_(
                    "Entry '%s' is not balanced. "
                    "Total Debit (%.2f) ≠ Total Credit (%.2f)."
                ) % (rec.entry_number, rec.total_debit, rec.total_credit))
            if not rec.line_ids:
                raise ValidationError(_(
                    "Entry '%s' has no journal lines."
                ) % rec.entry_number)
        self.write({"state": "posted"})

    def action_cancel(self):
        self.write({"state": "cancelled"})

    def action_draft(self):
        self.write({"state": "draft"})

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("entry_number", _("New")) == _("New"):
                vals["entry_number"] = self.env["ir.sequence"].next_by_code(
                    "elks.journal.entry"
                ) or _("New")
        return super().create(vals_list)


class ElksJournalEntryLine(models.Model):
    _name = "elks.journal.entry.line"
    _description = "Elks Journal Entry Line"
    _order = "sequence, id"

    entry_id = fields.Many2one(
        "elks.journal.entry", string="Journal Entry",
        required=True, ondelete="cascade", index=True,
    )
    sequence = fields.Integer(default=10)
    account_id = fields.Many2one(
        "elks.account", string="Account", required=True, index=True,
        domain="[('is_header', '=', False)]",
    )
    debit = fields.Monetary(
        "Debit", default=0.0, currency_field='currency_id',
    )
    credit = fields.Monetary(
        "Credit", default=0.0, currency_field='currency_id',
    )
    memo = fields.Char("Line Memo")
    currency_id = fields.Many2one(
        related="entry_id.currency_id",
        store=True,
    )
    department_id = fields.Many2one(
        related="account_id.department_id", store=True,
        string="Department",
    )
    date = fields.Date(
        related="entry_id.date", store=True, index=True,
    )
    entry_state = fields.Selection(
        related="entry_id.state", store=True, string="Status",
    )

    @api.constrains("debit", "credit")
    def _check_debit_credit(self):
        for line in self:
            if line.debit < 0 or line.credit < 0:
                raise ValidationError(_(
                    "Debit and credit amounts must be non-negative."
                ))
            if line.debit > 0 and line.credit > 0:
                raise ValidationError(_(
                    "A journal line cannot have both a debit and credit amount. "
                    "Use separate lines."
                ))
