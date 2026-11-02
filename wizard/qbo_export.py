import base64
import calendar
import csv
import io
from collections import defaultdict
from decimal import Decimal

from odoo import models, _
from odoo.exceptions import UserError


class FRSExport(models.TransientModel):
    _inherit = 'frs.export.wizard'

    def _export_actuals(self, settings):
        if not self.period_start or not self.period_end or self.period_start > self.period_end:
            raise UserError(_('Specify a valid reporting period.'))
        if self.period_start.day != 1 or self.period_end.day != calendar.monthrange(self.period_end.year, self.period_end.month)[1]:
            raise UserError(_('FRS actuals require complete calendar months.'))
        expenses = self.env['elks.qbo.expense'].search([
            ('date', '>=', self.period_start), ('date', '<=', self.period_end)])
        if any(not rec.journal_id or rec.journal_id.state != 'posted' or not rec.journal_id.is_balanced or rec.source_changed or rec.source_missing or rec.review_reason for rec in expenses):
            raise UserError(_('Resolve and record all imported QuickBooks expenses for this period before exporting FRS actuals.'))
        result = super()._export_actuals(settings)
        # FRS Exhibit J: monthly debit-minus-credit totals dated at period end.
        rows = csv.reader(io.StringIO(base64.b64decode(self.csv_file).decode()))
        header = next(rows)
        totals = defaultdict(Decimal)
        for lodge, account, date, amount in rows:
            month, _day, year = map(int, date.split('/'))
            period = f'{month:02d}/{calendar.monthrange(year, month)[1]:02d}/{year}'
            totals[(lodge, account, period)] += Decimal(amount)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(header)
        for key, amount in sorted(totals.items()):
            if amount:
                writer.writerow([*key, f'{amount:.2f}'])
        self.csv_file = base64.b64encode(output.getvalue().encode())
        return result

    def _export_budget(self, settings):
        if self.budget_id and self.budget_id.net_budget < 0:
            raise UserError(_('FRS does not accept a budgeted loss.'))
        return super()._export_budget(settings)
