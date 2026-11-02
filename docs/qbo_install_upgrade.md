# QuickBooks Online installation and upgrade

QuickBooks expenses are built into **Elks FRS 19.0.3.0.1**. Install or upgrade FRS;
there is no separate QuickBooks feature add-on to install. Native Odoo `purchase`
is a dependency so PO links always exist. Custom `elkspurchase` remains optional
to avoid a circular dependency: it already depends on FRS. FRS imports remain
available without it; approvals, matching and FRS posting present a clear request
to install Purchase Requests instead of failing registry initialization.

## Upgrade an existing database

1. Back up the Odoo database and filestore. Restore a staging copy and use it for
   the first upgrade. Stop Odoo workers during the production maintenance run.
2. Deploy the updated `elksfrs` and `elkspurchase` directories together. If the old
   `elksfrs_qbo` was installed, also deploy its compatibility directory. Keep that
   folder on the addons path during the transition; do not uninstall the old
   implementation with its old source code.
3. Restart Odoo and upgrade the existing applications. Example for a database
   with all three applications already installed (replace executable, config and
   database names with the server's actual values):

   ```sh
   odoo-bin -c /path/to/odoo.conf -d YOUR_DATABASE \
     -u elksfrs,elkspurchase,elksfrs_qbo --stop-after-init
   ```

   If the separate connector was never installed, omit `elksfrs_qbo`. If Purchase
   Requests is not installed, upgrade only FRS; install `elkspurchase` later when
   enabling approvals. Do not install the compatibility placeholder on new
   databases.
4. Restart normal workers. Open **Elks FRS → QuickBooks Online**. Check the
   connection, account and vendor mappings, imported charge count, links to POs
   and journals, and scheduled action settings against the staging snapshot.
5. If the connector already had credentials, they stay attached to the same
   connection and realm. Reauthorization is only necessary if Intuit has expired
   or revoked the existing authorization.

## What the automatic migration preserves

The model names and physical `elks_qbo_*` table names are unchanged. Connection,
account mapping, vendor mapping, expense and match records retain their IDs.
Related PO/journal foreign keys, OAuth credentials, source hashes, transaction
payloads, review state and chatter therefore stay attached to the original rows.
There is no transaction reimport, journal reposting, vendor merge, or account
reclassification during installation.

Before FRS loads its new XML records, the migration moves legacy external IDs
into the `elksfrs` namespace. Generic view/action/security names receive a `qbo_`
prefix; generated model/field/selection/constraint IDs keep their names. This
reuses existing menus, ACLs, rules, views, actions, products and scheduled jobs.
Existing `noupdate` values are retained, including the cron's active state,
interval and next execution time. Fresh databases receive a disabled daily cron
and Auto Record defaults to off.

Model/field metadata and constraint/relation ownership are transferred as well.
The compatibility module has no duplicate declarations. Its upgrade and uninstall
hooks protect remaining legacy ownership before Odoo's cleanup. After a successful
upgrade, it can be left installed harmlessly, or uninstalled through Odoo while
its source folder is still available. Uninstalling the actual FRS app is a normal
Odoo module removal and is not a supported way to disable syncing.

Migration is repeatable. If both namespaces already contain the same physical
record, the redundant legacy XML identifier is removed. If they point to different
records, migration stops with a conflict before altering ownership rather than
guessing which data should survive. Resolve such conflicts in staging before
production.

## First connection after a fresh installation

1. Assign the treasurer **Elks FRS Manager** and, for approval automation,
   **Purchase Requester** rights. Install Purchase Requests if it is not present.
2. Create a connection under **Connections and Mapping**. Start with an Intuit
   sandbox app and register the exact redirect URL displayed by Odoo.
3. Enter client credentials in Odoo and use **Connect to QuickBooks**. No secrets
   belong in configuration files committed to Git.
4. Choose the initial import cutoff, pull expenses, and map accounts/vendors.
   Recheck imported charges. Review opening balances and earlier CSV imports to
   avoid overlapping history.
5. Verify PO approval, officer signatures, budget controls and existing-entry
   reconciliation. Enable the scheduled action after testing. Auto Record is a
   separate opt-in after accounting checks pass.

## Supported scope

This merge preserves the connector's supported scope: recorded Purchase expenses,
checks and credit-card purchases; one charge to one full PO; positive,
company-currency account-based expense/COGS/fixed-asset lines without transaction
tax or billable allocations. It does not add pending bank-feed access, Bills or
BillPayments, payroll, refunds, item purchases, partial/combined payments or a
complete QuickBooks general-ledger mirror. Existing FRS journal/report paths and
CSV import history remain intact.

Source changes after matching or recording are retained as review exceptions;
automatic correction/reversal is not implemented. Multi-company FRS posting
remains blocked because the base FRS ledger is shared. Local purchasing bylaws
still need review before production. FRS actuals export groups activity by month
and blocks periods containing unresolved imported expenses.

## Verification

Standalone payload tests:

```sh
python3 -m unittest discover -s tests -p test_qbo_payload.py -v
```

Migration tests against a disposable PostgreSQL database (requires psycopg2):

```sh
FRS_QBO_TEST_DSN='dbname=disposable_test_database' \
  python3 tests/test_qbo_migration.py -v
```

Each migration test creates a separate schema and rolls it back. It exercises
fresh metadata, no legacy installation, repeat migration, ID/noupdate retention,
untouched credentials/links/schedule, metadata ownership, collision handling,
constraint safety on legacy removal, and the actual pre-migration entrypoint.

Odoo registry/integration tests on staging, with and without optional purchasing:

```sh
odoo-bin -c /path/to/odoo.conf -d FRS_ONLY_TEST \
  -i elksfrs --test-enable --test-tags /elksfrs --stop-after-init
odoo-bin -c /path/to/odoo.conf -d FRS_PURCHASE_TEST \
  -i elkspurchase --test-enable --test-tags /elksfrs --stop-after-init
```

The local PostgreSQL migration tests, payload tests and Python/XML/manifest checks
were run during development. A full Odoo server and Intuit sandbox connection are
not available in this workspace; registry installation and live authorization
still require the staging checks above.

## Recovery from the initial CSV loader error

Version 19.0.3.0.1 fixes the installation error `KeyError: qbo_access`. Odoo uses
the CSV filename to select the destination model. The QuickBooks access rows now
load through `security/ir.model.access.csv` with their existing XML IDs retained.
No access records or business data need to be deleted.

After this failure, restart the existing upgrade run so the affected modules can
finish upgrading. A request for a missing event field such as
`project_task.x_charity_disbursement_enabled` indicates that the event module's
schema upgrade has not completed; do not manually add empty columns in place of
the module upgrade.

The CSV regression tests use the real Odoo importer with an in-memory target:

```sh
ODOO_SOURCE_ROOT=/path/to/odoo python3 tests/test_qbo_acl_loader.py -v
```
