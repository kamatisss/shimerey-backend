import os
import base64
import json
import logging
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

# ── Zone-grid mapping (Vision-then-Math pipeline) ───────────────
# The Vision AI outputs only a zone label (< 100 tokens).
# resolve_coordinates() converts that label into exact grid cells —
# all spatial math stays in Python, not in the AI generation step.
#
# Single source of truth: the layout engine's map, where gz 0 is the back of the
# garden and gz 19 the front (nearest the viewer in the 3D/AR view). The previous
# local map had front and back swapped, so detected obstacles were kept clear on
# the wrong side of the garden.
from .layout_engine import ZONES as ZONE_GRID

_VALID_ZONES = set(ZONE_GRID.keys())
_GRID_COLS = 20
_GRID_ROWS = 20


def resolve_coordinates(
    zone_id: str,
    lot_width: float = 10.0,
    lot_length: float = 10.0,
    grid_cols: int = _GRID_COLS,
    grid_rows: int = _GRID_ROWS,
) -> dict:
    """
    Map a Vision-AI zone label to grid-center coordinates and excluded cells.

    The Vision AI only identifies *which zone* an obstacle occupies.
    This function does all the spatial math, keeping AI output tiny.

    Returns:
        {
          'gx': int, 'gz': int,              # zone centre in grid coords
          'x': float, 'z': float,            # real-world metres
          'excluded_cells': [(gx, gz), ...]  # every cell inside the zone
        }
    """
    zone = ZONE_GRID.get(zone_id, ZONE_GRID['midground_center'])
    gx_min, gx_max = zone['gx']
    gz_min, gz_max = zone['gz']

    gx_center = (gx_min + gx_max) // 2
    gz_center = (gz_min + gz_max) // 2

    cell_w = lot_width / grid_cols
    cell_l = lot_length / grid_rows

    excluded = [
        (gx, gz)
        for gx in range(gx_min, gx_max + 1)
        for gz in range(gz_min, gz_max + 1)
    ]

    return {
        'gx': gx_center,
        'gz': gz_center,
        'x': round(gx_center * cell_w, 3),
        'z': round(gz_center * cell_l, 3),
        'excluded_cells': excluded,
    }


ZONE_SCAN_PROMPT = None  # set below, shared by both scan functions


def scan_image_zones(image_file, timeout_ms=20_000):
    """
    Like scan_image_for_existing_elements, but reports failures instead of hiding them.

    Returns (zones, error): zones is [{zone, label, blocks_planting, is_existing}];
    error is None on success, or a short reason ('no_key', 'quota', 'unavailable', 'bad_response').
    An empty zones list with error None genuinely means "nothing found".
    """
    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key or api_key == "your_gemini_api_key_here":
        return [], 'no_key'
    try:
        if hasattr(image_file, 'read'):
            image_file.seek(0)
            image_bytes = image_file.read()
            image_file.seek(0)
            mime_type = getattr(image_file, 'content_type', 'image/jpeg') or 'image/jpeg'
        else:
            image_bytes, mime_type = image_file, 'image/jpeg'
        client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=timeout_ms))
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                      "List each existing garden feature with its placement zone as JSON."],
            config={"system_instruction": ZONE_SCAN_PROMPT, "response_mime_type": "application/json", "temperature": 0.1},
        )
    except Exception as e:  # network, timeout, quota…
        text = str(e)
        logger.warning("Zone scan failed: %s", text[:200])
        return [], 'quota' if ('429' in text or 'RESOURCE_EXHAUSTED' in text) else 'unavailable'
    try:
        cleaned = (response.text or '').strip().removeprefix('```json').removesuffix('```').strip()
        parsed = json.loads(cleaned or '[]')
        if not isinstance(parsed, list):
            raise ValueError('not a list')
    except (ValueError, json.JSONDecodeError):
        return [], 'bad_response'
    zones = [
        {'zone': item.get('zone'), 'label': item.get('label', 'unknown'),
         'blocks_planting': bool(item.get('blocks_planting', True)), 'is_existing': True}
        for item in parsed if isinstance(item, dict) and item.get('zone') in _VALID_ZONES
    ]
    return zones, None


def scan_image_for_existing_elements(image_file):
    """
    Vision pre-pass: scans a garden photo and returns high-level
    placement-zone annotations only — no coordinates, no sizes.

    The prompt asks the model for zone labels exclusively (< 100-token
    response). resolve_coordinates() in views_api.py then handles all
    the spatial math from those labels.

    Returns:
        list of dicts [{zone, label, blocks_planting, is_existing}] or []
    """
    if not image_file:
        logger.warning("No image file provided to scan_image_for_existing_elements.")
        return []

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        from django.conf import settings
        api_key = getattr(settings, "GEMINI_API_KEY", None)

    if not api_key:
        logger.error("GEMINI_API_KEY is not configured.")
        return []

    try:
        if hasattr(image_file, 'read'):
            image_file.seek(0)
            image_bytes = image_file.read()
            image_file.seek(0)
            mime_type = getattr(image_file, 'content_type', 'image/jpeg') or 'image/jpeg'
        elif isinstance(image_file, bytes):
            image_bytes = image_file
            mime_type = 'image/jpeg'
        else:
            logger.error("Invalid image_file type: must be file-like or bytes.")
            return []

        image_base64 = base64.b64encode(image_bytes).decode('utf-8')
    except Exception:
        logger.exception("Failed to encode image file.")
        return []

    try:
        client = genai.Client(api_key=api_key)
        image_part = types.Part.from_bytes(
            data=base64.b64decode(image_base64),
            mime_type=mime_type,
        )

        system_prompt = ZONE_SCAN_PROMPT

        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[
                image_part,
                "List each existing garden feature with its placement zone as JSON.",
            ],
            config={
                "system_instruction": system_prompt,
                "response_mime_type": "application/json",
                "temperature": 0.1,
            },
        )

        response_text = response.text
        if not response_text:
            logger.warning("Empty response from Vision AI zone scan.")
            return []

        cleaned = response_text.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

        parsed = json.loads(cleaned)
        if not isinstance(parsed, list):
            logger.warning("Vision AI returned non-list payload: %s", type(parsed))
            return []

        result = []
        for item in parsed:
            zone = item.get('zone', '')
            if zone not in _VALID_ZONES:
                logger.debug("Ignoring unrecognised zone '%s' from Vision AI.", zone)
                continue
            result.append({
                'zone': zone,
                'label': item.get('label', 'unknown'),
                'blocks_planting': bool(item.get('blocks_planting', True)),
                'is_existing': True,
            })
        return result

    except json.JSONDecodeError as e:
        logger.error("JSON decode failed for Vision AI zone scan: %s", e)
        return []
    except Exception:
        logger.exception("Failed during Vision AI zone scan.")
        return []


ZONE_SCAN_PROMPT = (
            "You are analyzing a garden or backyard photo to identify placement zones ONLY.\n\n"
            "Divide the visible ground into these eight named zones:\n"
            "  background_perimeter – far edge: fence line, house wall, back of garden\n"
            "  midground_left       – mid-distance, left third of the space\n"
            "  midground_center     – mid-distance, centre third\n"
            "  midground_right      – mid-distance, right third\n"
            "  foreground_left      – near viewer, left third\n"
            "  foreground_center    – near viewer, centre third\n"
            "  foreground_right     – near viewer, right third\n"
            "  blocked              – any area already occupied and unplantable "
            "(pool, patio, path, existing tree canopy, shed)\n\n"
            "For each distinct feature you see, output exactly ONE JSON object:\n"
            "  zone            – one of the eight zone names above\n"
            "  label           – a short snake_case label (e.g. swimming_pool, house_wall)\n"
            "  blocks_planting – true if this prevents new planting, false if visual backdrop only\n"
            "  is_existing     – always true\n\n"
            "CRITICAL: Return ONLY a raw JSON array. "
            "No coordinates. No sizes. No explanations.\n"
            'Example: [{"zone":"blocked","label":"patio","blocks_planting":true,"is_existing":true}]'
        )
