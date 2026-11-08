"""
Customer addresses — Quezon Province only, always with a map pin.

An address is picked, not typed: town and barangay from the official PSGC list (data/quezon_areas.json,
see the build note in that file's `sources`), then the street / house and an optional landmark, then
the customer drops a pin on their house. The pin is what the crew navigates to and what the
geofenced check-in measures against; the text is for people.

  GET  /api/address-areas/          anyone: the towns and barangays (+ map centres) for the pickers
  CRUD /api/addresses/              a customer's address book (SavedAddress)

clean_address(payload) is the one gate every order, booking and saved address goes through: codes must
exist and belong together, and the pin must be inside the chosen town (a little margin for the edges).
"""
import json
from functools import lru_cache
from pathlib import Path

from rest_framework import permissions, serializers, status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .models import SavedAddress

AREAS_FILE = Path(__file__).resolve().parent / 'data' / 'quezon_areas.json'
PROVINCE = 'Quezon'
PIN_MARGIN = 0.03          # degrees (~3 km) around a town's box: OSM boundaries and pins at the edge
MAX_SAVED = 10             # address book entries per customer
SOURCES = ('pin', 'gps')   # dragged on the map / the phone's location (then usually nudged)


class AddressError(ValueError):
    """A customer-readable reason the address can't be used."""


@lru_cache(maxsize=1)
def areas():
    with open(AREAS_FILE, encoding='utf-8') as f:
        data = json.load(f)
    data['by_city'] = {c['code']: c for c in data['cities']}
    data['by_barangay'] = {b['code']: (c, b) for c in data['cities'] for b in c['barangays']}
    return data


def _text(value, limit, name, required=False):
    text = ' '.join(str(value or '').split())
    if required and len(text) < 3:
        raise AddressError(f'Enter the {name}.')
    if len(text) > limit:
        raise AddressError(f'The {name} is too long (at most {limit} characters).')
    return text


def _coord(value, name):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise AddressError('Drop the pin on your house on the map.')
    if number != number:   # NaN
        raise AddressError('Drop the pin on your house on the map.')
    return round(number, 6)


def clean_address(payload):
    """
    {city_code, barangay_code, street, landmark?, lat, lng, source?} → a clean address detail dict
    (with names, for display and history). Raises AddressError with a message for the customer.
    """
    if not isinstance(payload, dict):
        raise AddressError('Choose your town and barangay, and pin your location on the map.')
    data = areas()
    city = data['by_city'].get(str(payload.get('city_code') or ''))
    if city is None:
        raise AddressError(f'Choose your town or city. We deliver and build within {PROVINCE} Province.')
    found = data['by_barangay'].get(str(payload.get('barangay_code') or ''))
    if found is None or found[0]['code'] != city['code']:
        raise AddressError(f"Choose your barangay in {city['name']}.")
    barangay = found[1]
    street = _text(payload.get('street'), 200, 'house number and street (or purok / sitio)', required=True)
    landmark = _text(payload.get('landmark'), 200, 'landmark')
    lat, lng = _coord(payload.get('lat'), 'latitude'), _coord(payload.get('lng'), 'longitude')
    south, west, north, east = city['bbox']
    if not (south - PIN_MARGIN <= lat <= north + PIN_MARGIN and west - PIN_MARGIN <= lng <= east + PIN_MARGIN):
        raise AddressError(f"The pin is outside {city['name']}. Move it onto your house, or check the town you chose.")
    source = payload.get('source') if payload.get('source') in SOURCES else 'pin'
    return {
        'province': PROVINCE,
        'city_code': city['code'], 'city': city['name'],
        'barangay_code': barangay['code'], 'barangay': barangay['name'],
        'street': street, 'landmark': landmark,
        'lat': lat, 'lng': lng, 'source': source,
    }


def format_address(detail):
    """The readable one-line address used everywhere text is shown (lists, emails, PDFs, exports)."""
    text = f"{detail['street']}, Brgy. {detail['barangay']}, {detail['city']}, {detail.get('province', PROVINCE)}"
    return f"{text} · near {detail['landmark']}" if detail.get('landmark') else text


def apply_to_order(order, detail):
    order.address_detail = detail
    order.customer_address = format_address(detail)
    order.delivery_lat, order.delivery_lng = detail['lat'], detail['lng']


def apply_to_booking(booking, detail):
    booking.address_detail = detail
    booking.service_address = format_address(detail)
    booking.site_lat, booking.site_lng = detail['lat'], detail['lng']


# ─────────────────────────────────────────────────────────────────────────────
# API
# ─────────────────────────────────────────────────────────────────────────────

@api_view(['GET'])
@permission_classes([AllowAny])
def address_areas(request):
    """The towns and barangays of the service area, with map centres (cached by browsers for a day)."""
    data = areas()
    response = Response({'province': data['province'], 'sources': data['sources'], 'cities': data['cities']})
    response['Cache-Control'] = 'public, max-age=86400'
    return response


class SavedAddressSerializer(serializers.ModelSerializer):
    city = serializers.SerializerMethodField()
    barangay = serializers.SerializerMethodField()
    text = serializers.SerializerMethodField()

    class Meta:
        model = SavedAddress
        fields = ['id', 'label', 'contact_name', 'contact_phone', 'city_code', 'city', 'barangay_code', 'barangay',
                  'street', 'landmark', 'lat', 'lng', 'is_default', 'text', 'updated_at']
        read_only_fields = ['id', 'is_default', 'updated_at']

    def _detail(self, obj):
        found = areas()['by_barangay'].get(obj.barangay_code)
        return found and {'city': found[0]['name'], 'barangay': found[1]['name']}

    def get_city(self, obj):
        return (self._detail(obj) or {}).get('city', '')

    def get_barangay(self, obj):
        return (self._detail(obj) or {}).get('barangay', '')

    def get_text(self, obj):
        found = self._detail(obj)
        if not found:
            return obj.street
        return format_address({**found, 'street': obj.street, 'landmark': obj.landmark})

    def validate(self, attrs):
        current = {f: getattr(self.instance, f) for f in ('city_code', 'barangay_code', 'street', 'landmark', 'lat', 'lng')} \
            if self.instance else {}
        try:
            detail = clean_address({**current, **attrs})
        except AddressError as e:
            raise serializers.ValidationError({'address': str(e)})
        from .views_api import normalize_ph_mobile
        phone = attrs.get('contact_phone')
        if phone:
            attrs['contact_phone'] = normalize_ph_mobile(phone)
            if not attrs['contact_phone']:
                raise serializers.ValidationError({'contact_phone': 'Enter a valid PH mobile number, e.g. 09171234567.'})
        attrs['label'] = _text(attrs.get('label', getattr(self.instance, 'label', 'Home')), 40, 'label') or 'Home'
        for f in ('street', 'landmark', 'lat', 'lng'):
            attrs[f] = detail[f]
        return attrs


class SavedAddressViewSet(viewsets.ModelViewSet):
    """A customer's own address book. The first address becomes the default; one default at a time."""
    serializer_class = SavedAddressSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return SavedAddress.objects.filter(user=self.request.user)

    def create(self, request, *args, **kwargs):
        if self.get_queryset().count() >= MAX_SAVED:
            return Response({'error': f'Your address book is full ({MAX_SAVED}). Remove one to add another.'},
                            status=status.HTTP_400_BAD_REQUEST)
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        first = not self.get_queryset().exists()
        serializer.save(user=self.request.user, is_default=first)

    def perform_destroy(self, instance):
        was_default = instance.is_default
        instance.delete()
        if was_default:
            nxt = self.get_queryset().first()
            if nxt:
                nxt.is_default = True
                nxt.save(update_fields=['is_default'])

    @action(detail=True, methods=['post'])
    def make_default(self, request, pk=None):
        address = self.get_object()
        self.get_queryset().exclude(pk=address.pk).update(is_default=False)
        address.is_default = True
        address.save(update_fields=['is_default'])
        return Response(self.get_serializer(address).data)


def address_from_request(data):
    """The structured `address` sent with an order or booking (a dict, or a JSON string from a form)."""
    raw = data.get('address')
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    return clean_address(raw)
