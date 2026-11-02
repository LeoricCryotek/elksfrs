from odoo.tests import TransactionCase, tagged
from unittest import SkipTest
from odoo.exceptions import UserError


@tagged('post_install', '-at_install')
class TestQBO(TransactionCase):
    def setUp(self):
        super().setUp()
        if 'x_approval_state' not in self.env['purchase.order']._fields:
            raise SkipTest('Purchase approval tests require optional elkspurchase')
        self.conn = self.env['elks.qbo.connection'].create({'auto_requisition': False})
        self.partner = self.env['res.partner'].create({'name': 'QBO test vendor'})
        self.env['elks.qbo.vendor.map'].create({'connection_id': self.conn.id, 'qbo_id': '8',
            'name': 'QBO test vendor', 'partner_id': self.partner.id})
        self.cost = self.env['elks.account'].search([('account_type', '=', 'expense'), ('is_header', '=', False)], limit=1)
        self.bank = self.env['elks.account'].search([('code', '=', '10100'), ('is_header', '=', False)], limit=1)
        for qbo_id, account in [('30', self.cost), ('9', self.bank)]:
            self.env['elks.qbo.account.map'].create({'connection_id': self.conn.id, 'qbo_id': qbo_id,
                'name': qbo_id, 'source_type': 'Bank' if qbo_id == '9' else 'Expense', 'account_id': account.id})
        self.row = {'Id': '17', 'SyncToken': '0', 'TxnDate': '2026-10-01', 'TotalAmt': 12.5,
                    'AccountRef': {'value': '9'}, 'EntityRef': {'value': '8'},
                    'Line': [{'Amount': 12.5, 'DetailType': 'AccountBasedExpenseLineDetail',
                              'AccountBasedExpenseLineDetail': {'AccountRef': {'value': '30'}}}]}

    def _import(self):
        return self.env['elks.qbo.expense']._upsert(self.conn, self.row)

    def test_idempotent_pull(self):
        first = self._import()
        self.assertEqual(first, self._import())
        self.assertFalse(first.journal_id)

    def test_unmapped_review(self):
        self.row['Line'][0]['AccountBasedExpenseLineDetail']['AccountRef']['value'] = 'unmapped'
        self.assertTrue(self._import().review_reason)

    def test_create_requires_approval(self):
        expense = self._import()
        expense.action_create_requisition()
        self.assertEqual(expense.purchase_id.x_approval_state, 'board')
        self.assertFalse(expense.journal_id)
        with self.assertRaises(UserError): expense.action_record_frs()

    def test_no_duplicate_requisitions(self):
        self.conn.auto_requisition = True
        expense = self._import()
        po = expense.purchase_id
        self.assertTrue(po)
        self.assertEqual(self._import().purchase_id, po)

    def test_changed_matched_source_preserves_po(self):
        expense = self._import()
        expense.action_create_requisition()
        self.row['TotalAmt'] = 14
        self.row['Line'][0]['Amount'] = 14
        self._import()
        self.assertTrue(expense.source_changed)
        self.assertEqual(expense.amount, 12.5)
        self.assertEqual(expense.purchase_id.amount_total, 12.5)

    def test_ordered_path_blocked(self):
        expense = self._import()
        expense.action_create_requisition()
        with self.assertRaises(UserError): expense.purchase_id.order_line.action_mark_ordered()

    def test_vendor_mismatch(self):
        expense = self._import()
        expense.action_create_requisition()
        self.assertEqual(expense.purchase_id.partner_id, self.partner)

    def test_unsupported_tax_no_requisition(self):
        self.conn.auto_requisition = True
        self.row['TxnTaxDetail'] = {'TotalTax': 1}
        expense = self._import()
        self.assertTrue(expense.review_reason)
        self.assertFalse(expense.purchase_id)

    def test_approved_expense_records_once_and_locks_journal(self):
        income = self.env['elks.account'].search([('account_type', '=', 'income'), ('is_header', '=', False)], limit=1)
        budget = self.env['elks.budget'].create({'fiscal_year_end': '2027-03-31', 'line_ids': [
            (0, 0, {'account_id': self.cost.id, 'amount': 100}),
            (0, 0, {'account_id': income.id, 'amount': 100}),
        ]})
        budget.action_submit_board()
        budget.action_board_approve()
        budget.action_submit_floor()
        budget.action_floor_approve()
        expense = self._import()
        expense.action_create_requisition()
        po = expense.purchase_id
        po.action_board_approve()
        po.action_floor_approve()
        # Dummy test signatures, not real officer authorizations.
        po.write({'x_er_signature': 'dGVzdA==', 'x_treasurer_signature': 'dGVzdA==', 'x_secretary_signature': 'dGVzdA=='})
        expense.action_record_frs()
        journal = expense.journal_id
        self.assertEqual(journal.state, 'posted')
        self.assertTrue(journal.is_balanced)
        self.assertEqual(journal.total_debit, 12.5)
        self.assertEqual(po.order_line.x_journal_entry_id, journal)
        expense.action_record_frs()
        self.assertEqual(expense.journal_id, journal)
        with self.assertRaises(UserError):
            journal.action_draft()
