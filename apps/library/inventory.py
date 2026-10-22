"""
Stock changes — one place, so every change is recorded (StockMovement) and none can overwrite another.

Staff never type a new stock number over the old one (an order placed while the edit form was open
used to be silently undone). They record what happened instead:
  restock  +N   a delivery arrived
  damaged  −N   plants died, items broke or went missing
  count    =N   a stock count found N on hand (the difference is recorded)
Orders record their own movements: sale (checkout) and released (cancelled or expired order).
Every change runs under a row lock on the item.
"""
import re

from django.db import transaction
from django.db.models import F

from .models import InventoryItem, StockMovement

STAFF_REASONS = ('restock', 'damaged', 'count')
MODEL_NAME = re.compile(r'^[a-z0-9][a-z0-9_-]{0,90}\.glb$')


class StockError(ValueError):
    pass


def record(item, change, reason, user=None, note=''):
    """Log a change that was already applied to `item` (its stock_quantity is the new total)."""
    StockMovement.objects.create(item=item, change=change, quantity_after=item.stock_quantity,
                                 reason=reason, user=user, note=note[:200])


def adjust(item_id, reason, quantity, user, note=''):
    """Apply a staff stock change. Returns the movement; raises StockError with a readable message."""
    if reason not in STAFF_REASONS:
        raise StockError('Choose restock, damaged or stock count.')
    try:
        quantity = int(quantity)
    except (TypeError, ValueError):
        raise StockError('Enter a whole number of units.')
    if quantity < 0 or quantity > 100000:
        raise StockError('Enter a number from 0 to 100,000.')
    with transaction.atomic():
        item = InventoryItem.objects.select_for_update().get(pk=item_id)
        if reason == 'restock':
            if quantity == 0:
                raise StockError('Enter how many units arrived.')
            change = quantity
        elif reason == 'damaged':
            if quantity == 0:
                raise StockError('Enter how many units were damaged or lost.')
            if quantity > item.stock_quantity:
                raise StockError(f'Only {item.stock_quantity} in stock: you can remove at most that many.')
            change = -quantity
        else:   # count: the number actually on hand
            change = quantity - item.stock_quantity
            if change == 0:
                raise StockError(f'Stock is already {quantity}. Nothing to change.')
        InventoryItem.objects.filter(pk=item.pk).update(stock_quantity=F('stock_quantity') + change)
        item.refresh_from_db(fields=['stock_quantity'])
        movement = StockMovement.objects.create(item=item, change=change, quantity_after=item.stock_quantity,
                                                reason=reason, user=user, note=str(note or '').strip()[:200])
    return movement
