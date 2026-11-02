"""Strict Purchase normalization; unsupported accounting stays in review."""
from decimal import Decimal, InvalidOperation


def normalize_purchase(row, currency):
    if row.get('CurrencyRef', {}).get('value', currency) != currency:
        raise ValueError('Foreign currency expenses require manual accounting review.')
    if row.get('Credit'):
        raise ValueError('Credit card credits require manual accounting review.')
    if row.get('TxnTaxDetail', {}).get('TotalTax', 0):
        raise ValueError('Tax-bearing expenses require manual accounting review.')
    lines = []
    for line in row.get('Line', []):
        if line.get('DetailType') in ('DescriptionOnly', 'SubTotalLineDetail'):
            continue
        detail = line.get('AccountBasedExpenseLineDetail')
        if not detail or detail.get('BillableStatus') == 'Billable':
            raise ValueError('Item-based or billable expenses require manual accounting review.')
        try:
            amount = Decimal(str(line['Amount']))
        except (KeyError, InvalidOperation):
            raise ValueError('Missing or invalid expense line amount.') from None
        if not amount.is_finite() or amount <= 0:
            raise ValueError('Expense amounts must be finite and positive.')
        account = detail.get('AccountRef', {}).get('value')
        if not account:
            raise ValueError('Missing expense account reference.')
        lines.append({'account': str(account), 'amount': float(amount),
                      'memo': line.get('Description') or 'QuickBooks expense'})
    total = Decimal(str(row.get('TotalAmt', 0)))
    if not total.is_finite() or not lines or abs(sum(Decimal(str(x['amount'])) for x in lines) - total) > Decimal('0.004'):
        raise ValueError('Expense lines do not balance to the payment total.')
    if not row.get('AccountRef', {}).get('value') or not row.get('TxnDate'):
        raise ValueError('Missing payment account or transaction date.')
    return lines
