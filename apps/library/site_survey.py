"""
The customer's survey of their yard (AI Designer → "Mark your yard"): what's already there, marked on their
photo (lined up with the yard, so each tap is a real spot) or on a plan of the lot. Lot metres throughout:
x 0..width left → right, z 0..length with z = 0 the BACK of the yard and z = length the front (house side).

  clear   {kind, x0, z0, x1, z1, label}  path, driveway, patio, slab, septic, meter…: nothing planted there
  keep    {kind, x, z, r, label}         an existing tree or plant that stays (r = canopy radius, m)
  entry   {kind, x, z, edge}             a gate or door on an edge (back / front / left / right)
  screen  {kind, edge, a, b}             a stretch of fence or wall to hide (a..b metres along that edge of the
          {kind, edge, x0, z0, x1, z1}   box), or, on a yard with its own shape, the stretch of its outline itself
  view    {kind, x0, z0, x1, z1}         keep the view open (low plants only)
and survey.outline: the yard's shape when it isn't the whole W × L box (lot_shape.py), survey.sun: sun_map.py.

The yard photo: once lined up (photoPose.js), every tap on it is a real spot, and the browser reads its colours
(photoHints.js) for paving and grass. What came from the photo is kept so the design can say so:
  item.src       'photo' = the customer marked it on their photo; 'found' = the photo reading suggested it and
                 the customer accepted it (nothing found is used unless accepted)
  survey.photo   {lined_up, sized, shaped, grass}: lined up with the yard; its size or shape was fixed from the
                 photo; the share of the seen yard that looks like grass (0..1, or None)
photo_facts() turns these into the "Based on your yard photo" lines (results and proposal).

Phase 1 (now): `clear` areas and `keep` canopies become keep-clear cells the engine already obeys, and each
kept plant is an existing-feature marker (3D view, crew setting-out E1, E2…). `entry`, `screen` and `view`
are checked and kept with the design for the engine to use in its next step (paths, screens, low planting).
"""
import json
import math

from .layout_engine import GRID, SiteRules
from . import lot_shape, sun_map

KINDS = ('clear', 'keep', 'entry', 'screen', 'view')
EDGES = ('back', 'front', 'left', 'right')
MAX_ITEMS = 40
MAX_LABEL = 40
KEEP_R = (0.2, 8.0)          # canopy radius of a kept plant, metres
SOURCES = ('photo', 'found')  # item.src: marked on the photo / found in it by the photo reading
GRASS_LAWN = 0.5             # this much of the seen yard looking like grass = an existing lawn


class SurveyError(ValueError):
    """Bad survey data; the message is shown to the customer."""


def _num(value, lo, hi):
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise SurveyError('The marks on your yard have a value that is not a number.')
    if not math.isfinite(n):
        raise SurveyError('The marks on your yard have a value that is not a number.')
    return round(min(hi, max(lo, n)), 3)


def _edge_length(edge, width, length):
    return width if edge in ('back', 'front') else length


def _parse_sun(raw):
    """{'facing': compass degrees the back of the yard faces, 'house': 'none'|'one'|'two'} or None (not given)."""
    if not isinstance(raw, dict) or raw.get('facing') in (None, ''):
        return None
    facing = _num(raw.get('facing'), -1e6, 1e6) % 360
    house = raw.get('house') if raw.get('house') in sun_map.HOUSE_HEIGHTS else 'one'
    return {'facing': round(facing, 1), 'house': house}


def _parse_photo(raw):
    """survey.photo → {'lined_up', 'sized', 'shaped': bool, 'grass': 0..1 | None}, or None (no photo used)."""
    if not isinstance(raw, dict):
        return None
    grass = raw.get('grass')
    try:
        grass = None if grass in (None, '') else round(min(1.0, max(0.0, float(grass))), 2)
    except (TypeError, ValueError):
        grass = None
    if grass is not None and not math.isfinite(grass):
        grass = None
    photo = {'lined_up': raw.get('lined_up') is True, 'sized': raw.get('sized') is True,
             'shaped': raw.get('shaped') is True, 'grass': grass}
    return photo if photo['lined_up'] else None


def parse(raw, width, length):
    """Survey JSON (string or list/dict) → a clean {'items': [...], 'sun': …|None} inside the lot."""
    if raw in (None, '', [], {}):
        return {'items': [], 'sun': None, 'outline': None}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raise SurveyError('The marks on your yard could not be read. Please mark them again.')
    sun = _parse_sun(raw.get('sun')) if isinstance(raw, dict) else None
    try:
        outline = lot_shape.clean(raw.get('outline'), float(width), float(length)) if isinstance(raw, dict) else None
    except lot_shape.ShapeError as e:
        raise SurveyError(str(e))
    items = (raw.get('items') or []) if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise SurveyError('The marks on your yard could not be read. Please mark them again.')
    if len(items) > MAX_ITEMS:
        raise SurveyError(f'Please mark at most {MAX_ITEMS} things on your yard.')
    W, L = float(width), float(length)
    out = []
    for item in items:
        if not isinstance(item, dict) or item.get('kind') not in KINDS:
            raise SurveyError('One of the marks on your yard is not something we know.')
        kind = item['kind']
        label = str(item.get('label') or '')[:MAX_LABEL]
        src = {'src': item['src']} if item.get('src') in SOURCES else {}
        if kind in ('clear', 'view'):
            x0, x1 = sorted((_num(item.get('x0'), 0, W), _num(item.get('x1'), 0, W)))
            z0, z1 = sorted((_num(item.get('z0'), 0, L), _num(item.get('z1'), 0, L)))
            if x1 - x0 < 0.1 or z1 - z0 < 0.1:
                continue   # a tap, not an area
            out.append({'kind': kind, 'x0': x0, 'z0': z0, 'x1': x1, 'z1': z1, 'label': label, **src})
        elif kind == 'keep':
            out.append({'kind': kind, 'x': _num(item.get('x'), 0, W), 'z': _num(item.get('z'), 0, L),
                        'r': _num(item.get('r'), *KEEP_R), 'label': label, **src})
        elif kind == 'entry':
            edge = item.get('edge')
            if edge not in EDGES:
                raise SurveyError('A gate or door must be on an edge of the yard.')
            out.append({'kind': kind, 'x': _num(item.get('x'), 0, W), 'z': _num(item.get('z'), 0, L),
                        'edge': edge, 'label': label, **src})
        else:   # screen
            edge = item.get('edge')
            if edge not in EDGES:
                raise SurveyError('A stretch to hide must be along an edge of the yard.')
            if all(item.get(k) not in (None, '') for k in ('x0', 'z0', 'x1', 'z1')):
                # A stretch of the yard's own outline (a shaped yard)
                seg = (_num(item.get('x0'), 0, W), _num(item.get('z0'), 0, L), _num(item.get('x1'), 0, W), _num(item.get('z1'), 0, L))
                if math.hypot(seg[2] - seg[0], seg[3] - seg[1]) < 0.3:
                    continue
                out.append({'kind': kind, 'edge': edge, 'x0': seg[0], 'z0': seg[1], 'x1': seg[2], 'z1': seg[3], 'label': label,
                            **src})
                continue
            n = _edge_length(edge, W, L)
            a, b = sorted((_num(item.get('a'), 0, n), _num(item.get('b'), 0, n)))
            if b - a < 0.3:
                continue
            out.append({'kind': kind, 'edge': edge, 'a': a, 'b': b, 'label': label, **src})
    photo = _parse_photo(raw.get('photo')) if isinstance(raw, dict) else None
    return {'items': out, 'sun': sun,
            'outline': [{'x': x, 'z': z, 'house': h} for (x, z), h in zip(*outline)] if outline else None,
            **({'photo': photo} if photo else {})}


def outline(survey):
    """The yard's shape as a tuple of (x, z), or None for the whole box."""
    pts = survey.get('outline')
    return tuple((float(p['x']), float(p['z'])) for p in pts) if pts else None


def house_walls(survey):
    """The sides of the yard's shape that are house walls: ((x0, z0), (x1, z1)), …; None = the lot's house edge."""
    pts = survey.get('outline')
    if not pts:
        return None
    return tuple(((float(a['x']), float(a['z'])), (float(b['x']), float(b['z'])))
                 for a, b in zip(pts, pts[1:] + pts[:1]) if a.get('house'))


def area(survey, width, length):
    """The yard's real area in m² (pricing: ground preparation)."""
    shape = outline(survey)
    return round(lot_shape.area(list(shape)), 1) if shape else float(width) * float(length)


def screen_segment(item, width, length):
    """A screen mark → ((x0, z0), (x1, z1)) along the fence."""
    if 'x0' in item:
        return (item['x0'], item['z0']), (item['x1'], item['z1'])
    W, L, a, b = float(width), float(length), item['a'], item['b']
    return {'back': ((a, 0.0), (b, 0.0)), 'front': ((a, L), (b, L)),
            'left': ((0.0, a), (0.0, b)), 'right': ((W, a), (W, b))}[item['edge']]


def keep_clear_cells(survey, site):
    """Engine grid cells (gx, gz) that touch a keep-clear area or a kept plant's canopy."""
    cells = set()
    cw, cl, grid = site.cell_w, site.cell_l, GRID
    shape = outline(survey)
    if shape:   # outside the yard's own shape (cells mostly outside it)
        cells |= {(gx, gz) for gx in range(grid) for gz in range(grid)
                  if not lot_shape.contains(shape, (gx + 0.5) * cw, (gz + 0.5) * cl)}
    for item in survey['items']:
        if item['kind'] == 'clear':
            gx0, gx1 = int(item['x0'] / cw), min(grid - 1, int((item['x1'] - 1e-9) / cw))
            gz0, gz1 = int(item['z0'] / cl), min(grid - 1, int((item['z1'] - 1e-9) / cl))
            cells |= {(gx, gz) for gx in range(gx0, gx1 + 1) for gz in range(gz0, gz1 + 1)}
        elif item['kind'] == 'keep':
            x, z, r = item['x'], item['z'], item['r']
            for gx in range(max(0, int((x - r) / cw)), min(grid - 1, int((x + r) / cw)) + 1):
                for gz in range(max(0, int((z - r) / cl)), min(grid - 1, int((z + r) / cl)) + 1):
                    # nearest point of the cell to the trunk: inside the canopy?
                    nx = min(max(x, gx * cw), (gx + 1) * cw)
                    nz = min(max(z, gz * cl), (gz + 1) * cl)
                    if math.hypot(nx - x, nz - z) < r:
                        cells.add((gx, gz))
    return cells


def sun_hours(survey, width, length):
    """Sun hours per engine cell (rows[gz][gx]) when the customer told us how the yard faces, else None."""
    sun = survey.get('sun')
    if not sun:
        return None
    keeps = tuple((i['x'], i['z'], i['r']) for i in survey['items'] if i['kind'] == 'keep')
    return sun_map.hours_grid(round(float(width), 2), round(float(length), 2), sun['facing'],
                              sun_map.HOUSE_HEIGHTS[sun['house']], keeps, GRID, outline(survey), house_walls(survey))


def rules(survey, width=None, length=None):
    """The survey as the engine's rules (exact shapes, gates, screens, views, sun): layout_engine.SiteRules."""
    items = survey['items']
    return SiteRules(
        sun=sun_hours(survey, width, length) if width and length else None,
        outline=outline(survey),
        house_walls=house_walls(survey),
        clear=tuple((i['x0'], i['z0'], i['x1'], i['z1']) for i in items if i['kind'] == 'clear'),
        keeps=tuple((i['x'], i['z'], i['r']) for i in items if i['kind'] == 'keep'),
        entries=tuple((i['x'], i['z'], i['edge']) for i in items if i['kind'] == 'entry'),
        screens=tuple((i['edge'], *screen_segment(i, width or 0, length or 0)) for i in items if i['kind'] == 'screen'),
        views=tuple((i['x0'], i['z0'], i['x1'], i['z1']) for i in items if i['kind'] == 'view'),
    )


def existing_markers(survey):
    """Kept trees and plants as existing-feature markers (3D view, setting-out plan, crew)."""
    return [{'plant_id': 'existing', 'label': item['label'] or 'Existing plant (kept)', 'x': item['x'], 'z': item['z'],
             'r': item['r'], 'rotation': 0.0, 'is_existing': True}
            for item in survey['items'] if item['kind'] == 'keep']


def note(survey):
    """One line for the design's notes: what we worked around (screens report themselves: layout_engine)."""
    count = {k: sum(1 for i in survey['items'] if i['kind'] == k) for k in KINDS}
    parts = []
    if count['clear']:
        parts.append(f"kept clear the {count['clear']} area{'s' if count['clear'] > 1 else ''} you marked")
    if count['keep']:
        parts.append(f"planted around the {count['keep']} tree{'s' if count['keep'] > 1 else ''} or plant"
                     f"{'s' if count['keep'] > 1 else ''} you're keeping")
    if count['entry']:
        parts.append(f"made a path from your {'gate or door' if count['entry'] == 1 else str(count['entry']) + ' gates and doors'} "
                     "to the lawn")
    if count['view']:
        parts.append(f"kept only low plants in the view{'s' if count['view'] > 1 else ''} you want to keep")
    if survey.get('sun'):
        parts.append('matched each plant to the sun or shade where it stands')
    if survey.get('outline'):
        parts.insert(0, "planned inside your yard's own shape")
    if not parts:
        return None
    return 'From your yard: we ' + (', '.join(parts[:-1]) + ' and ' + parts[-1] if len(parts) > 1 else parts[0]) + '.'


def _plural(n, one, many=None):
    return f'{n} {one if n == 1 else (many or one + "s")}'


def photo_facts(survey, new_lawn=True):
    """What the design took from the customer's yard photo, as short phrases ("kept clear the patio we found in
    it"), for the "Based on your yard photo" line in the results and the proposal. [] = the photo wasn't used."""
    photo = (survey or {}).get('photo')
    if not photo:
        return []
    items = survey.get('items') or []
    facts = []
    if photo.get('sized') and photo.get('shaped'):
        facts.append('used the size and shape your photo measured')
    elif photo.get('sized'):
        facts.append('used the yard size your photo measured')
    elif photo.get('shaped'):
        facts.append('used the yard shape your photo showed')
    found = [i for i in items if i.get('src') == 'found' and i['kind'] == 'clear']
    if found:
        names = list(dict.fromkeys((i.get('label') or 'paved area').lower() for i in found))   # as found: biggest first
        what = names[0] if len(names) == 1 else ', '.join(names[:-1]) + ' and ' + names[-1]
        facts.append(f'kept clear the {what} we found in it')
    found_trees = [i for i in items if i.get('src') == 'found' and i['kind'] == 'keep']
    if found_trees:
        names = list(dict.fromkeys((i.get('label') or 'tree').lower() for i in found_trees))
        what = (f'the {names[0]}' if len(found_trees) == 1
                else f'the {len(found_trees)} trees and plants ({", ".join(names)})')
        facts.append(f'planted around {what} we found in it')
    marked = [i for i in items if i.get('src') == 'photo']
    if marked:
        trees = sum(1 for i in marked if i['kind'] == 'keep')
        gates = sum(1 for i in marked if i['kind'] == 'entry')
        others = len(marked) - trees - gates
        parts = ([f'planted around the {_plural(trees, "tree or plant", "trees and plants")}'] if trees else []) \
            + ([f'led a path from the {_plural(gates, "gate or door", "gates and doors")}'] if gates else []) \
            + ([f'worked around the {_plural(others, "other area")}'] if others else [])
        facts.append(' and '.join(parts) + ' you marked on it')
    if not new_lawn and (photo.get('grass') or 0) >= GRASS_LAWN:
        facts.append(f'kept your existing lawn (about {round(photo["grass"] * 100)}% of your yard looks like grass)')
    return facts


def photo_note(survey, new_lawn=True):
    """One sentence from photo_facts, or None."""
    facts = photo_facts(survey, new_lawn)
    if not facts:
        return None
    return 'Based on your yard photo, we ' + (', '.join(facts[:-1]) + ' and ' + facts[-1] if len(facts) > 1 else facts[0]) + '.'


# ── Show sun & shade (Mark your yard), before the design is made ──
from rest_framework.decorators import api_view, permission_classes, throttle_classes   # noqa: E402
from rest_framework.permissions import AllowAny   # noqa: E402
from rest_framework.response import Response   # noqa: E402
from rest_framework.throttling import ScopedRateThrottle   # noqa: E402


class SunMapThrottle(ScopedRateThrottle):
    scope = 'sun_map'


@api_view(['POST'])
@permission_classes([AllowAny])          # the survey can be filled in before signing in
@throttle_classes([SunMapThrottle])
def sun_map_view(request):
    """
    POST /api/sun-map/ {lot_width, lot_length, site_survey: {items, sun: {facing, house}}}
    → {hours: rows[gz][gx] (z = 0 the back), grid, summary: {full_sun, partial_shade, full_shade}, classes:
    {full_sun, shade} thresholds}. The same sun hours the design engine uses (sun_map.py).
    """
    try:
        width, length = float(request.data.get('lot_width')), float(request.data.get('lot_length'))
        if not (0 < width <= 200 and 0 < length <= 200):
            raise ValueError
    except (TypeError, ValueError):
        return Response({'error': 'Lot width and length must be positive numbers (up to 200 m).'}, status=400)
    try:
        survey = parse(request.data.get('site_survey'), width, length)
    except SurveyError as e:
        return Response({'error': str(e)}, status=400)
    if not survey.get('sun'):
        return Response({'error': 'Tell us which way the back of your yard faces.'}, status=400)
    rows = sun_hours(survey, width, length)
    return Response({'hours': rows, 'grid': GRID, 'summary': sun_map.summary(rows),
                     'classes': {'full_sun': sun_map.FULL_SUN_H, 'shade': sun_map.SHADE_H}})
