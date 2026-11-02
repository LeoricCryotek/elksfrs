"""Exercise manifest CSVs through Odoo's real importer without a database.

Run directly with Odoo's Python interpreter; set ODOO_SOURCE_ROOT if needed.
"""
import ast
import os
from pathlib import Path
import sys
import unittest

if os.environ.get('ODOO_SOURCE_ROOT'):
    sys.path.insert(0, os.environ['ODOO_SOURCE_ROOT'])
try:
    from odoo.tools.convert import convert_csv_import
except ImportError:
    convert_csv_import = None


class ImportTarget:
    def __init__(self):
        self.rows = []

    def with_context(self, **context):
        return self

    def load(self, fields, rows):
        self.rows.extend(dict(zip(fields, row)) for row in rows)
        return {'messages': []}


class ImportEnvironment:
    context = {}
    def __init__(self):
        self.targets = {'ir.model.access': ImportTarget()}

    def __call__(self, **kwargs):
        return self

    def __getitem__(self, model):
        return self.targets[model]


@unittest.skipUnless(convert_csv_import, 'Requires the Odoo Python environment')
class TestACLLoader(unittest.TestCase):
    def test_manifest_csvs_target_the_access_model(self):
        base = Path(__file__).resolve().parents[1]
        manifest = ast.literal_eval((base / '__manifest__.py').read_text())
        env = ImportEnvironment()
        for filename in manifest['data']:
            if filename.endswith('.csv'):
                convert_csv_import(env, 'elksfrs', filename, (base / filename).read_bytes(), {}, mode='update')
        rows = env.targets['ir.model.access'].rows
        qbo = [row for row in rows if row['id'].startswith('qbo_')]
        self.assertEqual(len(qbo), 10)
        self.assertEqual(len({row['id'] for row in rows}), len(rows))
        self.assertTrue(all(row['model_id:id'].startswith('model_elks_qbo_') for row in qbo))

    def test_bad_filename_reproduces_the_original_failure(self):
        with self.assertRaisesRegex(KeyError, 'qbo_access'):
            convert_csv_import(ImportEnvironment(), 'elksfrs', 'security/qbo_access.csv',
                               b'id,name\ntest,Test\n', {}, mode='update')


if __name__ == '__main__':
    unittest.main()
