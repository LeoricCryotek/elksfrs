# -*- coding: utf-8 -*-
"""Elks Lodge Departments (Classes).

Each department is a logical grouping of income and expense accounts
used to create Profit & Loss statements per the Uniform Chart of Accounts.
Departments map to the 10-series prefixes in the COA (30xxx Lodge, 40xxx Bar, etc.).
"""
from odoo import fields, models


class ElksDepartment(models.Model):
    _name = "elks.department"
    _description = "Elks Lodge Department (Class)"
    _order = "code"

    name = fields.Char("Department Name", required=True, index=True)
    code = fields.Char(
        "Code", required=True, index=True,
        help="Two-digit series prefix, e.g. '30' for Lodge Operations.",
    )
    active = fields.Boolean(default=True)
    note = fields.Text("Notes")

    # Cost ratio targets per the AA Manual
    cogs_target_pct = fields.Float(
        "CoGS Target %", default=35.0,
        help="Cost of Goods Sold should not exceed this % of sales.",
    )
    labor_target_pct = fields.Float(
        "Labor Target %", default=35.0,
        help="Employee/labor costs should not exceed this % of sales.",
    )

    account_ids = fields.One2many(
        "elks.account", "department_id", string="Accounts",
    )
