from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestQBOCore(TransactionCase):
    def test_models_available_without_legacy_addon(self):
        for name in ('elks.qbo.connection', 'elks.qbo.expense', 'elks.qbo.account.map', 'elks.qbo.vendor.map', 'elks.qbo.match'):
            self.assertIn(name, self.env.registry)
        self.assertTrue(self.env.ref('elksfrs.qbo_action_connections'))
        self.assertTrue(self.env.ref('elksfrs.qbo_expense_product'))

    def test_legacy_ownership_removed(self):
        self.assertFalse(self.env['ir.model.data'].search_count([('module', '=', 'elksfrs_qbo')]))

    def test_missing_purchase_workflow_is_reviewable(self):
        if 'x_approval_state' in self.env['purchase.order']._fields:
            self.skipTest('This case exercises FRS without the optional purchase workflow')
        conn = self.env['elks.qbo.connection'].create({})
        self.assertFalse(conn.purchase_workflow_available)
        expense = self.env['elks.qbo.expense']._upsert(conn, {'Id': 'no-workflow', 'TxnDate': '2026-10-01', 'TotalAmt': 0})
        self.assertEqual(expense.state, 'review')
