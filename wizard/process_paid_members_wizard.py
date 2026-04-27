# -*- coding: utf-8 -*-
"""Wizard to bulk-create dues payment records for all paid members.

Finds members with x_is_dues_paid=True who don't yet have a posted
ElksDuesPayment for the current lodge year, then creates and posts
a payment record for each one with the full rate bundle (dues + per
capita + insurance + magazine + state fees).
"""
import datetime
import logging

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


def _current_lodge_year(today=None):
    if today is None:
        today = datetime.date.today()
    if today.month >= 4:
        return f"{today.year}-{today.year + 1}"
    return f"{today.year - 1}-{today.year}"


class ProcessPaidMembersWizard(models.TransientModel):
    _name = "process.paid.members.wizard"
    _description = "Process Paid Members — Bulk Create Dues Payments"

    state = fields.Selection([
        ('setup', 'Setup'),
        ('done', 'Done'),
    ], default='setup')

    payment_date = fields.Date(
        "Payment Date",
        default=fields.Date.context_today,
        help="Date to use on the created payment records.",
    )
    include_life = fields.Boolean("Include Life Members", default=True)
    include_regular = fields.Boolean("Include Regular Members", default=True)
    include_associate = fields.Boolean("Include Associate Members", default=True)

    # Preview counts (computed on form load)
    preview_regular = fields.Integer(
        "Regular Members to Process",
        compute="_compute_preview", store=False,
    )
    preview_life = fields.Integer(
        "Life Members to Process",
        compute="_compute_preview", store=False,
    )
    preview_associate = fields.Integer(
        "Associate Members to Process",
        compute="_compute_preview", store=False,
    )
    preview_total = fields.Integer(
        "Total to Process",
        compute="_compute_preview", store=False,
    )
    preview_already = fields.Integer(
        "Already Have Payment",
        compute="_compute_preview", store=False,
    )

    result_message = fields.Text("Results", readonly=True)

    @api.depends('include_life', 'include_regular', 'include_associate')
    def _compute_preview(self):
        for wiz in self:
            counts = wiz._get_member_counts()
            wiz.preview_regular = counts['regular'] if wiz.include_regular else 0
            wiz.preview_life = counts['life'] if wiz.include_life else 0
            wiz.preview_associate = counts['associate'] if wiz.include_associate else 0
            wiz.preview_total = wiz.preview_regular + wiz.preview_life + wiz.preview_associate
            wiz.preview_already = counts['already']

    def _get_member_counts(self):
        """Count paid members by type that need payment records."""
        Partner = self.env['res.partner']
        DuesPayment = self.env['elks.dues.payment']
        today = fields.Date.context_today(self)
        lodge_year = _current_lodge_year(today)

        # All paid members
        paid_members = Partner.search([
            ('x_is_member', '=', True),
            ('x_is_dues_paid', '=', True),
        ])

        # Members who already have a posted payment this lodge year
        existing = DuesPayment.search([
            ('partner_id', 'in', paid_members.ids),
            ('state', 'in', ('posted', 'draft')),
            ('lodge_year', '=', lodge_year),
        ])
        has_payment_ids = set(existing.mapped('partner_id').ids)

        # Filter to those without a payment
        needs_payment = paid_members.filtered(
            lambda p: p.id not in has_payment_ids
        )

        counts = {'regular': 0, 'life': 0, 'associate': 0,
                  'already': len(has_payment_ids)}
        for p in needs_payment:
            mtype = self._get_member_type(p)
            counts[mtype] = counts.get(mtype, 0) + 1

        return counts

    def action_process(self):
        """Create dues payment records for all qualifying paid members."""
        self.ensure_one()

        Partner = self.env['res.partner']
        DuesPayment = self.env['elks.dues.payment']
        DuesRate = self.env['elks.dues.rate']

        today = fields.Date.context_today(self)
        lodge_year = _current_lodge_year(today)
        payment_date = self.payment_date or today

        # Find paid members
        paid_members = Partner.search([
            ('x_is_member', '=', True),
            ('x_is_dues_paid', '=', True),
        ])

        # Exclude members who already have a payment this lodge year
        existing = DuesPayment.search([
            ('partner_id', 'in', paid_members.ids),
            ('state', 'in', ('posted', 'draft')),
            ('lodge_year', '=', lodge_year),
        ])
        has_payment_ids = set(existing.mapped('partner_id').ids)
        needs_payment = paid_members.filtered(
            lambda p: p.id not in has_payment_ids
        )

        if not needs_payment:
            raise UserError(_(
                "All paid members already have a dues payment record "
                "for lodge year %s."
            ) % lodge_year)

        # Load one-year rate bundle
        all_bundle_rates = DuesRate.search([
            ('include_in_one_year', '=', True),
            ('active', '=', True),
        ])
        if not all_bundle_rates:
            raise UserError(_(
                "No rates have the 'Include in 1-Year Payment' flag set. "
                "Configure rates under Dues & Payments → Lodge Rates."
            ))

        created = 0
        posted = 0
        skipped_type = 0
        errors = []

        for partner in needs_payment:
            mtype = self._get_member_type(partner)

            # Check if user wants to process this type
            if mtype == 'regular' and not self.include_regular:
                skipped_type += 1
                continue
            if mtype == 'life' and not self.include_life:
                skipped_type += 1
                continue
            if mtype == 'associate' and not self.include_associate:
                skipped_type += 1
                continue

            # Get rates for this member type
            rates = all_bundle_rates.filtered(
                lambda r: r.applies_to in (mtype, 'all')
            )
            if not rates:
                errors.append(
                    f"{partner.name}: no rates for type '{mtype}'"
                )
                continue

            # Find the primary dues rate
            primary_rate = rates.filtered('is_dues')[:1]

            # Build payment lines
            lines = []
            seq = 10
            for rate in rates:
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
                errors.append(
                    f"{partner.name}: all rates have $0 amount"
                )
                continue

            try:
                ptype = 'one_year_life' if mtype == 'life' else 'one_year'
                payment = DuesPayment.create({
                    'partner_id': partner.id,
                    'payment_type': ptype,
                    'payment_date': payment_date,
                    'rate_id': primary_rate.id if primary_rate else False,
                    'line_ids': lines,
                })
                created += 1

                payment.action_post()
                posted += 1

                # Mark as counted for budget
                if partner.x_dues_budget_year != lodge_year:
                    partner.write({'x_dues_budget_year': lodge_year})

            except Exception as e:
                member_num = (partner.x_detail_member_num or '').strip()
                errors.append(f"{partner.name} ({member_num}): {e}")

        # Build result message
        parts = [
            f"PROCESS PAID MEMBERS — {lodge_year}",
            f"Created: {created} payment records",
            f"Posted: {posted} (with journal entries)",
        ]
        if skipped_type:
            parts.append(f"Skipped (type filter): {skipped_type}")
        if has_payment_ids:
            parts.append(
                f"Already had payment: {len(has_payment_ids)}"
            )
        if errors:
            parts.append(f"\nErrors ({len(errors)}):")
            parts.extend(f"  {e}" for e in errors[:30])
            if len(errors) > 30:
                parts.append(f"  ... and {len(errors) - 30} more")

        self.write({
            'state': 'done',
            'result_message': "\n".join(parts),
        })

        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

    @staticmethod
    def _get_member_type(partner):
        title = (partner.x_detail_elk_title or '').lower()
        if 'life' in title or 'honorary' in title:
            return 'life'
        if 'associate' in title:
            return 'associate'
        if partner.x_last_life_date or partner.x_last_hon_life_date:
            return 'life'
        return 'regular'
