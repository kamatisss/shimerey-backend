"""
A customer's shop cart, saved on the server so it follows them between devices.

  GET /api/cart/   the cart, each line with the product as the shop lists it (archived items left out)
  PUT /api/cart/   {"items": [{"id": 12, "quantity": 3}, ...]} replaces it; answers like GET

The browser keeps its own copy (it works offline and shows at once); see frontend
context/CartContext.jsx for how the two are reconciled. Only ids and quantities are stored: prices
and stock always come from the catalog, and checkout re-checks both. A placed order empties the
saved cart (clear_saved_cart, called by checkout).
"""
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from .models import InventoryItem, SavedCart

MAX_LINES = 60     # different products in one cart
MAX_QUANTITY = 999


def clean_items(raw):
    """[{id, quantity}] from the request → a tidy list (known products only, merged, capped).
    Raises ValueError with a readable reason for a malformed request."""
    if not isinstance(raw, list):
        raise ValueError('items must be a list.')
    if len(raw) > MAX_LINES:
        raise ValueError(f'A cart can hold at most {MAX_LINES} different items.')
    merged = {}
    for line in raw:
        if not isinstance(line, dict):
            raise ValueError('Each item needs an id and a quantity.')
        try:
            item_id = int(line.get('id'))
            quantity = int(line.get('quantity'))
        except (TypeError, ValueError):
            raise ValueError('Each item needs an id and a quantity.')
        if quantity <= 0:
            continue
        merged[item_id] = min(MAX_QUANTITY, merged.get(item_id, 0) + quantity)
    known = set(InventoryItem.objects.filter(id__in=merged).values_list('id', flat=True))
    return [{'id': i, 'quantity': q} for i, q in merged.items() if i in known]


def _payload(cart):
    from django.db.models import Avg, Count, Q
    from .serializers import InventoryItemSerializer

    items = cart.items if cart else []
    public_reviews = Q(reviews__is_public=True)
    products = {p.id: p for p in InventoryItem.objects.filter(
        id__in=[line['id'] for line in items], is_active=True,
    ).annotate(avg_rating=Avg('reviews__rating', filter=public_reviews),
               review_count=Count('reviews', filter=public_reviews))}
    lines = [{'product': InventoryItemSerializer(products[line['id']]).data, 'quantity': line['quantity']}
             for line in items if line['id'] in products]
    return {'items': lines, 'updated_at': cart.updated_at if cart else None}


@api_view(['GET', 'PUT'])
@permission_classes([IsAuthenticated])
@throttle_classes([ScopedRateThrottle])
def my_cart(request):
    if request.method == 'GET':
        return Response(_payload(SavedCart.objects.filter(user=request.user).first()))
    try:
        items = clean_items(request.data.get('items'))
    except ValueError as e:
        return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    cart, _ = SavedCart.objects.update_or_create(user=request.user, defaults={'items': items})
    return Response(_payload(cart))


my_cart.cls.throttle_scope = 'cart_saves'


def clear_saved_cart(user):
    """After an order is placed: the cart became that order."""
    if user is not None and user.is_authenticated:
        SavedCart.objects.filter(user=user).update(items=[])
