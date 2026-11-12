"""
Build apps/library/data/quezon_barangay_bounds.json: the official barangay boundaries of Quezon Province,
used by geo_lookup.py to tell which town and barangay a map pin is in ("Use my location").

Source: faeldon/philippines-json-maps (MIT), GeoJSON "hires" release, one file per town
(bgysubmuns-municity-<10-digit PSGC>.0.1.json), built from OCHA COD-AB Philippines boundaries
by NAMRIA and PSA (CC BY-IGO 3.0: credit NAMRIA and PSA via OCHA; see the /credits page).

    python manage.py build_barangay_bounds <folder with the 41 Quezon files>

The boundaries use the 2023+ 10-digit PSGC; our address data (quezon_areas.json) uses the 9-digit
codes: RR PP MM BBB → RR 0PP MM BBB, except Lucena (a highly urbanised city, its own code 0431200BBB).
Every barangay must match one shape and every shape one barangay, or nothing is written.
Coordinates are rounded to 5 decimals (about 1 m) and stored flat: [lng, lat, lng, lat, …] per ring.
"""
import glob
import json
import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

DATA = os.path.join(settings.BASE_DIR, 'apps', 'library', 'data')
LUCENA_OLD = '045624'


def new_code(old):
    if old.startswith(LUCENA_OLD):
        return int('0431200' + old[6:])
    return int(old[:2] + '0' + old[2:])


def _rings(geometry):
    polys = geometry['coordinates'] if geometry['type'] == 'MultiPolygon' else [geometry['coordinates']]
    return [ring for poly in polys for ring in poly]


class Command(BaseCommand):
    help = 'Build quezon_barangay_bounds.json from the faeldon/philippines-json-maps barangay files.'

    def add_arguments(self, parser):
        parser.add_argument('folder')
        parser.add_argument('--psgc', default='2026-3q', help='PSGC edition of the source, recorded in the file')

    def handle(self, folder, psgc, **options):
        with open(os.path.join(DATA, 'quezon_areas.json'), encoding='utf-8') as f:
            towns = json.load(f)['cities']
        shapes = {}
        for path in glob.glob(os.path.join(folder, 'bgysubmuns-municity-*.json')):
            with open(path, encoding='utf-8') as f:
                for feature in json.load(f)['features']:
                    shapes[int(feature['properties']['adm4_psgc'])] = feature
        if not shapes:
            raise CommandError(f'No bgysubmuns-municity-*.json files in {folder}')

        rows, wanted = [], set()
        for town in towns:
            for brgy in town['barangays']:
                code = new_code(brgy['code'])
                wanted.add(code)
                feature = shapes.get(code)
                if feature is None:
                    raise CommandError(f'No boundary for {brgy["name"]}, {town["name"]} ({brgy["code"]} -> {code})')
                rings, xs, ys = [], [], []
                for ring in _rings(feature['geometry']):
                    flat = []
                    for lng, lat, *_ in ring:
                        flat += [round(lng, 5), round(lat, 5)]
                        xs.append(lng)
                        ys.append(lat)
                    rings.append(flat)
                rows.append({'code': brgy['code'], 'city': town['code'],
                             'bbox': [round(min(xs), 5), round(min(ys), 5), round(max(xs), 5), round(max(ys), 5)],
                             'rings': rings})
        extra = set(shapes) - wanted
        if extra:
            raise CommandError(f'{len(extra)} boundaries match no barangay in quezon_areas.json, e.g. {sorted(extra)[:5]}')

        out = {
            'about': 'Barangay boundaries of Quezon Province for map-pin lookup (geo_lookup.py).',
            'source': ('Boundaries: NAMRIA and PSA, via OCHA COD-AB Philippines (CC BY-IGO 3.0), '
                       'prepared by faeldon/philippines-json-maps (MIT).'),
            'psgc': psgc, 'count': len(rows), 'barangays': rows,
        }
        target = os.path.join(DATA, 'quezon_barangay_bounds.json')
        with open(target, 'w', encoding='utf-8') as f:
            json.dump(out, f, separators=(',', ':'), ensure_ascii=False)
        self.stdout.write(self.style.SUCCESS(f'{len(rows)} barangays -> {target} ({os.path.getsize(target) / 1e6:.1f} MB)'))
