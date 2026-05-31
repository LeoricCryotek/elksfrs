# -*- coding: utf-8 -*-
"""Cash Management — Movements.

A movement records cash flowing between two places.  Most types are
paper-first and digitised only as a convenience (change_request,
change_order, till_deposit, bank_stock).  ``bank_deposit`` is the one
type that posts an automatic journal entry, since the Monday deposit
moves cash off the books and onto the Operating Checking account.

Movement directions (Till-initiated workflow):

  * change_request : Till/Bag → Bank     (server hands large bills in)
  * change_order   : Bank → Till/Bag     (Bank fills with small bills)
  * till_deposit   : Till/Bag → Bank     (end-of-shift drop)
  * bank_deposit   : Bank → 10100        (Monday deposit run)  *posts JE*
  * bank_stock     : 10100 → Bank        (change-buy at the bank)  *posts JE*

Records never block on running-balance checks — the lodge's actual
physical reality may always run ahead of what's been typed in.
"""
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError, UserError

from .elks_cash_location import DENOM_NAMES, DENOM_FACE


# Code of the GL account representing physical cash in the safe.
# 10000 = Petty Cash in the canonical Elks COA.
_BANK_GL_CODE = '10000'
# 10100 = Operating Checking Account.
_CHECKING_GL_CODE = '10100'


class ElksCashMovement(models.Model):
    _name = 'elks.cash.movement'
    _description = 'Cash Movement'
    _order = 'move_date desc, id desc'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------
    name = fields.Char(
        'Reference', compute='_compute_name', store=True,
    )
    movement_type = fields.Selection([
        ('change_request', 'Change Request (Till → Bank)'),
        ('change_order',   'Change Order (Bank → Till)'),
        ('till_deposit',   'Till Deposit (Till → Bank)'),
        ('bank_deposit',   'Bank Deposit (Bank → Checking)'),
        ('bank_stock',     'Bank Stock-Up (Checking → Bank)'),
    ], string='Type', required=True, default='change_request',
       tracking=True, index=True,
    )
    posts_journal = fields.Boolean(
        compute='_compute_posts_journal', store=True,
        help="True for bank_deposit and bank_stock — these are the "
             "only types that create a journal entry on post.",
    )

    move_date = fields.Datetime(
        'Move Date', required=True, default=fields.Datetime.now,
        tracking=True, index=True,
    )

    state = fields.Selection([
        ('draft', 'Draft'),
        ('posted', 'Posted'),
        ('cancelled', 'Cancelled'),
    ], string='Status', default='draft', tracking=True, index=True, copy=False)

    # ------------------------------------------------------------------
    # Source / destination
    # ------------------------------------------------------------------
    from_location_id = fields.Many2one(
        'elks.cash.location', string='From Location',
        ondelete='restrict', tracking=True,
        help="Source of the cash. Required for movements where cash "
             "leaves an internal location.",
    )
    to_location_id = fields.Many2one(
        'elks.cash.location', string='To Location',
        ondelete='restrict', tracking=True,
        help="Destination of the cash. Required for movements where "
             "cash arrives at an internal location.",
    )
    from_account_code = fields.Char(
        'From GL Account', compute='_compute_gl_accounts', store=True,
        help="GL account the cash leaves (only used for bank_stock).",
    )
    to_account_code = fields.Char(
        'To GL Account', compute='_compute_gl_accounts', store=True,
        help="GL account the cash arrives at (only used for bank_deposit).",
    )

    # ------------------------------------------------------------------
    # People
    # ------------------------------------------------------------------
    done_by_id = fields.Many2one(
        'res.users', string='Done By',
        default=lambda self: self.env.user, tracking=True,
        help="Person who physically moved the cash.",
    )
    authorized_by_id = fields.Many2one(
        'res.users', string='Authorized By', tracking=True,
        help="Officer who approved the movement (signs the paper slip).",
    )

    # ------------------------------------------------------------------
    # Slip serial
    # ------------------------------------------------------------------
    slip_number = fields.Char(
        'Slip Serial', copy=False, index=True, tracking=True,
        help="Pre-printed serial on the paper slip.",
    )

    # ------------------------------------------------------------------
    # Denomination breakdown
    # ------------------------------------------------------------------
    qty_hundreds = fields.Integer('$100 Bills', default=0)
    qty_fifties = fields.Integer('$50 Bills', default=0)
    qty_twenties = fields.Integer('$20 Bills', default=0)
    qty_tens = fields.Integer('$10 Bills', default=0)
    qty_fives = fields.Integer('$5 Bills', default=0)
    qty_twos = fields.Integer('$2 Bills', default=0)
    qty_ones = fields.Integer('$1 Bills', default=0)
    qty_dollar_coins = fields.Integer('Dollar Coins ($1)', default=0)
    qty_half_dollars = fields.Integer('Half Dollars (50¢)', default=0)
    qty_quarters = fields.Integer('Quarters (25¢)', default=0)
    qty_dimes = fields.Integer('Dimes (10¢)', default=0)
    qty_nickels = fields.Integer('Nickels (5¢)', default=0)
    qty_pennies = fields.Integer('Pennies (1¢)', default=0)

    total_amount = fields.Monetary(
        'Total Amount', compute='_compute_total', store=True,
        currency_field='currency_id', tracking=True,
    )

    # ------------------------------------------------------------------
    # Auto-posted journal entry (bank_deposit / bank_stock only)
    # ------------------------------------------------------------------
    journal_entry_id = fields.Many2one(
        'elks.journal.entry', string='Journal Entry', readonly=True, copy=False,
    )

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------
    notes = fields.Text('Notes')
    currency_id = fields.Many2one(
        'res.currency', default=lambda self: self.env.company.currency_id,
    )

    # ==================================================================
    # Lifecycle
    # ==================================================================
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('slip_number'):
                seq_code = self._slip_sequence_code(vals.get('movement_type'))
                vals['slip_number'] = self.env['ir.sequence'].next_by_code(
                    seq_code
                ) or '/'
        return super().create(vals_list)

    @staticmethod
    def _slip_sequence_code(movement_type):
        return {
            'change_request': 'elks.cash.movement.slip.change',
            'change_order':   'elks.cash.movement.slip.change',
            'till_deposit':   'elks.cash.movement.slip.till_deposit',
            'bank_deposit':   'elks.cash.movement.slip.bank_deposit',
            'bank_stock':     'elks.cash.movement.slip.bank_stock',
        }.get(movement_type, 'elks.cash.movement.slip.change')

    # ==================================================================
    # Computes
    # ==================================================================
    @api.depends('movement_type', 'from_location_id', 'to_location_id',
                 'slip_number')
    def _compute_name(self):
        type_prefix = {
            'change_request': 'CHG-REQ',
            'change_order':   'CHG-ORD',
            'till_deposit':   'TILL-DEP',
            'bank_deposit':   'BANK-DEP',
            'bank_stock':     'BANK-STK',
        }
        for rec in self:
            prefix = type_prefix.get(rec.movement_type, 'MOVE')
            slip = rec.slip_number or 'NEW'
            rec.name = f"{prefix} {slip}"

    @api.depends('movement_type')
    def _compute_posts_journal(self):
        for rec in self:
            rec.posts_journal = rec.movement_type in ('bank_deposit',
                                                      'bank_stock')

    @api.depends('movement_type')
    def _compute_gl_accounts(self):
        for rec in self:
            if rec.movement_type == 'bank_deposit':
                rec.from_account_code = False
                rec.to_account_code = _CHECKING_GL_CODE
            elif rec.movement_type == 'bank_stock':
                rec.from_account_code = _CHECKING_GL_CODE
                rec.to_account_code = False
            else:
                rec.from_account_code = False
                rec.to_account_code = False

    @api.depends(*[f'qty_{d}' for d in DENOM_NAMES])
    def _compute_total(self):
        for rec in self:
            total = 0.0
            for denom in DENOM_NAMES:
                total += getattr(rec, f'qty_{denom}', 0) * DENOM_FACE[denom]
            rec.total_amount = total

    # ==================================================================
    # Constraints
    # ==================================================================
    @api.constrains('movement_type', 'from_location_id', 'to_location_id')
    def _check_required_locations(self):
        Bank = lambda l: l and l.location_type == 'bank'
        Internal = lambda l: l and l.location_type in (
            'till', 'bag', 'event_till'
        )
        for rec in self:
            mtype = rec.movement_type
            if mtype == 'change_request':
                if not Internal(rec.from_location_id):
                    raise ValidationError(_(
                        "Change Request must be FROM a Till, Bag, or Event Till."))
                if not Bank(rec.to_location_id):
                    raise ValidationError(_(
                        "Change Request must be TO the Bank."))
            elif mtype == 'change_order':
                if not Bank(rec.from_location_id):
                    raise ValidationError(_(
                        "Change Order must be FROM the Bank."))
                if not Internal(rec.to_location_id):
                    raise ValidationError(_(
                        "Change Order must be TO a Till, Bag, or Event Till."))
            elif mtype == 'till_deposit':
                if not Internal(rec.from_location_id):
                    raise ValidationError(_(
                        "Till Deposit must be FROM a Till, Bag, or Event Till."))
                if not Bank(rec.to_location_id):
                    raise ValidationError(_(
                        "Till Deposit must be TO the Bank."))
            elif mtype == 'bank_deposit':
                if not Bank(rec.from_location_id):
                    raise ValidationError(_(
                        "Bank Deposit must be FROM the Bank."))
                if rec.to_location_id:
                    raise ValidationError(_(
                        "Bank Deposit goes to the Operating Checking account, "
                        "not to another cash location. Clear 'To Location'."))
            elif mtype == 'bank_stock':
                if not Bank(rec.to_location_id):
                    raise ValidationError(_(
                        "Bank Stock-Up must be TO the Bank."))
                if rec.from_location_id:
                    raise ValidationError(_(
                        "Bank Stock-Up comes from the Operating Checking "
                        "account, not from another cash location. "
                        "Clear 'From Location'."))

    @api.constrains('total_amount')
    def _check_amount_positive(self):
        for rec in self:
            if rec.state == 'posted' and rec.total_amount <= 0:
                raise ValidationError(_(
                    "Posted movements must have a positive total amount."))

    # ==================================================================
    # Defaults — sensible to_location_id / from_location_id
    # ==================================================================
    @api.onchange('movement_type')
    def _onchange_movement_type(self):
        Loc = self.env['elks.cash.location']
        bank = Loc.search([('location_type', '=', 'bank')], limit=1)
        mtype = self.movement_type
        if mtype == 'change_request':
            self.to_location_id = bank
            if self.from_location_id and self.from_location_id.is_bank:
                self.from_location_id = False
        elif mtype == 'change_order':
            self.from_location_id = bank
            if self.to_location_id and self.to_location_id.is_bank:
                self.to_location_id = False
        elif mtype == 'till_deposit':
            self.to_location_id = bank
            if self.from_location_id and self.from_location_id.is_bank:
                self.from_location_id = False
        elif mtype == 'bank_deposit':
            self.from_location_id = bank
            self.to_location_id = False
        elif mtype == 'bank_stock':
            self.to_location_id = bank
            self.from_location_id = False

    # ==================================================================
    # State actions
    # ==================================================================
    def action_post(self):
        for rec in self:
            if rec.state == 'cancelled':
                raise UserError(_(
                    "Cannot post a cancelled movement. Reset to draft first."))
            if rec.state == 'posted':
                continue
            if rec.total_amount <= 0:
                raise UserError(_(
                    "Cannot post a movement with zero or negative total."))
            if rec.posts_journal:
                rec._create_journal_entry()
            rec.state = 'posted'
        return True

    def action_draft(self):
        for rec in self:
            if rec.journal_entry_id and rec.journal_entry_id.state == 'posted':
                raise UserError(_(
                    "Cancel or reverse the linked journal entry "
                    "(%s) before resetting this movement to draft.",
                    rec.journal_entry_id.name,
                ))
            rec.state = 'draft'
        return True

    def action_cancel(self):
        for rec in self:
            if rec.journal_entry_id and rec.journal_entry_id.state == 'posted':
                raise UserError(_(
                    "Cancel or reverse the linked journal entry "
                    "(%s) before cancelling this movement.",
                    rec.journal_entry_id.name,
                ))
            rec.state = 'cancelled'
        return True

    # ==================================================================
    # Journal entry creation (bank_deposit + bank_stock only)
    # ==================================================================
    def _create_journal_entry(self):
        """Create the GL journal entry for bank_deposit / bank_stock.

        bank_deposit: Dr 10100 Operating Checking / Cr 10000 Petty Cash
        bank_stock:   Dr 10000 Petty Cash         / Cr 10100 Operating Checking
        """
        self.ensure_one()
        Account = self.env['elks.account']
        bank_acct = Account.search([('code', '=', _BANK_GL_CODE)], limit=1)
        check_acct = Account.search([('code', '=', _CHECKING_GL_CODE)], limit=1)
        if not bank_acct:
            raise UserError(_(
                "GL account %s (Petty Cash / Bank) not found in the Chart of "
                "Accounts.", _BANK_GL_CODE))
        if not check_acct:
            raise UserError(_(
                "GL account %s (Operating Checking) not found in the Chart "
                "of Accounts.", _CHECKING_GL_CODE))

        amount = self.total_amount
        memo = self._journal_entry_memo()

        if self.movement_type == 'bank_deposit':
            dr_acct, cr_acct = check_acct, bank_acct
        elif self.movement_type == 'bank_stock':
            dr_acct, cr_acct = bank_acct, check_acct
        else:
            return False  # Not a JE-posting movement; shouldn't get here.

        entry = self.env['elks.journal.entry'].create({
            'date': fields.Date.to_date(self.move_date),
            'memo': memo,
            'line_ids': [
                (0, 0, {
                    'account_id': dr_acct.id,
                    'debit': amount,
                    'credit': 0.0,
                    'name': memo,
                }),
                (0, 0, {
                    'account_id': cr_acct.id,
                    'debit': 0.0,
                    'credit': amount,
                    'name': memo,
                }),
            ],
        })
        # Post if balanced.
        if hasattr(entry, 'action_post') and entry.is_balanced:
            entry.action_post()
        self.journal_entry_id = entry.id
        return entry

    def _journal_entry_memo(self):
        self.ensure_one()
        if self.movement_type == 'bank_deposit':
            return _("Bank deposit %(slip)s — cash from safe to checking.",
                     slip=self.slip_number or '')
        if self.movement_type == 'bank_stock':
            return _("Bank stock-up %(slip)s — change-buy from checking.",
                     slip=self.slip_number or '')
        return self.name or _("Cash movement")

    # ==================================================================
    # Reports
    # ==================================================================
    def action_print_slip(self):
        """Print the slip for this movement (or blank if draft)."""
        self.ensure_one()
        report_ref = {
            'change_request': 'elksfrs.action_report_change_slip',
            'change_order':   'elksfrs.action_report_change_slip',
            'till_deposit':   'elksfrs.action_report_till_deposit_slip',
            'bank_deposit':   'elksfrs.action_report_bank_deposit_slip',
            'bank_stock':     'elksfrs.action_report_bank_deposit_slip',
        }.get(self.movement_type)
        if not report_ref:
            raise UserError(_("No slip report configured for this movement type."))
        return self.env.ref(report_ref).report_action(self)
