# -*- coding: utf-8 -*-
"""Extend CLMS import to auto-create dues income journal entries.

When the monthly CLMS CSV import advances a member's dues-paid-to date,
this extension automatically creates an ``ElksDuesPayment`` record with
proper GL coding and posts it.

Member type determines the rate:
- **Regular** members: full annual rate, prorated by months advanced
- **Life** members: 50 % of the base rate (lodge covers the other half)
- **Associate** members: associate rate
- **Lodge assist**: the lodge pays from its own funds (debits Lodge
  Assistance expense instead of Operating Checking)
"""
import datetime
import logging

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)


class ClmsImportWizardDues(models.TransientModel):
    """Inherit the CLMS import wizard to create dues payments when the
    dues-paid-to date advances during an import."""

    _inherit = "clms.import.wizard"

    auto_create_dues = fields.Boolean(
        "Auto-Create Dues Income Records", default=False,
        help="When checked, a dues payment and journal entry will be "
             "created for every member whose dues-paid-to date advances "
             "during this import. NOTE: For budget income tracking, use "
             "the 'Update Dues Income' button on the Budget form instead.",
    )
    backfill_existing = fields.Boolean(
        "Backfill Existing Paid Members", default=False,
        help="Create dues income records for members who already have a "
             "dues-paid-to date but no dues payment on file. Use this "
             "after a first import to catch up.",
    )
    dues_payment_date = fields.Date(
        "Payment Date for Auto-Created Records",
        default=fields.Date.context_today,
        help="The date to use on the auto-created dues payment records. "
             "Defaults to today.",
    )

    # ------------------------------------------------------------------
    # Override the import to wrap with dues tracking
    # ------------------------------------------------------------------
    def _import_clms(self, content):
        """Extend to snapshot dues dates before import, then create
        payment records for any members whose dates advanced."""

        # ---- Step 1: snapshot current dues-paid-to dates ----
        snapshots = {}
        if self.auto_create_dues:
            partners = self.env['res.partner'].with_context(
                active_test=False,
            ).search([
                ('x_detail_member_num', '!=', False),
                ('x_is_not_member', '=', False),
            ])
            for p in partners:
                num = (p.x_detail_member_num or '').strip()
                if num:
                    snapshots[num] = {
                        'partner_id': p.id,
                        'old_date': p.x_detail_dues_paid_to_date,
                        'elk_title': p.x_detail_elk_title or '',
                        'life_date': p.x_last_life_date,
                        'hon_life_date': p.x_last_hon_life_date,
                    }

        # ---- Step 2: run the base import ----
        result = super()._import_clms(content)

        # ---- Step 3: detect date changes and create payments ----
        if self.auto_create_dues and snapshots:
            dues_result = self._create_dues_from_import(snapshots)
            if dues_result:
                result += "\n\n" + dues_result

        # ---- Step 4: backfill paid members with no dues payment ----
        if self.backfill_existing:
            backfill_result = self._backfill_paid_members()
            if backfill_result:
                result += "\n\n" + backfill_result

        return result

    # ------------------------------------------------------------------
    # Dues payment creation
    # ------------------------------------------------------------------
    def _create_dues_from_import(self, snapshots):
        """Compare current dues dates with snapshots and create payments."""
        Partner = self.env['res.partner']
        DuesPayment = self.env['elks.dues.payment']
        DuesRate = self.env['elks.dues.rate']

        # Reload partners to get updated dates
        partner_ids = [s['partner_id'] for s in snapshots.values()]
        partners = Partner.browse(partner_ids)
        partner_map = {
            (p.x_detail_member_num or '').strip(): p
            for p in partners if p.x_detail_member_num
        }

        # Pre-load rates by member type
        rate_cache = {
            'regular': DuesRate.search([
                ('is_dues', '=', True),
                ('applies_to', 'in', ('regular', 'all')),
                ('months_covered', '=', 12),
                ('include_in_one_year', '=', True),
            ], limit=1),
            'life': DuesRate.search([
                ('is_dues', '=', True),
                ('applies_to', '=', 'life'),
                ('months_covered', '=', 12),
            ], limit=1),
            'associate': DuesRate.search([
                ('is_dues', '=', True),
                ('applies_to', '=', 'associate'),
                ('months_covered', '=', 12),
            ], limit=1),
        }
        # Fallback: if no specific life/associate rate, use regular
        if not rate_cache['life']:
            rate_cache['life'] = rate_cache['regular']
        if not rate_cache['associate']:
            rate_cache['associate'] = rate_cache['regular']

        # Also load fee rates (per capita, insurance, etc.)
        fee_rates = DuesRate.search([
            ('is_dues', '=', False),
            ('months_covered', '=', 0),
            ('include_in_one_year', '=', True),
        ])

        created_count = 0
        first_import_count = 0
        total_amount = 0.0
        errors = []

        payment_date = self.dues_payment_date or fields.Date.today()

        for member_num, snap in snapshots.items():
            partner = partner_map.get(member_num)
            if not partner:
                continue

            old_date = snap['old_date']
            new_date = partner.x_detail_dues_paid_to_date

            # Skip if no paid-to date after import
            if not new_date:
                continue

            if old_date:
                # Ongoing import — skip if date didn't advance
                if new_date <= old_date:
                    continue
                months = self._months_between(old_date, new_date)
                if months <= 0:
                    continue
            else:
                # First import — member is paid, treat as full year
                months = 12
                first_import_count += 1

            # Determine member type
            member_type = self._get_member_type_from_partner(partner)
            rate = rate_cache.get(member_type, rate_cache['regular'])
            if not rate:
                errors.append(
                    f"{partner.name} ({member_num}): no dues rate found "
                    f"for type '{member_type}'"
                )
                continue

            # Calculate amount — prorate if not a full year
            annual_amount = rate.amount
            if member_type == 'life':
                annual_amount = annual_amount * 0.5

            if months >= 12:
                dues_amount = annual_amount
            else:
                # Prorate: monthly rate × months
                dues_amount = round((annual_amount / 12) * months, 2)

            # Build payment lines
            lines = [(0, 0, {
                'rate_id': rate.id,
                'description': (
                    f"CLMS Import: {rate.name} "
                    f"({months} mo, {old_date} → {new_date})"
                ),
                'amount_paid': dues_amount,
                'default_amount': rate.amount,
                'lodge_assisted': False,
            })]

            # Add fee lines for full-year payments
            if months >= 12:
                for fee in fee_rates:
                    # Filter fees by member type
                    if fee.applies_to and fee.applies_to != 'all':
                        if fee.applies_to != member_type:
                            continue
                    fee_amount = fee.amount
                    if fee_amount <= 0:
                        continue
                    lines.append((0, 0, {
                        'rate_id': fee.id,
                        'description': f"CLMS Import: {fee.name}",
                        'amount_paid': fee_amount,
                        'default_amount': fee.amount,
                        'lodge_assisted': False,
                    }))

            try:
                payment = DuesPayment.create({
                    'partner_id': partner.id,
                    'payment_type': 'custom',
                    'payment_date': payment_date,
                    'line_ids': lines,
                    'rate_id': rate.id,
                    'clms_status': 'processed',
                    'clms_processed_date': fields.Date.today(),
                })
                payment.action_post()
                created_count += 1
                total_amount += payment.amount_total
            except Exception as e:
                errors.append(
                    f"{partner.name} ({member_num}): {e}"
                )

        # Build summary
        parts = [
            "--- DUES INCOME RECORDS ---",
            f"Created {created_count} dues payment(s), "
            f"total ${total_amount:,.2f}",
        ]
        if first_import_count:
            parts.append(
                f"  ({first_import_count} from first import — "
                f"full year assumed)"
            )
        if errors:
            parts.append(f"Errors ({len(errors)}):")
            parts.extend(f"  {e}" for e in errors[:20])
            if len(errors) > 20:
                parts.append(f"  ... and {len(errors) - 20} more")

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Backfill
    # ------------------------------------------------------------------
    def _backfill_paid_members(self):
        """Create dues income for paid members who have no dues payment.

        This catches members imported before auto-dues was enabled, or
        members from a first import that was run without this feature.
        """
        Partner = self.env['res.partner']
        DuesPayment = self.env['elks.dues.payment']
        DuesRate = self.env['elks.dues.rate']

        # Find members with a dues-paid-to date
        paid_members = Partner.search([
            ('x_is_member', '=', True),
            ('x_detail_dues_paid_to_date', '!=', False),
        ])

        # Find which ones already have a dues payment
        existing_partner_ids = set()
        if paid_members:
            existing = DuesPayment.search([
                ('partner_id', 'in', paid_members.ids),
                ('state', 'in', ('posted', 'draft')),
            ])
            existing_partner_ids = {p.partner_id.id for p in existing}

        # Filter to members with no payment
        needs_backfill = paid_members.filtered(
            lambda p: p.id not in existing_partner_ids
        )

        if not needs_backfill:
            return "--- BACKFILL ---\nNo members need backfill."

        # Load rates
        rate_cache = {
            'regular': DuesRate.search([
                ('is_dues', '=', True),
                ('applies_to', 'in', ('regular', 'all')),
                ('months_covered', '=', 12),
                ('include_in_one_year', '=', True),
            ], limit=1),
            'life': DuesRate.search([
                ('is_dues', '=', True),
                ('applies_to', '=', 'life'),
                ('months_covered', '=', 12),
            ], limit=1),
            'associate': DuesRate.search([
                ('is_dues', '=', True),
                ('applies_to', '=', 'associate'),
                ('months_covered', '=', 12),
            ], limit=1),
        }
        if not rate_cache['life']:
            rate_cache['life'] = rate_cache['regular']
        if not rate_cache['associate']:
            rate_cache['associate'] = rate_cache['regular']

        fee_rates = DuesRate.search([
            ('is_dues', '=', False),
            ('months_covered', '=', 0),
            ('include_in_one_year', '=', True),
        ])

        payment_date = self.dues_payment_date or fields.Date.today()
        created = 0
        total = 0.0
        errors = []

        for partner in needs_backfill:
            member_type = self._get_member_type_from_partner(partner)
            rate = rate_cache.get(member_type, rate_cache['regular'])
            if not rate:
                continue

            annual_amount = rate.amount
            if member_type == 'life':
                annual_amount = annual_amount * 0.5

            lines = [(0, 0, {
                'rate_id': rate.id,
                'description': f"Backfill: {rate.name} (full year)",
                'amount_paid': annual_amount,
                'default_amount': rate.amount,
                'lodge_assisted': False,
            })]

            # Add fee lines
            for fee in fee_rates:
                if fee.applies_to and fee.applies_to != 'all':
                    if fee.applies_to != member_type:
                        continue
                if fee.amount <= 0:
                    continue
                lines.append((0, 0, {
                    'rate_id': fee.id,
                    'description': f"Backfill: {fee.name}",
                    'amount_paid': fee.amount,
                    'default_amount': fee.amount,
                    'lodge_assisted': False,
                }))

            try:
                payment = DuesPayment.create({
                    'partner_id': partner.id,
                    'payment_type': 'custom',
                    'payment_date': payment_date,
                    'line_ids': lines,
                    'rate_id': rate.id,
                    'clms_status': 'processed',
                    'clms_processed_date': fields.Date.today(),
                })
                payment.action_post()
                created += 1
                total += payment.amount_total
            except Exception as e:
                member_num = (partner.x_detail_member_num or '').strip()
                errors.append(f"{partner.name} ({member_num}): {e}")

        parts = [
            "--- BACKFILL DUES INCOME ---",
            f"Created {created} payment(s) for members with no prior "
            f"dues record, total ${total:,.2f}",
        ]
        if errors:
            parts.append(f"Errors ({len(errors)}):")
            parts.extend(f"  {e}" for e in errors[:20])

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _months_between(d1, d2):
        """Return the number of whole months between two dates."""
        if not d1 or not d2:
            return 0
        delta = relativedelta(d2, d1)
        return delta.years * 12 + delta.months

    @staticmethod
    def _get_member_type_from_partner(partner):
        """Determine member type from a partner record."""
        title = (partner.x_detail_elk_title or '').lower()
        if 'life' in title or 'honorary' in title:
            return 'life'
        if 'associate' in title:
            return 'associate'
        if partner.x_last_life_date or partner.x_last_hon_life_date:
            return 'life'
        return 'regular'
