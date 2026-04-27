# -*- coding: utf-8 -*-
"""FRS Submission Tracking.

Tracks monthly FRS submissions to Grand Lodge via Adaptive Insights.
Each record represents one month's submission with status tracking,
CSV generation, and deadline monitoring.

Per the FRS Manual:
- Monthly actuals CSV due by 3rd Friday of the following month
- Fines: $100/month late, up to $500 max
- CSV format: LodgeNumber, LodgeGLAccount, Date, Amount
"""
import base64
import csv
import io
import datetime

from odoo import api, fields, models, _
from odoo.exceptions import UserError

import logging

_logger = logging.getLogger(__name__)


def _third_friday(year, month):
    """Return the date of the 3rd Friday in the given month."""
    # First day of month
    d = datetime.date(year, month, 1)
    # Find first Friday (weekday 4)
    offset = (4 - d.weekday()) % 7
    first_friday = d + datetime.timedelta(days=offset)
    return first_friday + datetime.timedelta(weeks=2)


class ElksFrsSubmission(models.Model):
    _name = "elks.frs.submission"
    _description = "FRS Monthly Submission"
    _order = "period_end desc"
    _inherit = ["mail.thread", "mail.activity.mixin"]

    name = fields.Char(
        "Period", compute="_compute_name", store=True,
    )
    period_start = fields.Date(
        "Period Start", required=True, index=True,
    )
    period_end = fields.Date(
        "Period End", required=True, index=True,
    )
    lodge_year = fields.Char(
        "Lodge Year", compute="_compute_lodge_year", store=True,
    )
    due_date = fields.Date(
        "Due Date", compute="_compute_due_date", store=True,
        help="3rd Friday of the month following the reporting period.",
    )
    state = fields.Selection([
        ('draft', 'Not Started'),
        ('generated', 'CSV Generated'),
        ('submitted', 'Submitted'),
        ('accepted', 'Accepted'),
        ('rejected', 'Rejected'),
    ], string="Status", default='draft', tracking=True, index=True)

    is_overdue = fields.Boolean(
        "Overdue", compute="_compute_is_overdue", store=True,
    )
    submission_date = fields.Date("Submitted On", tracking=True)
    notes = fields.Text("Notes")

    # CSV file attachment
    csv_file = fields.Binary("Actuals CSV", readonly=True)
    csv_filename = fields.Char("CSV Filename")

    # Summary totals
    total_amount = fields.Monetary(
        "Total Amount", compute="_compute_totals",
        currency_field='currency_id',
    )
    line_count = fields.Integer(
        "Line Count", compute="_compute_totals",
    )
    currency_id = fields.Many2one(
        "res.currency", default=lambda self: self.env.company.currency_id,
    )

    @api.depends("period_start", "period_end")
    def _compute_name(self):
        for rec in self:
            if rec.period_start:
                rec.name = rec.period_start.strftime("%B %Y")
            else:
                rec.name = "New Period"

    @api.depends("period_start")
    def _compute_lodge_year(self):
        for rec in self:
            if rec.period_start:
                d = rec.period_start
                start_yr = d.year if d.month >= 4 else d.year - 1
                rec.lodge_year = f"{start_yr}-{start_yr + 1}"
            else:
                rec.lodge_year = False

    @api.depends("period_end")
    def _compute_due_date(self):
        for rec in self:
            if rec.period_end:
                # Due by 3rd Friday of the month following period_end
                next_month = rec.period_end.month + 1
                next_year = rec.period_end.year
                if next_month > 12:
                    next_month = 1
                    next_year += 1
                rec.due_date = _third_friday(next_year, next_month)
            else:
                rec.due_date = False

    @api.depends("due_date", "state")
    def _compute_is_overdue(self):
        today = fields.Date.context_today(self)
        for rec in self:
            rec.is_overdue = bool(
                rec.due_date
                and today > rec.due_date
                and rec.state in ('draft', 'generated')
            )

    def _compute_totals(self):
        """Compute totals from posted journal lines in the period."""
        for rec in self:
            lines = self.env["elks.journal.entry.line"].search([
                ("date", ">=", rec.period_start),
                ("date", "<=", rec.period_end),
                ("entry_state", "=", "posted"),
            ])
            rec.line_count = len(lines)
            rec.total_amount = sum(lines.mapped("debit"))

    def action_generate_csv(self):
        """Generate the FRS actuals CSV per the FRS Manual format.

        CSV columns: LodgeNumber, LodgeGLAccount, Date, Amount
        - One row per account that had activity in the period
        - Amount is net (debits positive, credits negative for income)
        """
        self.ensure_one()
        settings = self.env["elks.lodge.settings"].sudo().search([], limit=1)
        if not settings or not settings.lodge_number:
            raise UserError(_(
                "Please configure your Lodge Number in Elks FRS → Settings "
                "before generating the CSV."
            ))

        lodge_num = settings.lodge_number

        # Get all posted journal lines in the period
        lines = self.env["elks.journal.entry.line"].search([
            ("date", ">=", self.period_start),
            ("date", "<=", self.period_end),
            ("entry_state", "=", "posted"),
        ])

        if not lines:
            raise UserError(_(
                "No posted journal entries found for %s."
            ) % self.name)

        # Aggregate by account and date
        # Format: LodgeNumber, LodgeGLAccount, Date, Amount
        rows = {}
        for line in lines:
            acct_code = line.account_id.code
            if line.account_id.subaccount:
                acct_code = f"{acct_code}{line.account_id.subaccount}"
            date_str = line.date.strftime("%m/%d/%Y")
            key = (acct_code, date_str)
            amount = line.debit - line.credit
            rows[key] = rows.get(key, 0.0) + amount

        # Write CSV
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["LodgeNumber", "LodgeGLAccount", "Date", "Amount"])
        for (acct, date_str), amount in sorted(rows.items()):
            if abs(amount) >= 0.005:  # skip zero-amount rows
                writer.writerow([lodge_num, acct, date_str, f"{amount:.2f}"])

        csv_data = output.getvalue()
        period_label = self.period_start.strftime("%Y%m")
        filename = f"FRS_Actuals_{lodge_num}_{period_label}.csv"

        self.write({
            "csv_file": base64.b64encode(csv_data.encode("utf-8")),
            "csv_filename": filename,
            "state": "generated",
        })

        _logger.info(
            "Generated FRS actuals CSV for %s: %d rows",
            self.name, len(rows),
        )

    def action_mark_submitted(self):
        self.write({
            "state": "submitted",
            "submission_date": fields.Date.context_today(self),
        })

    def action_mark_accepted(self):
        self.write({"state": "accepted"})

    def action_mark_rejected(self):
        self.write({"state": "rejected"})

    def action_reset_draft(self):
        self.write({"state": "draft", "csv_file": False, "csv_filename": False})

    @api.model
    def cron_create_monthly_submission(self):
        """Auto-create next month's FRS submission record.

        Runs on the 1st of each month, creates a record for the
        previous month's reporting period.
        """
        today = fields.Date.context_today(self)
        # Previous month
        if today.month == 1:
            period_start = datetime.date(today.year - 1, 12, 1)
        else:
            period_start = datetime.date(today.year, today.month - 1, 1)

        # End of previous month
        period_end = today.replace(day=1) - datetime.timedelta(days=1)

        # Check if already exists
        existing = self.search([
            ("period_start", "=", period_start),
            ("period_end", "=", period_end),
        ], limit=1)

        if not existing:
            self.create({
                "period_start": period_start,
                "period_end": period_end,
            })
            _logger.info(
                "Created FRS submission for %s",
                period_start.strftime("%B %Y"),
            )

    @api.model
    def cron_check_overdue(self):
        """Send reminders for overdue FRS submissions."""
        today = fields.Date.context_today(self)
        overdue = self.search([
            ("state", "in", ("draft", "generated")),
            ("due_date", "<", today),
        ])
        for rec in overdue:
            days_late = (today - rec.due_date).days
            fine = min(days_late // 30 * 100, 500)
            _logger.warning(
                "FRS submission for %s is %d days overdue. "
                "Estimated fine: $%d",
                rec.name, days_late, fine,
            )
