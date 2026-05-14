# -*- coding: utf-8 -*-
{
    "name": "Elks FRS - Financial Reporting System",
    "version": "19.0.1.6",
    "category": "Accounting",
    "summary": "Elks Lodge General Ledger, Chart of Accounts, Dues Processing, and FRS CSV exports",
    "description": """
Implements the Elks Lodge Uniform Chart of Accounts, General Ledger,
Dues Payment Processing, and Financial Reporting System (FRS) per the
Grand Lodge Auditing & Accounting Manual.

Features:
- Full Uniform Chart of Accounts (10xxx – 99xxx)
- Lodge Rates / TranCodeID mapping (CLMS-compatible)
- Dues payment processing with auto journal entries
- General journal entries with Elks department tracking
- Monthly FRS actuals CSV export (LodgeNumber, LodgeGLAccount, Date, Amount)
- Annual FRS budget CSV export (LodgeNumber, LodgeGLAccount, FYE, Version, Annual)
- FRS submission tracking with automated monthly reminders
- QuickBooks Desktop CSV/IIF import wizard
- Clover POS transaction CSV import wizard
- Cost-to-sales ratio monitoring (CoGS ≤ 35%, Labor ≤ 35%)
- Year-end closing support with restricted fund accounting
    """,
    "author": "Danny Santiago",
    "website": "https://dannysantiago.info",
    "license": "LGPL-3",
    "depends": [
        "base",
        "account",
        "mail",
        "elkscontacts",
    ],
    "data": [
        "security/elksfrs_groups.xml",
        "security/ir.model.access.csv",
        "data/elks_department_data.xml",
        "data/elks_coa_data.xml",
        "data/elks_dues_rate_data.xml",
        "data/elks_frs_cron.xml",
        "wizard/frs_export_wizard_views.xml",
        "wizard/qb_import_wizard_views.xml",
        "wizard/budget_import_wizard_views.xml",
        "wizard/clms_import_dues_views.xml",
        "wizard/process_paid_members_wizard_views.xml",
        "wizard/qb_pnl_reconcile_wizard_views.xml",
        "wizard/clms_payment_import_wizard_views.xml",
        "views/elks_budget_amendment_views.xml",
        "views/elks_account_views.xml",
        "views/elks_journal_views.xml",
        "views/elks_dues_views.xml",
        "views/elks_dues_deposit_views.xml",
        "views/elks_register_views.xml",
        "views/elks_cash_register_views.xml",
        "report/dues_receipt_report.xml",
        "report/budget_report.xml",
        "report/treasurer_report.xml",
        "views/elks_frs_submission_views.xml",
        "views/elks_budget_views.xml",
        "views/elks_frs_dashboard_views.xml",
        "views/res_partner_frs_views.xml",
        "views/elksfrs_menus.xml",
    ],
    "installable": True,
    "application": True,
    "pre_init_hook": "_pre_init_migrate_budget_states",
    "post_init_hook": "_post_init_set_default_accounts",
}
