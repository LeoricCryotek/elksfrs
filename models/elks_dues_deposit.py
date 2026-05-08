# -*- coding: utf-8 -*-
"""Elks Daily Batch, Bank Deposit, and Cash On Hand models.

Daily Batch
-----------
Collects all dues payments received during a single business day.
Replaces the former "Dues Deposit" concept.  A batch is *closed* at end
of day (locking it from new payments), then later linked to a Bank
Deposit when physically taken to the bank.

Bank Deposit
------------
Groups one or more closed Daily Batches into a single trip to the bank.
Requires Treasurer sign-off and a "received by" signature before the
linked batches are marked as deposited and permanently locked.

Cash On Hand
------------
Semi-annual (or ad-hoc) count of physical cash and checks held at the
lodge.  Compares the counted amount to the expected amount (sum of all
undeposited batch totals).  Requires an explanation note when there is a
variance, and creates an adjustment record for reconciliation.
"""
import logging

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


# =====================================================================
# Daily Batch  (replaces elks.dues.deposit)
# =====================================================================

class ElksDailyBatch(models.Model):
    """Daily collection batch for dues payments and other receipts.

    Workflow:  Draft → Closed → Deposited
      - Draft:     payments can be added / removed
      - Closed:    batch is locked, no more payments can come in
      - Deposited: batch has been included in a Bank Deposit and
                   physically taken to the bank
    """
    _name = "elks.dues.deposit"          # keep table name for migration
    _description = "Daily Batch"
    _order = "batch_date desc, id desc"
    _inherit = ["mail.thread"]

    name = fields.Char(
        "Batch Reference", compute="_compute_name", store=True,
    )
    batch_date = fields.Date(
        "Batch Date", required=True,
        default=fields.Date.context_today, index=True, tracking=True,
        help="The business day this batch covers.",
    )
    prepared_by = fields.Many2one(
        "res.users", string="Prepared By",
        default=lambda self: self.env.user, tracking=True,
    )
    state = fields.Selection([
        ('draft', 'Open'),
        ('closed', 'Closed'),
        ('deposited', 'Deposited'),
        ('cancelled', 'Cancelled'),
    ], default='draft', tracking=True, index=True,
       help="Open: accepting payments. "
            "Closed: batch locked, awaiting bank deposit. "
            "Deposited: included in a Bank Deposit.",
    )

    # Payments
    payment_ids = fields.One2many(
        "elks.dues.payment", "deposit_id", string="Payments",
    )
    payment_count = fields.Integer(
        "# Payments", compute="_compute_totals", store=True,
    )
    expected_total = fields.Monetary(
        "Batch Total", compute="_compute_totals", store=True,
        currency_field='currency_id',
        help="Sum of all payments in this batch.",
    )
    currency_id = fields.Many2one(
        "res.currency", default=lambda self: self.env.company.currency_id,
    )

    # Bank Deposit link
    bank_deposit_id = fields.Many2one(
        "elks.bank.deposit", string="Bank Deposit",
        ondelete="set null", index=True, readonly=True,
        help="The Bank Deposit this batch was included in.",
    )

    note = fields.Text("Notes")

    # Backward compatibility — keep old field as alias
    deposit_date = fields.Date(
        related="batch_date", string="Deposit Date", store=True,
    )
    bank_reference = fields.Char(
        related="bank_deposit_id.bank_reference",
        string="Bank Deposit #", store=True, readonly=True,
    )

    # -----------------------------------------------------------------
    # Computed
    # -----------------------------------------------------------------
    @api.depends("batch_date")
    def _compute_name(self):
        for rec in self:
            if rec.batch_date:
                rec.name = f"Daily Batch {rec.batch_date}"
            else:
                rec.name = "New Daily Batch"

    @api.depends("payment_ids.amount_total")
    def _compute_totals(self):
        for rec in self:
            rec.payment_count = len(rec.payment_ids)
            rec.expected_total = sum(rec.payment_ids.mapped('amount_total'))

    # -----------------------------------------------------------------
    # Constraints
    # -----------------------------------------------------------------
    @api.constrains('payment_ids')
    def _check_batch_not_locked(self):
        """Prevent adding payments to a closed or deposited batch."""
        for rec in self:
            if rec.state in ('closed', 'deposited') and rec.payment_ids:
                # Only raise if a NEW payment was added (check in write)
                pass  # Enforced in write() override instead

    def write(self, vals):
        """Block adding payments when batch is closed or deposited."""
        if 'payment_ids' in vals and any(
            rec.state in ('closed', 'deposited') for rec in self
        ):
            # Check if this is adding new lines (not just updating existing)
            for cmd in vals.get('payment_ids', []):
                if isinstance(cmd, (list, tuple)):
                    # 0=create, 4=link — both add payments
                    if cmd[0] in (0, 4):
                        locked = self.filtered(
                            lambda r: r.state in ('closed', 'deposited')
                        )
                        if locked:
                            raise UserError(_(
                                "Cannot add payments to a closed or deposited "
                                "batch.  Batch '%s' is %s.",
                                locked[0].name,
                                dict(locked[0]._fields['state'].selection).get(
                                    locked[0].state),
                            ))
        return super().write(vals)

    # -----------------------------------------------------------------
    # Actions
    # -----------------------------------------------------------------
    def action_post_and_close(self):
        """Post all draft payments and close the batch."""
        for rec in self:
            if rec.state != 'draft':
                raise UserError(_(
                    "Only open (draft) batches can be closed."
                ))
            drafts = rec.payment_ids.filtered(lambda p: p.state == 'draft')
            if drafts:
                drafts.action_post()
            rec.state = 'closed'
            rec.message_post(
                body=_(
                    "<strong>Daily Batch Closed</strong><br/>"
                    "%(count)d payment(s) posted.<br/>"
                    "Batch total: $%(total).2f<br/>"
                    "Batch is now locked — no additional payments can be added.",
                    count=len(drafts),
                    total=rec.expected_total,
                ),
                message_type='comment',
                subtype_xmlid='mail.mt_note',
            )

    def action_reopen(self):
        """Re-open a closed batch (only if not yet deposited)."""
        for rec in self:
            if rec.state != 'closed':
                raise UserError(_(
                    "Only closed batches can be re-opened."
                ))
            if rec.bank_deposit_id:
                raise UserError(_(
                    "This batch is linked to Bank Deposit '%s'. "
                    "Remove it from the deposit first.",
                    rec.bank_deposit_id.name,
                ))
            rec.state = 'draft'
            rec.message_post(
                body="<strong>Batch Re-opened</strong>",
                message_type='comment',
                subtype_xmlid='mail.mt_note',
            )

    def action_cancel(self):
        """Cancel the batch and all attached payments."""
        for rec in self:
            if rec.state == 'deposited':
                raise UserError(_(
                    "Cannot cancel a deposited batch.  Cancel the "
                    "Bank Deposit first."
                ))
            rec.payment_ids.filtered(
                lambda p: p.state == 'posted'
            ).action_cancel()
            rec.state = 'cancelled'
            rec.message_post(
                body="<strong>Batch Cancelled</strong>",
                message_type='comment',
                subtype_xmlid='mail.mt_note',
            )

    # Legacy compatibility
    def action_post_all(self):
        """Backward-compat alias — closes the batch."""
        return self.action_post_and_close()

    def action_view_payments(self):
        """Smart button: show payments in this batch."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Payments in this Batch'),
            'res_model': 'elks.dues.payment',
            'view_mode': 'list,form',
            'domain': [('deposit_id', '=', self.id)],
            'context': {'default_deposit_id': self.id},
        }


# =====================================================================
# Bank Deposit  (groups multiple Daily Batches)
# =====================================================================

class ElksBankDeposit(models.Model):
    """Groups one or more closed Daily Batches into a single bank deposit.

    Workflow:  Draft → Signed Off → Deposited
      - Draft:      batches can be added / removed
      - Signed Off: Treasurer has approved, locked for transport
      - Deposited:  confirmed at bank, all linked batches marked deposited
    """
    _name = "elks.bank.deposit"
    _description = "Bank Deposit"
    _order = "deposit_date desc, id desc"
    _inherit = ["mail.thread"]

    name = fields.Char(
        "Deposit Reference", compute="_compute_name", store=True,
    )
    deposit_date = fields.Date(
        "Deposit Date", required=True,
        default=fields.Date.context_today, index=True, tracking=True,
        help="Date the deposit is being taken to the bank.",
    )
    bank_reference = fields.Char(
        "Bank Deposit #", tracking=True,
        help="Deposit slip or bank transaction reference number.",
    )
    state = fields.Selection([
        ('draft', 'Draft'),
        ('signed', 'Signed Off'),
        ('deposited', 'Deposited'),
        ('cancelled', 'Cancelled'),
    ], default='draft', tracking=True, index=True)

    # Batches included in this deposit
    batch_ids = fields.One2many(
        "elks.dues.deposit", "bank_deposit_id",
        string="Daily Batches",
        help="Closed daily batches included in this bank deposit.",
    )
    batch_count = fields.Integer(
        compute="_compute_totals", store=True,
    )

    # Totals
    expected_total = fields.Monetary(
        "Expected Total", compute="_compute_totals", store=True,
        currency_field='currency_id',
        help="Sum of all batch totals being deposited.",
    )
    bank_confirmed_total = fields.Monetary(
        "Bank Confirmed Total", tracking=True,
        currency_field='currency_id',
        help="Actual amount confirmed by the bank on the deposit receipt.",
    )
    variance = fields.Monetary(
        "Variance", compute="_compute_totals", store=True,
        currency_field='currency_id',
        help="Expected total − bank confirmed total.",
    )
    currency_id = fields.Many2one(
        "res.currency", default=lambda self: self.env.company.currency_id,
    )

    # Sign-off
    treasurer_id = fields.Many2one(
        "res.users", string="Treasurer Sign-Off", tracking=True,
        help="Treasurer who approved this deposit for bank transport.",
    )
    treasurer_sign_date = fields.Datetime(
        "Treasurer Signed At", readonly=True,
    )
    received_by = fields.Char(
        "Received By", tracking=True,
        help="Name of person who physically received the deposit "
             "for transport to the bank.",
    )
    received_date = fields.Datetime(
        "Received At", readonly=True,
    )

    prepared_by = fields.Many2one(
        "res.users", string="Prepared By",
        default=lambda self: self.env.user, tracking=True,
    )
    note = fields.Text("Notes")

    # -----------------------------------------------------------------
    # Computed
    # -----------------------------------------------------------------
    @api.depends("deposit_date", "bank_reference")
    def _compute_name(self):
        for rec in self:
            parts = ["Bank Deposit"]
            if rec.deposit_date:
                parts.append(str(rec.deposit_date))
            if rec.bank_reference:
                parts.append(f"#{rec.bank_reference}")
            rec.name = " ".join(parts)

    @api.depends("batch_ids.expected_total", "bank_confirmed_total")
    def _compute_totals(self):
        for rec in self:
            rec.batch_count = len(rec.batch_ids)
            rec.expected_total = sum(rec.batch_ids.mapped('expected_total'))
            rec.variance = rec.expected_total - (rec.bank_confirmed_total or 0)

    # -----------------------------------------------------------------
    # Constraints
    # -----------------------------------------------------------------
    @api.constrains('batch_ids')
    def _check_batches_closed(self):
        """Only closed batches can be added to a bank deposit."""
        for rec in self:
            bad = rec.batch_ids.filtered(lambda b: b.state not in ('closed', 'deposited'))
            if bad:
                raise ValidationError(_(
                    "Only closed batches can be added to a bank deposit.  "
                    "Batch '%s' is still %s.",
                    bad[0].name,
                    dict(bad[0]._fields['state'].selection).get(bad[0].state),
                ))

    # -----------------------------------------------------------------
    # Actions
    # -----------------------------------------------------------------
    def action_treasurer_sign_off(self):
        """Treasurer approves the deposit for bank transport."""
        for rec in self:
            if rec.state != 'draft':
                raise UserError(_("Only draft deposits can be signed off."))
            if not rec.batch_ids:
                raise UserError(_("Add at least one daily batch before signing off."))
            rec.write({
                'state': 'signed',
                'treasurer_id': self.env.user.id,
                'treasurer_sign_date': fields.Datetime.now(),
            })
            rec.message_post(
                body=_(
                    "<strong>Treasurer Sign-Off</strong><br/>"
                    "Approved by %(user)s.<br/>"
                    "%(count)d batch(es), total: $%(total).2f",
                    user=self.env.user.name,
                    count=rec.batch_count,
                    total=rec.expected_total,
                ),
                message_type='comment',
                subtype_xmlid='mail.mt_note',
            )

    def action_mark_received(self):
        """Record who received the deposit for transport."""
        for rec in self:
            if rec.state != 'signed':
                raise UserError(_(
                    "The deposit must be signed off by the Treasurer first."
                ))
            if not rec.received_by:
                raise UserError(_(
                    "Please enter the name of the person receiving "
                    "the deposit before marking it as received."
                ))
            rec.write({
                'received_date': fields.Datetime.now(),
            })
            rec.message_post(
                body=_(
                    "<strong>Deposit Received for Transport</strong><br/>"
                    "Received by: %(person)s",
                    person=rec.received_by,
                ),
                message_type='comment',
                subtype_xmlid='mail.mt_note',
            )

    def action_confirm_deposited(self):
        """Confirm the deposit was made at the bank.

        Marks all linked batches as 'deposited' and locks them permanently.
        """
        for rec in self:
            if rec.state != 'signed':
                raise UserError(_(
                    "The deposit must be signed off before it can be "
                    "confirmed as deposited."
                ))
            if not rec.bank_reference:
                raise UserError(_(
                    "Please enter the Bank Deposit # (deposit slip or "
                    "reference number) before confirming."
                ))
            rec.state = 'deposited'
            # Mark all linked batches as deposited
            rec.batch_ids.write({'state': 'deposited'})
            rec.message_post(
                body=_(
                    "<strong>Deposit Confirmed at Bank</strong><br/>"
                    "Bank ref: %(ref)s<br/>"
                    "%(count)d batch(es) marked as deposited.",
                    ref=rec.bank_reference,
                    count=rec.batch_count,
                ),
                message_type='comment',
                subtype_xmlid='mail.mt_note',
            )

    def action_cancel(self):
        """Cancel this bank deposit and unlink batches."""
        for rec in self:
            if rec.state == 'deposited':
                raise UserError(_(
                    "Cannot cancel a confirmed deposit. "
                    "Contact the Treasurer."
                ))
            # Revert batches back to 'closed' if they were linked
            rec.batch_ids.filtered(
                lambda b: b.state == 'deposited'
            ).write({'state': 'closed'})
            rec.batch_ids.write({'bank_deposit_id': False})
            rec.state = 'cancelled'
            rec.message_post(
                body="<strong>Bank Deposit Cancelled</strong>",
                message_type='comment',
                subtype_xmlid='mail.mt_note',
            )


# =====================================================================
# Cash On Hand Count
# =====================================================================

class ElksCashOnHand(models.Model):
    """Semi-annual or ad-hoc count of physical cash and checks on hand.

    Compares counted amounts against the expected total (sum of all
    undeposited daily batch totals).  When a variance exists, an
    explanation is required and an adjustment record is created.
    """
    _name = "elks.cash.on.hand"
    _description = "Cash On Hand Count"
    _order = "count_date desc, id desc"
    _inherit = ["mail.thread"]

    name = fields.Char(
        "Count Reference", compute="_compute_name", store=True,
    )
    count_date = fields.Date(
        "Count Date", required=True,
        default=fields.Date.context_today, index=True, tracking=True,
    )
    count_type = fields.Selection([
        ('semi_annual', 'Semi-Annual Count'),
        ('adhoc', 'Ad-Hoc Count'),
    ], default='semi_annual', required=True, tracking=True,
       help="Semi-annual counts are the required twice-yearly audit. "
            "Ad-hoc counts can be done any time for verification.",
    )
    state = fields.Selection([
        ('draft', 'In Progress'),
        ('done', 'Finalized'),
        ('cancelled', 'Cancelled'),
    ], default='draft', tracking=True, index=True)

    # Expected totals — from undeposited batches
    expected_cash = fields.Monetary(
        "Expected Cash On Hand", compute="_compute_expected",
        store=True, currency_field='currency_id',
        help="Total of all closed but undeposited daily batch amounts. "
             "This is what you should have on hand.",
    )

    # Actual counted amounts
    counted_cash = fields.Monetary(
        "Counted Cash", currency_field='currency_id', tracking=True,
        help="Total cash physically counted (bills and coins).",
    )
    counted_checks = fields.Monetary(
        "Counted Checks", currency_field='currency_id', tracking=True,
        help="Total checks physically counted.",
    )
    counted_other = fields.Monetary(
        "Other (Credit Card Slips, etc.)", currency_field='currency_id',
        tracking=True,
        help="Any other payment instruments on hand.",
    )
    counted_total = fields.Monetary(
        "Total Counted", compute="_compute_counted_total",
        store=True, currency_field='currency_id',
    )

    # Variance
    variance = fields.Monetary(
        "Variance", compute="_compute_variance",
        store=True, currency_field='currency_id',
        help="Counted total minus expected. Positive = overage, "
             "negative = shortage.",
    )
    has_variance = fields.Boolean(
        compute="_compute_variance", store=True,
    )
    variance_explanation = fields.Text(
        "Variance Explanation",
        help="Required when there is a variance. Explain the discrepancy.",
    )

    # Adjustment
    adjustment_amount = fields.Monetary(
        "Adjustment Amount", readonly=True,
        currency_field='currency_id',
        help="Adjustment entry created to reconcile the variance.",
    )
    adjustment_date = fields.Datetime(
        "Adjustment Created", readonly=True,
    )

    # People
    counted_by = fields.Many2one(
        "res.users", string="Counted By",
        default=lambda self: self.env.user, tracking=True,
    )
    witnessed_by = fields.Many2one(
        "res.users", string="Witnessed By", tracking=True,
        help="Second person who witnessed the count (required for "
             "semi-annual counts).",
    )

    currency_id = fields.Many2one(
        "res.currency", default=lambda self: self.env.company.currency_id,
    )
    note = fields.Text("Notes")

    # -----------------------------------------------------------------
    # Computed
    # -----------------------------------------------------------------
    @api.depends("count_date", "count_type")
    def _compute_name(self):
        type_labels = dict(self._fields['count_type'].selection)
        for rec in self:
            label = type_labels.get(rec.count_type, 'Count')
            if rec.count_date:
                rec.name = f"Cash Count — {label} — {rec.count_date}"
            else:
                rec.name = f"Cash Count — {label}"

    @api.depends("count_date")
    def _compute_expected(self):
        """Sum of all closed (undeposited) batch totals as of the count date."""
        Batch = self.env['elks.dues.deposit']
        for rec in self:
            domain = [('state', '=', 'closed')]
            if rec.count_date:
                domain.append(('batch_date', '<=', rec.count_date))
            batches = Batch.search(domain)
            rec.expected_cash = sum(batches.mapped('expected_total'))

    @api.depends("counted_cash", "counted_checks", "counted_other")
    def _compute_counted_total(self):
        for rec in self:
            rec.counted_total = (
                (rec.counted_cash or 0)
                + (rec.counted_checks or 0)
                + (rec.counted_other or 0)
            )

    @api.depends("counted_total", "expected_cash")
    def _compute_variance(self):
        for rec in self:
            rec.variance = (rec.counted_total or 0) - (rec.expected_cash or 0)
            rec.has_variance = abs(rec.variance) > 0.01

    # -----------------------------------------------------------------
    # Constraints
    # -----------------------------------------------------------------
    @api.constrains('count_type', 'witnessed_by')
    def _check_witness_required(self):
        """Semi-annual counts require a witness."""
        for rec in self:
            if rec.count_type == 'semi_annual' and rec.state == 'done' \
                    and not rec.witnessed_by:
                raise ValidationError(_(
                    "Semi-annual cash counts require a witness. "
                    "Please select who witnessed the count."
                ))

    # -----------------------------------------------------------------
    # Actions
    # -----------------------------------------------------------------
    def action_refresh_expected(self):
        """Manually refresh the expected cash on hand total."""
        self._compute_expected()

    def action_finalize(self):
        """Finalize the count and create adjustment if needed."""
        for rec in self:
            if rec.state != 'draft':
                raise UserError(_("Only in-progress counts can be finalized."))

            if rec.count_type == 'semi_annual' and not rec.witnessed_by:
                raise UserError(_(
                    "Semi-annual counts require a witness. "
                    "Please select who witnessed the count."
                ))

            if rec.has_variance:
                if not rec.variance_explanation:
                    raise UserError(_(
                        "There is a variance of $%(var).2f. "
                        "Please provide an explanation before finalizing.",
                        var=rec.variance,
                    ))
                # Create the adjustment record
                rec.write({
                    'adjustment_amount': rec.variance,
                    'adjustment_date': fields.Datetime.now(),
                })
                rec.message_post(
                    body=_(
                        "<strong>Cash Count Finalized — Adjustment Created"
                        "</strong><br/>"
                        "Expected: $%(expected).2f<br/>"
                        "Counted: $%(counted).2f<br/>"
                        "Variance: $%(var).2f<br/>"
                        "Adjustment: $%(adj).2f<br/>"
                        "Explanation: %(expl)s",
                        expected=rec.expected_cash,
                        counted=rec.counted_total,
                        var=rec.variance,
                        adj=rec.adjustment_amount,
                        expl=rec.variance_explanation,
                    ),
                    message_type='comment',
                    subtype_xmlid='mail.mt_note',
                )
            else:
                rec.message_post(
                    body=_(
                        "<strong>Cash Count Finalized — No Variance</strong>"
                        "<br/>"
                        "Expected: $%(expected).2f<br/>"
                        "Counted: $%(counted).2f<br/>"
                        "All clear.",
                        expected=rec.expected_cash,
                        counted=rec.counted_total,
                    ),
                    message_type='comment',
                    subtype_xmlid='mail.mt_note',
                )
            rec.state = 'done'

    def action_cancel(self):
        """Cancel this count."""
        for rec in self:
            rec.state = 'cancelled'
            rec.message_post(
                body="<strong>Cash Count Cancelled</strong>",
                message_type='comment',
                subtype_xmlid='mail.mt_note',
            )
