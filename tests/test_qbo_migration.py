"""Real PostgreSQL tests, independent of an Odoo server.

Set FRS_QBO_TEST_DSN to a DISPOSABLE database. Each test creates its own schema
and rolls back every change. Run this file directly to avoid Odoo imports.
"""
import importlib.util
import os
from pathlib import Path
import unittest
import uuid
import types
from unittest.mock import Mock, patch

try:
    import psycopg2
except ImportError:
    psycopg2 = None

spec = importlib.util.spec_from_file_location('qbo_migration', Path(__file__).parents[1] / 'qbo_migration.py')
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


@unittest.skipUnless(psycopg2 and os.environ.get('FRS_QBO_TEST_DSN'), 'Requires a disposable PostgreSQL database')
class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.conn = psycopg2.connect(os.environ['FRS_QBO_TEST_DSN'])
        self.cr = self.conn.cursor()
        schema = 'frs_qbo_test_' + uuid.uuid4().hex
        self.cr.execute('CREATE SCHEMA ' + schema)
        self.cr.execute('SET search_path TO ' + schema)
        self.cr.execute('''
            CREATE TABLE ir_module_module(id integer PRIMARY KEY, name varchar UNIQUE);
            INSERT INTO ir_module_module VALUES (1, 'elksfrs'), (2, 'elksfrs_qbo'), (3, 'unrelated');
            CREATE TABLE ir_model_data(id serial PRIMARY KEY, module varchar NOT NULL,
                name varchar NOT NULL, model varchar NOT NULL, res_id integer NOT NULL,
                noupdate boolean DEFAULT FALSE, UNIQUE(module, name));
            CREATE TABLE ir_model_constraint(id integer PRIMARY KEY,
                module integer REFERENCES ir_module_module(id) ON DELETE CASCADE);
            CREATE TABLE ir_model_relation(id integer PRIMARY KEY,
                module integer REFERENCES ir_module_module(id) ON DELETE CASCADE);
            CREATE TABLE ir_model_fields(id integer PRIMARY KEY, modules varchar);
            CREATE TABLE ir_model(id integer PRIMARY KEY, modules varchar);
            CREATE TABLE elks_qbo_connection(id integer PRIMARY KEY, refresh_token varchar, realm_id varchar);
            CREATE TABLE elks_qbo_expense(id integer PRIMARY KEY, connection_id integer
                REFERENCES elks_qbo_connection(id), purchase_id integer, journal_id integer, source_hash varchar);
            CREATE TABLE ir_cron(id integer PRIMARY KEY, active boolean, nextcall timestamp, interval_number integer);
            INSERT INTO elks_qbo_connection VALUES(12, 'fake-refresh-token-for-test', '123456');
            INSERT INTO elks_qbo_expense VALUES(44, 12, 250, 500, 'original-source-hash');
            INSERT INTO ir_cron VALUES(81, TRUE, '2026-11-01 09:15:00', 3);
            INSERT INTO ir_model_constraint VALUES(1, 2), (2, 3);
            INSERT INTO ir_model_relation VALUES(1, 2), (2, 3);
            INSERT INTO ir_model_fields VALUES(1, 'elksfrs_qbo'), (2, 'unrelated, elksfrs_qbo,elksfrs'), (3, 'unrelated');
            INSERT INTO ir_model VALUES(1, 'elksfrs_qbo'), (2, 'unrelated');
        ''')

    def tearDown(self):
        self.conn.rollback()
        self.cr.close()
        self.conn.close()

    def xmlid(self, name, model='ir.ui.view', res_id=80, module='elksfrs_qbo', noupdate=False):
        self.cr.execute('INSERT INTO ir_model_data(module,name,model,res_id,noupdate) VALUES(%s,%s,%s,%s,%s)',
                        [module, name, model, res_id, noupdate])

    def rows(self, table):
        self.cr.execute('SELECT * FROM ' + table + ' ORDER BY id')
        return self.cr.fetchall()

    def test_fresh_database_without_metadata(self):
        self.cr.execute('DROP TABLE ir_model_data')
        migration.adopt_legacy_qbo(self.cr)

    def test_no_legacy_records_keeps_other_xmlids(self):
        self.xmlid('my_view', module='unrelated')
        before = self.rows('ir_model_data')
        migration.adopt_legacy_qbo(self.cr)
        self.assertEqual(before, self.rows('ir_model_data'))

    def test_transfer_preserves_ids_and_noupdate(self):
        self.xmlid('connection_form', res_id=71)
        self.xmlid('cron_qbo_pull', model='ir.cron', res_id=81, noupdate=True)
        migration.adopt_legacy_qbo(self.cr)
        self.cr.execute('SELECT module,name,res_id,noupdate FROM ir_model_data ORDER BY id')
        self.assertEqual(self.cr.fetchall(), [('elksfrs','qbo_connection_form',71,False),('elksfrs','cron_qbo_pull',81,True)])

    def test_tables_credentials_links_and_cron_untouched(self):
        self.xmlid('cron_qbo_pull', model='ir.cron', res_id=81, noupdate=True)
        tables = ['elks_qbo_connection', 'elks_qbo_expense', 'ir_cron']
        before = {table: self.rows(table) for table in tables}
        migration.adopt_legacy_qbo(self.cr)
        self.assertEqual(before, {table: self.rows(table) for table in tables})

    def test_migration_is_repeatable(self):
        self.xmlid('connection_form')
        migration.adopt_legacy_qbo(self.cr)
        before = self.rows('ir_model_data')
        migration.adopt_legacy_qbo(self.cr)
        self.assertEqual(before, self.rows('ir_model_data'))

    def test_same_record_alias_coalesces(self):
        self.xmlid('connection_form')
        self.xmlid('qbo_connection_form', module='elksfrs')
        migration.adopt_legacy_qbo(self.cr)
        self.assertEqual(len(self.rows('ir_model_data')), 1)
        self.assertEqual(self.rows('ir_model_data')[0][1], 'elksfrs')

    def test_collision_fails_before_any_changes(self):
        self.xmlid('connection_list', res_id=70)
        self.xmlid('connection_form', res_id=71)
        self.xmlid('qbo_connection_form', res_id=999, module='elksfrs')
        before = self.rows('ir_model_data')
        with self.assertRaisesRegex(RuntimeError, 'migration conflict'):
            migration.adopt_legacy_qbo(self.cr)
        self.assertEqual(before, self.rows('ir_model_data'))

    def test_generated_model_field_selection_identifiers_move(self):
        for name in ['model_elks_qbo_expense', 'field_elks_qbo_expense__purchase_id',
                     'selection__elks_qbo_expense__state__review']:
            self.xmlid(name)
        migration.adopt_legacy_qbo(self.cr)
        self.assertTrue(all(row[1] == 'elksfrs' for row in self.rows('ir_model_data')))
        self.assertEqual(len(self.rows('ir_model_data')), 3)

    def test_constraint_and_relation_ownership_moves(self):
        migration.adopt_legacy_qbo(self.cr)
        for table in ['ir_model_constraint', 'ir_model_relation']:
            self.assertEqual(self.rows(table), [(1, 1), (2, 3)])

    def test_module_metadata_replaces_token_and_deduplicates(self):
        migration.adopt_legacy_qbo(self.cr)
        self.assertEqual(self.rows('ir_model_fields'), [(1,'elksfrs'),(2,'elksfrs,unrelated'),(3,'unrelated')])
        self.assertEqual(self.rows('ir_model'), [(1,'elksfrs'),(2,'unrelated')])

    def test_removing_legacy_module_does_not_cascade_owned_constraints(self):
        self.xmlid('model_elks_qbo_expense', model='ir.model', res_id=1)
        migration.adopt_legacy_qbo(self.cr)
        self.cr.execute("DELETE FROM ir_module_module WHERE name='elksfrs_qbo'")
        self.assertEqual(self.rows('ir_model_constraint'), [(1, 1), (2, 3)])
        self.assertEqual(self.rows('ir_model_relation'), [(1, 1), (2, 3)])
        self.assertEqual(len(self.rows('elks_qbo_expense')), 1)

    def test_optional_metadata_columns_are_not_required(self):
        self.cr.execute('ALTER TABLE ir_model_fields DROP COLUMN modules')
        self.cr.execute('DROP TABLE ir_model_relation')
        self.xmlid('connection_form')
        migration.adopt_legacy_qbo(self.cr)
        self.assertEqual(self.rows('ir_model_data')[0][1:3], ('elksfrs','qbo_connection_form'))

    def test_pre_migration_entrypoint_runs_helper(self):
        path = Path(__file__).parents[1] / 'migrations/19.0.3.0.0/pre-migration.py'
        spec = importlib.util.spec_from_file_location('frs_pre_migration', path)
        pre = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pre)
        self.xmlid('connection_form')
        pre.migrate(self.cr, '19.0.2.1')
        self.assertEqual(self.rows('ir_model_data')[0][1:3], ('elksfrs','qbo_connection_form'))


    def _compat_module(self, relative_path):
        path = Path(__file__).parents[2] / 'elksfrs_qbo' / relative_path
        spec = importlib.util.spec_from_file_location('qbo_compat_hook', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _helper_modules(self):
        # Do not import Odoo: exercise the real compatibility hooks against SQL.
        return {'odoo': types.ModuleType('odoo'),
                'odoo.addons': types.ModuleType('odoo.addons'),
                'odoo.addons.elksfrs': types.ModuleType('odoo.addons.elksfrs'),
                'odoo.addons.elksfrs.qbo_migration': migration}

    def test_upgrading_only_the_placeholder_adopts_before_cleanup(self):
        compat = self._compat_module('migrations/19.0.2.0.0/pre-migration.py')
        self.xmlid('connection_form')
        with patch.dict('sys.modules', self._helper_modules()):
            compat.migrate(self.cr, '19.0.1.0.0')
        self.assertEqual(self.rows('ir_model_data')[0][1:3], ('elksfrs','qbo_connection_form'))

    def test_placeholder_uninstall_adopts_before_record_removal(self):
        compat = self._compat_module('__init__.py')
        self.xmlid('connection_form')
        env = Mock(cr=self.cr)
        with patch.dict('sys.modules', self._helper_modules()):
            compat._adopt_before_removal(env)
        self.assertEqual(self.rows('ir_model_data')[0][1:3], ('elksfrs','qbo_connection_form'))
        env.flush_all.assert_called_once()
        env.invalidate_all.assert_called_once()
        env.registry.clear_cache.assert_called_once()


if __name__ == '__main__':
    unittest.main()
