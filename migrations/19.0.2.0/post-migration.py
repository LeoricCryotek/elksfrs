# -*- coding: utf-8 -*-
"""Migrate cash-management data from the old register-based models into
the new elks.cash.location / elks.cash.count / elks.cash.movement
schema.

This runs once when the module is upgraded from any pre-19.0.2.0 version
to 19.0.2.0+.  It reads the OLD tables via raw SQL (because the old
model classes have been deleted from this version's Python code) and
creates new records via the env.

What gets migrated:

  1. elks_register                 → elks.cash.location (location_type='till')
  2. elks_register_count_line      → elks.cash.count (one per till counted)
  3. elks_register_transfer        → elks.cash.movement (change_order)

What does NOT get migrated:

  • elks.cash.on.hand WITHOUT register_line_ids — kept as-is; it's the
    dues-batch reconciliation tool, not part of this redesign.
  • Computed deposit_* / change_* breakdown fields — those are derived
    on the fly, no migration needed.

Safety:

  • Old tables are NOT dropped — they stay as orphans for rollback
    safety. After verifying the new system, drop them manually:
        DROP TABLE elks_register_transfer    CASCADE;
        DROP TABLE elks_register_count_line  CASCADE;
        DROP TABLE elks_register             CASCADE;
  • If the upgrade is run a second time (rare), this script will create
    duplicates. There's no good cheap way to dedupe across schema
    changes; just don't run --update twice on the same DB.
"""
import logging

_logger = logging.getLogger(__name__)


# 13 denomination fields are named identically on old and new schemas.
_DENOM_NAMES = (
    'hundreds', 'fifties', 'twenties', 'tens', 'fives', 'twos', 'ones',
    'dollar_coins', 'half_dollars', 'quarters', 'dimes', 'nickels', 'pennies',
)


def _table_exists(cr, table_name):
    cr.execute("""
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = %s
    """, (table_name,))
    return bool(cr.fetchone())


def migrate(cr, version):
    if not version:
        # Fresh install — nothing to migrate.
        return

    from odoo import api, SUPERUSER_ID
    env = api.Environment(cr, SUPERUSER_ID, {})

    Location = env['elks.cash.location']
    Count = env['elks.cash.count']
    Movement = env['elks.cash.movement']

    bank = Location.search([('location_type', '=', 'bank')], limit=1)
    if not bank:
        _logger.warning(
            "Cash migration: no Bank singleton found. "
            "Aborting register migration; load data/elks_cash_location_data.xml first."
        )
        return

    # ------------------------------------------------------------------
    # Step 1: elks_register → elks.cash.location
    # ------------------------------------------------------------------
    register_id_to_location_id = {}

    if _table_exists(cr, 'elks_register'):
        starter_cols = ', '.join(f'starter_{d}' for d in _DENOM_NAMES)
        cr.execute(f"""
            SELECT id, name, location, active, sequence,
                   {starter_cols}
            FROM elks_register
        """)
        rows = cr.fetchall()
        col_names = ('id', 'name', 'location', 'active', 'sequence') + tuple(
            f'starter_{d}' for d in _DENOM_NAMES
        )
        for row in rows:
            data = dict(zip(col_names, row))
            old_id = data['id']
            # Build a stable code from the register name.
            code_base = (data['name'] or 'TILL').upper()
            code = ''.join(c if c.isalnum() else '-' for c in code_base)[:32]
            # Ensure uniqueness — append _Nn if collision.
            base_code = code
            n = 1
            while Location.search_count([('code', '=', code)]) > 0:
                n += 1
                code = f"{base_code[:28]}_{n}"
            vals = {
                'name': data['name'] or f"Migrated Register #{old_id}",
                'code': code,
                'location_type': 'till',
                'active': bool(data['active']),
                'sequence': data['sequence'] or 10,
                'notes': data['location'] or '',
            }
            for d in _DENOM_NAMES:
                vals[f'starter_{d}'] = data[f'starter_{d}'] or 0
            new_loc = Location.create(vals)
            register_id_to_location_id[old_id] = new_loc.id
            _logger.info("Migrated register %s → cash.location %s (%s)",
                         old_id, new_loc.id, new_loc.code)
    else:
        _logger.info("Cash migration: no elks_register table — skipping step 1.")

    # ------------------------------------------------------------------
    # Step 2: elks_register_count_line → elks.cash.count
    # ------------------------------------------------------------------
    if _table_exists(cr, 'elks_register_count_line') and _table_exists(cr, 'elks_cash_on_hand'):
        qty_cols = ', '.join(f'l.qty_{d}' for d in _DENOM_NAMES)
        cr.execute(f"""
            SELECT l.id, l.cash_on_hand_id, l.register_id, l.sequence,
                   h.count_date, h.counted_by, h.witnessed_by,
                   {qty_cols}
            FROM elks_register_count_line l
            LEFT JOIN elks_cash_on_hand h ON h.id = l.cash_on_hand_id
        """)
        rows = cr.fetchall()
        col_names = (
            'id', 'cash_on_hand_id', 'register_id', 'sequence',
            'count_date', 'counted_by_id', 'witnessed_by_id',
        ) + tuple(f'qty_{d}' for d in _DENOM_NAMES)
        for row in rows:
            data = dict(zip(col_names, row))
            new_loc_id = register_id_to_location_id.get(data['register_id'])
            if not new_loc_id:
                _logger.warning(
                    "Skipping count line %s — no migrated location for "
                    "old register %s.", data['id'], data['register_id'],
                )
                continue
            vals = {
                'location_id': new_loc_id,
                'count_date': data['count_date'] or None,
                'count_type': 'shift_close',
                'state': 'done',
                'counted_by_id': data['counted_by_id'] or SUPERUSER_ID,
                'witnessed_by_id': data['witnessed_by_id'],
                'notes': "Migrated from old elks.register.count.line "
                         f"#{data['id']} (cash.on.hand #{data['cash_on_hand_id']}).",
            }
            for d in _DENOM_NAMES:
                vals[f'qty_{d}'] = data[f'qty_{d}'] or 0
            new_count = Count.create(vals)
            _logger.info("Migrated register count line %s → cash.count %s",
                         data['id'], new_count.id)
    else:
        _logger.info(
            "Cash migration: no elks_register_count_line table — skipping step 2.")

    # ------------------------------------------------------------------
    # Step 3: elks_register_transfer → elks.cash.movement (change_order)
    # ------------------------------------------------------------------
    if _table_exists(cr, 'elks_register_transfer') and _table_exists(cr, 'elks_cash_on_hand'):
        qty_cols = ', '.join(f't.qty_{d}' for d in _DENOM_NAMES)
        cr.execute(f"""
            SELECT t.id, t.cash_on_hand_id, t.from_register_id, t.to_register_id,
                   t.sequence, h.count_date,
                   {qty_cols}
            FROM elks_register_transfer t
            LEFT JOIN elks_cash_on_hand h ON h.id = t.cash_on_hand_id
        """)
        rows = cr.fetchall()
        col_names = (
            'id', 'cash_on_hand_id', 'from_register_id', 'to_register_id',
            'sequence', 'move_date',
        ) + tuple(f'qty_{d}' for d in _DENOM_NAMES)
        for row in rows:
            data = dict(zip(col_names, row))
            from_loc_id = register_id_to_location_id.get(data['from_register_id'])
            to_loc_id = register_id_to_location_id.get(data['to_register_id'])
            if not from_loc_id or not to_loc_id:
                _logger.warning(
                    "Skipping transfer %s — missing migrated location "
                    "(from=%s to=%s).",
                    data['id'], data['from_register_id'], data['to_register_id'],
                )
                continue
            # Old transfers were till-to-till.  In the new model these become
            # change_order from the Bank, but since the old data was till-to-till
            # we'll record them as informational with the Bank as via-point.
            # Simpler: record as till_deposit FROM the source till TO the Bank,
            # then a change_order FROM the Bank TO the destination till.
            vals_deposit = {
                'movement_type': 'till_deposit',
                'from_location_id': from_loc_id,
                'to_location_id': bank.id,
                'move_date': data['move_date'] or None,
                'state': 'posted',
                'done_by_id': SUPERUSER_ID,
                'notes': f"Migrated from old elks.register.transfer #{data['id']} "
                         f"(leg 1: source till to Bank).",
            }
            for d in _DENOM_NAMES:
                vals_deposit[f'qty_{d}'] = data[f'qty_{d}'] or 0
            new_dep = Movement.create(vals_deposit)

            vals_order = {
                'movement_type': 'change_order',
                'from_location_id': bank.id,
                'to_location_id': to_loc_id,
                'move_date': data['move_date'] or None,
                'state': 'posted',
                'done_by_id': SUPERUSER_ID,
                'notes': f"Migrated from old elks.register.transfer #{data['id']} "
                         f"(leg 2: Bank to destination till).",
            }
            for d in _DENOM_NAMES:
                vals_order[f'qty_{d}'] = data[f'qty_{d}'] or 0
            new_ord = Movement.create(vals_order)
            _logger.info("Migrated transfer %s → movements %s + %s",
                         data['id'], new_dep.id, new_ord.id)
    else:
        _logger.info(
            "Cash migration: no elks_register_transfer table — skipping step 3.")

    _logger.info(
        "Cash management migration complete. "
        "Old tables (elks_register, elks_register_count_line, "
        "elks_register_transfer) are kept as orphans for rollback safety. "
        "After verifying the migration, drop them manually with DROP TABLE."
    )
