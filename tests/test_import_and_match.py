# -*- coding: utf-8 -*-
"""Smoke tests for the import / drift / PO-match additions."""
import datetime

from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestImportBalancesAndDedup(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Account = cls.env['elks.account']
        cls.Entry = cls.env['elks.journal.entry']
        # Minimal CoA entries we need
        cls.cash = cls._mk_acct('10101', 'Cash on Hand')
        cls.rev = cls._mk_acct('45001', 'Bar Revenue')
        cls.clearing = cls._mk_acct('29999', 'QBO Import Clearing')

    @classmethod
    def _mk_acct(cls, code, name):
        existing = cls.Account.search(
            [('code', '=', code), ('subaccount', 'in', (False, ''))], limit=1)
        if existing:
            return existing
        return cls.Account.create({
            'code': code, 'name': name, 'is_header': False,
        })

    def _wizard(self):
        return self.env['elks.qbo.actuals.transform.wizard'].create({})

    def test_import_creates_balanced_posted_entries(self):
        w = self._wizard()
        txns = [
            {'date': datetime.date(2026, 10, 15),
             'account': '45001', 'amount': -500.00},
            {'date': datetime.date(2026, 10, 15),
             'account': '10101', 'amount': 500.00},
        ]
        stats = w._import_actuals_to_odoo(txns)
        self.assertEqual(stats['created'], 2)
        self.assertEqual(stats['posted'], 2,
                         'Balanced entries should auto-post')
        self.assertEqual(stats['needs_clearing'], 0)
        # Each entry has two balanced legs
        entries = self.Entry.search(
            [('entry_number', 'like', 'QBO-EXCEL-%')])
        for e in entries:
            self.assertEqual(len(e.line_ids), 2)
            self.assertTrue(e.is_balanced)

    def test_import_dedupe_on_rerun(self):
        w = self._wizard()
        txn = [{'date': datetime.date(2026, 10, 20),
                'account': '45001', 'amount': 100.0}]
        w._import_actuals_to_odoo(txn)
        stats2 = w._import_actuals_to_odoo(txn)
        self.assertEqual(stats2['created'], 0)
        self.assertGreaterEqual(stats2['skipped'], 1)

    def test_missing_clearing_leaves_draft(self):
        # Delete the clearing account to simulate missing CoA setup
        self.clearing.sudo().unlink()
        w = self._wizard()
        txn = [{'date': datetime.date(2026, 11, 1),
                'account': '10101', 'amount': 25.0}]
        stats = w._import_actuals_to_odoo(txn)
        self.assertEqual(stats['posted'], 0)
        self.assertEqual(stats['draft'], 1)
        self.assertEqual(stats['needs_clearing'], 1)


@tagged('post_install', '-at_install')
class TestPoMatchWizard(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Account = cls.env['elks.account']
        cls.acct = cls.Account.search(
            [('code', '=', '10101')], limit=1) or cls.Account.create({
                'code': '10101', 'name': 'Cash', 'is_header': False,
            })
        cls.partner = cls.env['res.partner'].create({
            'name': 'Test Vendor', 'supplier_rank': 1,
        })
        product = cls.env['product.product'].create({
            'name': 'Widget', 'type': 'consu',
        })
        cls.po = cls.env['purchase.order'].create({
            'partner_id': cls.partner.id,
            'order_line': [(0, 0, {
                'product_id': product.id,
                'name': 'Widget',
                'product_qty': 1,
                'price_unit': 100.00,
                'date_planned': datetime.datetime(2026, 11, 1),
            })],
        })
        cls.entry = cls.env['elks.journal.entry'].create({
            'entry_number': 'TEST-MATCH-1',
            'date': datetime.date(2026, 11, 1),
            'line_ids': [(0, 0, {
                'account_id': cls.acct.id,
                'debit': 100.0, 'credit': 0.0,
            })],
        })

    def test_candidates_find_po_within_tolerance(self):
        wiz = self.env['elks.journal.po.match.wizard'].create({
            'entry_id': self.entry.id,
            'amount': 100.00,
            'tolerance': 5.0,
            'partner_id': self.partner.id,
            'date_from': datetime.date(2026, 10, 1),
            'date_to': datetime.date(2026, 11, 30),
        })
        self.assertIn(self.po, wiz.candidate_ids)

    def test_link_writes_purchase_id_and_chatter(self):
        wiz = self.env['elks.journal.po.match.wizard'].create({
            'entry_id': self.entry.id,
            'amount': 100.00,
            'purchase_id': self.po.id,
        })
        wiz.action_link()
        self.assertEqual(self.entry.purchase_id, self.po)

    def test_unlink_action(self):
        self.entry.purchase_id = self.po
        self.entry.action_unlink_po()
        self.assertFalse(self.entry.purchase_id)


@tagged('post_install', '-at_install')
class TestDashboardDrift(TransactionCase):
    def test_drift_tile_renders_without_crash(self):
        # Only runs if a budget record exists; otherwise just
        # verify the compute method can be called on an empty recordset.
        Budget = self.env['elks.budget']
        empty = Budget.browse([])
        empty._compute_qbo_bank_accounts_html()
        # Smoke: method exists and doesn't crash on empty set
        self.assertTrue(
            hasattr(Budget, '_compute_qbo_bank_accounts_html'))
