# -*- coding: utf-8 -*-
from . import models
from . import wizard

import logging

_logger = logging.getLogger(__name__)


def _pre_init_migrate_budget_states(env):
    """Migrate old 'approved' budget state to 'board_approved'.

    Runs before module update so existing records match the new
    selection values.  Safe to run multiple times.
    """
    env.cr.execute("""
        SELECT 1 FROM information_schema.tables
        WHERE table_name = 'elks_budget'
    """)
    if env.cr.fetchone():
        env.cr.execute("""
            UPDATE elks_budget
            SET state = 'board_approved'
            WHERE state = 'approved'
        """)


def _post_init_set_default_accounts(env):
    """Create Elks COA accounts in Odoo's account.account and set
    Accounts Receivable / Accounts Payable as default property values
    for all contacts.

    Safe to run multiple times — only creates accounts that don't exist
    and only sets defaults if not already set.
    """
    Account = env['account.account']
    company = env.company

    # --- Accounts Receivable (10700) ---
    # Odoo 19: account.account no longer has company_id
    ar = Account.search([('code', '=', '10700')], limit=1)
    if not ar:
        ar = Account.create({
            'code': '10700',
            'name': 'Accounts Receivable',
            'account_type': 'asset_receivable',
            'reconcile': True,
        })
        _logger.info("Created Odoo account 10700 Accounts Receivable")

    # --- Accounts Payable (20000) ---
    ap = Account.search([('code', '=', '20000')], limit=1)
    if not ap:
        ap = Account.create({
            'code': '20000',
            'name': 'Accounts Payable',
            'account_type': 'liability_payable',
            'reconcile': True,
        })
        _logger.info("Created Odoo account 20000 Accounts Payable")

    # --- Set as default values for all partners ---
    # Odoo 17+: ir.property removed; use ir.default for company-dependent fields
    IrDefault = env['ir.default']

    existing_ar = IrDefault._get('res.partner', 'property_account_receivable_id')
    if not existing_ar:
        IrDefault.set('res.partner', 'property_account_receivable_id', ar.id)
        _logger.info("Set default Accounts Receivable to 10700")

    existing_ap = IrDefault._get('res.partner', 'property_account_payable_id')
    if not existing_ap:
        IrDefault.set('res.partner', 'property_account_payable_id', ap.id)
        _logger.info("Set default Accounts Payable to 20000")
