# -*- coding: utf-8 -*-
"""Extend res.partner with dues payment history and auto-budget tracking.

When a member's ``x_is_dues_paid`` status becomes True (whether from a
CLMS import, manual edit, dues payment, or the daily cron), this module
automatically creates a journal entry crediting the appropriate dues
income accounts and marks the member as counted for the current lodge
year so they are never double-counted.

Trigger points:
- ``write`` on ``x_detail_dues_paid_to_date`` (CLMS import, manual edit)
- ``write`` on ``x_is_dues_paid`` (daily cron sets it explicitly)
- ``create`` (new members from CLMS import with a paid-to date)
"""
import datetime
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


def _current_lodge_year(today=None):
    """Return the current lodge year string (e.g. '2025-2026').

    Lodge year runs April 1 – March 31.
    """
    if today is None:
        today = datetime.date.today()
    if today.month >= 4:
        return f"{today.year}-{today.year + 1}"
    else:
        return f"{today.year - 1}-{today.year}"


class ResPartnerFRS(models.Model):
    _inherit = "res.partner"

    x_dues_payment_ids = fields.One2many(
        'elks.dues.payment', 'partner_id',
        string='Dues Payments',
    )
    x_dues_budget_year = fields.Char(
        "Dues Counted for Budget Year",
        index=True,
        help="Lodge year (e.g. '2025-2026') for which this member's dues "
             "have already been counted toward the budget income actual. "
             "Prevents double-counting.",
    )

    # Fields that signal a possible dues status change
    _DUES_TRIGGER_FIELDS = {
        'x_detail_dues_paid_to_date',  # source date field
        'x_is_dues_paid',              # daily cron writes this directly
    }

    # Fields that affect member type classification (Regular/Life/Associate)
    _TYPE_TRIGGER_FIELDS = {
        'x_detail_elk_title',
        'x_last_life_date',
        'x_last_hon_life_date',
    }

    # ------------------------------------------------------------------
    # write override — detect dues status changes & member type changes
    # ------------------------------------------------------------------
    def write(self, vals):
        """Detect dues status or member type changes and auto-update."""
        val_keys = set(vals.keys())
        dues_triggers = self._DUES_TRIGGER_FIELDS & val_keys
        type_triggers = self._TYPE_TRIGGER_FIELDS & val_keys

        if not dues_triggers and not type_triggers:
            return super().write(vals)

        # --- Snapshot BEFORE the write ---
        was_not_paid_ids = set()
        old_types = {}

        if dues_triggers:
            was_not_paid_ids = set(
                self.filtered(lambda p: not p.x_is_dues_paid).ids
            )

        if type_triggers:
            # Record each member's type before the change
            for p in self:
                old_types[p.id] = self._get_member_type_for_dues(p)

        result = super().write(vals)

        # --- After write + recompute ---

        # 1) Newly paid → auto-count for budget
        if was_not_paid_ids:
            newly_paid = self.filtered(
                lambda p: p.id in was_not_paid_ids
                and p.x_is_dues_paid
                and p.x_is_member
            )
            if newly_paid:
                newly_paid._auto_count_dues_for_budget()

        # 2) Member type changed → adjust existing payment
        if old_types:
            for p in self:
                if p.id in old_types:
                    new_type = self._get_member_type_for_dues(p)
                    if new_type != old_types[p.id] and p.x_is_member:
                        p._handle_member_type_change(
                            old_types[p.id], new_type,
                        )

        return result

    # ------------------------------------------------------------------
    # create override — handle new members with a paid-to date
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)

        # Check if any new records are paid members
        newly_paid = records.filtered(
            lambda p: p.x_is_dues_paid
            and p.x_is_member
            and not p.x_dues_budget_year
        )
        if newly_paid:
            newly_paid._auto_count_dues_for_budget()

        return records

    # ------------------------------------------------------------------
    # Core: auto-count dues for budget
    # ------------------------------------------------------------------
    def _auto_count_dues_for_budget(self):
        """Create a journal entry for newly-paid members' dues income.

        Called automatically when x_is_dues_paid becomes True.  Only counts
        members not already counted for the current lodge year.  Creates
        one batch journal entry for all members in the recordset.
        """
        today = fields.Date.context_today(self)
        lodge_year = _current_lodge_year(today)

        # Filter to uncounted members
        uncounted = self.filtered(
            lambda p: p.x_dues_budget_year != lodge_year
        )
        if not uncounted:
            return

        Account = self.env['elks.account']
        JournalEntry = self.env['elks.journal.entry']
        DuesRate = self.env['elks.dues.rate']
        Budget = self.env['elks.budget']

        acct_10100 = Account.search([('code', '=', '10100')], limit=1)
        if not acct_10100:
            _logger.warning(
                "Auto-dues: Operating Checking (10100) not found. Skipping."
            )
            return

        # Load one-year rate bundle
        all_bundle_rates = DuesRate.search([
            ('include_in_one_year', '=', True),
            ('active', '=', True),
        ])
        if not all_bundle_rates:
            _logger.warning(
                "Auto-dues: No rates with include_in_one_year=True. Skipping."
            )
            return

        # Classify members
        members_by_type = {
            'regular': self.env['res.partner'],
            'life': self.env['res.partner'],
            'associate': self.env['res.partner'],
        }
        for partner in uncounted:
            mtype = self._get_member_type_for_dues(partner)
            members_by_type[mtype] |= partner

        # Build credits by account
        credits_map = {}  # {account_id: {'amount': float, 'parts': []}}
        grand_total = 0.0
        summary_parts = []

        for mtype, members in members_by_type.items():
            if not members:
                continue

            rates = all_bundle_rates.filtered(
                lambda r: r.applies_to in (mtype, 'all')
            )
            count = len(members)
            type_total = 0.0

            for rate in rates:
                if rate.amount <= 0:
                    continue

                acct = self._resolve_dues_credit_account(rate, Account)
                if not acct:
                    continue

                line_total = round(rate.amount * count, 2)
                type_total += line_total

                if acct.id not in credits_map:
                    credits_map[acct.id] = {'amount': 0.0, 'parts': []}
                credits_map[acct.id]['amount'] += line_total
                credits_map[acct.id]['parts'].append(
                    f"{rate.name}: {count} × ${rate.amount:,.2f}"
                )

            grand_total += type_total
            if type_total > 0:
                summary_parts.append(
                    f"{count} {mtype} = ${type_total:,.2f}"
                )

        if grand_total <= 0 or not credits_map:
            _logger.info(
                "Auto-dues: $0 total for %d members. No JE created.",
                len(uncounted),
            )
            return

        # Build journal entry
        je_lines = []
        for acct_id, info in credits_map.items():
            je_lines.append((0, 0, {
                'account_id': acct_id,
                'debit': 0.0,
                'credit': round(info['amount'], 2),
                'memo': '; '.join(info['parts']),
            }))

        je_lines.append((0, 0, {
            'account_id': acct_10100.id,
            'debit': round(grand_total, 2),
            'credit': 0.0,
            'memo': f"Dues income — {lodge_year}",
        }))

        try:
            entry = JournalEntry.create({
                'date': today,
                'memo': (
                    f"Auto dues income — {lodge_year} "
                    f"({'; '.join(summary_parts)})"
                ),
                'line_ids': je_lines,
            })
            entry.action_post()

            # Mark members as counted
            uncounted.write({'x_dues_budget_year': lodge_year})

            _logger.info(
                "Auto-dues: Created JE %s for %d members, $%.2f (%s)",
                entry.entry_number, len(uncounted), grand_total,
                ', '.join(summary_parts),
            )

            # Post to budget chatter if budget exists
            budget = Budget.search([
                ('lodge_year', '=', lodge_year),
            ], limit=1)
            if budget:
                body = (
                    f"<strong>Dues Income Auto-Updated</strong><br/>"
                    f"New members counted: {len(uncounted)}<br/>"
                    f"{'<br/>'.join(summary_parts)}<br/>"
                    f"<strong>Total: ${grand_total:,.2f}</strong><br/>"
                    f"Journal Entry: {entry.entry_number}"
                )
                budget.message_post(
                    body=body,
                    message_type='comment',
                    subtype_xmlid='mail.mt_note',
                )

        except Exception:
            _logger.exception(
                "Auto-dues: Failed to create JE for %d members.",
                len(uncounted),
            )

    @staticmethod
    def _get_member_type_for_dues(partner):
        """Determine member type for dues: regular, life, or associate."""
        title = (partner.x_detail_elk_title or '').lower()
        if 'life' in title or 'honorary' in title:
            return 'life'
        if 'associate' in title:
            return 'associate'
        if partner.x_last_life_date or partner.x_last_hon_life_date:
            return 'life'
        return 'regular'

    @staticmethod
    def _resolve_dues_credit_account(rate, Account):
        """Resolve a rate's credit account — linked record or by code."""
        acct = rate.credit_account_id
        if acct:
            return acct
        code = rate.credit_account_code
        if not code:
            return None
        acct = Account.search([('code', '=', code)], limit=1)
        if not acct:
            base = code[:5] if len(code) > 5 else code
            acct = Account.search([('code', '=', base)], limit=1)
        if acct:
            rate.sudo().write({'credit_account_id': acct.id})
        return acct

    # ------------------------------------------------------------------
    # Member type change → adjust existing dues payment
    # ------------------------------------------------------------------
    def _handle_member_type_change(self, old_type, new_type):
        """Cancel and recreate dues payment when member type changes.

        If a member already has a posted or draft dues payment for the
        current lodge year (e.g. as a Regular), and is then reclassified
        (e.g. to Life), this method cancels the old payment and creates
        a new one with the correct rate bundle for the new type.
        """
        self.ensure_one()
        today = fields.Date.context_today(self)
        lodge_year = _current_lodge_year(today)

        DuesPayment = self.env['elks.dues.payment']
        DuesRate = self.env['elks.dues.rate']

        # Find the member's current-year payment
        existing = DuesPayment.search([
            ('partner_id', '=', self.id),
            ('lodge_year', '=', lodge_year),
            ('state', 'in', ('draft', 'posted')),
        ], limit=1, order='id desc')

        if not existing:
            _logger.info(
                "Type change %s → %s for %s: no existing payment to adjust.",
                old_type, new_type, self.name,
            )
            return

        # Load new rate bundle
        new_rates = DuesRate.search([
            ('include_in_one_year', '=', True),
            ('active', '=', True),
            '|',
            ('applies_to', '=', new_type),
            ('applies_to', '=', 'all'),
        ])
        if not new_rates:
            _logger.warning(
                "Type change %s → %s for %s: no bundle rates for '%s'.",
                old_type, new_type, self.name, new_type,
            )
            return

        try:
            payment_date = existing.payment_date

            # Cancel old payment (reverses JE + dues-paid-to date)
            if existing.state == 'posted':
                existing.action_cancel()
            else:
                existing.state = 'cancelled'

            # Build new payment lines
            primary_rate = new_rates.filtered('is_dues')[:1]
            lines = []
            seq = 10
            for rate in new_rates:
                if rate.amount <= 0:
                    continue
                lines.append((0, 0, {
                    'sequence': seq,
                    'rate_id': rate.id,
                    'description': rate.name,
                    'default_amount': rate.amount,
                    'amount_paid': rate.amount,
                    'lodge_assisted': False,
                }))
                seq += 10

            if not lines:
                _logger.warning(
                    "Type change for %s: all %s rates are $0. "
                    "No replacement payment created.",
                    self.name, new_type,
                )
                return

            ptype = 'one_year_life' if new_type == 'life' else 'one_year'
            new_payment = DuesPayment.create({
                'partner_id': self.id,
                'payment_type': ptype,
                'payment_date': payment_date,
                'rate_id': primary_rate.id if primary_rate else False,
                'line_ids': lines,
            })
            new_payment.action_post()

            # Mark budget year tracking
            if self.x_dues_budget_year != lodge_year:
                self.write({'x_dues_budget_year': lodge_year})

            _logger.info(
                "Type change %s → %s for %s: cancelled payment %s, "
                "created %s ($%.2f).",
                old_type, new_type, self.name,
                existing.name, new_payment.name,
                new_payment.amount_total,
            )

            # Post a chatter note on the member
            self.message_post(
                body=(
                    f"<strong>Member Type Changed: "
                    f"{old_type.title()} → {new_type.title()}</strong><br/>"
                    f"Dues payment {existing.name} cancelled and replaced "
                    f"with {new_payment.name} "
                    f"(${new_payment.amount_total:,.2f})."
                ),
                message_type='comment',
                subtype_xmlid='mail.mt_note',
            )

        except Exception:
            _logger.exception(
                "Type change %s → %s for %s: failed to adjust payment.",
                old_type, new_type, self.name,
            )

    # ------------------------------------------------------------------
    # Pay Dues action (launched from contact form)
    # ------------------------------------------------------------------
    def action_pay_dues(self):
        """Open a new Dues Payment form pre-filled for this member."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Pay Dues',
            'res_model': 'elks.dues.payment',
            'view_mode': 'form',
            'target': 'current',
            'context': {
                'default_partner_id': self.id,
            },
        }
