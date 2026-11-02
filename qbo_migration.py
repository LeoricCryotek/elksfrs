"""Adopt the former connector in place; never copy, delete, or repost ledger data."""
import logging

_logger = logging.getLogger(__name__)
LEGACY_MODULE = 'elksfrs_qbo'
TARGET_MODULE = 'elksfrs'
XMLID_RENAMES = {'connection_company': 'qbo_connection_company', 'account_company': 'qbo_account_company', 'vendor_company': 'qbo_vendor_company', 'expense_company': 'qbo_expense_company', 'connection_list': 'qbo_connection_list', 'connection_form': 'qbo_connection_form', 'expense_list': 'qbo_expense_list', 'expense_form': 'qbo_expense_form', 'match_form': 'qbo_match_form', 'purchase_form': 'qbo_purchase_form', 'action_connections': 'qbo_action_connections', 'action_expenses': 'qbo_action_expenses', 'menu_qbo': 'qbo_menu_qbo', 'menu_expenses': 'qbo_menu_expenses', 'menu_connections': 'qbo_menu_connections', 'qbo_expense_product': 'qbo_expense_product', 'cron_qbo_pull': 'cron_qbo_pull', 'connection_user': 'qbo_connection_user', 'connection_manager': 'qbo_connection_manager', 'connection_admin': 'qbo_connection_admin', 'account_user': 'qbo_account_user', 'account_manager': 'qbo_account_manager', 'vendor_user': 'qbo_vendor_user', 'vendor_manager': 'qbo_vendor_manager', 'expense_user': 'qbo_expense_user', 'expense_manager': 'qbo_expense_manager', 'match_manager': 'qbo_match_manager'}


def _table_exists(cr, table):
    cr.execute("SELECT to_regclass(%s)", [table])
    return bool(cr.fetchone()[0])


def _column_exists(cr, table, column):
    cr.execute("""SELECT 1 FROM information_schema.columns
                  WHERE table_schema=current_schema() AND table_name=%s AND column_name=%s""", [table, column])
    return bool(cr.fetchone())


def adopt_legacy_qbo(cr):
    """Run before FRS XML/model reflection; preserve every old res_id and noupdate flag.

    Repeated runs and databases without the old add-on are harmless. A conflicting
    XML ID stops the upgrade with the database transaction intact, instead of
    guessing which credentials, schedule, or model metadata should survive.
    """
    if not _table_exists(cr, 'ir_model_data'):
        return
    cr.execute("SELECT id, name, model, res_id FROM ir_model_data WHERE module=%s ORDER BY id", [LEGACY_MODULE])
    legacy = cr.fetchall()
    # Preflight all collisions before making any change.
    for ident, name, model, res_id in legacy:
        target_name = XMLID_RENAMES.get(name, name)
        cr.execute("SELECT model, res_id FROM ir_model_data WHERE module=%s AND name=%s", [TARGET_MODULE, target_name])
        target = cr.fetchone()
        if target and tuple(target) != (model, res_id):
            raise RuntimeError(
                "QuickBooks migration conflict at %s.%s; different records already exist. "
                "Upgrade stopped without merging or discarding data. Restore/review the staging migration first."
                % (TARGET_MODULE, target_name))
    for ident, name, model, res_id in legacy:
        target_name = XMLID_RENAMES.get(name, name)
        cr.execute("SELECT id FROM ir_model_data WHERE module=%s AND name=%s", [TARGET_MODULE, target_name])
        if cr.fetchone():
            # Both identifiers already describe the same physical record.
            cr.execute("DELETE FROM ir_model_data WHERE id=%s", [ident])
        else:
            cr.execute("UPDATE ir_model_data SET module=%s, name=%s WHERE id=%s", [TARGET_MODULE, target_name, ident])
    if _table_exists(cr, 'ir_module_module'):
        cr.execute("SELECT id, name FROM ir_module_module WHERE name IN (%s, %s)", [LEGACY_MODULE, TARGET_MODULE])
        modules = {name: ident for ident, name in cr.fetchall()}
        if LEGACY_MODULE in modules and TARGET_MODULE in modules:
            # Without this transfer, uninstalling the compatibility shim can drop
            # tables/constraints owned by the former connector.
            for table in ('ir_model_constraint', 'ir_model_relation'):
                if _table_exists(cr, table) and _column_exists(cr, table, 'module'):
                    cr.execute(f'UPDATE {table} SET module=%s WHERE module=%s',
                               [modules[TARGET_MODULE], modules[LEGACY_MODULE]])
    # Odoo metadata also tracks defining modules on inherited fields/models.
    for table in ('ir_model_fields', 'ir_model'):
        if _table_exists(cr, table) and _column_exists(cr, table, 'modules'):
            cr.execute(f"SELECT id, modules FROM {table} WHERE modules IS NOT NULL")
            for ident, value in cr.fetchall():
                names = [part.strip() for part in value.split(',') if part.strip()]
                if LEGACY_MODULE in names:
                    updated = ','.join(sorted(set(TARGET_MODULE if name == LEGACY_MODULE else name for name in names)))
                    cr.execute(f'UPDATE {table} SET modules=%s WHERE id=%s', [updated, ident])
    if legacy:
        _logger.info('Adopted %d legacy QuickBooks XML identifiers into Elks FRS; existing rows and schedules retained.', len(legacy))
