# -*- coding: utf-8 -*-
"""DEPRECATED — replaced in 19.0.2.0.

This file extended elks.cash.on.hand with register_line_ids /
transfer_ids One2manys and aggregated denomination computes.  The
register chain it depended on (elks.register, elks.register.count.line,
elks.register.transfer) is gone; this extension is therefore no longer
imported.

elks.cash.on.hand reverts to the simple lump-sum form defined in
elks_dues_deposit.py — appropriate for the dues-batch reconciliation
audit it was originally written for.  Denomination-level counting is
handled in the new elks.cash.count model.

The register_line_ids / transfer_ids columns on the elks_cash_on_hand
table are left intact for rollback safety; drop them manually after
verifying the new system:

    ALTER TABLE elks_cash_on_hand DROP COLUMN IF EXISTS register_count CASCADE;
    ALTER TABLE elks_cash_on_hand DROP COLUMN IF EXISTS total_starter_banks CASCADE;
    ALTER TABLE elks_cash_on_hand DROP COLUMN IF EXISTS total_excess CASCADE;

Safe to delete from the repo.
"""
