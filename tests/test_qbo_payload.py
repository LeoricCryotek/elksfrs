"""Run without Odoo: python -m unittest discover -s tests -p test_payload.py."""
import copy
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('payload', Path(__file__).parents[1] / 'models' / 'qbo' / 'payload.py')
payload = importlib.util.module_from_spec(spec)
spec.loader.exec_module(payload)


class PayloadTests(unittest.TestCase):
    def setUp(self):
        self.row = {'Id': '17', 'TxnDate': '2026-10-01', 'TotalAmt': 12.5,
                    'CurrencyRef': {'value': 'USD'}, 'AccountRef': {'value': '9'},
                    'Line': [{'Amount': 12.5, 'Description': 'Office supplies',
                              'DetailType': 'AccountBasedExpenseLineDetail',
                              'AccountBasedExpenseLineDetail': {'AccountRef': {'value': '30'}}}]}

    def test_valid(self):
        self.assertEqual(payload.normalize_purchase(self.row, 'USD')[0]['amount'], 12.5)

    def test_split_allocations(self):
        self.row['Line'][0]['Amount'] = 5
        second = copy.deepcopy(self.row['Line'][0])
        second['Amount'] = 7.5
        self.row['Line'].append(second)
        self.assertEqual(len(payload.normalize_purchase(self.row, 'USD')), 2)

    def test_unbalanced(self):
        self.row['TotalAmt'] = 13
        with self.assertRaises(ValueError): payload.normalize_purchase(self.row, 'USD')

    def test_tax(self):
        self.row['TxnTaxDetail'] = {'TotalTax': 1}
        with self.assertRaises(ValueError): payload.normalize_purchase(self.row, 'USD')

    def test_foreign_currency(self):
        with self.assertRaises(ValueError): payload.normalize_purchase(self.row, 'CAD')

    def test_credit(self):
        self.row['Credit'] = True
        with self.assertRaises(ValueError): payload.normalize_purchase(self.row, 'USD')

    def test_items(self):
        self.row['Line'][0].pop('AccountBasedExpenseLineDetail')
        with self.assertRaises(ValueError): payload.normalize_purchase(self.row, 'USD')

    def test_missing_bank(self):
        self.row.pop('AccountRef')
        with self.assertRaises(ValueError): payload.normalize_purchase(self.row, 'USD')

    def test_negative_zero_nonfinite(self):
        for amount in (-1, 0, 'NaN', 'Infinity'):
            with self.subTest(amount=amount):
                self.row['Line'][0]['Amount'] = amount
                with self.assertRaises(ValueError): payload.normalize_purchase(self.row, 'USD')

    def test_missing_expense_account(self):
        self.row['Line'][0]['AccountBasedExpenseLineDetail'] = {}
        with self.assertRaises(ValueError): payload.normalize_purchase(self.row, 'USD')

    def test_description_only(self):
        self.row['Line'].append({'DetailType': 'DescriptionOnly'})
        self.assertEqual(len(payload.normalize_purchase(self.row, 'USD')), 1)

    def test_billable(self):
        self.row['Line'][0]['AccountBasedExpenseLineDetail']['BillableStatus'] = 'Billable'
        with self.assertRaises(ValueError): payload.normalize_purchase(self.row, 'USD')
