# QuickBooks Online Sync — Design Doc

**Status:** Draft v1 — for review
**Author:** Danny Santiago (with Claude)
**Module:** `elksfrs` — new `qbo_*` subsystem
**Target version:** `19.0.3.0`
**Scope:** One-way pull sync from QuickBooks Online into elksfrs models, refreshed nightly, driving FRS submissions and treasurer reports.
**Out of scope for this doc:** Two-way sync, migration off QBO, real-time webhooks.

---

## 0. Guiding principles

1. **QBO is the source of truth.** Treasurer books everything in QBO. Odoo mirrors it. Anything typed into elksfrs journals directly (dues auto-count, cash movements) must be traceable so it doesn't get clobbered by a sync.
2. **One-way pull.** No writes back to QBO in this design. If a lodge later wants to move off QBO, the same mapping table becomes the migration ledger.
3. **Nightly is the default.** A 2am cron is enough for FRS reports run monthly. On-demand button exists for treasurers who want fresh numbers before publishing.
4. **Everything is idempotent.** Every sync run can be repeated safely. Every QBO row maps to zero-or-one Odoo record via a persistent mapping table keyed on the QBO GUID.
5. **Failures don't silently poison data.** Every sync run writes a `qbo.sync.log` row. Errors block the "last successful sync" watermark from advancing, so the next run retries the same window rather than skipping past bad rows.
6. **Credentials never leave the DB.** Client secret and refresh token stored in `qbo.connection` with `groups='base.group_system'` restriction. No config files, no env vars, no filesystem writes.

---

## 1. Intuit developer app registration (you do this once)

Before we write any code, you need an Intuit app. Steps:

1. Go to <https://developer.intuit.com> and sign in with the Intuit account you use for QBO (or create a dev account).
2. **Create an app** in the developer dashboard:
   - **App name:** "Lewiston Elks FRS Sync" (or whatever)
   - **Category:** Accounting
   - **Scopes:** check `com.intuit.quickbooks.accounting` (only)
3. Under **Keys & OAuth**, you'll get **two sets** of credentials:
   - **Development / sandbox keys** — use these against `sandbox-quickbooks.api.intuit.com`
   - **Production keys** — use these against the real `quickbooks.api.intuit.com`
   - We register both; UI has a "Environment" selector.
4. **Redirect URIs** — add both:
   - `https://<your-lodge-domain>/qbo/oauth/callback` (production)
   - `http://localhost:8069/qbo/oauth/callback` (dev, for testing on your laptop)
   - **Exact match matters.** If your production host is `lewistonelks896.com` use that scheme + host + path exactly.
5. Note the **Client ID** and **Client Secret** for each environment. You'll paste them into the QBO Connection form on first setup.
6. Note your **Realm ID** — this is the QBO Company ID, visible under Settings → Additional Info in QBO, or you'll capture it automatically when you complete the OAuth flow (Intuit returns it as a query param).

**What Intuit charges:** developer accounts are free; sandbox is free; production API calls are free under 500 req/min per realm — a lodge never approaches that.

---

## 2. OAuth2 flow — what the code needs to handle

Intuit uses standard OAuth2 with a wrinkle: the refresh token itself rolls each time you use it, and dies after 100 days of not being used.

### First-time connection (interactive)

```
1. Admin navigates to Elks FRS → Configuration → QuickBooks Connection.
2. Enters Client ID, Client Secret, Environment (sandbox/production).
3. Clicks "Connect to QuickBooks".
4. Odoo redirects browser to:
     https://appcenter.intuit.com/connect/oauth2
       ?client_id=<id>
       &response_type=code
       &scope=com.intuit.quickbooks.accounting
       &redirect_uri=<odoo-callback>
       &state=<csrf-token>
5. User logs into QBO, approves access, selects the company.
6. Intuit redirects back to Odoo:
     /qbo/oauth/callback?code=<auth-code>&realmId=<company-id>&state=<csrf-token>
7. Odoo controller:
     - verifies csrf state matches
     - exchanges auth code for token pair (POST to
       https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer)
     - stores access_token, refresh_token, realm_id on qbo.connection
     - records token expiry timestamps
     - redirects to a "Connected" confirmation page
```

### Ongoing token maintenance

- **Access token** lasts 1 hour. Before every API call, check `expires_at`; if within 5 min of expiry, refresh.
- **Refresh token** lasts 100 days *from last use*. Each refresh returns a **new** refresh token that replaces the old one. Store the new one atomically.
- **If refresh fails** (revoked, 100-day timeout, admin disconnected): flip `qbo.connection.state` to `'disconnected'`, disable the cron, send a mail activity to the admin group.

### Disconnect

- User can click "Disconnect" on the connection form. Odoo POSTs to Intuit's revocation endpoint (`https://developer.api.intuit.com/v2/oauth2/tokens/revoke`) and clears local tokens.

---

## 3. Model design

Six new persistent models plus a few transient wizards.

### 3.1 `qbo.connection` (singleton)

Stores the OAuth credentials and current sync state for the lodge's one QBO company.

| Field | Type | Notes |
|---|---|---|
| `name` | Char | Display label, e.g. "Lewiston Elks #896 QBO" |
| `environment` | Selection | `sandbox` / `production` |
| `client_id` | Char (system group) | Intuit app Client ID |
| `client_secret` | Char (system group, invisible) | Intuit app Client Secret |
| `realm_id` | Char, readonly | QBO Company ID, captured from callback |
| `access_token` | Char (system group, invisible) | Bearer token |
| `access_token_expires_at` | Datetime | Refresh when within 5 min |
| `refresh_token` | Char (system group, invisible) | Rolling refresh token |
| `refresh_token_expires_at` | Datetime | 100-day watchdog |
| `state` | Selection | `draft` / `connecting` / `connected` / `disconnected` / `error` |
| `last_sync_at` | Datetime, readonly | Watermark for next CDC pull |
| `last_error` | Text, readonly | Truncated last-known error |
| `active` | Boolean | Disable to pause all sync |

Singleton constraint via `@api.constrains` (Odoo 19 `models.Constraint` for the SQL side).

### 3.2 `qbo.entity.mapping` (persistent)

The load-bearing table. One row per QBO entity we've ever seen. Maps `(entity_type, qbo_id)` → Odoo model + record.

| Field | Type | Notes |
|---|---|---|
| `qbo_entity_type` | Selection | `Account`, `JournalEntry`, `Invoice`, `Bill`, `Payment`, `BillPayment`, `Deposit`, `Purchase`, `SalesReceipt`, `Transfer`, `CreditMemo`, `RefundReceipt`, `Customer`, `Vendor`, `Budget` |
| `qbo_id` | Char, indexed | The QBO GUID |
| `qbo_sync_token` | Char | QBO's optimistic-lock counter — for detecting stale local copies |
| `qbo_last_updated` | Datetime | From QBO's MetaData.LastUpdatedTime — drives CDC |
| `odoo_model` | Char, indexed | e.g. `elks.journal.entry`, `res.partner`, `elks.account` |
| `odoo_res_id` | Integer, indexed | FK to the actual record |
| `first_seen_at` | Datetime | When we first synced this |
| `last_seen_at` | Datetime | When we last touched it |
| `is_deleted_in_qbo` | Boolean | Soft delete marker |

Unique constraint: `(qbo_entity_type, qbo_id)`.

The mapping table survives resyncs, module upgrades, and DB moves. Never truncate it without also nuking the mirrored Odoo records.

### 3.3 `qbo.sync.log` (persistent)

Audit trail. One row per sync run. Truncate old rows via a retention cron (default 90 days).

| Field | Type | Notes |
|---|---|---|
| `connection_id` | Many2one | |
| `started_at` / `finished_at` | Datetime | |
| `trigger` | Selection | `manual` / `cron` / `initial_full` / `entity_refresh` |
| `entities` | Char | Comma-separated list of entities synced this run |
| `stats_created` / `updated` / `deleted` / `skipped` / `errors` | Integer | |
| `cdc_from` / `cdc_to` | Datetime | Window synced |
| `state` | Selection | `running` / `success` / `partial` / `failed` |
| `error_summary` | Text | |
| `detail_ids` | One2many → `qbo.sync.log.line` | Per-entity per-record row for drill-down |

### 3.4 `qbo.sync.log.line`

Per-entity per-record row. Enables "why did this JE fail to import?" drill-down.

### 3.5 `qbo.field.map` (optional, later phase)

For customizing how QBO Class → Elks Department mapping works when out-of-the-box prefix rules don't fit. Punt until phase 3.

### 3.6 Extensions to existing models

- `elks.account.qbo_id` — Many2one to `qbo.entity.mapping` (or just a Char). Lets us round-trip.
- `elks.journal.entry.qbo_id` — same.
- `res.partner.qbo_id` — same.

---

## 4. API client design

A single thin wrapper in `models/qbo_client.py`.

```python
class QboClient:
    def __init__(self, connection):
        self.conn = connection
        self.session = requests.Session()

    def query(self, sql, page_size=100):
        """Yield rows for a SELECT ... FROM Entity query with pagination."""

    def cdc(self, entities, changed_since):
        """Return dict {entity_type: [rows]} for changes since timestamp."""

    def get(self, entity_type, qbo_id):
        """Fetch a single entity by GUID."""

    # --- internals ---
    def _authorized_headers(self): ...
    def _refresh_if_needed(self): ...
    def _retry(self, method, url, **kwargs):
        """Retries with backoff on 5xx / 429; refresh + retry on 401."""
```

**Rules:**
- Every request goes through `_retry` — nothing bypasses.
- Backoff: 1s, 2s, 4s on 5xx / 429; abort on 3rd failure.
- 401 → refresh token, retry once. If refresh itself 401s, mark connection disconnected.
- Uses `Accept: application/json` (Intuit's default is XML — surprise).
- Logs every request (URL + status + duration) at DEBUG. INFO for retries. WARNING for token refresh. ERROR for permanent failures.
- No third-party client library dependency. `requests` is already in Odoo's requirements.

**Rate limiting:** Intuit allows 500 req/min per realm. Our nightly sync fires ~50-500 requests total. We add a courtesy `time.sleep(0.05)` between calls to avoid bursting.

---

## 5. Entity mappings — QBO → Odoo

This is the meat. Each QBO entity type becomes rows in one or more elksfrs models.

### 5.1 `Account` → `elks.account`

Straightforward. Same 4-column QB semantics as the CSV wizard I already built, but pulled live via `SELECT * FROM Account`.

QBO field | elks.account field | Notes
--- | --- | ---
`AcctNum` | `code` + `subaccount` (split by regex) | Same letter-sub split as CSV wizard
`Name` | `name` |
`AccountType` | `account_type` | Same two-stage classifier as CSV wizard
`AccountSubType` | `account_type` (falls back) |
`ParentRef.value` | `parent_id` | Resolved via mapping table
`Active` | `active` |
`Description` | `note` |
`Classification` | (informational) | For sanity checks

### 5.2 `JournalEntry` → `elks.journal.entry` + lines

QBO's JournalEntry has an outer header + a `Line[]` array. Each Line has either DebitAmount or CreditAmount and an `AccountRef` + optional `ClassRef` (department).

QBO field | elks.journal.entry(.line) field | Notes
--- | --- | ---
`Id` | `qbo_id` on the entry | Via mapping table
`TxnDate` | `date` |
`DocNumber` | `entry_number` | Falls back to `JE-<Id>` if blank
`PrivateNote` | `memo` |
`Line[].AccountRef.value` | `line_ids[].account_id` | Resolved via mapping
`Line[].DebitAmount` | `line_ids[].debit` |
`Line[].CreditAmount` | `line_ids[].credit` |
`Line[].Description` | `line_ids[].name` |
`Line[].ClassRef.value` | `line_ids[].department_id` | Optional; QB "Class" ≈ Elks "Department"

Post state: created entries land as `posted` (QBO doesn't have a draft concept for JEs).

### 5.3 Transactional entities → synthesized JE

QBO models `Invoice`, `Bill`, `Payment`, etc. as first-class transactions with their own line details. Rather than mirror each type as its own Odoo model, we **synthesize an equivalent `elks.journal.entry`** for each one, following standard double-entry rules.

Entity | JE lines synthesized
--- | ---
`Invoice` | Dr AR / Cr each `Line.SalesItemLineDetail.ItemAccountRef` (or IncomeAccount default)
`Bill` | Dr each `Line.AccountBasedExpenseLineDetail.AccountRef` / Cr AP
`Payment` | Dr Bank / Cr AR (matches invoice)
`BillPayment` | Dr AP / Cr Bank
`Deposit` | Dr Bank / Cr each `Line.DepositLineDetail.AccountRef`
`Purchase` | Dr each expense line / Cr `AccountRef` (payment source)
`SalesReceipt` | Dr Bank/UndepositedFunds / Cr each Line's income account
`Transfer` | Dr `ToAccountRef` / Cr `FromAccountRef`
`CreditMemo` | reverse of Invoice
`RefundReceipt` | reverse of SalesReceipt

Each synthesized JE gets `entry_number = f"{QBO_ENTITY_TYPE}-{QBO_ID[:8]}"` (e.g. `INV-a4f1c992`) so treasurer can trace back to QBO by the entry number.

**Balance sanity:** every synthesized JE is validated for `sum(debit) == sum(credit)`. Mismatches (rounding, currency edge cases) go to `qbo.sync.log.line` with `state=error` and the entry is NOT created.

### 5.4 `Customer` / `Vendor` → `res.partner`

Merged into one Odoo partner table with `customer_rank` / `supplier_rank` set appropriately. Existing partners matched by email if present, else name (fuzzy), else new. Manual re-linking wizard covers the ambiguity — this is where partner-dedup gotchas live (see `feedback_vendor_merge_safety` in memory).

### 5.5 `Budget` → `elks.budget`

QBO's Budget entity has BudgetDetail rows per Account per month. We aggregate to annual (matches Elks FRS budget file format) and populate `elks.budget.line` rows.

Sync frequency: once per year at fiscal-year rollover, plus manual "Refresh Budget from QBO" button.

### 5.6 QBO Class → Elks Department

QBO "Class" is optional per-line categorization. Some lodges use it, some don't. We treat it as follows:

1. If line has `ClassRef.value`, look up mapping via `qbo.field.map` (later phase) or by name match against `elks.department.name`.
2. If unmapped, fall back to code-prefix rules (10xxx = Balance Sheet, 40xxx = Bar, etc.) — same rules as the CSV wizard.

---

## 6. Sync strategies

### 6.1 Initial full sync (one-time)

First-time connection, or a "reset and re-pull everything" admin action. Runs in this order:

1. **Accounts** — foundation for everything else
2. **Customers + Vendors** — partner FK targets
3. **Budgets** — before JEs so budget lines have accounts to reference
4. **Transactional entities** in date order:
   - JournalEntry, Invoice, Bill, Payment, BillPayment, Deposit, Purchase, SalesReceipt, Transfer, CreditMemo, RefundReceipt
5. Update `qbo.connection.last_sync_at = now()`

Chunking: pull by year, then quarter, then month, so a mid-run failure limits the retry window.

### 6.2 Incremental sync (nightly, cron)

Uses Intuit's Change Data Capture endpoint. One HTTP request returns changes across all entities since a timestamp.

```
GET /v3/company/{realmId}/cdc
    ?entities=Account,JournalEntry,Invoice,Bill,Payment,BillPayment,
             Deposit,Purchase,SalesReceipt,Transfer,CreditMemo,
             RefundReceipt,Customer,Vendor
    &changedSince=<last_sync_at ISO8601>
```

Response is grouped by entity type. Process each type in dependency order.

**CDC has a 30-day lookback limit.** If `last_sync_at` is more than 30 days ago, fall back to a full sync of the missing window.

**Deleted entities** appear with `status="Deleted"` — flip `qbo.entity.mapping.is_deleted_in_qbo = True` and archive the linked Odoo record (do NOT unlink — treasurer might still need audit trail).

### 6.3 Entity refresh (manual)

Small "Refresh <Entity>" buttons on the connection form. Pull all of one entity type (bounded by date range). Useful when treasurer says "you're missing bill 4237" and we need to force a fresh look.

---

## 7. Scheduling

Two crons.

### `cron_qbo_incremental_sync` — nightly at 2:00 AM lodge-local time

```python
def _cron_qbo_incremental_sync(self):
    conn = self.env['qbo.connection'].sudo().search(
        [('state', '=', 'connected'), ('active', '=', True)], limit=1)
    if not conn:
        return
    log = self.env['qbo.sync.log'].create({
        'connection_id': conn.id, 'trigger': 'cron',
        'started_at': fields.Datetime.now(),
    })
    try:
        self.env['qbo.sync']._run_cdc(conn, log)
    except Exception as e:
        log.write({'state': 'failed', 'error_summary': str(e)})
        _logger.exception("QBO cron sync failed")
    else:
        log.write({'state': 'success',
                   'finished_at': fields.Datetime.now()})
```

Cron `interval_type='days'`, `interval_number=1`, `nextcall` seeded to today 2:00 AM UTC minus lodge offset.

### `cron_qbo_log_retention` — weekly

Deletes `qbo.sync.log` rows older than 90 days (except failed ones — those stay indefinitely for postmortem).

---

## 8. Error handling & idempotency

### Idempotency rules

Every sync operation is a lookup-then-upsert on `qbo.entity.mapping`:

```python
def _upsert(self, entity_type, qbo_row):
    Mapping = self.env['qbo.entity.mapping'].sudo()
    m = Mapping.search([
        ('qbo_entity_type', '=', entity_type),
        ('qbo_id', '=', qbo_row['Id']),
    ], limit=1)

    qbo_updated = parse_qbo_datetime(qbo_row['MetaData']['LastUpdatedTime'])
    if m and m.qbo_last_updated >= qbo_updated:
        return m  # already current
    ...
```

If a nightly sync repeats a JE from last night, the check on `qbo_last_updated` short-circuits before we touch the JE.

### Failure modes and responses

| Failure | Response |
|---|---|
| Network / DNS / 5xx | Retry 3× with backoff; on final failure log to sync.log and stop this run — `last_sync_at` unchanged so tomorrow retries. |
| 401 unauthorized | Refresh token; retry request once. If refresh 401s, mark disconnected + email admin. |
| 429 rate limit | Sleep the `Retry-After` seconds; retry. |
| 400 malformed / unsupported entity | Log detail row, skip that entity, continue with others. |
| Missing FK (JE references account we haven't synced) | Force sync that one account inline, then retry the JE. |
| Unbalanced synthesized JE | Do not create; log detail row with source QBO Id for treasurer to review. |
| Refresh token expired (100 days) | Mark disconnected; email + activity to admin; UI shows "Reconnect" button. |

### Partial success

If some entities succeeded but others failed:
- `qbo.sync.log.state = 'partial'`
- `last_sync_at` advances **only for entity types that fully succeeded**
- Per-type high-water marks stored in `qbo.connection.per_entity_watermarks_json`

---

## 9. Security

- Client secret, access token, refresh token: `groups='base.group_system'`, `no_copy=True`, hidden in list/kanban views.
- OAuth callback controller checks CSRF `state` param against a server-side value stored in the user's session.
- No PII in `qbo.sync.log` beyond QBO entity IDs.
- Only `group_elksfrs_manager` can manually trigger a sync or edit the connection.

Nice-to-have (not blocking): encrypt tokens at rest via `cryptography.fernet` with a key stored in `ir.config_parameter`. Punt to phase 5 unless the lodge's threat model demands it.

---

## 10. Testing plan

### Sandbox first
- Every phase gets tested against Intuit's sandbox realm (they seed a fake company with a few hundred transactions).
- Automated tests: fixture JSON files captured from real sandbox responses; mock the HTTP layer.

### Live-fire dry run
- Before turning on the cron in production, do a **read-only** initial sync into a **fresh** Odoo staging DB. Verify:
  - Every QBO account has an `elks.account` row.
  - `SUM(debit)` and `SUM(credit)` on synthesized JEs match QBO P&L totals within $0.01.
  - Trial balance matches QBO's trial balance to the penny.
- Only then flip the switch on production.

### FRS-report comparison
- Run the existing FRS Actuals export against Odoo data.
- Diff against the treasurer's manual QBO-based FRS submission.
- Zero diff before we sign off on the sync.

---

## 11. File layout

```
elksfrs/
├── models/
│   ├── qbo_connection.py         # config singleton
│   ├── qbo_entity_mapping.py     # QBO GUID → Odoo record map
│   ├── qbo_sync_log.py           # sync run history
│   ├── qbo_sync_log_line.py      # per-entity per-row detail
│   ├── qbo_client.py             # HTTP client (requests wrapper)
│   ├── qbo_sync.py               # orchestrator: initial + CDC + per-entity
│   └── qbo_entity_handlers/      # one file per entity type
│       ├── __init__.py
│       ├── account.py
│       ├── journal_entry.py
│       ├── invoice.py
│       ├── bill.py
│       ├── payment.py
│       ├── bill_payment.py
│       ├── deposit.py
│       ├── purchase.py
│       ├── sales_receipt.py
│       ├── transfer.py
│       ├── credit_memo.py
│       ├── refund_receipt.py
│       ├── customer.py
│       ├── vendor.py
│       └── budget.py
├── controllers/
│   └── qbo_oauth.py              # /qbo/oauth/callback + /qbo/oauth/start
├── data/
│   └── qbo_cron.xml              # nightly sync + retention cron
├── views/
│   ├── qbo_connection_views.xml
│   ├── qbo_entity_mapping_views.xml
│   ├── qbo_sync_log_views.xml
│   └── qbo_menus.xml
├── security/
│   └── (rows added to ir.model.access.csv)
├── wizard/
│   ├── qbo_initial_sync_wizard.py    # "run initial full sync"
│   └── qbo_reconnect_wizard.py       # OAuth re-auth helper
└── docs/
    └── qbo_sync_design.md        # this file
```

---

## 12. Phased implementation plan

Each phase is a shippable increment. You can pause between any two.

### Phase 1 — Connection + Accounts (≈3-4 days)
- `qbo.connection` model + view + singleton constraint
- OAuth controller (`start` + `callback`)
- `qbo.client` with token refresh
- Account entity handler
- "Connect to QuickBooks" and "Sync Accounts Now" buttons
- Manual test: connect to sandbox, sync ~50 accounts

### Phase 2 — Journal Entries + Partners (≈4-5 days)
- Customer, Vendor entity handlers → res.partner
- JournalEntry entity handler → elks.journal.entry
- Balance validation on synthesized entries
- `qbo.sync.log` + `qbo.sync.log.line`
- Manual test: sync ~500 JEs from sandbox, verify trial balance

### Phase 3 — Full transactional coverage (≈5-7 days)
- Invoice, Bill, Payment, BillPayment, Deposit, Purchase, SalesReceipt, Transfer, CreditMemo, RefundReceipt handlers
- Class → Department mapping
- Manual test: full-year sandbox sync, compare P&L to QBO exact-match

### Phase 4 — Nightly cron + CDC (≈2-3 days)
- CDC-based incremental sync
- Cron scheduling
- Failure alerting (mail activity to admin)
- Per-entity watermarks
- Log retention cron

### Phase 5 — Polish + FRS Mapping Export (≈3-4 days)
- Budget entity handler
- FRS Mapping File CSV export using `elks_standard_code`
- Reconnect wizard for expired refresh tokens
- Token encryption (if wanted)
- Admin dashboard: last sync, next sync, error count trends

### Phase 6 — Nice-to-haves (backlog)
- Reconciliation view (side-by-side QBO vs Odoo JE)
- "Sync single transaction" quick action
- Bulk re-link partners wizard
- Webhook receiver (real-time)

**Total est. Phases 1-5: 17-23 focused days.** Realistically, ~5-6 weeks including your testing time.

---

## 13. Open questions

1. **QBO Class handling** — how much do you actually use QBO Classes today? If you don't, we skip the mapping wizard and stick with prefix rules.
2. **Multi-realm** — you presumably only have one lodge / one QBO company. Confirm we don't need to handle multi-company (simplifies a lot).
3. **Historical depth** — for the initial full sync, do you want to pull *all history* or start from a cutover date (e.g. current fiscal year)?
4. **Reconciliation of pre-existing Odoo JEs** — you already have JEs from CLMS dues, Clover POS, cash management. Should those be:
   - Preserved as-is; QBO sync adds new JEs alongside (double-counting risk if treasurer books them in QBO too)
   - Deleted before initial sync (cleaner but loses local context)
   - Marked as "local origin, don't sync to QBO" and reconciled against matching QBO entries
5. **Sandbox testing** — you don't have Intuit credentials yet; will you register a dev account so we can build against sandbox, or do we go straight to production?
6. **Failure notifications** — email to `dslewiston@gmail.com`, mail activity in Odoo, both?

---

## 14. Estimated cost / dependencies

- **New Python packages:** none. `requests` is already in Odoo's stack.
- **Intuit account cost:** free (developer + sandbox + production API).
- **Infrastructure:** one nightly cron. Zero DB size growth to speak of beyond the JE data you're already keeping.
- **Ongoing maintenance:** Intuit occasionally deprecates API versions; ~1 day/year to bump versions.

---

**Next step:** review this doc, mark up sections you want changed (especially the open questions in §13), and if it looks right I'll start with Phase 1 — connection model, OAuth flow, and Account sync.
