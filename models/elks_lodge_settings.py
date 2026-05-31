# -*- coding: utf-8 -*-
"""Elks Lodge FRS Settings.

Stores lodge-specific configuration needed for FRS CSV exports
and financial reporting.
"""
from odoo import _, fields, models
from odoo.exceptions import UserError


class ElksLodgeSettings(models.Model):
    _name = "elks.lodge.settings"
    _description = "Elks Lodge FRS Configuration"

    name = fields.Char(
        "Lodge Name", required=True,
        help="Full lodge name, e.g. 'Anytown Lodge No. 1234'.",
    )
    lodge_number = fields.Char(
        "Lodge Number", required=True, index=True,
        help="Numeric lodge number used in FRS CSV submissions.",
    )

    # Branding / logos
    logo_primary = fields.Binary(
        "Primary Logo (Elks USA)",
        help="Main Elks logo shown on PDF reports and dashboards. "
             "Recommended: PNG with transparent background, at least 300px wide.",
    )
    logo_lodge = fields.Binary(
        "Lodge Logo",
        help="Lodge-specific logo (e.g. Elks #896). "
             "Shown alongside the primary logo on reports.",
    )
    officer_poster_emblem = fields.Binary(
        "Officer Poster Emblem",
        help="Emblem used on the large-format Officer Photo Poster "
             "(top-left corner). If left blank the Lodge Logo is used. "
             "Recommended: square PNG, at least 400px.",
    )
    state_association = fields.Char("State Association")
    district = fields.Char("District")
    area = fields.Char(
        "Grand Lodge Area",
        help="Grand Lodge Area number (1-8).",
    )

    # Lodge address (for public pages, receipts, etc.)
    lodge_address = fields.Char(
        "Lodge Address",
        help="Street address, e.g. '3444 10th St'.",
    )
    lodge_city = fields.Char("City")
    lodge_state = fields.Selection([
        ('AL', 'Alabama'), ('AK', 'Alaska'), ('AZ', 'Arizona'),
        ('AR', 'Arkansas'), ('CA', 'California'), ('CO', 'Colorado'),
        ('CT', 'Connecticut'), ('DE', 'Delaware'), ('FL', 'Florida'),
        ('GA', 'Georgia'), ('HI', 'Hawaii'), ('ID', 'Idaho'),
        ('IL', 'Illinois'), ('IN', 'Indiana'), ('IA', 'Iowa'),
        ('KS', 'Kansas'), ('KY', 'Kentucky'), ('LA', 'Louisiana'),
        ('ME', 'Maine'), ('MD', 'Maryland'), ('MA', 'Massachusetts'),
        ('MI', 'Michigan'), ('MN', 'Minnesota'), ('MS', 'Mississippi'),
        ('MO', 'Missouri'), ('MT', 'Montana'), ('NE', 'Nebraska'),
        ('NV', 'Nevada'), ('NH', 'New Hampshire'), ('NJ', 'New Jersey'),
        ('NM', 'New Mexico'), ('NY', 'New York'), ('NC', 'North Carolina'),
        ('ND', 'North Dakota'), ('OH', 'Ohio'), ('OK', 'Oklahoma'),
        ('OR', 'Oregon'), ('PA', 'Pennsylvania'), ('RI', 'Rhode Island'),
        ('SC', 'South Carolina'), ('SD', 'South Dakota'), ('TN', 'Tennessee'),
        ('TX', 'Texas'), ('UT', 'Utah'), ('VT', 'Vermont'),
        ('VA', 'Virginia'), ('WA', 'Washington'), ('WV', 'West Virginia'),
        ('WI', 'Wisconsin'), ('WY', 'Wyoming'),
        ('DC', 'District of Columbia'),
        ('PR', 'Puerto Rico'), ('GU', 'Guam'), ('VI', 'U.S. Virgin Islands'),
    ], string="State")
    lodge_zip = fields.Char("ZIP Code")
    lodge_phone = fields.Char("Lodge Phone")

    # Accounting preferences
    accounting_basis = fields.Selection([
        ('accrual', 'Accrual'),
        ('modified_cash', 'Modified Cash'),
    ], string="Accounting Basis", default='accrual',
        help="Per the AA Manual, cash basis is NOT permitted.",
    )

    # FRS submission email
    frs_email = fields.Char(
        "FRS Submission Email",
        default="adaptive@elks.cloud",
        help="Email address for FRS CSV submissions.",
    )

    # Tax info
    group_exemption_number = fields.Char(
        "Group Exemption Number (GEN)",
        default="1156",
        help="IRS Group Exemption Number for BPOE lodges.",
    )
    tax_classification = fields.Char(
        "Tax Classification",
        default="501(c)(8)",
    )
    fiscal_year_start_month = fields.Integer(
        "Fiscal Year Start Month", default=4,
        help="Month number (4 = April).",
    )
    fiscal_year_start_day = fields.Integer(
        "Fiscal Year Start Day", default=1,
    )

    # Year-end settings
    closing_method = fields.Selection([
        ('year_end_accounts', 'Year-End Closing Accounts (99001/99002)'),
        ('zero_out', 'Zero Out Accounts'),
        ('direct_equity', 'Direct Unrestricted Equity Adjustment'),
    ], string="Restricted Fund Closing Method",
        default='year_end_accounts',
        help="Preferred method per the AA Manual Appendix J.",
    )

    def action_open_lodge_settings(self):
        """Open the singleton Lodge Settings record in a form view.

        If no record exists yet, create one with sensible defaults so the
        user always lands on an editable form instead of a blank 'New' record.
        """
        settings = self.sudo().search([], limit=1)
        if not settings:
            settings = self.sudo().create({
                "name": "My Lodge",
                "lodge_number": "0000",
            })
        return {
            "type": "ir.actions.act_window",
            "res_model": "elks.lodge.settings",
            "res_id": settings.id,
            "view_mode": "form",
            "target": "current",
        }
