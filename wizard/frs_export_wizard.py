# -*- coding: utf-8 -*-
"""FRS Export Wizard.

Provides a user-friendly dialog for generating FRS CSV exports
for both actuals and budgets.
"""
import base64
import calendar
import csv
import datetime
import io

from odoo import api, fields, models, _
from odoo.exceptions import UserError


class FrsExportWizard(models.TransientModel):
    _name = "frs.export.wizard"
    _description = "FRS CSV Export Wizard"

    export_type = fields.Selection([
        ('actuals', 'Actuals'),
        ('budget', 'Annual Budget'),
    ], string="Export Type", default='actuals', required=True)

    # Actuals options
    # Period preset — defaults to Fiscal YTD because FRS Adaptive
    # overwrites prior data on resubmission, so sending the full
    # year-to-date each time keeps every prior correction in sync.
    # Monthly-only is the risky mode (misses backdated fixes).
    period_preset = fields.Selection([
        ('fytd',          'Fiscal YTD (Apr 1 → last complete month)'),
        ('fytd_today',    'Fiscal YTD (Apr 1 → today, includes partial current month)'),
        ('this_month',    'This Month to Date'),
        ('last_month',    'Prior Full Month'),
        ('current_fy',    'Current Fiscal Year (full Apr 1 → Mar 31)'),
        ('prior_fy',      'Prior Fiscal Year (full)'),
        ('custom',        'Custom Date Range'),
    ], string="Period", default='fytd', required=True,
       help="FRS Adaptive overwrites prior submissions on resubmit, so "
            "sending the full year-to-date each time is the safest mode — "
            "it carries forward every correction. Switch to Prior Full "
            "Month only when you're explicitly submitting a single month.")
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

    # ==================================================================
    # Period preset resolution
    # ==================================================================
    @staticmethod
    def _fy_bounds(d):
        """Elks fiscal year (Apr 1 → Mar 31) containing date d.

        Returns (fy_start, fy_end) as date objects.
        A date on 2026-11-15 → start 2026-04-01, end 2027-03-31.
        A date on 2026-02-15 → start 2025-04-01, end 2026-03-31.
        """
        if d.month >= 4:
            return (datetime.date(d.year, 4, 1),
                    datetime.date(d.year + 1, 3, 31))
        return (datetime.date(d.year - 1, 4, 1),
                datetime.date(d.year, 3, 31))

    @staticmethod
    def _last_complete_month_end(d):
        """End-of-month date for the last fully completed month before d."""
        first_of_this_month = d.replace(day=1)
        return first_of_this_month - datetime.timedelta(days=1)

    def _apply_preset(self):
        """Populate period_start/period_end from the preset selector."""
        today = fields.Date.context_today(self)
        fy_start, fy_end = self._fy_bounds(today)
        preset = self.period_preset or 'fytd'

        if preset == 'fytd':
            # Apr 1 → end of last complete month, clamped to fiscal year.
            end = self._last_complete_month_end(today)
            # If today is April 1-30, there's no completed month in this FY
            # yet — fall back to including the current (partial) month so
            # the export isn't empty.
            self.period_start = fy_start
            self.period_end = end if end >= fy_start else today
        elif preset == 'fytd_today':
            self.period_start = fy_start
            self.period_end = today
        elif preset == 'this_month':
            self.period_start = today.replace(day=1)
            self.period_end = today
        elif preset == 'last_month':
            end = self._last_complete_month_end(today)
            self.period_start = end.replace(day=1)
            self.period_end = end
        elif preset == 'current_fy':
            self.period_start = fy_start
            self.period_end = fy_end
        elif preset == 'prior_fy':
            prior_end = datetime.date(fy_start.year, 3, 31)
            self.period_start = datetime.date(prior_end.year - 1, 4, 1)
            self.period_end = prior_end
        # 'custom' intentionally leaves dates untouched so the user can set them.

    @api.onchange('export_type', 'period_preset')
    def _onchange_period(self):
        if self.export_type == 'actuals' and self.period_preset != 'custom':
            self._apply_preset()

    @api.model
    def default_get(self, fields_list):
        """Seed the dates from the default preset on wizard open."""
        vals = super().default_get(fields_list)
        if vals.get('export_type', 'actuals') == 'actuals':
            preset = vals.get('period_preset', 'fytd')
            if preset != 'custom':
                # Use a transient instance to compute dates via the shared logic.
                tmp = self.new(vals)
                tmp._apply_preset()
                vals['period_start'] = tmp.period_start
                vals['period_end'] = tmp.period_end
        return vals

    def _period_label(self):
        """Produce a descriptive filename tag based on the preset + dates."""
        self.ensure_one()
        start, end = self.period_start, self.period_end
        preset = self.period_preset
        fy_start, fy_end = self._fy_bounds(start)

        if preset in ('fytd', 'fytd_today'):
            # YTD ends at a snapshot; name by through-date for clarity.
            # Example: YTD_2026-04_through_2026-10
            tag = 'YTD'
            if preset == 'fytd_today':
                tag = 'YTD_partial'
            return f"{tag}_{fy_start.strftime('%Y-%m')}_through_{end.strftime('%Y-%m')}"
        if preset == 'current_fy' or preset == 'prior_fy':
            # Full fiscal year → FY2027 means Apr 2026 → Mar 2027.
            return f"FY{fy_end.year}"
        if preset == 'last_month':
            return start.strftime('%Y-%m')
        if preset == 'this_month':
            return f"{start.strftime('%Y-%m')}_partial"
        # Custom: fall back to range or single-month formatting
        if start.year == end.year and start.month == end.month:
            return start.strftime('%Y-%m')
        return f"{start.strftime('%Y-%m')}_to_{end.strftime('%Y-%m')}"

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
        # Filename matches the FRS 2019 Introduction + QBO Online Guide
        # PDF section titles exactly: "Actual Monthly Activity File".
        filename = (f"Lodge {lodge_num} - Actual Monthly Activity - "
                    f"{self._period_label()}.csv")

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
        # Filename matches the FRS 2019 Introduction + Grand Lodge
        # Actuals/Budgets PDF section title: "Annual Budget File".
        # FYE naming follows the FRS spec (FYE 2027 = Apr 2026 → Mar 2027).
        fye_year = budget.fiscal_year_end.year if budget.fiscal_year_end else ''
        filename = (f"Lodge {lodge_num} - Annual Budget - "
                    f"FYE {fye_year or budget.lodge_year}.csv")

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
