# -*- coding: utf-8 -*-
"""FRS Export Wizard.

Provides a user-friendly dialog for generating FRS CSV exports
for both actuals and budgets.
"""
import base64
import csv
import datetime
import io

from odoo import api, fields, models, _
from odoo.exceptions import UserError


class FrsExportWizard(models.TransientModel):
    _name = "frs.export.wizard"
    _description = "FRS CSV Export Wizard"

    export_type = fields.Selection([
        ('actuals', 'Monthly Actuals'),
        ('budget', 'Annual Budget'),
    ], string="Export Type", default='actuals', required=True)

    # Actuals options
    period_start = fields.Date("Period Start")
    period_end = fields.Date("Period End")

    # Budget options
    budget_id = fields.Many2one("elks.budget", string="Budget")

    # Output
    csv_file = fields.Binary("Generated CSV", readonly=True)
    csv_filename = fields.Char("Filename")
    state = fields.Selection([
        ('setup', 'Setup'),
        ('done', 'Done'),
    ], default='setup')

    @api.onchange('export_type')
    def _onchange_export_type(self):
        if self.export_type == 'actuals':
            # Default to previous month
            today = fields.Date.context_today(self)
            if today.month == 1:
                self.period_start = datetime.date(today.year - 1, 12, 1)
            else:
                self.period_start = datetime.date(today.year, today.month - 1, 1)
            self.period_end = today.replace(day=1) - datetime.timedelta(days=1)

    def action_export(self):
        self.ensure_one()
        settings = self.env["elks.lodge.settings"].sudo().search([], limit=1)
        if not settings or not settings.lodge_number:
            raise UserError(_(
                "Please configure your Lodge Number in Elks FRS → Settings."
            ))

        if self.export_type == 'actuals':
            return self._export_actuals(settings)
        else:
            return self._export_budget(settings)

    def _export_actuals(self, settings):
        if not self.period_start or not self.period_end:
            raise UserError(_("Please specify the reporting period."))

        if self.period_start > self.period_end:
            raise UserError(_(
                "Period Start (%(s)s) cannot be after Period End (%(e)s)."
            ) % {'s': self.period_start, 'e': self.period_end})

        lodge_num = settings.lodge_number

        lines = self.env["elks.journal.entry.line"].search([
            ("date", ">=", self.period_start),
            ("date", "<=", self.period_end),
            ("entry_state", "=", "posted"),
        ])

        if not lines:
            raise UserError(_(
                "No posted journal entries found between %(s)s and %(e)s. "
                "Verify the period and that journal entries have been posted "
                "(not just saved as draft)."
            ) % {'s': self.period_start, 'e': self.period_end})

        # Validate accounts: every line must have an account with a code.
        bad_lines = lines.filtered(
            lambda l: not l.account_id or not l.account_id.code
        )
        if bad_lines:
            raise UserError(_(
                "%(n)d journal line(s) reference an account with no GL code. "
                "Fix or assign codes to those accounts before exporting."
            ) % {'n': len(bad_lines)})

        rows = {}
        skipped_zero = 0
        for line in lines:
            acct_code = line.account_id.code
            if line.account_id.subaccount:
                acct_code = f"{acct_code}{line.account_id.subaccount}"
            date_str = line.date.strftime("%m/%d/%Y")
            key = (acct_code, date_str)
            amount = line.debit - line.credit
            rows[key] = rows.get(key, 0.0) + amount

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["LodgeNumber", "LodgeGLAccount", "Date", "Amount"])
        for (acct, date_str), amount in sorted(rows.items()):
            if abs(amount) >= 0.005:
                writer.writerow([lodge_num, acct, date_str, f"{amount:.2f}"])
            else:
                skipped_zero += 1

        csv_data = output.getvalue()
        period_label = self.period_start.strftime("%Y%m")
        filename = f"FRS_Actuals_{lodge_num}_{period_label}.csv"

        # The number of zero-net rows skipped is tracked but not surfaced
        # to the UI here — could be added to a chatter note if desired.

        self.write({
            "csv_file": base64.b64encode(csv_data.encode("utf-8")),
            "csv_filename": filename,
            "state": "done",
        })
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

    def _export_budget(self, settings):
        if not self.budget_id:
            raise UserError(_("Please select a budget."))

        lodge_num = settings.lodge_number
        budget = self.budget_id

        if not budget.fiscal_year_end:
            raise UserError(_(
                "Budget '%(b)s' has no Fiscal Year End set."
            ) % {'b': budget.name})

        # Sanity check: Elks fiscal years end March 31
        fye = budget.fiscal_year_end
        if not (fye.month == 3 and fye.day == 31):
            raise UserError(_(
                "Budget Fiscal Year End is %(d)s but Elks fiscal years "
                "must end March 31.  Please correct the date before exporting."
            ) % {'d': fye.strftime("%m/%d/%Y")})

        if not budget.line_ids:
            raise UserError(_(
                "Budget '%(b)s' has no line items.  Add budget lines before "
                "exporting."
            ) % {'b': budget.name})

        # Validate accounts
        bad_lines = budget.line_ids.filtered(
            lambda l: not l.account_id or not l.account_id.code
        )
        if bad_lines:
            raise UserError(_(
                "%(n)d budget line(s) have no GL account or no account code."
            ) % {'n': len(bad_lines)})

        fye_str = fye.strftime("%m/%d/%Y")

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "LodgeNumber", "LodgeGLAccount", "FYE", "Version", "Annual",
        ])
        for line in budget.line_ids.sorted(key=lambda l: l.account_id.code):
            acct_code = line.account_id.code
            if line.account_id.subaccount:
                acct_code = f"{acct_code}{line.account_id.subaccount}"
            writer.writerow([
                lodge_num, acct_code, fye_str,
                budget.version or "1",
                f"{line.amount:.2f}",
            ])

        csv_data = output.getvalue()
        filename = f"FRS_Budget_{lodge_num}_{budget.lodge_year}.csv"

        self.write({
            "csv_file": base64.b64encode(csv_data.encode("utf-8")),
            "csv_filename": filename,
            "state": "done",
        })
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }
