"""
Customer reviews — landing page highlights and office moderation.

Both review types are verified by construction: a ServiceReview needs a
signed-off booking, a ProductReview a delivered order item.

Endpoints
  GET   /api/review-highlights/?source=all|service|product   anyone: carousel cards + honest summary
  GET   /api/review-moderation/?source=&status=&rating=&q=&page=   office admin: every review
  PATCH /api/review-moderation/<service|product>/<id>/       office admin: is_hidden, is_featured, reply

Rules
  * `is_public` is the customer's choice. Staff can hide a public review (spam, abuse,
    personal data) but can never publish a private one, and can only feature public ones.
  * Rating summaries count every review the customer made public, hidden ones included,
    so moderation can't be used to inflate the average.
"""
import re

from django.db.models import Avg, Count, Q
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from . import notifications
from .models import ProductReview, ServiceReview
from .portfolio import _media_url, city_from_address, public_media
from .serializers import public_reviewer_name
from .views_api import IsOfficeAdmin

HIGHLIGHT_LIMIT = 12
PAGE_SIZE = 20
MAX_REPLY = 1000
SOURCES = {'service': ServiceReview, 'product': ProductReview}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def is_showcase_quality(comment):
    """Enough real words to be worth featuring — filters test input like 'drdfdfdfdg'."""
    text = (comment or '').strip()
    words = re.findall(r"[A-Za-zÀ-ÿñÑ']+", text)
    if len(text) < 20 or len(words) < 4:
        return False
    letters = sum(len(w) for w in words)
    vowels = sum(1 for ch in text.lower() if ch in 'aeiouáéíóúñ')
    return letters / len(text) >= 0.6 and vowels / max(letters, 1) >= 0.25


def _normalize(comment):
    return re.sub(r'\W+', ' ', comment.lower()).strip()


def _summary(model):
    """Average + 1–5★ distribution over every review the customer made public."""
    qs = model.objects.filter(is_public=True)
    agg = qs.aggregate(avg=Avg('rating'), n=Count('id'))
    dist = dict(qs.values_list('rating').annotate(c=Count('id')))
    return {
        'average': round(agg['avg'], 1) if agg['avg'] else None,
        'count': agg['n'],
        'distribution': {str(star): dist.get(star, 0) for star in range(5, 0, -1)},
    }


def _service_card(review, request):
    booking = review.booking
    project = getattr(booking, 'portfolio', None)
    consent = getattr(project, 'consent', None) if project else None
    link = image = None
    # Link the published project page only when the customer let it carry their review
    if project and project.status == 'published' and consent and consent.status == 'granted' and consent.include_review:
        link = f'/projects/{project.slug}'
        visible = public_media(project)
        cover = project.cover if project.cover_id and visible.filter(pk=project.cover_id).exists() else visible.first()
        image = _media_url(request, cover.image) if cover else None
    return {
        'kind': 'project',
        'label': booking.get_service_type_display(),
        'city': city_from_address(booking.service_address),
        'image_url': image,
        'link': link,
    }


def display_name(name):
    """Catalog slugs like 'volcanic_rock' → 'Volcanic rock'; proper names are left alone."""
    text = name.replace('_', ' ').strip()
    return text[:1].upper() + text[1:] if text == text.lower() else text


def _product_card(review, request):
    item = review.product
    image = None
    if item:
        image = _media_url(request, item.thumbnail) if item.thumbnail else (item.image_url or None)
    return {
        'kind': 'purchase',
        'label': display_name(item.name) if item else 'Garden product',
        'city': '',
        'image_url': image,
        'link': f'/shop?product={item.id}' if item else None,
    }


def _card(source, review, request):
    return {
        'id': review.id,
        'source': source,
        'rating': review.rating,
        'comment': review.comment.strip(),
        'name': public_reviewer_name(review.user),
        'date': review.created_at.date(),
        'is_featured': review.is_featured,
        'reply': review.reply,
        'replied_at': review.replied_at,
        'context': (_service_card if source == 'service' else _product_card)(review, request),
    }


def _showcase(source, request, limit):
    model = SOURCES[source]
    qs = (model.objects
          .filter(is_public=True, is_hidden=False)
          .filter(Q(is_featured=True) | Q(rating__gte=4))
          .exclude(user__is_staff=True).exclude(user__is_superuser=True)
          .select_related('user')
          .order_by('-is_featured', '-created_at'))
    if source == 'service':
        qs = qs.select_related('booking', 'booking__portfolio', 'booking__portfolio__consent', 'booking__portfolio__cover')
    else:
        qs = qs.select_related('product')
    cards, seen = [], set()
    for review in qs[:300]:
        key = _normalize(review.comment)
        # Featured reviews were chosen by a person; the quality filter is for the automatic picks
        if key in seen or not (review.is_featured or is_showcase_quality(review.comment)):
            continue
        seen.add(key)
        cards.append(_card(source, review, request))
        if len(cards) >= limit:
            break
    return cards


# ─────────────────────────────────────────────────────────────────────────────
# Public
# ─────────────────────────────────────────────────────────────────────────────

@api_view(['GET'])
@permission_classes([AllowAny])
def review_highlights(request):
    source = request.query_params.get('source', 'all')
    if source not in ('all', 'service', 'product'):
        return Response({'error': "source must be 'all', 'service' or 'product'."}, status=status.HTTP_400_BAD_REQUEST)

    if source == 'all':
        service = _showcase('service', request, HIGHLIGHT_LIMIT)
        product = _showcase('product', request, HIGHLIGHT_LIMIT)
        # Featured first, then alternate projects and purchases so both stories show
        featured = sorted([c for c in service + product if c['is_featured']], key=lambda c: c['date'], reverse=True)
        rest_s = [c for c in service if not c['is_featured']]
        rest_p = [c for c in product if not c['is_featured']]
        mixed = []
        for i in range(max(len(rest_s), len(rest_p))):
            mixed += rest_s[i:i + 1] + rest_p[i:i + 1]
        results = (featured + mixed)[:HIGHLIGHT_LIMIT]
    else:
        results = _showcase(source, request, HIGHLIGHT_LIMIT)

    return Response({
        'summary': {'service': _summary(ServiceReview), 'product': _summary(ProductReview)},
        'results': results,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Office moderation
# ─────────────────────────────────────────────────────────────────────────────

def _admin_row(source, review):
    user = review.user
    if source == 'service':
        subject = f"{review.booking.get_service_type_display()} · Booking #{review.booking_id}"
    else:
        name = review.product.name if review.product else 'Deleted item'
        subject = f"{name} · Order #{review.order_item.order_id}"
    return {
        'id': review.id,
        'source': source,
        'rating': review.rating,
        'comment': review.comment,
        'customer': user.get_full_name() or user.username,
        'public_name': public_reviewer_name(user),
        'subject': subject,
        'created_at': review.created_at,
        'is_public': review.is_public,
        'is_hidden': review.is_hidden,
        'is_featured': review.is_featured,
        'reply': review.reply,
        'replied_at': review.replied_at,
        'showcase_quality': is_showcase_quality(review.comment),
    }


STATUS_FILTERS = {
    'live':        Q(is_public=True, is_hidden=False),
    'featured':    Q(is_featured=True),
    'hidden':      Q(is_hidden=True),
    'private':     Q(is_public=False),
    'needs_reply': Q(rating__lte=3, reply=''),
}


@api_view(['GET'])
@permission_classes([IsOfficeAdmin])
def review_moderation_list(request):
    params = request.query_params
    source = params.get('source', 'all')
    sources = [source] if source in SOURCES else list(SOURCES)

    filters = Q()
    if params.get('status') in STATUS_FILTERS:
        filters &= STATUS_FILTERS[params['status']]
    if params.get('rating', '').isdigit():
        filters &= Q(rating=int(params['rating']))
    query = params.get('q', '').strip()

    rows = []
    for src in sources:
        qs = SOURCES[src].objects.filter(filters).select_related('user')
        qs = qs.select_related('booking') if src == 'service' else qs.select_related('product', 'order_item')
        if query:
            search = (Q(comment__icontains=query) | Q(user__first_name__icontains=query)
                      | Q(user__last_name__icontains=query))
            if src == 'service' and query.isdigit():
                search |= Q(booking_id=int(query))
            if src == 'product':
                search |= Q(product__name__icontains=query)
                if query.isdigit():
                    search |= Q(order_item__order_id=int(query))
            qs = qs.filter(search)
        rows += [(src, r) for r in qs]
    rows.sort(key=lambda pair: pair[1].created_at, reverse=True)

    try:
        page = max(1, int(params.get('page', 1)))
    except ValueError:
        page = 1
    pages = max(1, -(-len(rows) // PAGE_SIZE))
    page = min(page, pages)
    chunk = rows[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]

    needs_reply = sum(m.objects.filter(STATUS_FILTERS['needs_reply']).count() for m in SOURCES.values())
    return Response({
        'count': len(rows), 'page': page, 'pages': pages,
        'needs_reply': needs_reply,
        'results': [_admin_row(src, r) for src, r in chunk],
    })


@api_view(['PATCH'])
@permission_classes([IsOfficeAdmin])
def moderate_review(request, source, pk):
    model = SOURCES.get(source)
    if model is None:
        return Response({'error': 'Unknown review type.'}, status=status.HTTP_404_NOT_FOUND)
    review = model.objects.select_related('user').filter(pk=pk).first()
    if review is None:
        return Response({'error': 'Review not found.'}, status=status.HTTP_404_NOT_FOUND)

    data = request.data
    if 'is_hidden' in data:
        review.is_hidden = bool(data['is_hidden'])
        if review.is_hidden:
            review.is_featured = False
    if 'is_featured' in data:
        want = bool(data['is_featured'])
        if want and not review.is_public:
            return Response({'error': 'The customer chose to keep this review private, so it cannot be featured.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if want and review.is_hidden:
            return Response({'error': 'Unhide the review before featuring it.'}, status=status.HTTP_400_BAD_REQUEST)
        review.is_featured = want

    reply_changed = False
    if 'reply' in data:
        reply = str(data['reply'] or '').strip()
        if len(reply) > MAX_REPLY:
            return Response({'error': f'Replies are limited to {MAX_REPLY} characters.'}, status=status.HTTP_400_BAD_REQUEST)
        reply_changed = bool(reply) and reply != review.reply
        review.reply = reply
        review.replied_at = timezone.now() if reply else None

    review.save()
    if reply_changed:
        what = (f"your {review.booking.get_service_type_display().lower()} project" if source == 'service'
                else display_name(review.product.name) if review.product else 'your purchase')
        notifications.notify([review.user], 'review_reply', f"Garden Studio replied to your review of {what}: \"{review.reply[:140]}\"",
                             link=notifications.MY_BOOKINGS if source == 'service' else notifications.MY_ORDERS)
    return Response(_admin_row(source, review))
