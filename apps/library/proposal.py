"""
Garden design proposal: a printable PDF of one saved design, for the customer to keep, show their
family or take to their subdivision for approval.

  POST /api/designs/<id>/proposal/   owner only. multipart, all optional:
        render        the 3D view of the design (JPEG/PNG), made in the browser
        photo_before  their yard photo   } from the AI Designer's photo overlay
        photo_after   the same with the garden drawn in }
        credits       one line crediting third-party 3D models shown in the pictures
     → application/pdf

Pages: cover (picture, key facts) · site plan to scale + item list · estimate, care, next steps.
Prices are the ones saved with the design (what the customer saw), dated and valid VALID_DAYS days.
Uploaded pictures are re-encoded: no EXIF (a phone photo's GPS position never reaches the PDF).
"""
import io
import os
from datetime import timedelta
from xml.sax.saxutils import escape

from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from .design_items import lot_items
from .models import BusinessProfile, GardenDesign, InventoryItem

VALID_DAYS = 30
MAX_UPLOAD = 8 * 1024 * 1024
MAX_PICTURE_SIDE = 1800
IMAGE_FIELDS = ('render', 'photo_before', 'photo_after')
SUN = dict(InventoryItem.SUNLIGHT_CHOICES)
WATER = {'low': 'Low (drought tolerant)', 'moderate': 'Moderate', 'high': 'High (keep moist)'}
CATEGORY = {'plant': 'Plant', 'hardscape': 'Garden feature', 'furniture': 'Seating'}


class ProposalThrottle(ScopedRateThrottle):
    scope = 'design_proposals'


# ── Fonts: Helvetica has no peso sign, so use a TrueType font that does ─────────────────────────
_FONT_CANDIDATES = [
    ('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'),
    ('C:/Windows/Fonts/arial.ttf', 'C:/Windows/Fonts/arialbd.ttf'),
    ('C:/Windows/Fonts/segoeui.ttf', 'C:/Windows/Fonts/segoeuib.ttf'),
    ('/System/Library/Fonts/Supplemental/Arial.ttf', '/System/Library/Fonts/Supplemental/Arial Bold.ttf'),
]
_fonts = None


def fonts():
    """(regular, bold, peso): a TTF pair with ₱ when one is installed, else Helvetica and 'PHP '."""
    global _fonts
    if _fonts:
        return _fonts
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    for regular, bold in _FONT_CANDIDATES:
        if not (os.path.exists(regular) and os.path.exists(bold)):
            continue
        try:
            face = TTFont('GSProposal', regular)
            if 0x20B1 not in face.face.charToGlyph:
                continue
            pdfmetrics.registerFont(face)
            pdfmetrics.registerFont(TTFont('GSProposal-Bold', bold))
            # So <b> inside a paragraph switches to the bold file
            pdfmetrics.registerFontFamily('GSProposal', normal='GSProposal', bold='GSProposal-Bold',
                                          italic='GSProposal', boldItalic='GSProposal-Bold')
            _fonts = ('GSProposal', 'GSProposal-Bold', '₱')
            return _fonts
        except Exception:   # a damaged or unreadable font file: try the next one
            continue
    _fonts = ('Helvetica', 'Helvetica-Bold', 'PHP ')
    return _fonts


def clean_picture(upload):
    """An uploaded picture → (JPEG bytes, width, height), at most MAX_PICTURE_SIDE, without EXIF.
    Raises ValueError if it isn't a readable picture."""
    from PIL import Image, UnidentifiedImageError
    if upload.size > MAX_UPLOAD:
        raise ValueError('Each picture can be at most 8 MB.')
    try:
        img = Image.open(upload)
        img.load()
    except (UnidentifiedImageError, OSError):
        raise ValueError('One of the pictures could not be read.')
    if img.mode in ('RGBA', 'LA', 'P'):
        bg = Image.new('RGB', img.size, (255, 255, 255))
        rgba = img.convert('RGBA')
        bg.paste(rgba, mask=rgba.split()[-1])
        img = bg
    else:
        img = img.convert('RGB')
    img.thumbnail((MAX_PICTURE_SIDE, MAX_PICTURE_SIDE))
    out = io.BytesIO()
    img.save(out, 'JPEG', quality=85, optimize=True)   # a fresh file: no EXIF / GPS carried over
    return out.getvalue(), img.width, img.height


def _num(value, default=0.0):
    try:
        n = float(value)
        return n if n == n else default
    except (TypeError, ValueError):
        return default


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([ProposalThrottle])
def design_proposal(request, pk):
    design = get_object_or_404(GardenDesign, pk=pk)
    if design.user_id != request.user.id:
        return Response({'error': 'Design not found.'}, status=status.HTTP_404_NOT_FOUND)
    pictures = {}
    for field in IMAGE_FIELDS:
        upload = request.FILES.get(field)
        if upload:
            try:
                pictures[field] = clean_picture(upload)
            except ValueError as e:
                return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    credits = str(request.data.get('credits') or '')[:600]
    pdf = build_proposal(design, request.user, pictures, credits=credits)
    slug = ''.join(ch if ch.isalnum() else '-' for ch in (design.name or 'garden').lower()).strip('-')[:50] or 'garden'
    response = HttpResponse(pdf, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{slug}-proposal.pdf"'
    response['Access-Control-Expose-Headers'] = 'Content-Disposition'
    return response


def _site_plan(design, rows, items_by_id, width, max_height, font, bold):
    """The lot from above, to scale: back of the garden at the top, house side at the bottom (as the
    customer sees it in 3D). Beds and lawn, then every plant as its spread, numbered like the list."""
    from reportlab.graphics.shapes import Drawing, Rect, Circle, Ellipse, Line, String
    from reportlab.lib import colors

    dims = design.dimensions if isinstance(design.dimensions, dict) else {}
    lot_w = max(_num(dims.get('width'), 10), 1)
    lot_l = max(_num(dims.get('length'), 15), 1)
    top_pad, bottom_pad, side_pad = 22, 34, 14
    scale = min((width - 2 * side_pad) / lot_w, (max_height - top_pad - bottom_pad) / lot_l)
    pw, ph = lot_w * scale, lot_l * scale
    height = ph + top_pad + bottom_pad
    ox, oy = (width - pw) / 2, bottom_pad
    X = lambda x: ox + x * scale                 # noqa: E731  lot metres → drawing points
    Y = lambda z: oy + (lot_l - z) * scale       # noqa: E731  z = 0 (back) at the top

    d = Drawing(width, height)
    plan = dims.get('gardenPlan') if isinstance(dims.get('gardenPlan'), dict) else None
    lawn_green, bed_brown = colors.HexColor('#bbf7d0'), colors.HexColor('#e7d3b8')
    d.add(Rect(ox, oy, pw, ph, fillColor=bed_brown if plan else colors.HexColor('#ecfdf5'), strokeColor=None))
    if plan and isinstance(plan.get('lawn'), dict):
        g = plan['lawn']
        if g.get('shape') == 'ellipse':
            d.add(Ellipse(X(_num(g.get('cx'))), Y(_num(g.get('cz'))), _num(g.get('rx')) * scale, _num(g.get('rz')) * scale,
                          fillColor=lawn_green, strokeColor=None))
        else:
            x0, x1, z0, z1 = (_num(g.get(k)) for k in ('x0', 'x1', 'z0', 'z1'))
            d.add(Rect(X(x0), Y(z1), (x1 - x0) * scale, (z1 - z0) * scale, fillColor=lawn_green, strokeColor=None))
        e = plan.get('entry') if isinstance(plan.get('entry'), dict) else None
        if e:
            x0, x1, z0, z1 = (_num(e.get(k)) for k in ('x0', 'x1', 'z0', 'z1'))
            d.add(Rect(X(x0), Y(z1), (x1 - x0) * scale, (z1 - z0) * scale, fillColor=lawn_green, strokeColor=None))
    # 1 m grid, faint
    grid = colors.HexColor('#94a3b8')
    for i in range(1, int(lot_w) + (0 if lot_w == int(lot_w) else 1)):
        d.add(Line(X(i), oy, X(i), oy + ph, strokeColor=grid, strokeWidth=0.15, strokeOpacity=0.5))
    for i in range(1, int(lot_l) + (0 if lot_l == int(lot_l) else 1)):
        d.add(Line(ox, Y(i), ox + pw, Y(i), strokeColor=grid, strokeWidth=0.15, strokeOpacity=0.5))
    d.add(Rect(ox, oy, pw, ph, fillColor=None, strokeColor=colors.HexColor('#047857'), strokeWidth=1.2))

    # Items: number by list row, size by spread (a readable minimum)
    number = {str(r.get('plant_id')): i + 1 for i, r in enumerate(rows)}
    fills = {'plant': '#16a34a', 'hardscape': '#78716c', 'furniture': '#b45309'}
    placed = lot_items(design)   # AI and Studio items alike (design_items.py)
    for p in placed:
        if not isinstance(p, dict) or p.get('is_existing') or 'x' not in p or 'z' not in p:
            continue
        pid = str(p.get('plant_id'))
        item = items_by_id.get(pid)
        category = (item.category if item else 'plant') or 'plant'
        spread = (item.established_spread or (item.spacing_cm or 0) / 100 or (item.real_world_size if category != 'plant' else 0)) if item else 0
        r = max(5.5, (spread or 0.5) * scale / 2)
        cx, cy = X(_num(p['x'])), Y(_num(p['z']))
        fill = colors.HexColor(fills.get(category, '#16a34a'))
        if category == 'plant':
            d.add(Circle(cx, cy, r, fillColor=fill, fillOpacity=0.55, strokeColor=colors.white, strokeWidth=0.6))
        else:
            d.add(Rect(cx - r, cy - r, 2 * r, 2 * r, rx=2, ry=2, fillColor=fill, fillOpacity=0.6, strokeColor=colors.white, strokeWidth=0.6))
        if pid in number:
            d.add(String(cx, cy - 2.6, str(number[pid]), fontName=bold, fontSize=7.5, fillColor=colors.white, textAnchor='middle'))

    dark, mid = colors.HexColor('#0f172a'), colors.HexColor('#475569')
    d.add(String(ox + pw / 2, oy + ph + 8, 'BACK OF THE GARDEN', fontName=bold, fontSize=7.5, fillColor=mid, textAnchor='middle'))
    d.add(String(ox + pw / 2, oy - 13, 'HOUSE SIDE (where you stand)', fontName=bold, fontSize=7.5, fillColor=dark, textAnchor='middle'))
    d.add(String(ox + pw / 2, oy - 26, f'{lot_w:g} m wide × {lot_l:g} m deep · each square is 1 m', fontName=font, fontSize=7, fillColor=mid, textAnchor='middle'))
    return d


def build_proposal(design, user, pictures=None, credits='', now=None):
    """The proposal PDF as bytes. pictures: {field: (jpeg_bytes, w, h)} from clean_picture."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import (Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer,
                                    Table, TableStyle)

    pictures = pictures or {}
    now = now or timezone.now()
    font, bold, peso_sign = fonts()
    peso = lambda n: f'{peso_sign}{_num(n):,.0f}'   # noqa: E731
    arrow = '→' if font != 'Helvetica' else '-'   # Helvetica has no arrow
    biz = BusinessProfile.current()
    business = biz.name or 'Garden Studio'

    DARK, MID, LIGHT = colors.HexColor('#0f172a'), colors.HexColor('#475569'), colors.HexColor('#94a3b8')
    GREEN, GREEN_DARK, RULE, TINT = (colors.HexColor('#059669'), colors.HexColor('#047857'),
                                     colors.HexColor('#e2e8f0'), colors.HexColor('#f0fdf4'))
    S = lambda name, **kw: ParagraphStyle(name, fontName=kw.pop('fn', font), **kw)   # noqa: E731
    EYEBROW = S('eb', fn=bold, fontSize=8, textColor=GREEN, leading=10, spaceAfter=4)
    H1 = S('h1', fn=bold, fontSize=24, textColor=DARK, leading=28, spaceAfter=4)
    H2 = S('h2', fn=bold, fontSize=13, textColor=DARK, leading=16, spaceBefore=6, spaceAfter=6)
    BODY = S('body', fontSize=9.5, textColor=MID, leading=13.5)
    SMALL = S('small', fontSize=7.5, textColor=LIGHT, leading=10)
    CELL = S('cell', fontSize=8.5, textColor=DARK, leading=11)
    CELL_MUTED = S('cellm', fontSize=8, textColor=MID, leading=10.5)
    CELL_R = S('cellr', fontSize=8.5, textColor=DARK, leading=11, alignment=TA_RIGHT)
    HEAD = S('head', fn=bold, fontSize=7.5, textColor=MID, leading=10)
    HEAD_R = S('headr', fn=bold, fontSize=7.5, textColor=MID, leading=10, alignment=TA_RIGHT)
    FACT_L = S('factl', fn=bold, fontSize=7, textColor=LIGHT, leading=9)
    FACT_V = S('factv', fn=bold, fontSize=12, textColor=DARK, leading=15)
    CAPTION = S('cap', fn=bold, fontSize=8, textColor=MID, leading=10, spaceBefore=3)
    P = lambda text, style=BODY: Paragraph(text, style)   # noqa: E731

    # ── The design's numbers ──
    dims = design.dimensions if isinstance(design.dimensions, dict) else {}
    lot_w, lot_l = max(_num(dims.get('width'), 10), 1), max(_num(dims.get('length'), 15), 1)
    rows = [r for r in (design.plant_breakdown or []) if isinstance(r, dict)]
    ids = []
    for r in rows:
        try:
            ids.append(int(r.get('plant_id')))
        except (TypeError, ValueError):
            pass
    items_by_id = {str(i.id): i for i in InventoryItem.objects.filter(id__in=ids)}
    category_of = lambda r: (items_by_id[str(r.get('plant_id'))].category if str(r.get('plant_id')) in items_by_id   # noqa: E731
                             else r.get('category')) or 'plant'
    plants_qty = sum(int(_num(r.get('quantity'))) for r in rows if category_of(r) == 'plant')
    features_qty = sum(int(_num(r.get('quantity'))) for r in rows if category_of(r) != 'plant')
    estimate = design.cost_estimate if isinstance(design.cost_estimate, dict) else {}
    lines = [l for l in (estimate.get('lines') or []) if isinstance(l, dict) and _num(l.get('amount')) > 0]
    installed = any(l.get('key') in ('labour', 'site_prep', 'mobilization') for l in lines)
    total = _num(design.total_cost)
    priced_on = timezone.localtime(design.created_at).date()
    valid_until = priced_on + timedelta(days=VALID_DAYS)
    expired = timezone.localtime(now).date() > valid_until
    customer = escape(user.get_full_name() or user.username)
    ref = f'GS-{design.id:05d}'
    share = design.shares.filter(revoked_at__isnull=True).first() if hasattr(design, 'shares') else None
    site = settings.FRONTEND_URL.rstrip('/')
    fmt = lambda d: f'{d.day} {d:%B %Y}'   # noqa: E731  9 November 2026 (no leading zero, any OS)

    buf = io.BytesIO()
    margin = 1.7 * cm
    page_w, page_h = A4
    col = page_w - 2 * margin

    def chrome(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(GREEN_DARK)
        canvas.rect(0, page_h - 0.55 * cm, page_w, 0.55 * cm, stroke=0, fill=1)
        canvas.setFont(bold, 9)
        canvas.setFillColor(DARK)
        canvas.drawString(margin, page_h - 1.25 * cm, business)
        canvas.setFont(font, 8)
        canvas.setFillColor(LIGHT)
        canvas.drawRightString(page_w - margin, page_h - 1.25 * cm, f'Garden design proposal · {ref}')
        contact = ' · '.join(x for x in (biz.phone, biz.email, biz.service_area) if x)
        canvas.setFont(font, 7.5)
        canvas.drawString(margin, 1.0 * cm, contact[:140])
        canvas.drawRightString(page_w - margin, 1.0 * cm, f'Page {doc.page}')
        canvas.restoreState()

    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=margin, rightMargin=margin, topMargin=1.9 * cm,
                            bottomMargin=1.7 * cm, title=f'{design.name} – garden design proposal', author=business,
                            subject='Garden design proposal')
    story = []

    # ── Page 1: cover ──
    story += [P('GARDEN DESIGN PROPOSAL', EYEBROW), P(escape(design.name or 'Garden design'), H1),
              P(f'Prepared for <b>{customer}</b> · {fmt(timezone.localtime(now).date())} · Reference {ref}'), Spacer(1, 10)]

    def picture(field, max_w, max_h):
        data, w, h = pictures[field]
        k = min(max_w / w, max_h / h)
        img = Image(io.BytesIO(data), width=w * k, height=h * k)
        img.hAlign = 'CENTER'
        return img

    if 'photo_before' in pictures and 'photo_after' in pictures:
        half = (col - 8) / 2
        story.append(Table([[picture('photo_before', half, 9 * cm), picture('photo_after', half, 9 * cm)],
                            [P('YOUR YARD TODAY', CAPTION), P('WITH YOUR NEW GARDEN', CAPTION)]],
                           colWidths=[half + 4, half + 4],
                           style=[('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LEFTPADDING', (0, 0), (-1, -1), 0),
                                  ('RIGHTPADDING', (0, 0), (-1, -1), 4)]))
        if 'render' in pictures:
            story += [Spacer(1, 8), picture('render', col, 6 * cm), P('3D VIEW OF THE DESIGN', CAPTION)]
    elif 'render' in pictures:
        story += [picture('render', col, 11 * cm), P('3D VIEW OF THE DESIGN', CAPTION)]
    elif 'photo_after' in pictures:
        story += [picture('photo_after', col, 11 * cm), P('YOUR YARD WITH THE NEW GARDEN', CAPTION)]
    story.append(Spacer(1, 12))

    facts = [('YARD', f'{lot_w:g} × {lot_l:g} m'), ('PLANTS', str(plants_qty)),
             ('GARDEN FEATURES', str(features_qty)), ('ESTIMATED TOTAL', peso(total)),
             ('PRICES VALID UNTIL', fmt(valid_until))]
    fact_w = col / len(facts)
    story.append(Table([[P(l, FACT_L) for l, _ in facts], [P(escape(v), FACT_V) for _, v in facts]],
                       colWidths=[fact_w] * len(facts),
                       style=[('BACKGROUND', (0, 0), (-1, -1), TINT), ('BOX', (0, 0), (-1, -1), 0.6, RULE),
                              ('LINEBEFORE', (1, 0), (-1, -1), 0.6, RULE), ('TOPPADDING', (0, 0), (-1, 0), 8),
                              ('BOTTOMPADDING', (0, -1), (-1, -1), 9), ('LEFTPADDING', (0, 0), (-1, -1), 9)]))
    story.append(Spacer(1, 10))
    story.append(P(
        (f'This design is <b>planted and built by our own crew</b>: site preparation, soil, planting and clean-up are '
         f'included in the estimate.' if installed else
         'This design is a <b>plants-only</b> order: the plants and items listed, delivered by our crew, for you to plant.')
        + (f' See it in 3D and share it: <link href="{escape(site)}/d/{escape(share.token)}" color="#047857">'
           f'{escape(site.split("//")[-1])}/d/{escape(share.token)}</link>' if share else '')))

    # ── Page 2: site plan + list ──
    story += [PageBreak(), P('Site plan', H2),
              P('Seen from above, to scale. Numbers match the list below; circles show how wide each plant grows '
                'in about two years.', BODY), Spacer(1, 6),
              _site_plan(design, rows, items_by_id, col, 12.5 * cm, font, bold), Spacer(1, 10), P('Plants and items', H2)]

    table = [[P('NO.', HEAD), P('ITEM', HEAD), P('QTY', HEAD_R), P(f'HEIGHT: PLANTED {arrow} GROWN', HEAD),
              P('SUN · WATER', HEAD), P('EACH', HEAD_R), P('SUBTOTAL', HEAD_R)]]
    for i, r in enumerate(rows, start=1):
        item = items_by_id.get(str(r.get('plant_id')))
        is_plant = category_of(r) == 'plant'
        if item and is_plant and (item.planting_height or item.mature_height):
            size = f'{_num(item.planting_height):g} {arrow} {_num(item.mature_height or item.real_world_size):g} m'
        elif item:
            size = f'{_num(item.real_world_size):g} m'
        else:
            size = '—'
        care = f'{SUN.get(item.sunlight, "—")} · {WATER.get(item.water_need, "—").split(" (")[0]}' if item and is_plant else '—'
        name = escape(str(r.get('name') or (item.name if item else 'Item')))
        kind = '' if is_plant else f'<br/><font size="7" color="#94a3b8">{CATEGORY.get(category_of(r), "")}</font>'
        table.append([P(str(i), CELL), P(f'<b>{name}</b>{kind}', CELL), P(str(int(_num(r.get('quantity')))), CELL_R),
                      P(size, CELL_MUTED), P(care, CELL_MUTED), P(peso(r.get('unit_price')), CELL_R),
                      P(peso(r.get('subtotal')), CELL_R)])
    if len(table) == 1:
        table.append([P('—', CELL), P('No item list was saved with this design.', CELL_MUTED), '', '', '', '', ''])
    widths = [0.9 * cm, 4.6 * cm, 1.0 * cm, 3.4 * cm, 3.3 * cm, 1.8 * cm]
    widths.append(col - sum(widths))
    story.append(Table(table, colWidths=widths, repeatRows=1,
                       style=[('LINEBELOW', (0, 0), (-1, 0), 0.8, DARK), ('LINEBELOW', (0, 1), (-1, -1), 0.4, RULE),
                              ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'), ('TOPPADDING', (0, 0), (-1, -1), 4),
                              ('BOTTOMPADDING', (0, 0), (-1, -1), 4)]))

    # ── Page 3: estimate, care, next steps ──
    story += [PageBreak(), P('Price estimate', H2)]
    est = [[P(escape(str(l.get('label', ''))), CELL), P(peso(l.get('amount')), CELL_R)] for l in lines]
    if not est:
        est = [[P('Plants and items (see the list)', CELL), P(peso(total), CELL_R)]]
    est.append([P('<b>Estimated total</b>', S('tt', fn=bold, fontSize=11, textColor=DARK, leading=14)),
                P(f'<b>{peso(total)}</b>', S('ttr', fn=bold, fontSize=13, textColor=GREEN_DARK, leading=16, alignment=TA_RIGHT))])
    story.append(Table(est, colWidths=[col - 4 * cm, 4 * cm],
                       style=[('LINEBELOW', (0, 0), (-1, -2), 0.4, RULE), ('LINEABOVE', (0, -1), (-1, -1), 1, DARK),
                              ('TOPPADDING', (0, 0), (-1, -1), 5), ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
                              ('VALIGN', (0, 0), (-1, -1), 'MIDDLE')]))
    story.append(Spacer(1, 6))
    story.append(P(
        (f'<font color="#b45309"><b>These prices are from {fmt(priced_on)} and have expired.</b> Prices and stock may have '
         f'changed: ask us for a fresh quote.</font> ' if expired else
         f'Prices as of {fmt(priced_on)}, valid until <b>{fmt(valid_until)}</b>. ')
        + 'The final price is confirmed after we visit your site and check the measurements, soil and access.', BODY))

    plant_rows = [r for r in rows if category_of(r) == 'plant' and str(r.get('plant_id')) in items_by_id]
    if plant_rows:
        care = [P('Caring for your garden', H2),
                P('Water new plants every morning for the first two weeks while their roots settle, then as each '
                  'plant needs below. Water at the base, early in the day.'), Spacer(1, 4)]
        for r in plant_rows:
            item = items_by_id[str(r.get('plant_id'))]
            grows = []
            if item.mature_height:
                grows.append(f'about {item.mature_height:g} m tall')
            if item.mature_spread:
                grows.append(f'{item.mature_spread:g} m wide')
            care.append(P(f'• <b>{escape(item.name)}</b>: {SUN.get(item.sunlight, "Sun: ask us").lower()}, '
                          f'{WATER.get(item.water_need, "moderate").lower()} watering'
                          + (f'. Grows to {" and ".join(grows)}.' if grows else '.'), BODY))
        story += [Spacer(1, 6), KeepTogether(care[:3])] + care[3:]

    steps = ([f'<b>Book a site inspection</b> at {escape(site.split("//")[-1])}/book-service or from your account. '
              'Tell us the reference ' + ref + '.',
              '<b>We visit your yard</b>, check the measurements, soil and access, and confirm the final price.',
              '<b>Our own crew plants and builds it.</b> You can follow the progress with photos in your account.']
             if installed else
             ['<b>Order the plants</b> from the AI Designer ("Add these plants to my cart") or our shop.',
              '<b>We deliver</b> them to your address with our own crew.',
              '<b>Plant them</b> using the site plan on page 2. Need a hand? Book our crew any time.'])
    story += [Spacer(1, 6), P('Next steps', H2)] + [P(f'{i}. {s}') for i, s in enumerate(steps, start=1)]
    story += [Spacer(1, 14), P(
        'This proposal is an estimate based on the yard size and choices you gave. Pictures and the 3D view are '
        'illustrations: real plants vary in size, shape and colour as they grow.'
        + (f' {escape(credits)}' if credits else ''), SMALL)]

    doc.build(story, onFirstPage=chrome, onLaterPages=chrome)
    return buf.getvalue()
