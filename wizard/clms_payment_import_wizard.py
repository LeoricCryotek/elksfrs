# -*- coding: utf-8 -*-
"""Import CLMS "Dues Payments by Date" CSV export.

Reads the transaction-level CSV that CLMS exports (one row per fee line),
groups rows by ``TranGroupID`` (= one member payment), matches the member
by ``TranMemberNum``, creates an ``elks.dues.payment`` with proper line
items, and posts it (which creates a journal entry and advances the
member's paid-to date).

Key CSV columns used
~~~~~~~~~~~~~~~~~~~~
- **TranGroupID** – groups all fee lines belonging to one payment
- **TranMemberNum** – lodge member number → matched to ``x_detail_member_num``
- **TranLastName** – "Last, First" for display / error messages
- **TranPaidToDate** – only on the primary dues line; advances paid-to
- **TranDate** – transaction date
- **TranAmount** – amount for this fee line
- **TranCheckNum** – check number or "CC" for credit card
- **TranCreditAccount** – CLMS GL credit account (e.g. 3010003)
- **TranDebitAccount** – CLMS GL debit account (e.g. 1010101)
- **TranCodeDescription** – human-readable fee name
- **TranTranCode** – short CLMS tran code (5F, 20, 19 …)
- **TranCodeType** – CLP / CNM / OD etc.
"""
import base64
import csv
import io
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ClmsPaymentImportWizard(models.TransientModel):
    _name = "clms.payment.import.wizard"
    _description = "CLMS Dues Payment Import"

    csv_file = fields.Binary("CSV File", required=True)
    csv_filename = fields.Char("Filename")
    update_paid_to = fields.Boolean(
        "Update Paid-To Date", default=True,
        help="Advance each member's dues-paid-to date to TranPaidToDate "
             "from the CSV. Uncheck if you only want the financial records.",
    )
    skip_existing = fields.Boolean(
        "Skip Already-Imported Transactions", default=True,
        help="Skip payments where a dues payment already exists for this "
             "member on the same date with the same check number.",
    )
    result_text = fields.Text("Import Results", readonly=True)
    state = fields.Selection([
        ('upload', 'Upload'),
        ('done', 'Done'),
    ], default='upload')

    # ------------------------------------------------------------------
    # Import action
    # ------------------------------------------------------------------
    def action_import(self):
        self.ensure_one()
        if not self.csv_file:
            raise UserError(_("Please upload a CSV file."))

        raw = base64.b64decode(self.csv_file)
        # Handle BOM
        text = raw.decode('utf-8-sig')
        reader = csv.DictReader(io.StringIO(text))

        # Validate required columns
        required = {
            'TranGroupID', 'TranMemberNum', 'TranLastName',
            'TranDate', 'TranAmount', 'TranCreditAccount',
            'TranCodeDescription', 'TranTranCode',
        }
        if reader.fieldnames:
            missing = required - set(reader.fieldnames)
            if missing:
                raise UserError(_(
                    "CSV is missing required columns: %s",
                    ", ".join(sorted(missing)),
                ))

        # Read all rows
        rows = list(reader)
        if not rows:
            raise UserError(_("CSV file is empty."))

        # Group by TranGroupID (one group = one member payment)
        groups = {}
        for row in rows:
            gid = row.get('TranGroupID', '').strip()
            if gid:
                groups.setdefault(gid, []).append(row)

        result = self._process_groups(groups)
        self.write({
            'result_text': result,
            'state': 'done',
        })
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    # ------------------------------------------------------------------
    # Core processing
    # ------------------------------------------------------------------
    def _process_groups(self, groups):
        Partner = self.env['res.partner']
        DuesPayment = self.env['elks.dues.payment']
        DuesRate = self.env['elks.dues.rate']
        Account = self.env['elks.account']

        # Pre-load rate lookup by credit_account_code
        all_rates = DuesRate.search([])
        rate_by_credit = {}
        for r in all_rates:
            code = (r.credit_account_code or '').strip()
            if code:
                rate_by_credit.setdefault(code, []).append(r)

        # Pre-load accounts by CLMS code pattern
        # CLMS uses 7-digit codes like 3010003; our COA uses 5-digit like 30010
        # Build a mapping: CLMS code → elks.account
        all_accounts = Account.search([])
        acct_by_code = {a.code: a for a in all_accounts}

        created = 0
        skipped_existing = 0
        skipped_no_member = 0
        errors = []
        total_amount = 0.0
        members_processed = []

        for gid, group_rows in groups.items():
            try:
                result = self._process_one_group(
                    gid, group_rows,
                    Partner, DuesPayment, DuesRate,
                    rate_by_credit, acct_by_code,
                )
                if result == 'skipped_existing':
                    skipped_existing += 1
                elif result == 'skipped_no_member':
                    skipped_no_member += 1
                elif result:
                    created += 1
                    total_amount += result['amount']
                    members_processed.append(result['name'])
            except Exception as e:
                member_name = group_rows[0].get('TranLastName', '?')
                member_num = group_rows[0].get('TranMemberNum', '?')
                errors.append(f"{member_name} ({member_num}): {e}")
                _logger.exception(
                    "CLMS payment import error for group %s", gid
                )

        # Build summary
        parts = [
            "═══ CLMS DUES PAYMENT IMPORT ═══",
            f"Transaction groups in CSV: {len(groups)}",
            f"Payments created: {created}",
            f"Total amount: ${total_amount:,.2f}",
        ]
        if skipped_existing:
            parts.append(f"Skipped (already imported): {skipped_existing}")
        if skipped_no_member:
            parts.append(f"Skipped (member not found): {skipped_no_member}")
        if members_processed:
            parts.append("")
            parts.append("Members processed:")
            for name in members_processed:
                parts.append(f"  • {name}")
        if errors:
            parts.append("")
            parts.append(f"Errors ({len(errors)}):")
            for e in errors[:30]:
                parts.append(f"  ✗ {e}")
            if len(errors) > 30:
                parts.append(f"  … and {len(errors) - 30} more")

        return "\n".join(parts)

    def _process_one_group(self, gid, rows, Partner, DuesPayment,
                           DuesRate, rate_by_credit, acct_by_code):
        """Process one TranGroupID (= one member payment).

        Returns dict with 'name' and 'amount', or a skip string.
        """
        first = rows[0]
        member_num = (first.get('TranMemberNum') or '').strip()
        member_name = (first.get('TranLastName') or '').strip()
        check_num = (first.get('TranCheckNum') or '').strip()

        # Parse transaction date
        tran_date_str = (first.get('TranDate') or '').strip()
        tran_date = self._parse_date(tran_date_str)
        if not tran_date:
            raise UserError(_(
                "Cannot parse date '%(date)s' for %(member)s",
                date=tran_date_str, member=member_name,
            ))

        # Find the member
        partner = Partner.search([
            ('x_detail_member_num', '=', member_num),
        ], limit=1)
        if not partner:
            # Try with leading zeros stripped
            partner = Partner.search([
                ('x_detail_member_num', '=', member_num.lstrip('0')),
            ], limit=1)
        if not partner:
            _logger.warning(
                "CLMS payment import: member %s (%s) not found",
                member_num, member_name,
            )
            return 'skipped_no_member'

        # Check for duplicate if requested
        if self.skip_existing:
            existing = DuesPayment.search([
                ('partner_id', '=', partner.id),
                ('payment_date', '=', tran_date),
                ('check_number', '=', check_num or False),
                ('clms_status', '=', 'processed'),
            ], limit=1)
            if existing:
                return 'skipped_existing'

        # Parse paid-to date from the primary dues line
        paid_to_date = False
        for row in rows:
            ptd = (row.get('TranPaidToDate') or '').strip()
            if ptd:
                paid_to_date = self._parse_date(ptd)
                break

        # Build payment lines
        payment_lines = []
        total = 0.0
        for row in rows:
            amount = float(row.get('TranAmount', 0))
            if amount <= 0:
                continue

            credit_code = (row.get('TranCreditAccount') or '').strip()
            description = (row.get('TranCodeDescription') or '').strip()
            tran_code = (row.get('TranTranCode') or '').strip()

            # Try to match a dues rate
            rate = self._find_rate(
                credit_code, tran_code, description,
                rate_by_credit,
            )

            line_vals = {
                'description': description or f"CLMS code {tran_code}",
                'amount_paid': amount,
                'default_amount': rate.amount if rate else amount,
                'lodge_assisted': False,
            }
            if rate:
                line_vals['rate_id'] = rate.id

            payment_lines.append((0, 0, line_vals))
            total += amount

        if not payment_lines:
            return None

        # Determine payment type from the primary dues line
        payment_type = 'custom'

        # Create the dues payment
        payment_vals = {
            'partner_id': partner.id,
            'payment_type': payment_type,
            'payment_date': tran_date,
            'check_number': check_num or False,
            'line_ids': payment_lines,
            'clms_status': 'processed',
            'clms_processed_date': fields.Date.today(),
        }

        # Set primary rate if we found a dues rate
        for row in rows:
            credit_code = (row.get('TranCreditAccount') or '').strip()
            if credit_code.startswith('3010'):
                rate = self._find_rate(
                    credit_code,
                    (row.get('TranTranCode') or '').strip(),
                    (row.get('TranCodeDescription') or '').strip(),
                    rate_by_credit,
                )
                if rate:
                    payment_vals['rate_id'] = rate.id
                    break

        payment = DuesPayment.create(payment_vals)

        # Update paid-to date before posting so the snapshot works
        if self.update_paid_to and paid_to_date:
            current_ptd = partner.x_detail_dues_paid_to_date
            if not current_ptd or paid_to_date > current_ptd:
                partner.write({
                    'x_detail_dues_paid_to_date': paid_to_date,
                })

        # Post the payment (creates journal entry)
        try:
            payment.action_post()
        except Exception as e:
            _logger.warning(
                "CLMS payment import: could not auto-post payment for "
                "%s (%s): %s — leaving as draft",
                member_name, member_num, e,
            )

        return {
            'name': f"{member_name} ({member_num}) — ${total:,.2f}",
            'amount': total,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _find_rate(self, credit_code, tran_code, description, rate_by_credit):
        """Find the best-matching dues rate for a CSV line."""
        candidates = rate_by_credit.get(credit_code, [])
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]

        # Multiple rates share this credit code — try to match by
        # the short tran code in the rate name (e.g. "[5F]")
        if tran_code:
            bracket = f"[{tran_code}]"
            for r in candidates:
                if bracket in (r.name or ''):
                    return r

        # Fallback: match by description keywords
        desc_lower = (description or '').lower()
        for r in candidates:
            rate_name = (r.name or '').lower()
            # Check if key words match
            if 'per capita' in desc_lower and 'per capita' in rate_name:
                return r
            if 'insurance' in desc_lower and 'insurance' in rate_name:
                return r
            if 'magazine' in desc_lower and 'magazine' in rate_name:
                return r
            if 'state fee' in desc_lower and 'state fee' in rate_name:
                return r

        # Last resort: return first
        return candidates[0]

    @staticmethod
    def _parse_date(date_str):
        """Parse various date formats from CLMS CSV."""
        if not date_str:
            return False
        import datetime
        # Try ISO format first (2026-04-22)
        for fmt in ('%Y-%m-%d', '%m/%d/%Y', '%m/%d/%Y %I:%M:%S %p',
                    '%m/%d/%Y %H:%M:%S', '%Y-%m-%dT%H:%M:%S'):
            try:
                return datetime.datetime.strptime(date_str, fmt).date()
            except ValueError:
                continue
        return False
