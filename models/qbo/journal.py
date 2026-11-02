"""Keep the ledger side of a completed QBO reconciliation immutable."""
from odoo import api, models, _
from odoo.exceptions import UserError


class QBOJournal(models.Model):
    _inherit = 'elks.journal.entry'

    def _check_qbo_immutable(self):
        self.flush_recordset()
        for entry in self.sorted('id'):
            self.env.cr.execute('SELECT id FROM elks_journal_entry WHERE id=%s FOR UPDATE', [entry.id])
        if self.env['elks.qbo.expense'].sudo().search_count([('journal_id', 'in', self.ids)]):
            raise UserError(_('This entry is reconciled to QuickBooks. Use an audited adjustment instead of editing or cancelling it.'))

    def write(self, vals):
        if {'date', 'currency_id', 'line_ids', 'state', 'entry_number'}.intersection(vals):
            self.check_access('write')
            self._check_qbo_immutable()
        return super().write(vals)

    def unlink(self):
        self.check_access('unlink')
        self._check_qbo_immutable()
        return super().unlink()


class QBOJournalLine(models.Model):
    _inherit = 'elks.journal.entry.line'

    @api.model_create_multi
    def create(self, vals_list):
        entries = self.env['elks.journal.entry'].browse([vals['entry_id'] for vals in vals_list if vals.get('entry_id')])
        entries.check_access('write')
        entries._check_qbo_immutable()
        return super().create(vals_list)

    def write(self, vals):
        if {'entry_id', 'account_id', 'debit', 'credit'}.intersection(vals):
            self.check_access('write')
            entries = self.entry_id | self.env['elks.journal.entry'].browse(vals.get('entry_id', []))
            entries.check_access('write')
            entries._check_qbo_immutable()
        return super().write(vals)

    def unlink(self):
        self.check_access('unlink')
        self.entry_id.check_access('write')
        self.entry_id._check_qbo_immutable()
        return super().unlink()
