# -*- coding: utf-8 -*-
"""Pre-migration cleanup for elksfrs 19.0.2.0.

When this version was written, the following deprecated view / data
files were removed from the manifest:

  * views/elks_cash_register_views.xml  — extended elks.cash.on.hand
    with register_line_ids / transfer_ids / register_count fields and
    a "Print Treasurer Report" button.
  * views/elks_register_views.xml       — register CRUD views.
  * report/treasurer_report.xml         — used register_line_ids /
    transfer_ids fields.

Removing them from the manifest does NOT delete the ir.ui.view (and
ir.actions.report) records that they had previously created in the
database.  Those orphan records reference fields/models that no longer
exist, and Odoo's view validator rejects them when the new code loads.

This pre-migration runs BEFORE the new XML is parsed, deleting any
elksfrs-owned views or report actions that mention now-removed fields
or models.  The deletions are scoped to records owned by the elksfrs
module (via ir.model.data) so unrelated views can't get hit.
"""
import logging

_logger = logging.getLogger(__name__)


# Field/model name fragments that, if found inside arch_db, indicate
# a view from the old register extension.  In Odoo 19, ir.ui.view.arch_db
# is jsonb (one entry per language), so we match on the ::text projection
# and use bare token names rather than full XML attribute syntax —
# jsonb's text representation escapes the inner quotes, which would
# otherwise require backslash-quotes in the patterns.
_DEPRECATED_TOKENS = (
    'register_count',
    'register_line_ids',
    'transfer_ids',
    'total_starter_banks',
    'total_excess',
    'deposit_total_denom',
    'change_total_denom',
    'from_register_id',
    'to_register_id',
    'action_print_treasurer_report',
    'elks.register.count.line',
    'elks.register.transfer',
)


def migrate(cr, version):
    if not version:
        return  # Fresh install — nothing to clean.

    # ---- 1. Delete elksfrs-owned ir.ui.view records referencing removed fields.
    # arch_db is jsonb in Odoo 19; cast to text for LIKE matching.
    cr.execute("""
        SELECT DISTINCT v.id, v.name, d.name AS xml_id
        FROM ir_ui_view v
        JOIN ir_model_data d
          ON d.res_id = v.id AND d.model = 'ir.ui.view' AND d.module = 'elksfrs'
        WHERE %s
    """ % ' OR '.join("v.arch_db::text LIKE %s" for _ in _DEPRECATED_TOKENS),
        tuple(f'%{tok}%' for tok in _DEPRECATED_TOKENS))
    rows = cr.fetchall()
    if rows:
        for vid, vname, xmlid in rows:
            _logger.info(
                "Pre-migration: removing stale view %s "
                "(id=%s, xmlid=elksfrs.%s)", vname, vid, xmlid,
            )
        view_ids = tuple(r[0] for r in rows)
        cr.execute("DELETE FROM ir_ui_view WHERE id IN %s", (view_ids,))
        cr.execute(
            "DELETE FROM ir_model_data WHERE model = 'ir.ui.view' AND res_id IN %s",
            (view_ids,))
        _logger.info("Pre-migration: removed %s stale view records.", len(rows))

    # ---- 2. Delete elksfrs-owned ir.actions.report records for removed models
    cr.execute("""
        SELECT DISTINCT r.id, r.report_name, d.name AS xml_id
        FROM ir_act_report_xml r
        JOIN ir_model_data d
          ON d.res_id = r.id AND d.model = 'ir.actions.report' AND d.module = 'elksfrs'
        WHERE r.report_name IN (
            'elksfrs.treasurer_report_template',
            'elksfrs.treasurer_cash_report'
        )
        OR r.model IN ('elks.register', 'elks.register.count.line', 'elks.register.transfer')
    """)
    rows = cr.fetchall()
    if rows:
        for rid, rname, xmlid in rows:
            _logger.info(
                "Pre-migration: removing stale report %s "
                "(id=%s, xmlid=elksfrs.%s)", rname, rid, xmlid,
            )
        report_ids = tuple(r[0] for r in rows)
        cr.execute("DELETE FROM ir_act_report_xml WHERE id IN %s", (report_ids,))
        cr.execute(
            "DELETE FROM ir_model_data WHERE model = 'ir.actions.report' AND res_id IN %s",
            (report_ids,))
        _logger.info("Pre-migration: removed %s stale report records.", len(rows))

    # ---- 3. Delete elksfrs-owned act_window records targeting removed models
    cr.execute("""
        SELECT DISTINCT a.id, a.name, d.name AS xml_id
        FROM ir_act_window a
        JOIN ir_model_data d
          ON d.res_id = a.id AND d.model = 'ir.actions.act_window' AND d.module = 'elksfrs'
        WHERE a.res_model IN (
            'elks.register', 'elks.register.count.line', 'elks.register.transfer'
        )
    """)
    rows = cr.fetchall()
    if rows:
        for aid, aname, xmlid in rows:
            _logger.info(
                "Pre-migration: removing stale action %s "
                "(id=%s, xmlid=elksfrs.%s)", aname, aid, xmlid,
            )
        action_ids = tuple(r[0] for r in rows)
        cr.execute("DELETE FROM ir_act_window WHERE id IN %s", (action_ids,))
        cr.execute(
            "DELETE FROM ir_model_data WHERE model = 'ir.actions.act_window' AND res_id IN %s",
            (action_ids,))
        _logger.info("Pre-migration: removed %s stale action records.", len(rows))

    # ---- 4. Delete elksfrs-owned menus that pointed at deleted actions.
    # (Odoo will null these out automatically when their action is deleted,
    # but the menus themselves become orphaned. Clean them up by xml_id.)
    # The list of deprecated menu xml_ids:
    deprecated_menu_xmlids = ('menu_elksfrs_registers',)
    cr.execute("""
        DELETE FROM ir_ui_menu
        WHERE id IN (
            SELECT res_id FROM ir_model_data
            WHERE model = 'ir.ui.menu'
              AND module = 'elksfrs'
              AND name = ANY(%s)
        )
    """, (list(deprecated_menu_xmlids),))
    if cr.rowcount:
        _logger.info(
            "Pre-migration: removed %s stale menu records.", cr.rowcount)
    cr.execute("""
        DELETE FROM ir_model_data
        WHERE model = 'ir.ui.menu'
          AND module = 'elksfrs'
          AND name = ANY(%s)
    """, (list(deprecated_menu_xmlids),))
