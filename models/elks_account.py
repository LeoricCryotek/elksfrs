# -*- coding: utf-8 -*-
"""Elks Lodge Uniform Chart of Accounts.

The Elks Uniform COA uses 5-digit parent accounts with optional 2-character
subaccounts.  Account numbers follow a departmental series:
  10xxx  Assets
  15xxx  Fixed Assets
  20xxx  Liabilities
  21xxx  Deferred Income
  23xxx  Long-term Liabilities
  29xxx  Equity
  30xxx  Lodge Operations
  40xxx  Bar
  50xxx  Food Service
  60xxx  Entertainment / Social
  61xxx  Fitness Center / Swimming Pool
  62xxx  Golf Course
  63xxx  Bowling
  64xxx  RV Park / Camping
  65xxx  Shooting Range / Skeet
  66xxx  Rental Activities (16.030 Corp)
  67xxx  Other Business Operations
  90xxx+ Restricted Funds
"""
from odoo import api, fields, models


ACCOUNT_TYPES = [
    ('bank', 'Bank'),
    ('asset', 'Other Current Asset'),
    ('receivable', 'Accounts Receivable'),
    ('fixed_asset', 'Fixed Asset'),
    ('other_asset', 'Other Asset'),
    ('payable', 'Accounts Payable'),
    ('liability', 'Other Current Liability'),
    ('long_term_liability', 'Long Term Liability'),
    ('equity', 'Equity'),
    ('income', 'Income'),
    ('cogs', 'Cost of Goods Sold'),
    ('expense', 'Expense'),
]


class ElksAccount(models.Model):
    _name = "elks.account"
    _description = "Elks Uniform Chart of Accounts"
    _order = "code"
    _rec_name = "display_name"

    code = fields.Char(
        "Account Number", required=True, index=True,
        help="5-digit account code per the Uniform COA (e.g. 10100).",
    )
    subaccount = fields.Char(
        "Sub-Account", size=2,
        help="Optional 2-character sub-account suffix.",
    )
    name = fields.Char("Account Name", required=True, index=True)
    account_type = fields.Selection(
        ACCOUNT_TYPES, string="Type", required=True, index=True,
    )
    department_id = fields.Many2one(
        "elks.department", string="Department",
        help="The Elks department (class) this account belongs to.",
    )
    parent_id = fields.Many2one(
        "elks.account", string="Parent Account",
        help="Parent account for sub-accounts.",
    )
    child_ids = fields.One2many(
        "elks.account", "parent_id", string="Sub-Accounts",
    )

    active = fields.Boolean(default=True)
    is_restricted = fields.Boolean(
        "Restricted Fund Account",
        help="True for accounts in the 9xxxx restricted fund series.",
    )
    is_header = fields.Boolean(
        "Header / Summary Account",
        help="Header accounts are used for grouping and cannot have postings.",
    )
    note = fields.Text("Description")

    # Link to Odoo native account (optional, for integration)
    odoo_account_id = fields.Many2one(
        "account.account", string="Odoo Account",
        help="Linked Odoo accounting account for journal entries.",
    )

    display_name = fields.Char(
        compute="_compute_display_name", store=True,
    )

    @api.depends("code", "subaccount", "name")
    def _compute_display_name(self):
        for rec in self:
            code = rec.code or ""
            if rec.subaccount:
                code = f"{code}.{rec.subaccount}"
            rec.display_name = f"{code} {rec.name}" if code else rec.name or ""
