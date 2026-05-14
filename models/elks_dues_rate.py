# -*- coding: utf-8 -*-
"""Elks Lodge Dues & Fee Rate Codes.

Maps the CLMS Lodge Rates (TranCodeID) to the Elks Uniform COA.
Each rate defines a dues or fee type with its amount and the
GL credit/debit accounts used when the payment is processed.

Dues Payments now carry individual line items (ElksDuesPaymentLine)
so each component of a payment (dues, per capita, insurance,
magazine, state fees, charitable contributions) is tracked separately.
"""
from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError


TRAN_TYPES = [
    ('CLP', 'Current Lodge Payment'),
    ('CNM', 'Current New Member'),
    ('ALP', 'Advance Lodge Payment'),
    ('APP', 'Application Payment'),
    ('REI', 'Reinstatement Payment'),
    ('OD', 'Other Deduction / Donation'),
    ('OTH', 'Other Payment'),
]

RATE_CODES = [
    ('', 'None'),
    ('A1', 'Associate Member'),
    ('A2', 'Associate Rate 2'),
]

PAYMENT_TYPES = [
    ('one_year', 'One Year Dues Payment'),
    ('one_year_life', 'Life Member Dues Payment'),
    ('six_months', 'Six Months Dues Payment'),
    ('prorated', 'Pro-Rated Dues Payment'),
    ('custom', 'Custom / Misc. Payment'),
]


class ElksDuesRate(models.Model):
    _name = "elks.dues.rate"
    _description = "Elks Lodge Dues / Fee Rate Code"
    _order = "tran_code_id"

    tran_code_id = fields.Char(
        "TranCodeID", required=True, index=True,
        help="CLMS transaction code identifier (e.g., 81518).",
    )
    tran_type = fields.Selection(
        TRAN_TYPES, string="Transaction Type", default='CLP',
        help="CLMS transaction type code.",
    )
    rate_code = fields.Char(
        "Rate Code",
        help="Optional rate code (e.g., A1 for Associate Member).",
    )
    name = fields.Char(
        "Description", required=True, index=True,
        help="Full description, e.g. '[5A] Regular - Current Dues 12 Mos Prorated'.",
    )
    amount = fields.Monetary(
        "Amount", currency_field='currency_id',
        help="Standard rate amount.",
    )
    credit_account_id = fields.Many2one(
        "elks.account", string="Credit Account",
        help="GL account credited when this rate is applied (income side).",
    )
    credit_account_code = fields.Char(
        "Credit Acct Code",
        help="CLMS credit account code (e.g., 3010001). "
             "Used for reference and CSV export even if not linked to an elks.account.",
    )
    debit_account_id = fields.Many2one(
        "elks.account", string="Debit Account",
        help="GL account debited when this rate is applied (cash/receivable side).",
    )
    debit_account_code = fields.Char(
        "Debit Acct Code",
        help="CLMS debit account code (e.g., 1010101). "
             "Used for reference and CSV export even if not linked to an elks.account.",
    )
    active = fields.Boolean(default=True)
    is_dues = fields.Boolean(
        "Is Dues", default=True,
        help="True if this rate affects the member's dues paid-to date.",
    )
    months_covered = fields.Integer(
        "Months Covered", default=12,
        help="Number of months this payment covers (12 for annual, 6 for semi-annual).",
    )
    applies_to = fields.Selection([
        ('regular', 'Regular Members'),
        ('life', 'Life Members'),
        ('associate', 'Associate Members'),
        ('all', 'All Members'),
    ], string="Applies To", default='all')

    # Bundle grouping: which payment_type auto-includes this rate
    include_in_one_year = fields.Boolean(
        "Include in 1-Year Payment", default=False,
        help="Automatically include this fee when processing a one-year dues payment.",
    )
    include_in_six_months = fields.Boolean(
        "Include in 6-Month Payment", default=False,
        help="Automatically include this fee when processing a six-month dues payment.",
    )

    currency_id = fields.Many2one(
        "res.currency", default=lambda self: self.env.company.currency_id,
    )

    note = fields.Text("Notes")


# -------------------------------------------------------------------------
# Dues Payment (header)
# -------------------------------------------------------------------------
class ElksDuesPayment(models.Model):
    _name = "elks.dues.payment"
    _description = "Elks Member Dues Payment"
    _order = "payment_date desc, id desc"
    _inherit = ["mail.thread"]

    name = fields.Char(
        "Reference", compute="_compute_name", store=True,
    )
    partner_id = fields.Many2one(
        "res.partner", string="Member", required=True, index=True,
        domain="[('x_is_member', '=', True)]",
    )
    member_number = fields.Char(
        related="partner_id.x_detail_member_num", store=True,
        string="Member No.",
    )
    dues_paid_to = fields.Date(
        related="partner_id.x_detail_dues_paid_to_date",
        string="Currently Paid To", readonly=True,
    )
    payment_type = fields.Selection(
        PAYMENT_TYPES, string="Payment Type", default='one_year',
        required=True,
        help="Selects which bundle of fees to include.",
    )
    # Keep rate_id for backward compatibility and as the "primary" rate
    rate_id = fields.Many2one(
        "elks.dues.rate", string="Rate / Fee",
        help="Primary dues rate (auto-filled from payment type).",
    )
    payment_date = fields.Date(
        "Transaction Date", required=True,
        default=fields.Date.context_today, index=True,
    )
    check_number = fields.Char("Check Number")
    line_ids = fields.One2many(
        "elks.dues.payment.line", "payment_id",
        string="Payment Lines",
    )
    amount_total = fields.Monetary(
        "Total Paid", currency_field='currency_id',
        compute="_compute_amount_total", store=True,
    )
    currency_id = fields.Many2one(
        "res.currency", default=lambda self: self.env.company.currency_id,
    )

    lodge_year = fields.Char(
        "Lodge Year", compute="_compute_lodge_year", store=True,
    )
    state = fields.Selection([
        ('draft', 'Draft'),
        ('posted', 'Posted'),
        ('cancelled', 'Cancelled'),
    ], default='draft', tracking=True, index=True)

    journal_entry_id = fields.Many2one(
        "elks.journal.entry", string="Journal Entry",
        readonly=True, copy=False,
        help="Auto-generated journal entry for this payment.",
    )

    # Reversal bookkeeping: snapshot the member's dues-paid-to-date at
    # the moment we post, so we can restore it exactly on cancel.
    dues_paid_to_before = fields.Date(
        "Dues Paid-To Before Payment",
        readonly=True, copy=False,
        help="Snapshot of the member's dues-paid-to-date before this "
             "payment was posted.  Used to restore on cancel.",
    )
    dues_paid_to_after = fields.Date(
        "Dues Paid-To After Payment",
        readonly=True, copy=False,
    )

    # Deposit / batch grouping
    deposit_id = fields.Many2one(
        "elks.dues.deposit", string="Daily Batch",
        ondelete="set null", index=True,
        help="The daily batch this payment belongs to.",
    )

    # CLMS processing tracker
    clms_status = fields.Selection([
        ('pending', 'Pending CLMS Entry'),
        ('processed', 'Processed in CLMS'),
        ('na', 'Not Applicable'),
    ], string="CLMS Status", default='pending', tracking=True, index=True,
        help="Has this payment been entered into CLMS yet?",
    )
    clms_processed_date = fields.Date(
        "CLMS Processed On", tracking=True, copy=False,
    )
    clms_processed_by = fields.Many2one(
        "res.users", string="Processed in CLMS By", tracking=True, copy=False,
    )

    # Link back to the membership application that triggered this payment
    application_id = fields.Many2one(
        "elks.membership.application", string="Membership Application",
        ondelete="set null", copy=False,
        help="The membership application this payment is for (initiation/investigation fees).",
    )

    note = fields.Text("Notes")

    # --- compatibility shim: 'amount' returns total ---
    amount = fields.Monetary(
        "Amount Paid", currency_field='currency_id',
        compute="_compute_amount_total", store=True,
    )

    @api.depends("partner_id", "payment_type", "payment_date")
    def _compute_name(self):
        for rec in self:
            parts = []
            if rec.partner_id:
                parts.append(rec.partner_id.name or "")
            if rec.payment_type:
                label = dict(PAYMENT_TYPES).get(rec.payment_type, '')
                parts.append(label)
            if rec.payment_date:
                parts.append(str(rec.payment_date))
            rec.name = " / ".join(parts) if parts else "New"

    @api.depends("line_ids.amount_paid")
    def _compute_amount_total(self):
        for rec in self:
            rec.amount_total = sum(rec.line_ids.mapped('amount_paid'))
            rec.amount = rec.amount_total

    @api.depends("payment_date")
    def _compute_lodge_year(self):
        for rec in self:
            if rec.payment_date:
                d = rec.payment_date
                start = d.year if d.month >= 4 else d.year - 1
                rec.lodge_year = f"{start}-{start + 1}"
            else:
                rec.lodge_year = False

    @api.onchange("partner_id")
    def _onchange_partner_id_payment_type(self):
        """Auto-switch payment type when member changes.

        If the selected member is a life/honorary member, default to the
        Life Member Dues Payment type so the correct rates are loaded.
        """
        if not self.partner_id:
            return
        member_type = self._get_member_type()
        if member_type == 'life' and self.payment_type in ('one_year', False):
            self.payment_type = 'one_year_life'
        elif member_type == 'regular' and self.payment_type == 'one_year_life':
            self.payment_type = 'one_year'

    @api.onchange("payment_type", "partner_id")
    def _onchange_payment_type(self):
        """Auto-populate payment lines when payment type or member changes.

        For one-year or six-month payments the method looks up rates that
        have the corresponding bundle flag set.  If no flagged rates are
        found (e.g. the data hasn't been upgraded yet) it falls back to
        selecting rates by ``months_covered`` plus all non-dues fee rates
        that match the member type.

        The ``one_year_life`` payment type explicitly selects life-member
        rates.  Life-member rate records already carry the correct amounts
        (e.g. $50.00 dues vs $120.00 for regular members) so no discount
        is applied.
        """
        if not self.payment_type or not self.partner_id:
            return

        if self.payment_type == 'custom':
            return

        # --- Determine member type & bundle flag ---
        is_prorated = self.payment_type == 'prorated'

        if self.payment_type == 'one_year_life':
            member_type = 'life'
            bundle_field = 'include_in_one_year'
            months = 12
        elif self.payment_type in ('one_year', 'prorated'):
            # Pro-rated uses the same one-year bundle, then scales amounts
            member_type = self._get_member_type()
            bundle_field = 'include_in_one_year'
            months = 12
        else:  # six_months
            member_type = 'regular'
            bundle_field = 'include_in_six_months'
            months = 6

        DuesRate = self.env['elks.dues.rate']

        # Primary search: use the bundle flags
        rates = DuesRate.search([
            (bundle_field, '=', True),
            ('active', '=', True),
            '|',
            ('applies_to', '=', member_type),
            ('applies_to', '=', 'all'),
        ])

        # Fallback: if no flagged rates, auto-select by months_covered
        if not rates:
            dues_rates = DuesRate.search([
                ('is_dues', '=', True),
                ('months_covered', '=', months),
                ('active', '=', True),
                '|',
                ('applies_to', '=', member_type),
                ('applies_to', '=', 'all'),
            ], limit=1)
            fee_rates = DuesRate.search([
                ('is_dues', '=', False),
                ('active', '=', True),
                '|',
                ('applies_to', '=', member_type),
                ('applies_to', '=', 'all'),
            ])
            rates = dues_rates | fee_rates

        # --- Build lines (amounts come directly from rate records) ---
        lines = []
        seq = 10
        for rate in rates:
            lines.append((0, 0, {
                'sequence': seq,
                'rate_id': rate.id,
                'description': rate.name,
                'default_amount': rate.amount,
                'amount_paid': rate.amount,
                'lodge_assisted': False,
            }))
            seq += 10

        self.line_ids = [(5, 0, 0)] + lines  # clear then add

        # Pro-rate amounts if this is a prorated payment
        if is_prorated and self.payment_date:
            month = self.payment_date.month
            if month >= 4:
                months_remaining = 16 - month
            else:
                months_remaining = 4 - month
            if months_remaining <= 0:
                months_remaining = 12
            for line in self.line_ids:
                if line.default_amount:
                    line.amount_paid = round(
                        line.default_amount * months_remaining / 12, 2,
                    )
            self.note = (
                f"Pro-rated initiation payment: "
                f"{months_remaining} of 12 months remaining in lodge year."
            )

        # Set primary rate (first dues line found)
        dues_rate = rates.filtered('is_dues')[:1]
        if dues_rate:
            self.rate_id = dues_rate.id

    def _get_member_type(self):
        """Determine the member type from the partner record.

        Checks the CLMS DetailElkTitle, and also looks at the
        LastLifeDate / LastHonLifeDate fields as a fallback.
        """
        partner = self.partner_id
        if not partner:
            return 'regular'

        title = (partner.x_detail_elk_title or '').lower()
        if 'life' in title or 'honorary' in title:
            return 'life'
        if 'associate' in title:
            return 'associate'

        # Fallback: if the member has a life or honorary life date, treat as life
        if partner.x_last_life_date or partner.x_last_hon_life_date:
            return 'life'

        return 'regular'

    # -----------------------------------------------------------------
    # Recalculate pro-rated amounts when transaction date changes
    # -----------------------------------------------------------------
    @api.onchange("payment_date")
    def _onchange_payment_date_prorate(self):
        """Recalculate pro-rated line amounts when the date changes.

        Only fires for Custom payments that are linked to a membership
        application (i.e. initiation/reinstatement payments).  Regular
        one-year or six-month payments are not affected.
        """
        if self.payment_type not in ('custom', 'prorated'):
            return
        if self.payment_type == 'custom' and not self.application_id:
            return
        if not self.payment_date or not self.line_ids:
            return

        month = self.payment_date.month
        if month >= 4:
            months_remaining = 16 - month
        else:
            months_remaining = 4 - month
        if months_remaining <= 0:
            months_remaining = 12

        for line in self.line_ids:
            if line.default_amount:
                line.amount_paid = round(
                    line.default_amount * months_remaining / 12, 2,
                )

        self.note = (
            f"Pro-rated initiation payment: "
            f"{months_remaining} of 12 months remaining in lodge year."
        )

    # -----------------------------------------------------------------
    # Account lookup helper
    # -----------------------------------------------------------------
    def _resolve_account(self, rate, side='credit'):
        """Return an elks.account record for the given rate and side.

        First checks the linked Many2one; if empty, falls back to
        searching by the account code stored on the rate.
        """
        Account = self.env['elks.account']
        if side == 'credit':
            acct = rate.credit_account_id
            code = rate.credit_account_code
        else:
            acct = rate.debit_account_id
            code = rate.debit_account_code

        if acct:
            return acct

        if code:
            # Try exact match first
            acct = Account.search([('code', '=', code)], limit=1)
            if not acct and len(code) > 5:
                # CLMS 7-digit codes don't have a reliable algorithmic mapping
                # to the Elks 5-digit COA.  The correct link should always be
                # set via credit_account_id / debit_account_id in the rate
                # data.  Only attempt safe fallbacks that won't mis-map.
                # Try first 5 digits (works for asset/liability codes like
                # 1010101 → 10101).
                base5 = code[:5]
                acct = Account.search([('code', '=', base5)], limit=1)
                # Do NOT fall back to broader patterns (e.g. first 3 digits)
                # as this can silently assign the wrong account (e.g.
                # 3010003 → 30100 instead of 30010).
                if not acct:
                    import logging
                    _logger = logging.getLogger(__name__)
                    _logger.warning(
                        "Rate %s: no Elks account found for CLMS code %s. "
                        "Please set credit/debit account manually.",
                        rate.display_name, code,
                    )
            if acct:
                # Cache the link for future use
                if side == 'credit':
                    rate.sudo().write({'credit_account_id': acct.id})
                else:
                    rate.sudo().write({'debit_account_id': acct.id})
            return acct

        return Account  # empty recordset

    def _get_lodge_assistance_account(self):
        """Look up the Lodge Assistance expense account used when a line
        is marked lodge_assisted.  Prefers code 31000 but falls back to
        any account whose name contains 'Lodge Assist' or 'Member Assist'."""
        Account = self.env['elks.account']
        acct = Account.search([('code', '=', '31000')], limit=1)
        if acct:
            return acct
        acct = Account.search([
            '|', ('name', 'ilike', 'lodge assist'),
            ('name', 'ilike', 'member assist'),
        ], limit=1)
        if acct:
            return acct
        # Fall back to any expense account in Lodge Operations dept
        return Account.search([
            ('account_type', '=', 'expense'),
        ], limit=1)

    # -----------------------------------------------------------------
    # Pro-rated initiation / reinstatement payment
    # -----------------------------------------------------------------
    @api.model
    def create_prorated_initiation_payment(self, partner, application=None):
        """Create a draft pro-rated dues payment for initiation / reinstatement.

        Uses the same rate-lookup logic as the UI onchange by creating a
        virtual record with ``payment_type='prorated'`` and manually
        triggering the onchanges.  This guarantees the lines populated
        here match exactly what the Secretary would see if they chose
        "Pro-Rated Dues Payment" from the dropdown.

        Args:
            partner: res.partner record of the member being initiated.
            application: optional elks.membership.application record.

        Returns:
            The newly created elks.dues.payment record (draft state).
        """
        import logging
        _logger = logging.getLogger(__name__)

        # Use the application's initiation date if available
        ref_date = fields.Date.context_today(self)
        if application and hasattr(application, 'date_initiated') and application.date_initiated:
            ref_date = application.date_initiated

        # Build a virtual record and trigger the onchanges that
        # populate lines (the same code path the UI uses).
        tmp = self.new({
            'partner_id': partner.id,
            'payment_type': 'prorated',
            'payment_date': ref_date,
        })
        tmp._onchange_partner_id_payment_type()
        tmp._onchange_payment_type()

        _logger.info(
            "Pro-rated payment: partner=%s, ref_date=%s, "
            "onchange populated %d lines",
            partner.id, ref_date, len(tmp.line_ids),
        )

        if not tmp.line_ids:
            _logger.warning(
                "No lines generated for pro-rated initiation payment "
                "(partner=%s). Check that dues rates have "
                "include_in_one_year=True.", partner.id,
            )
            return self.browse()

        # Extract line data from the virtual record into create-tuples
        lines = []
        for line in tmp.line_ids:
            lines.append((0, 0, {
                'sequence': line.sequence,
                'rate_id': line.rate_id.id,
                'description': line.description,
                'default_amount': line.default_amount,
                'amount_paid': line.amount_paid,
                'lodge_assisted': line.lodge_assisted,
            }))

        payment_vals = {
            'partner_id': partner.id,
            'payment_type': 'prorated',
            'payment_date': ref_date,
            'rate_id': tmp.rate_id.id if tmp.rate_id else False,
            'line_ids': lines,
            'note': tmp.note or '',
        }
        if application:
            payment_vals['application_id'] = application.id

        payment = self.create(payment_vals)

        _logger.info(
            "Created pro-rated dues payment %s for partner %s "
            "(%d lines, $%.2f total)",
            payment.id, partner.id,
            len(payment.line_ids), payment.amount_total,
        )

        return payment

    # -----------------------------------------------------------------
    # Post payment
    # -----------------------------------------------------------------
    def action_post(self):
        """Post the payment: create a journal entry from all lines and update member dues."""
        JournalEntry = self.env["elks.journal.entry"]

        for rec in self:
            if rec.state != 'draft':
                continue

            if not rec.line_ids:
                raise UserError(_("Cannot post a payment with no line items."))

            # Snapshot member's dues paid-to date BEFORE we change it
            rec.dues_paid_to_before = rec.partner_id.x_detail_dues_paid_to_date

            # Build journal entry lines from payment lines
            journal_lines = []
            cash_amount = 0.0          # what the member actually paid
            assisted_amount = 0.0      # what the lodge covered
            log_parts = []
            skipped_lines = []

            for line in rec.line_ids:
                if line.amount_paid <= 0:
                    if line.amount_paid == 0:
                        log_parts.append(
                            f"  • {line.description}: $0.00 (skipped — zero amount)"
                        )
                    continue

                rate = line.rate_id
                credit_acct = rec._resolve_account(rate, 'credit') if rate else None

                # Track cash vs lodge-assisted allocation for this line
                if line.lodge_assisted:
                    assisted_amount += line.amount_paid
                else:
                    cash_amount += line.amount_paid

                if credit_acct:
                    journal_lines.append((0, 0, {
                        'account_id': credit_acct.id,
                        'debit': 0.0,
                        'credit': line.amount_paid,
                        'memo': f"{line.description}: {rec.partner_id.name}",
                    }))
                    assisted = " (Lodge Assisted)" if line.lodge_assisted else ""
                    log_parts.append(
                        f"  • {line.description}: ${line.amount_paid:,.2f}"
                        f" → CR {credit_acct.code} {credit_acct.name}{assisted}"
                    )
                else:
                    skipped_lines.append(line.description)
                    log_parts.append(
                        f"  • {line.description}: ${line.amount_paid:,.2f}"
                        f" (no GL credit account mapped — included in debit total)"
                    )

            total_amount = cash_amount + assisted_amount

            if not journal_lines:
                raise UserError(_(
                    "No payment lines have a mapped GL credit account. "
                    "Please set up the credit accounts on the Lodge Rates."
                ))

            # If there were lines with no credit account, create a single
            # "Unallocated Dues Income" credit line so the entry balances.
            # Use the first available credit account as fallback.
            if skipped_lines:
                # Sum of skipped line amounts
                mapped_credit = sum(
                    jl[2]['credit'] for jl in journal_lines
                )
                unallocated = round(total_amount - mapped_credit, 2)
                if unallocated > 0:
                    fallback_acct = journal_lines[0][2]['account_id']
                    journal_lines.append((0, 0, {
                        'account_id': fallback_acct,
                        'debit': 0.0,
                        'credit': unallocated,
                        'memo': (
                            f"Unallocated dues income: "
                            f"{', '.join(skipped_lines)}"
                        ),
                    }))
                    log_parts.append(
                        f"  • Unallocated (unmapped rates): "
                        f"${unallocated:,.2f} → CR fallback account"
                    )

            # Debit side:
            #   cash portion → operating checking
            #   lodge-assisted portion → lodge assistance expense
            debit_acct = None
            for line in rec.line_ids:
                if line.rate_id:
                    debit_acct = rec._resolve_account(line.rate_id, 'debit')
                    if debit_acct:
                        break

            if not debit_acct:
                raise UserError(_(
                    "No debit (cash/checking) account found on any rate. "
                    "Please configure debit accounts on the Lodge Rates."
                ))

            if cash_amount > 0:
                journal_lines.append((0, 0, {
                    'account_id': debit_acct.id,
                    'debit': cash_amount,
                    'credit': 0.0,
                    'memo': f"Dues payment received: {rec.partner_id.name}",
                }))
                log_parts.append(
                    f"  • Cash received: ${cash_amount:,.2f}"
                    f" → DR {debit_acct.code} {debit_acct.name}"
                )

            if assisted_amount > 0:
                assist_acct = rec._get_lodge_assistance_account()
                if not assist_acct:
                    raise UserError(_(
                        "Lodge-assisted amount is $%(amt).2f but no "
                        "Lodge Assistance expense account is configured. "
                        "Please create account 61000 or an expense account "
                        "with 'Lodge Assist' in its name."
                    ) % {'amt': assisted_amount})
                journal_lines.append((0, 0, {
                    'account_id': assist_acct.id,
                    'debit': assisted_amount,
                    'credit': 0.0,
                    'memo': f"Lodge-assisted portion: {rec.partner_id.name}",
                }))
                log_parts.append(
                    f"  • Lodge-assisted expense: ${assisted_amount:,.2f}"
                    f" → DR {assist_acct.code} {assist_acct.name}"
                )

            # Create and post the journal entry
            entry = JournalEntry.create({
                'date': rec.payment_date,
                'memo': f"Dues payment: {rec.partner_id.name}",
                'line_ids': journal_lines,
            })
            entry.action_post()
            rec.journal_entry_id = entry.id

            # Update member's dues paid-to date
            old_paid_to = rec.partner_id.x_detail_dues_paid_to_date
            dues_lines = rec.line_ids.filtered(
                lambda l: (l.rate_id and l.rate_id.is_dues
                           and l.rate_id.months_covered > 0
                           and l.amount_paid > 0)
            )
            new_paid_to = None
            if dues_lines:
                primary = dues_lines[0]
                new_paid_to = rec._update_member_dues_date(rec, primary.rate_id)
                rec.dues_paid_to_after = new_paid_to

            rec.state = 'posted'

            # --- Log to chatter ---
            payment_label = dict(PAYMENT_TYPES).get(rec.payment_type, rec.payment_type)
            body_lines = [
                f"<strong>Payment Posted</strong> — {payment_label}",
                f"<br/>Member: {rec.partner_id.name}"
                f" (#{rec.member_number or 'N/A'})",
            ]
            if rec.check_number:
                body_lines.append(f"<br/>Check #: {rec.check_number}")
            body_lines.append(f"<br/>Date: {rec.payment_date}")
            body_lines.append(f"<br/>Lodge Year: {rec.lodge_year}")
            body_lines.append("<br/><br/><strong>Line Items:</strong><br/>")
            body_lines.append("<br/>".join(log_parts))
            body_lines.append(
                f"<br/><br/><strong>Total: ${total_amount:,.2f}</strong>"
            )
            if new_paid_to:
                body_lines.append(
                    f"<br/>Dues Paid-To updated: {old_paid_to or 'N/A'}"
                    f" → {new_paid_to}"
                )
            body_lines.append(
                f"<br/>Journal Entry: {entry.entry_number}"
            )

            rec.message_post(
                body="".join(body_lines),
                message_type='comment',
                subtype_xmlid='mail.mt_note',
            )

    def _update_member_dues_date(self, payment, rate):
        """Advance the member's dues-paid-to date by the months covered.

        Elks fiscal year runs April 1 – March 31.  Dues are "paid through"
        March 31 so a one-year payment lands on March 31 of the next year.

        Logic:
        - If the member already has a paid-to date, advance from the day
          after that date (i.e. the next due date) by the months covered,
          then subtract one day so the result is the last day covered.
        - If no paid-to date exists, start from April 1 of the current
          lodge year, advance by months covered, then subtract one day.
          For a 12-month payment that gives March 31 of the next year.

        Returns the new date for logging purposes.
        """
        import datetime
        from dateutil.relativedelta import relativedelta

        partner = payment.partner_id
        current_date = partner.x_detail_dues_paid_to_date
        months = rate.months_covered

        if current_date:
            # Start from the day AFTER current paid-to (the next due date)
            due_date = current_date + relativedelta(days=1)
            new_date = due_date + relativedelta(months=months) - relativedelta(days=1)
        else:
            # No existing paid-to: assume coverage starts April 1 of
            # the current lodge year
            today = payment.payment_date or fields.Date.context_today(self)
            if today.month >= 4:
                start = datetime.date(today.year, 4, 1)
            else:
                start = datetime.date(today.year - 1, 4, 1)
            # Advance by months and subtract 1 day → March 31
            new_date = start + relativedelta(months=months) - relativedelta(days=1)

        partner.write({'x_detail_dues_paid_to_date': new_date})

        # Log to the member's contact chatter as well
        partner.message_post(
            body=(
                f"<strong>Dues Payment Received</strong><br/>"
                f"Amount: ${payment.amount_total:,.2f}<br/>"
                f"Payment Type: {dict(PAYMENT_TYPES).get(payment.payment_type, '')}<br/>"
                f"Date: {payment.payment_date}<br/>"
                f"Dues Paid-To: {current_date or 'N/A'} → {new_date}<br/>"
                f"Check #: {payment.check_number or 'N/A'}"
            ),
            message_type='comment',
            subtype_xmlid='mail.mt_note',
        )
        return new_date

    # -----------------------------------------------------------------
    # Cancel / Reset
    # -----------------------------------------------------------------
    def action_cancel(self):
        for rec in self:
            entry_ref = (rec.journal_entry_id.entry_number
                         if rec.journal_entry_id else 'N/A')
            was_posted = rec.state == 'posted'

            if rec.journal_entry_id and rec.journal_entry_id.state == 'posted':
                rec.journal_entry_id.action_cancel()

            # Reverse the member's dues-paid-to date.  We restore the
            # snapshot captured at post time; if no snapshot exists (e.g.
            # cancelling from draft), leave the member's date alone.
            restored_date = None
            if was_posted and rec.dues_paid_to_before is not None:
                current = rec.partner_id.x_detail_dues_paid_to_date
                rec.partner_id.write({
                    'x_detail_dues_paid_to_date': rec.dues_paid_to_before,
                })
                restored_date = rec.dues_paid_to_before
                rec.partner_id.message_post(
                    body=(
                        f"<strong>Dues Paid-To Reversed (Payment Cancelled)</strong><br/>"
                        f"Previous Paid-To: {current or 'N/A'}<br/>"
                        f"Restored To: {rec.dues_paid_to_before or 'N/A'}<br/>"
                        f"Journal Entry {entry_ref} was reversed."
                    ),
                    message_type='comment',
                    subtype_xmlid='mail.mt_note',
                )

            rec.state = 'cancelled'

            rec.message_post(
                body=(
                    f"<strong>Payment Cancelled</strong><br/>"
                    f"Member: {rec.partner_id.name}"
                    f" (#{rec.member_number or 'N/A'})<br/>"
                    f"Amount: ${rec.amount_total:,.2f}<br/>"
                    f"Journal Entry {entry_ref} reversed."
                    + (
                        f"<br/>Dues paid-to restored to: {restored_date}"
                        if restored_date else ""
                    )
                ),
                message_type='comment',
                subtype_xmlid='mail.mt_note',
            )

            # Notify on the member's contact too (brief version)
            rec.partner_id.message_post(
                body=(
                    f"<strong>Dues Payment Cancelled</strong><br/>"
                    f"Amount: ${rec.amount_total:,.2f}<br/>"
                    f"Date: {rec.payment_date}<br/>"
                    f"Journal Entry {entry_ref} reversed."
                ),
                message_type='comment',
                subtype_xmlid='mail.mt_note',
            )

    def action_draft(self):
        for rec in self:
            rec.state = 'draft'
            rec.message_post(
                body="<strong>Payment reset to Draft</strong>",
                message_type='comment',
                subtype_xmlid='mail.mt_note',
            )

    def _check_secretary_group(self):
        """Guard for CLMS state-change actions.

        Only members of `elkscontacts.group_elks_secretary` may mark a
        payment as Processed in CLMS or reverse that flag. Reception/
        Officer users posting dues payments must NOT have this group —
        accidentally marking a payment as processed would cause the
        Secretary to skip pushing it into CLMS at Grand Lodge, and the
        member would keep getting renewal notices.

        The button is hidden in the UI via groups= in the view; this
        Python check enforces the same constraint for RPC/API callers
        (defense in depth).
        """
        if not self.env.user.has_group('elkscontacts.group_elks_secretary'):
            raise AccessError(_(
                "Only the Lodge Secretary can change a payment's CLMS "
                "status. Ask the Secretary to mark this payment as "
                "Processed in CLMS after they enter it at Grand Lodge."
            ))

    def action_mark_clms_processed(self):
        self._check_secretary_group()
        for rec in self:
            rec.write({
                'clms_status': 'processed',
                'clms_processed_date': fields.Date.context_today(self),
                'clms_processed_by': self.env.user.id,
            })
            rec.message_post(
                body=(
                    f"<strong>Marked Processed in CLMS</strong><br/>"
                    f"Processed by {self.env.user.name} on "
                    f"{fields.Date.context_today(self)}"
                ),
                message_type='comment',
                subtype_xmlid='mail.mt_note',
            )

    def action_mark_clms_pending(self):
        self._check_secretary_group()
        for rec in self:
            rec.write({
                'clms_status': 'pending',
                'clms_processed_date': False,
                'clms_processed_by': False,
            })
            rec.message_post(
                body="<strong>CLMS status reset to Pending</strong>",
                message_type='comment',
                subtype_xmlid='mail.mt_note',
            )


# -------------------------------------------------------------------------
# Dues Payment Line (individual fee/allocation on a payment)
# -------------------------------------------------------------------------
class ElksDuesPaymentLine(models.Model):
    _name = "elks.dues.payment.line"
    _description = "Dues Payment Line Item"
    _order = "sequence, id"

    payment_id = fields.Many2one(
        "elks.dues.payment", string="Payment",
        required=True, ondelete="cascade", index=True,
    )
    sequence = fields.Integer(default=10)
    rate_id = fields.Many2one(
        "elks.dues.rate", string="Rate / Fee",
        help="The rate code this line applies to.",
    )
    description = fields.Char(
        "Description", required=True,
        help="Fee description (auto-filled from rate, editable).",
    )
    amount_paid = fields.Monetary(
        "Amount Paid", currency_field='currency_id',
        help="Actual amount paid for this fee. Can differ from default.",
    )
    default_amount = fields.Monetary(
        "Default Amount", currency_field='currency_id',
        help="Standard amount for reference (from the rate code).",
    )
    lodge_assisted = fields.Boolean(
        "Lodge Assisted", default=False,
        help="Check if the lodge is paying this portion on behalf of the member.",
    )
    currency_id = fields.Many2one(
        "res.currency", default=lambda self: self.env.company.currency_id,
    )

    @api.onchange('rate_id')
    def _onchange_rate_id(self):
        if self.rate_id:
            self.description = self.rate_id.name
            self.amount_paid = self.rate_id.amount
            self.default_amount = self.rate_id.amount
