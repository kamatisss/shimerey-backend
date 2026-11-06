"""
Botanical compatibility check for the customer's preferred plants (AI Garden Designer).
Soft warnings only: the customer can still generate. The layout engine enforces the matching
separation (layout_engine.companion_gap), so every recommendation here is something the layout does.
Mirrored in the frontend: src/data/plantCompatibility.js (keep the two in sync).
"""
from .layout_engine import CANOPY_GAP, CANOPY_HEIGHT, GROUNDCOVER_HEIGHT, HYDROZONE_GAP, ROOT_BARRIER_GAP

def evaluate_plant_compatibility(items):
    """
    Audits a collection of plant objects (InventoryItem instances or dicts)
    and returns structured warnings explaining biological and companion incompatibilities.

    Returns:
        List of dicts:
        [
            {
                'id': str,
                'type': 'water' | 'sunlight' | 'roots' | 'canopy',
                'severity': 'warning' | 'advisory',
                'title': str,
                'message': str,
                'affected_plants': list of plant names,
                'recommendation': str,
            }
        ]
    """
    if not items or len(items) < 2:
        return []

    # Standardize item access (model instance or dict)
    plants = []
    for item in items:
        if hasattr(item, 'name'):
            plants.append({
                'id': str(item.id),
                'name': item.name,
                'sunlight': getattr(item, 'sunlight', 'full_sun') or 'full_sun',
                'water_need': getattr(item, 'water_need', 'moderate') or 'moderate',
                'is_invasive': bool(getattr(item, 'is_invasive', False)),
                # Fully grown when known: shade and root conflicts are about the plant it becomes
                'height': float(getattr(item, 'mature_height', None) or getattr(item, 'real_world_size', 1.0) or 1.0),
            })
        elif isinstance(item, dict):
            plants.append({
                'id': str(item.get('id', '')),
                'name': item.get('name', 'Plant'),
                'sunlight': item.get('sunlight', 'full_sun') or 'full_sun',
                'water_need': item.get('water_need', 'moderate') or 'moderate',
                'is_invasive': bool(item.get('is_invasive', False)),
                'height': float(item.get('mature_height') or item.get('real_world_size') or item.get('height') or 1.0),
            })

    warnings = []

    # 1. Moisture & Irrigation Conflict (Low vs High)
    low_water = [p['name'] for p in plants if p['water_need'] == 'low']
    high_water = [p['name'] for p in plants if p['water_need'] == 'high']
    if low_water and high_water:
        low_str = ', '.join(low_water[:3])
        high_str = ', '.join(high_water[:3])
        warnings.append({
            'id': 'moisture_conflict',
            'type': 'water',
            'severity': 'warning',
            'title': 'Soil Moisture & Irrigation Mismatch',
            'message': (
                f"Drought-tolerant flora ({low_str}) and moisture-loving tropicals ({high_str}) "
                "require conflicting watering schedules. Placing them in the same bed risks root rot for drought species."
            ),
            'affected_plants': list(set(low_water + high_water)),
            'recommendation': (
                f"The layout keeps them in separate watering zones (at least {HYDROZONE_GAP:g} m of extra soil apart). "
                "Plant the drought-tolerant ones in raised, gravelly or sandy beds that drain fast."
            ),
        })

    # 2. Sunlight Exposure Conflict (Full Sun vs Full Shade)
    full_sun = [p['name'] for p in plants if p['sunlight'] == 'full_sun']
    full_shade = [p['name'] for p in plants if p['sunlight'] == 'full_shade']
    if full_sun and full_shade:
        sun_str = ', '.join(full_sun[:3])
        shade_str = ', '.join(full_shade[:3])
        warnings.append({
            'id': 'sunlight_conflict',
            'type': 'sunlight',
            'severity': 'warning',
            'title': 'Sunlight Exposure Conflict',
            'message': (
                f"Full-sun varieties ({sun_str}) require open direct light, whereas shade-dwelling varieties "
                f"({shade_str}) suffer leaf scorch when exposed to harsh tropical midday sun."
            ),
            'affected_plants': list(set(full_sun + full_shade)),
            'recommendation': (
                "The layout tucks the shade plants beside taller plants and keeps the open beds for sun-lovers. "
                "On site, a wall or tree that blocks the afternoon (west) sun is the best spot for them."
            ),
        })

    # 3. Invasive Root Competition (e.g. Bamboo)
    invasive = [p['name'] for p in plants if p['is_invasive']]
    bedding_companions = [p['name'] for p in plants if not p['is_invasive']]
    if invasive and bedding_companions:
        inv_str = ', '.join(invasive[:2])
        comp_str = ', '.join(bedding_companions[:3])
        warnings.append({
            'id': 'invasive_roots',
            'type': 'roots',
            'severity': 'advisory',
            'title': 'Aggressive Root Competition',
            'message': (
                f"Aggressive rhizome spreaders ({inv_str}) can overtake the root systems of companion flora "
                f"({comp_str}) if planted in shared soil."
            ),
            'affected_plants': list(set(invasive + bedding_companions)),
            'recommendation': (
                f"The layout leaves at least {ROOT_BARRIER_GAP:g} m around the spreader. For installation we "
                "recommend a 60 cm deep HDPE root barrier around it."
            ),
        })

    # 4. Dense Canopy Shading Over Sun-Dependent Groundcovers
    canopy_plants = [p['name'] for p in plants if p['height'] >= CANOPY_HEIGHT]
    sun_groundcovers = [p['name'] for p in plants if p['height'] <= GROUNDCOVER_HEIGHT and p['sunlight'] == 'full_sun']
    if canopy_plants and sun_groundcovers:
        canopy_str = ', '.join(canopy_plants[:2])
        ground_str = ', '.join(sun_groundcovers[:2])
        warnings.append({
            'id': 'canopy_shading',
            'type': 'canopy',
            'severity': 'advisory',
            'title': 'Overhead Canopy Shading',
            'message': (
                f"Tall canopy specimens ({canopy_str}) create dense shadow pockets that will suppress flowering "
                f"on low sun-dependent groundcovers ({ground_str}) if placed directly beneath them."
            ),
            'affected_plants': list(set(canopy_plants + sun_groundcovers)),
            'recommendation': (
                f"The layout puts tall plants at the back and keeps low sun-lovers at least {CANOPY_GAP:g} m "
                "beyond their normal spacing, in the open front border."
            ),
        })

    return warnings


def format_compatibility_prompt_note(warnings):
    """Formats compatibility warnings into a concise instruction note for the Gemini AI model."""
    if not warnings:
        return ""
    lines = ["BOTANICAL COMPATIBILITY GUIDELINES FOR THIS SITE:"]
    for w in warnings:
        lines.append(f"- {w['title']} ({', '.join(sorted(w['affected_plants']))}): {w['recommendation']}")
    return "\n".join(lines) + "\n\n"
