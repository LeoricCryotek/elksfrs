# Manual QBO → FRS Workflow

**For:** Lodge treasurers producing FRS submissions from QuickBooks Online data without the OAuth API integration.
**Time budget:** ~15 minutes per month, once set up.
**Requires:** QBO Online login, Odoo login with FRS Manager group, elksfrs module v19.0.2.1+.

---

## 0. One-time setup

Do these three steps **once**, on initial FRS rollout or when you first install this module.

### 0.1. Configure Lodge Settings

**In Odoo:**

1. **Elks FRS → Configuration → Lodge Settings**
2. Fill in:
   - **Lodge Name**: `Lewiston Elks Lodge No. 896`
   - **Lodge Number**: `0896` (4 digits, leading zero if needed)
   - **Lodge Charter Date**: date the lodge was chartered
   - **Cash on Hand GL Code**: `10000` (Uniform CoA) or `10101` (Lewiston's actual code)
   - **Operating Checking GL Code**: `10100` (Uniform) or `10201` (Lewiston's)

### 0.2. Import your Chart of Accounts from QuickBooks

**In QBO:**

1. **Accounting → Chart of Accounts**
2. Top-right gear icon → **Export to Excel** → save `.xlsx` file
3. Convert to CSV (Excel → Save As → CSV) if the wizard needs `.csv`

**In Odoo:**

1. **Elks FRS → Import → Import Chart of Accounts (QB CSV)**
2. Upload the CSV, select encoding `UTF-8 (BOM)` (default)
3. Click **Preview** — review the proposed create/update list
4. Click **Import** when the preview looks right

### 0.3. Submit the one-time FRS Beginning Balance file

Only required if your lodge is just starting to report to Adaptive. If you've been submitting FRS for a while, skip this.

**In QBO:**

1. **Reports → Balance Sheet** (under "Business overview")
2. Set **As of** to the day before your FRS reporting period begins (e.g. `3/31/<prior year>` for a new fiscal year)
3. **Customize** → make sure **Show account numbers** is ON
4. **Run report** → Export → **Export to Excel**

**In Odoo:**

1. **Elks FRS → FRS Reporting → QBO Export → FRS CSV**
2. Pick **Report Type = Balance Sheet (as-of snapshot)**
3. **Source = Upload Excel / CSV export**
4. Upload the file, verify lodge number
5. Click **Transform** → **Download CSV**
6. Email the CSV to `adaptive@elks.cloud` with **"Actual"** in the subject line

---

## 1. Monthly FRS Actuals submission

**Deadline:** 3rd Friday of each month, for the month that just ended.
**Best practice:** submit Fiscal YTD every month, not just the current month. Adaptive overwrites, so YTD keeps every backdated correction in sync.

### Step 1 — Run and export the QBO report (5 min)

**In QBO:**

1. **Reports** → search for and open **Transaction Detail by Account**
2. Click the **three dots** next to the report title → **Customize**
3. **Report period:**
   - For YTD (recommended): Start date = `04/01/<FY start year>`, End date = end of the last completed month
   - For single month: just that month (e.g. `10/01/2026` to `10/31/2026`)
4. **Rows / Columns → Change columns** — set these exactly, in this order, nothing else:
   - Account #
   - Date
   - Debit
   - Credit
5. Click **Run report**
6. Top-right **Export icon** (just left of the gear) → **Export to Excel**
7. Save the file

### Step 2 — Transform in Odoo (2 min)

**In Odoo:**

1. **Elks FRS → FRS Reporting → QBO Export → FRS CSV**
2. Pick:
   - **Report Type = Actuals (Transaction Detail by Account)**
   - **Source = Upload Excel / CSV export**
3. Upload the file
4. Verify **Lodge Number** (should be pre-filled from Lodge Settings)
5. **Aggregation = One row per transaction** (default; monthly rollup also fine)
6. Click **Transform**
7. Verify the preview shows:
   - A row count that looks right
   - No skipped rows (or acceptable ones, listed in the report)
   - The suggested filename matches the FRS convention:
     - YTD: `Lodge 0896 - Actual Monthly Activity - YTD through Oct 2026.csv`
     - Single month: `Lodge 0896 - Actual Monthly Activity - Oct 2026.csv`
8. Click **Download CSV**

### Step 3 — Email to Adaptive (2 min)

1. New email to `adaptive@elks.cloud`
2. **Subject line must contain the word "Actual"** — the lodge number and month after it are optional (e.g. `Actual - Lodge 0896 - Oct 2026 YTD`)
3. Attach the CSV from step 2
4. Send
5. Watch for the auto-reply within 5 minutes:
   - **Success**: filed — done
   - **Error**: open the email, read the detailed error list, correct in QBO, re-run steps 1-3

---

## 2. Annual Budget submission

**When:** After submitting April and May Actuals for the new fiscal year (so Adaptive has prior-year actuals to allocate the budget against).
**Frequency:** Once per fiscal year, plus any time you revise the budget.

### Option A — Export from QBO

**In QBO:**

1. **Gear icon (top-right) → Budgeting**
2. Open the current fiscal year's budget
3. Edit dropdown → **Run Budget Overview Report**
4. **Export icon → Export to Excel**

*Note:* QBO's budget export needs manual formatting that Odoo's wizard doesn't yet automate. For now, follow the "Creating The Budget File" instructions in the bundled `FRS_QuickBooks_Online_Guide.pdf`:

**Elks FRS → FRS Reporting → QBO Export → FRS CSV → Instructions tab**

### Option B — Build the budget in Odoo

**In Odoo:**

1. **Elks FRS → FRS Reporting → Budgets → New**
2. Enter line-by-line, or use the **Import Budget** wizard
3. Workflow states: Draft → Board Approved → Floor Approved → Submitted to FRS
4. Once Floor Approved, go to **FRS Reporting → Export FRS CSV**
5. Pick **Export Type = Annual Budget**, pick the budget, click **Generate CSV**
6. Filename will be `Lodge 0896 - Annual Budget - FYE 2027.csv`

Email to `adaptive@elks.cloud` with **"Budget"** in the subject line.

---

## 3. Chart of Accounts changes

Any time you add, rename, or deactivate an account in QBO, resubmit the updated Mapping file to Adaptive.

**For now:** manually build a 3-column CSV per the 2019 FRS Introduction PDF:

```
LodgeNumber,LodgeGLAccount,ElksStandardGLAccount
0896,10101,10000
0896,10201,10100
...
```

Email to `adaptive@elks.cloud` with **"Map"** in the subject line.

A one-click Mapping export from `elks.account.elks_standard_code` is a planned future enhancement.

---

## 4. Monthly checklist (printable)

Tape this to the treasurer's monitor:

```
 Monthly FRS Checklist — due 3rd Friday
 ───────────────────────────────────────
 [ ] 1. QBO → Reports → Transaction Detail by Account
 [ ] 2. Customize: Account # / Date / Debit / Credit
 [ ] 3. Set period: Apr 1 → end of last complete month (YTD)
 [ ] 4. Export to Excel
 [ ] 5. Odoo → Elks FRS → FRS Reporting → QBO Export → FRS CSV
 [ ] 6. Upload, Transform, Download CSV
 [ ] 7. Email adaptive@elks.cloud
        Subject must contain: Actual
        Attach: Lodge 0896 - Actual Monthly Activity - YTD ...
 [ ] 8. Check inbox for success confirmation (~5 min)
```

---

## 5. Troubleshooting

| Problem | Fix |
|---|---|
| "No transactions found in the file" | QBO columns are wrong order or missing. Re-run step 1.4. |
| "Account # column missing" | Enable **Show account numbers** in QBO → Account and Settings → Advanced → Chart of Accounts |
| Adaptive rejects with "unknown account" | A QBO account doesn't match the Uniform CoA. Fix in QBO or submit a Mapping file. |
| "Date format" error from Adaptive | Make sure QBO's locale is set to US (`mm/dd/yyyy`). |
| Skipped rows in the preview report | Likely rows with no transactions (totals, headers). Expected. Only worry if the count is wildly off. |

---

## 6. Reference PDFs (bundled)

Open the **QBO Export → FRS CSV** wizard, **Instructions** tab, for direct download links to the four official FRS reference documents:

- FRS Introduction 2019
- FRS QuickBooks Online Guide
- FRS Actuals and Budgets
- Elks Balance Sheet Reference
