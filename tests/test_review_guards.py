from datetime import timedelta
from unittest.mock import Mock, patch

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, tagged, new_test_user


@tagged('post_install', '-at_install')
class TestReviewGuards(TransactionCase):
    def setUp(self):
        super().setUp()
        self.conn = self.env['elks.qbo.connection'].create({'auto_requisition': False})

    def test_source_rows_cannot_be_forged(self):
        with self.assertRaises(AccessError):
            self.env['elks.qbo.expense'].create({'name': 'Forged', 'connection_id': self.conn.id, 'qbo_id': 'forged'})

    def _expense(self):
        return self.env['elks.qbo.expense']._upsert(self.conn, {'Id': '1', 'TxnDate': '2026-10-01', 'TotalAmt': 0})

    def test_source_status_cannot_be_overwritten(self):
        expense = self._expense()
        for vals in ({'source_changed': False}, {'journal_id': False}, {'amount': 500}, {'payload': {}}):
            with self.subTest(vals=vals), self.assertRaises(AccessError):
                expense.write(vals)

    def test_api_fault_or_malformed_response_is_not_empty_result(self):
        self.conn.write({'realm_id': '1234', 'access_token': 'fake', 'expires_at': fields.Datetime.now() + timedelta(hours=1)})
        for data in ({'Fault': {}}, {}, {'QueryResponse': None}, {'QueryResponse': {'Purchase': [{}]}}):
            response = Mock(status_code=200)
            response.json.return_value = data
            with patch('odoo.addons.elksfrs.models.qbo.connection.requests.get', return_value=response):
                with self.assertRaises(UserError):
                    list(self.conn._query('Purchase'))

    def test_valid_empty_result_remains_supported(self):
        self.conn.write({'realm_id': '1234', 'access_token': 'fake', 'expires_at': fields.Datetime.now() + timedelta(hours=1)})
        response = Mock(status_code=200)
        response.json.return_value = {'QueryResponse': {}}
        with patch('odoo.addons.elksfrs.models.qbo.connection.requests.get', return_value=response):
            self.assertEqual(list(self.conn._query('Purchase')), [])

    def test_reconciled_journal_is_immutable(self):
        expense = self._expense()
        journal = self.env['elks.journal.entry'].create({'memo': 'Test'})
        account = self.env['elks.account'].search([('is_header', '=', False)], limit=1)
        line = self.env['elks.journal.entry.line'].create({'entry_id': journal.id, 'account_id': account.id})
        expense._write_import({'journal_id': journal.id})
        for operation in (lambda: journal.write({'date': '2026-01-01'}), journal.action_cancel,
                          lambda: line.write({'debit': 5}), line.unlink, journal.unlink,
                          lambda: self.env['elks.journal.entry.line'].create({'entry_id': journal.id, 'account_id': account.id})):
            with self.assertRaises(UserError):
                operation()

    def test_draft_budget_cannot_skip_approval(self):
        budget = self.env['elks.budget'].create({'fiscal_year_end': '2027-03-31'})
        with self.assertRaises(ValidationError):
            budget.action_floor_approve()
        with self.assertRaises(ValidationError):
            self.env['elks.budget'].create({'fiscal_year_end': '2027-03-31', 'state': 'floor_approved'})

    def test_transfer_cannot_cross_budgets(self):
        budgets = self.env['elks.budget'].create([{'fiscal_year_end': '2027-03-31'}, {'fiscal_year_end': '2028-03-31'}])
        account = self.env['elks.account'].search([('is_header', '=', False)], limit=1)
        lines = self.env['elks.budget.line'].create([{'budget_id': budget.id, 'account_id': account.id, 'amount': 100} for budget in budgets])
        with self.env.cr.savepoint(), self.assertRaises(ValidationError):
            self.env['elks.budget.transfer'].create({'budget_id': budgets[0].id, 'from_line_id': lines[0].id,
                'to_line_id': lines[1].id, 'amount': 5, 'reason': 'Invalid cross-budget transfer'})

    def test_manager_cannot_repoint_connection_realm(self):
        manager = new_test_user(self.env(context=dict(self.env.context, no_reset_password=True)), login='frs_review_manager', groups='elksfrs.group_elksfrs_manager')
        with self.assertRaises(AccessError):
            self.conn.with_user(manager).write({'realm_id': '99999'})

    def test_new_account_can_be_mapped_after_first_recording(self):
        expense = self._expense()
        journal = self.env['elks.journal.entry'].create({'memo': 'Test'})
        expense._write_import({'journal_id': journal.id})
        mapping = self.env['elks.qbo.account.map'].create({'connection_id': self.conn.id, 'qbo_id': 'new', 'name': 'New'})
        account = self.env['elks.account'].search([('is_header', '=', False)], limit=1)
        mapping.account_id = account
        self.assertEqual(mapping.account_id, account)

    def test_account_names_are_escaped_in_budget_html(self):
        account = self.env['elks.account'].search([('account_type', '=', 'expense'), ('is_header', '=', False)], limit=1)
        account.name = '<img src=x onerror=alert(1)>'
        budget = self.env['elks.budget'].create({'fiscal_year_end': '2027-03-31',
            'line_ids': [(0, 0, {'account_id': account.id, 'amount': 1})]})
        self.assertIn('&lt;img', budget.pnl_html)
        self.assertNotIn('<img src=x', budget.pnl_html)
