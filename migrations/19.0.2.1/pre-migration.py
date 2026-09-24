# -*- coding: utf-8 -*-
"""Pre-migration for 19.0.2.1 — configurable Cash Management GL codes.

19.0.2.1 adds two required Char fields to elks.lodge.settings:

    default_cash_gl_code       (default '10000')
    default_checking_gl_code   (default '10100')

Odoo's ORM applies the Python default only to NEW rows.  Existing
settings rows on an upgraded DB start out with NULL on the new columns,
which then breaks the `required=True` constraint when the row is next
saved.

This pre-migration runs BEFORE the new model code loads.  It adds the
columns manually (idempotently) and backfills every existing
`elks_lodge_settings` row with the Uniform CoA defaults.  Lodges with
a custom CoA (Lewiston, etc.) can update the values in the settings
form immediately after upgrade.

Fresh installs skip this entirely (no `installed_version` arg → early
return).  The migration is safe to re-run.
"""
import logging

_logger = logging.getLogger(__name__)


def _column_exists(cr, table, column):
    cr.execute("""
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = %s
          AND column_name = %s
    """, (table, column))
    return bool(cr.fetchone())


def migrate(cr, version):
    if not version:
        # Fresh install — the model's Python default handles new rows,
        # and there are no existing rows to backfill.
        return

    # Table may not exist on very old upgrades; be defensive.
    cr.execute("""
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'elks_lodge_settings'
    """)
    if not cr.fetchone():
        _logger.info(
            "19.0.2.1 pre-migration: elks_lodge_settings table does not "
            "exist yet — skipping GL-code backfill.")
        return

    for column, default_value in (
        ('default_cash_gl_code', '10000'),
        ('default_checking_gl_code', '10100'),
    ):
        if not _column_exists(cr, 'elks_lodge_settings', column):
            cr.execute(
                f'ALTER TABLE elks_lodge_settings '
                f'ADD COLUMN {column} varchar'
            )
            _logger.info(
                "19.0.2.1 pre-migration: added column "
                "elks_lodge_settings.%s", column)

        # Backfill NULL / empty values on any existing rows.
        cr.execute(
            f"UPDATE elks_lodge_settings "
            f"SET {column} = %s "
            f"WHERE {column} IS NULL OR {column} = ''",
            (default_value,),
        )
        if cr.rowcount:
            _logger.info(
                "19.0.2.1 pre-migration: backfilled %s row(s) with "
                "%s=%s", cr.rowcount, column, default_value)
