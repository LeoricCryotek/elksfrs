# Cash Management Redesign — Bank, Tills, and Movements

**Status:** Draft v2 — for review
**Author:** Danny Santiago (with Claude)
**Module:** `elksfrs` v19.0.1.6 → next
**Scope:** Replace the current `elks.cash.on.hand` + `elks.register` + `elks.register.transfer` + `elks_cash_register_ext.py` cluster with a cleaner three-model workflow centered on **The Bank**, a set of **Tills**, and **Personal Till Bags**.
**Out of scope:** Check tracking (handled separately by the bookkeeper). This system is **cash-only**.

## 0. Guiding principle — paper first, count digital

The **only** required digital record is the **count**. Everything else (change requests, change orders, till deposits, even Monday bank deposits) is **paper-first** — printed blank from this system, hand-filled, kept in the bag/till/safe binder. Digitizing those slips is **optional convenience**, not a workflow gate.

- Print blanks → drop in tills/bags → people hand-fill → counts get entered later. That's the loop.
- The only digital event that *must* exist is the periodic cash count.
- One exception: the **Monday Bank deposit** is the GL-posting event, so a treasurer who wants the JE auto-created records it here. Anyone who'd rather post a manual journal entry can skip even that.

---

## 1. Problem with the current model

The existing cash-management code has the right *pieces* but they don't fit how the lodge actually works.

| Symptom in code | Real-world cause |
|---|---|
| `elks.lodge.settings.par_*` implicitly represents "the Bank" but nothing counts what's actually in the safe against those pars. | The Bank isn't a first-class entity. The system treats par levels as targets but has no place to record the safe's real contents. |
| `elks_cash_register_ext.py:59-84` overrides `qty_pennies`, etc. on `elks.cash.on.hand` to be computed from register lines. A standalone Bank count returns zero. | One model (`elks.cash.on.hand`) tries to be both "weekly Bank count" and "shift till count." |
| `elks.register.transfer` is till-to-till and only inside a counting session. | Real change orders are **Bank → Till**, happen any day, and need a person + slip number for audit. |
| Deposit and change-order quantities are *computed*, never *recorded* as events. | You can't see "we deposited $10,742.05 on Mon 2026-05-11" — only the count that suggested it. Charts over time are impossible. |
| No way to print a blank denomination slip for hand-counting before data entry. | Paper-first workflow is not supported. |

## 2. New mental model

> **The Bank** is the lodge's central cash reserve, kept in the safe, used to fill change requests.
> **Tills** (Main Bar, Dining) and **Personal Till Bags** (per-server) hold working cash at point-of-sale.
> When a Till or Bag is short on a denomination, **it requests change from the Bank** — Till-initiated, not Bank-pushed.
> Cash gets **Counted** at points in time — weekly for the Bank, end-of-shift for Tills/Bags. **Counts are the data.**
> Everything else is paper.

Three new models replace four old ones. The Till is the protagonist.

```
┌──────────────────────────────────────────────────────────────────────┐
│                      elks.cash.location                              │
│                                                                      │
│   ┌─────────────────────────────────┐         ┌──────────┐           │
│   │    Tills & Personal Bags        │  ───▶   │   BANK   │           │
│   │  Main Bar │ Dining-Susan-Bag    │ requests│ singleton│           │
│   │  Event 2026-06-15               │ change  └──────────┘           │
│   └─────────────────────────────────┘         (fills request)        │
│        ▲                                            ▲                │
│        │ counted at shift close                     │ counted weekly │
│        │                                            │                │
│   ┌────┴───────┐                              ┌─────┴────────┐       │
│   │ cash.count │  ← REQUIRED DIGITAL          │  cash.count  │       │
│   └────────────┘                              └──────────────┘       │
│                                                                      │
│   ┌──────────────────────────────────────────────────────┐           │
│   │       elks.cash.movement (mostly OPTIONAL)            │          │
│   │  change_request: Till → Bank  (paper-first, optional) │          │
│   │  change_order  : Bank → Till  (paper-first, optional) │          │
│   │  till_deposit  : Till → Bank  (paper-first, optional) │          │
│   │  bank_deposit  : Bank → 10100 (the one JE event)      │          │
│   │  bank_stock    : 10100 → Bank (occasional change buy) │          │
│   └──────────────────────────────────────────────────────┘           │
└──────────────────────────────────────────────────────────────────────┘
```

## 3. Models

### 3.1 `elks.cash.location`

Master data — one record per cash-holding location.

| Field | Type | Notes |
|---|---|---|
| `name` | Char, required | "The Bank", "Main Bar Till", "Susan Dining Bag", "Event Bar 2026-06-15" |
| `code` | Char, unique | Short code for slips: `BANK`, `MAIN`, `DINING-SUSAN`, `EV-20260615` |
| `location_type` | Selection: `bank` / `till` / `bag` / `event_till` | Drives behavior |
| `is_bank` | Boolean, computed/stored from type | Used in singleton constraint |
| `assigned_to_id` | Many2one(`res.partner`), optional | For `bag` type — the server who carries the bag. Optional for tills. |
| `event_date` | Date, optional | For event tills; auto-archives after this date + N days |
| `active` | Boolean, default True | |
| `starter_*` | Integer per denomination (13 fields) | Suggested starter level. Bank doesn't use this. |
| `starter_total` | Monetary, computed | |
| `current_balance` | Monetary, computed (non-stored) | Running balance from prior counts + recorded movements since. **Informational only.** Will be approximate if movements aren't digitized — that's expected. |
| `last_count_id` | Many2one(`elks.cash.count`), computed | Most recent count |
| `last_count_date` | Date, related | |

**`bag` vs `till`** — functionally identical (both hold change, both request from Bank, both get counted). The distinction exists for reporting/filtering only. A `bag` *typically* has `assigned_to_id` set; a `till` typically doesn't.

**Constraint:** exactly one record with `location_type='bank'` (singleton).

```python
_sql_constraints = [
    ('unique_bank',
     "EXCLUDE (location_type WITH =) WHERE (location_type = 'bank')",
     "Only one Bank location is allowed."),
]
```

(Or simpler — a Python `@api.constrains` since SQL exclusion needs `btree_gist`.)

### 3.2 `elks.cash.count`

One denomination count event for one location at a point in time.

| Field | Type | Notes |
|---|---|---|
| `name` | Char, computed | "BANK 2026-05-11" or "MAIN 2026-05-08 22:30" |
| `location_id` | Many2one(`elks.cash.location`), required | |
| `count_date` | Datetime, required, default=now | |
| `count_type` | Selection: `weekly_bank`, `shift_close`, `event_close`, `audit`, `other` | |
| `counted_by_id` | Many2one(`res.users`), required, default=current user | |
| `witnessed_by_id` | Many2one(`res.users`), optional | For two-person counts |
| `state` | Selection: `draft`, `done` | Done is read-only |
| `qty_*` | Integer per denomination (13 fields) | Direct entry — no overrides, no aggregation |
| `sub_*` | Monetary per denomination, computed | qty × face value |
| `total_bills` | Monetary, computed | |
| `total_coins` | Monetary, computed | |
| `total_cash` | Monetary, computed | bills + coins |
| `notes` | Text | |
| `slip_attachment_ids` | Many2many(`ir.attachment`) | Scan/photo of the paper slip |

**Slip print:** `Print → Cash Count Slip` produces a printable QWeb PDF in the layout of your reference picture (bills column, coins column, totals at the bottom — no checks). The PDF can be printed *blank* (all qty=0) from a count in `draft` state, used for hand-counting, then the numbers entered back into Odoo.

### 3.3 `elks.cash.movement` (mostly optional)

One cash event moving between locations or between a location and an Odoo account. **Records are optional except for `bank_deposit`** (which exists to post the Monday journal entry). The slip prints below — that's the thing that gets used daily; the model is here to capture them if/when anyone bothers to enter one.

| Field | Type | Notes |
|---|---|---|
| `name` | Char, computed | "CHG-REQ 2026-05-08 #142" |
| `movement_type` | Selection (see below) | Drives source/destination requirements |
| `move_date` | Datetime, required | When the cash physically moved |
| `from_location_id` | Many2one(`elks.cash.location`), conditional | See movement-type table |
| `to_location_id` | Many2one(`elks.cash.location`), conditional | See movement-type table |
| `from_account_code` | Char, conditional | "10100" for bank_stock — money leaving Operating Checking |
| `to_account_code` | Char, conditional | "10100" for bank_deposit — money going to Operating Checking |
| `done_by_id` | Many2one(`res.users`) | The person who physically moved the cash. Required for digitized movements; blank fine on prints. |
| `authorized_by_id` | Many2one(`res.users`), optional | Officer who signed the slip |
| `slip_number` | Char | Sequence-generated or transcribed from paper slip |
| `qty_*` | Integer per denomination (13 fields) | Denomination breakdown |
| `total_amount` | Monetary, computed | |
| `journal_entry_id` | Many2one(`elks.journal.entry`), readonly | Auto-created only for `bank_deposit` and `bank_stock` |
| `state` | Selection: `draft`, `posted`, `cancelled` | Posted creates the JE if applicable |
| `notes` | Text | |

**Movement types — direction follows the lodge's actual workflow (Till initiates):**

| Type | From | To | Posts JE? | Required digital? | Use case |
|---|---|---|---|---|---|
| `change_request` | Till/Bag | Bank | No | No | Server: "I need 20× $5 = $100" — hands large bills to Bank with slip |
| `change_order` | Bank | Till/Bag | No | No | Bank fills the request — hands back small bills with signature |
| `till_deposit` | Till/Bag | Bank | No | No | End-of-shift cash dropped in safe |
| `bank_deposit` | Bank | (account 10100) | **Yes** — Dr 10100 / Cr 10000 | **Yes (or post manual JE)** | Monday run to the actual bank |
| `bank_stock` | (account 10100) | Bank | Yes — Dr 10000 / Cr 10100 | No (rare) | Treasurer bought $500 of singles at the bank |

**Note on `change_request` + `change_order`:** these are two sides of the same paper interaction. In practice one slip captures both (denominations turned in + denominations received). The two record types exist so a digitized capture can distinguish them; the printed slip has both columns side-by-side and only generates a `change_order` record if the bank-side signature is filled in. If you never digitize change slips, neither record type ever exists in the DB — that's fine.

### 3.4 Printable slips (the actually-used artifacts)

These are printed **blank** from the system, stocked in piles near the safe and at each till, and hand-filled with a pen. Each carries a pre-printed serial number from an Odoo `ir.sequence` so paper-and-digital can be cross-referenced later if needed.

| Slip | Trigger | Layout |
|---|---|---|
| **Cash Count Slip** | Print blank stack; one filled at each count | Bills column / Coins column with subtotals → grand total. Header has location name, date, counted-by signature line. Mirrors the picture Danny attached. |
| **Change Request + Order Slip** | Print blank stack at each till/bag | Two columns: "Turned In" (denoms going Till→Bank) and "Received" (denoms going Bank→Till). Server signs the request side; Bank manager signs the order side. |
| **Deposit Slip** | Print one per Monday deposit | Denomination grid + total + signatures (treasurer + witness). Matches what the real bank wants. |
| **Bag Tag** | Print on creating a personal till bag | Small tag with bag code, assigned-to name, starter total. Stays in the bag. |

All slips are QWeb reports with a "Print Blank" button on the corresponding model/location that emits the form with zero quantities pre-filled.

## 4. Workflows

### 4.1 Per-shift Till / Bag workflow (paper-only, optional digital)

1. **During shift** — Server or bartender is short on $5s. Pulls a blank **Change Request + Order Slip** from the till/bag's stack. Fills in the "Turned In" column (e.g. one $100) and the "Received" column they want (e.g. 20× $5). Walks to the Bank.
2. **At the Bank** — Bank manager verifies, fills the order, both sign the slip. Slip goes back in the till/bag.
3. **End of shift** — Bartender counts the till using a printed **Cash Count Slip**. Fills denomination quantities. Drops the cash + the count slip into the safe (or into the till bag for the bag workflow).
4. **Next day** — Treasurer enters the count: `Cash Management → Counts → New (Main Bar Till, shift_close)`. This is the **only required digital step**.
5. (Optional) Change Request/Order slips can be entered too, but nothing depends on it.

### 4.2 Weekly Monday Bank workflow (count + deposit, both digital)

1. **Monday AM** — Treasurer opens the safe.
2. **Count** the Bank: `Cash Management → Counts → New (Bank, weekly_bank)`. Enter denomination quantities. Save → `state='done'`.
3. **Decide deposit amount.** Treasurer eyeballs the count, decides how much cash to deposit and how much to leave in the safe for next week's change requests (informed by par-level guidelines, but not enforced).
4. **Create `bank_deposit` movement.** From Bank → account `10100`. Enter denomination breakdown of what's going to the bank. Print **Deposit Slip**. Save → `state='posted'` → journal entry auto-created: `Dr 10100 Operating Checking / Cr 10000 Cash on Hand`.
5. **If Bank is short** on small bills/coins for the coming week, treasurer takes a change request to the actual bank along with the deposit. When the change comes back, record a `bank_stock` movement (Account 10100 → Bank): `Dr 10000 / Cr 10100`. Net effect of deposit + restock leaves 10100 up by the net deposit and the safe's denomination mix rebalanced.

### 4.3 Event Till workflow

1. Treasurer creates a new event till location: `Event Bar 2026-06-15` (location_type=`event_till`).
2. Pre-event: server fills a Change Request slip from Bank → Event Bar for the starting float.
3. Post-event: `cash.count` of the Event Bar. (Required digital step.)
4. Cash dropped in safe; (optional) `till_deposit` movement recorded.
5. Location auto-archives after `event_date + 30 days` (cron).

## 5. Reporting / charting

Because counts are the spine, charts come straight off `elks.cash.count` with standard Odoo graph/pivot views — no custom rendering needed.

- **Weekly Bank cash on hand over time** — graph view on `elks.cash.count.total_cash` filtered to `location.is_bank=True, count_type='weekly_bank'`, grouped by week. Line chart shows trend.
- **Total counted by location** — pivot: rows=`location_id`, columns=month, values=`sum(total_cash)`.
- **Per-shift till performance** — pivot on `location_id` × week for `count_type='shift_close'`. Spots which tills consistently come in higher.
- **Monday deposit total over time** (if digitized) — graph on `elks.cash.movement` filtered to `movement_type='bank_deposit'`, grouped by week, sum `total_amount`.
- **Who counted what** — pivot on `counted_by_id`, useful for ensuring counts are spread across officers (audit hygiene).

Every chart is a saved search on `elks.cash.count` or `elks.cash.movement` — no new code.

## 6. Migration plan

Existing data:

- `elks.register` records → migrate to `elks.cash.location` with `location_type='till'`, carrying `starter_*` quantities forward.
- Insert one `elks.cash.location` with `location_type='bank', name='The Bank', code='BANK'`.
- `elks.cash.on.hand` records:
  - If they have `register_line_ids` → split into one `elks.cash.count` per register line (each line had its own location + quantities). Session metadata (date, counted_by, witnessed_by) copied onto each new count.
  - If they have no register_line_ids → migrate as one `elks.cash.count` against The Bank.
- `elks.register.transfer` records → migrate to `elks.cash.movement` with `movement_type='change_order'` (or `change_request` depending on direction inferred from the registers). Optional — these are mostly noise; could also just be dropped.
- `elks.lodge.settings.par_*` → keep, repurposed as **suggested Bank par levels** shown on the Bank count form as a sidebar reference. No enforcement.

Migration is a `post_init_hook` extension that runs once on the upgrade. After successful migration the old tables can be dropped in a follow-up migration.

## 7. Files to add / modify / delete

**New files:**

```
models/elks_cash_location.py             # new
models/elks_cash_count.py                # new (replaces cash_on_hand + denomination + par_levels + register_ext)
models/elks_cash_movement.py             # new (replaces register_transfer; mostly optional records)
views/elks_cash_location_views.xml       # new (incl. "Print Bag Tag" button)
views/elks_cash_count_views.xml          # new (incl. graph view + "Print Blank Count Slip" button)
views/elks_cash_movement_views.xml       # new (incl. graph + pivot)
report/cash_count_slip.xml               # new — matches Danny's reference picture (bills + coins, no checks)
report/change_request_order_slip.xml     # new — two-column slip (Turned In / Received)
report/bank_deposit_slip.xml             # new
report/bag_tag.xml                       # new — small tag for personal till bags
data/elks_cash_location_data.xml         # new — creates The Bank singleton on install
data/elks_cash_sequences.xml             # new — sequences for slip numbers
migrations/19.0.2.0.0/post-migrate.py    # new — data migration
```

**Modified:**

```
__manifest__.py                      # bump to 19.0.2.0, add new files, drop old
__init__.py                          # remove old post_init bank seeding if any
models/__init__.py                   # add new modules
models/elks_lodge_settings.py        # repurpose par_* fields as Bank par hints (no behavior change in storage)
views/elksfrs_menus.xml              # restructure menu: Cash Management → Locations / Counts / Movements / Reports
```

**Deleted:**

```
models/elks_cash_denomination.py     # absorbed into elks_cash_count.py
models/elks_cash_par_levels.py       # par fields move to lodge settings (already there)
models/elks_cash_register_ext.py     # gone — no more cash.on.hand override
models/elks_register.py              # replaced by elks_cash_location.py
models/elks_register_count_line.py   # replaced by elks_cash_count.py
models/elks_register_transfer.py     # replaced by elks_cash_movement.py
models/elks_dues_deposit.py          # KEEP — dues deposits are separate, not affected
views/elks_register_views.xml        # replaced
views/elks_cash_register_views.xml   # replaced
views/elks_dues_deposit_views.xml    # KEEP
```

## 8. Open questions / decisions for Danny

1. **Singleton enforcement** — Python `@api.constrains` (portable) or PostgreSQL exclusion constraint (requires `btree_gist`)? Recommend the Python constraint.
2. **GL accounts** — confirm:
   - `10000` = Cash on Hand (asset) — exists in COA? (Current COA goes 10100 = Operating Checking.)
   - If `10000` doesn't exist, add it. The Bank's cash *must* live in a GL account separate from `10100`, or `bank_deposit` becomes a no-op.
3. **Slip serial numbers** — pre-print sequences on every slip (so paper slips reference real-world serials even if never digitized), or only generate a serial when a slip is *entered* in Odoo? Recommend **pre-print sequences**: each "Print Blank" action pulls the next slip number, so paper slips have unique serials whether or not they're ever digitized.
4. **Running balance display** — informational only per your earlier answer. Show on the Bank location form anyway as a sanity check before deposits, with a "as of last count + movements since" caveat? Recommend yes.
5. **Old `qty_*` data on cash.on.hand** — the existing override likely stored zeros for most records. Confirm whether anything live needs migration, or whether we can wipe the cash-management tables and start fresh after a DB backup.
6. **Personal bag assignment field** — `assigned_to_id` should point to `res.partner` (so non-employee volunteers work) or `res.users` (so it ties to login for audit)? Recommend `res.partner` — bags are physical objects assigned to humans, not user accounts.
7. **Bag tag print on creation** — auto-open the bag tag report when a new `bag` location is saved, or button-driven? Recommend button-driven so treasurer can re-print on demand.

## 9. What would not change

- All accounting models (`elks.account`, `elks.journal.entry`, `elks.budget`, `elks.dues.rate`, `elks.frs.submission`) — untouched.
- All wizards — untouched. `frs_export_wizard`, `qb_import_wizard`, `clms_import_dues`, etc. operate on journal entries, which `bank_deposit` and `bank_stock` movements will produce in the same shape.
- COA data (except possibly adding 10000 Cash on Hand if missing).
- Dues processing flow.

## 10. Estimated effort

- Models + views + reports: **~3 days** of focused work.
- Migration script + testing on a staging DB copy: **~1 day**.
- Documentation + treasurer training notes: **~half day**.

Total: **~5 days** end to end.

---

**Next step:** review this, mark up sections you want changed (especially section 8 open questions), and if it looks right I'll start with `elks_cash_location.py` + `elks_cash_count.py` and the migration.
