import math
import os
import json
from datetime import timedelta
from unittest.mock import patch, MagicMock
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from django.contrib.auth.models import User
from rest_framework_simplejwt.tokens import RefreshToken


# A real, pinned delivery / site address (Barangay 1, Lucena City): orders and bookings require one
QUEZON_ADDRESS = {'city_code': '045624000', 'barangay_code': '045624002', 'street': '12 Quezon Ave',
                  'landmark': 'beside the chapel', 'lat': 13.9358, 'lng': 121.6129, 'source': 'pin'}


def _lawn_centre(plan):
    """The middle of a garden plan's lawn (rect or ellipse)."""
    shape, a, b, c, d = plan.lawn
    return ((a + b) / 2, (c + d) / 2) if shape == 'rect' else (a, b)


class GenerateLayoutsViewTests(APITestCase):
    """AI Garden Designer: hybrid Gemini + smart layout engine. Customers always get 3 designs."""

    def setUp(self):
        from django.core.cache import cache
        from apps.library.models import InventoryItem
        cache.clear()  # throttle history, the layout cache and the Gemini breaker live here
        # Never call the real Gemini from tests (it spends the daily free quota and makes tests flaky):
        # no key unless a test sets its own fake one and mocks the client. GOOGLE_API_KEY wins over
        # GEMINI_API_KEY in garden_ai.gemini_api_key, so both are cleared.
        keys = patch.dict(os.environ, {'GOOGLE_API_KEY': '', 'GEMINI_API_KEY': ''})
        keys.start()
        self.addCleanup(keys.stop)
        self.user = User.objects.create_user(username='testuser', password='password123')
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(self.user).access_token}')
        self.url = reverse('generate-layouts')
        InventoryItem.objects.create(id=1, name="Oak Tree", category="plant", unit_price=1000, stock_quantity=10, real_world_size=4.0)
        InventoryItem.objects.create(id=2, name="Fern", category="plant", unit_price=500, stock_quantity=20, real_world_size=1.0)
        InventoryItem.objects.create(id=3, name="Palm Tree", category="plant", unit_price=800, stock_quantity=2, real_world_size=5.0)
        InventoryItem.objects.create(id=4, name="Rose", category="plant", unit_price=200, stock_quantity=40, real_world_size=0.5)
        self.valid = {'budget': 80000, 'lot_width': 10, 'lot_length': 12, 'preferred_plant_ids': ['1', '2']}

    def _ai_design(self, plants):
        from apps.library.garden_ai import GardenDesignSchema, PlantArrangement
        response = MagicMock()
        response.parsed = GardenDesignSchema(
            design_name='Lush / Organic', reasoning='Layered tropical planting.', total_cost=0,
            plants=[PlantArrangement(plant_id=p, gx=gx, gz=gz, rotation=0.0) for p, gx, gz in plants],
            plant_breakdown=[],
        )
        return response

    def _assert_three_valid_designs(self, data, budget=80000):
        from dataclasses import replace
        from apps.library import layout_engine as E
        from apps.library.models import InventoryItem, PricingSettings
        from apps.library.pricing import installed_price, plant_budget_from_all_in
        self.assertEqual([d['design_name'] for d in data['designs']], list(E.STYLES))
        ps = PricingSettings.current()
        items = {str(i.id): i for i in InventoryItem.objects.filter(category='plant')}
        specs = [replace(s, price=installed_price(ps, items[s.id])) for s in E.specs_from_inventory(items.values())]
        plant_budget = plant_budget_from_all_in(budget, 120, ps)
        site = E.Site(10, 12, E.zone_cells([z['zone'] for z in data.get('detected_zones', [])]))
        for d in data['designs']:
            plants = [p for p in d['plants'] if not p.get('is_existing')]
            self.assertTrue(plants, d['design_name'])
            plan = E.GardenPlan.from_dict(d['garden_plan'])
            self.assertIsNotNone(plan.lawn, d['design_name'])   # a 10 × 12 m yard always keeps a lawn
            self.assertEqual(E.check(plants, specs, site, plant_budget, plan), [], d['design_name'])
            self.assertEqual(d['plant_cost'], sum(r['subtotal'] for r in d['plant_breakdown']))
            # The all-in estimate stays within the customer's budget
            self.assertLessEqual(d['cost_breakdown']['total'], budget + 2, d['design_name'])
            self.assertEqual(d['cost_breakdown']['total'], sum(l['amount'] for l in d['cost_breakdown']['lines']))

    # ── validation & access ──────────────────────────────────────────────────
    def test_generate_layouts_without_auth(self):
        self.client.credentials()
        response = self.client.post(self.url, self.valid, format='json')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_invalid_input(self):
        for bad in ({'lot_width': 10, 'lot_length': 10}, {'budget': -100, 'lot_width': 10, 'lot_length': 10},
                    {'budget': 'hundred', 'lot_width': 10, 'lot_length': 10}, {'budget': 5000, 'lot_length': 10},
                    {'budget': 5000, 'lot_width': -10, 'lot_length': 10}, {'budget': 5000, 'lot_width': 'ten', 'lot_length': 10},
                    {**self.valid, 'keep_clear_zones': 'not json'}):
            self.assertEqual(self.client.post(self.url, bad, format='json').status_code, 400, bad)

    # ── availability: the feature never depends on Gemini ───────────────────
    @patch.dict(os.environ, {'GEMINI_API_KEY': 'your_gemini_api_key_here'})
    def test_no_api_key_still_returns_three_smart_layouts(self):
        r = self.client.post(self.url, self.valid, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['ai_status'], 'disabled')
        self.assertTrue(all(d['source'] == 'engine' for d in r.data['designs']))
        self._assert_three_valid_designs(r.data)

    @patch.dict(os.environ, {'GEMINI_API_KEY': 'test_api_key'})
    @patch('google.genai.Client')
    def test_hybrid_ai_design_is_checked_and_repaired(self, mock_client_class):
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        # Two oaks in the same cell, a palm over stock, an invented id
        mock_client.models.generate_content.return_value = self._ai_design(
            [('1', 5, 5), ('1', 5, 5), ('3', 2, 2), ('3', 10, 2), ('3', 15, 2), ('999', 8, 8)])
        r = self.client.post(self.url, self.valid, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['ai_status'], 'ok')
        self.assertEqual(mock_client.models.generate_content.call_count, 1)  # one Gemini call, not three
        sources = {d['design_name']: d['source'] for d in r.data['designs']}
        self.assertEqual(sources, {'Symmetrical': 'engine', 'Minimalist': 'engine', 'Lush / Organic': 'ai'})
        lush = r.data['designs'][2]
        self.assertTrue(lush['adjustments'])  # the customer is told what was fixed
        self._assert_three_valid_designs(r.data)

    @patch.dict(os.environ, {'GEMINI_API_KEY': 'test_api_key'})
    @patch('google.genai.Client')
    def test_gemini_failure_falls_back_and_pauses_gemini(self, mock_client_class):
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.models.generate_content.side_effect = Exception(
            '429 RESOURCE_EXHAUSTED quotaId GenerateRequestsPerDayPerProjectPerModel-FreeTier')
        r = self.client.post(self.url, self.valid, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['ai_status'], 'quota')
        self.assertIn("today's limit", r.data['notice'])
        self._assert_three_valid_designs(r.data)
        # Next request: Gemini isn't even tried until the quota resets
        calls = mock_client.models.generate_content.call_count
        r2 = self.client.post(self.url, {**self.valid, 'seed': 7}, format='json')
        self.assertEqual(r2.data['ai_status'], 'quota')
        self.assertEqual(mock_client.models.generate_content.call_count, calls)

    @patch.dict(os.environ, {'GEMINI_API_KEY': 'test_api_key', 'AI_MODE': 'engine_only'})
    @patch('google.genai.Client')
    def test_engine_only_mode_never_calls_gemini(self, mock_client_class):
        r = self.client.post(self.url, self.valid, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['ai_status'], 'disabled')
        mock_client_class.assert_not_called()

    @patch.dict(os.environ, {'GEMINI_API_KEY': 'test_api_key'})
    @patch('google.genai.Client')
    def test_example_yard_uses_the_engine_only(self, mock_client_class):
        # "Try an example yard": three engine designs, no Gemini call, so the customer's daily AI designs stay unused
        r = self.client.post(self.url, {**self.valid, 'example': '1'}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['ai_status'], 'disabled')
        self.assertEqual(len(r.data['designs']), 3)
        self.assertTrue(all(d['source'] == 'engine' for d in r.data['designs']))
        mock_client_class.assert_not_called()

    # ── quotas ───────────────────────────────────────────────────────────────
    @patch.dict(os.environ, {'GEMINI_API_KEY': 'test_api_key'})
    @patch('google.genai.Client')
    def test_daily_ai_quota_then_smart_layouts_and_cache_is_free(self, mock_client_class):
        from apps.library.garden_ai import AILayoutThrottle
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.models.generate_content.return_value = self._ai_design([('1', 5, 5), ('2', 10, 10)])
        with patch.object(AILayoutThrottle, 'get_rate', return_value='1/day'):
            AILayoutThrottle.rate, AILayoutThrottle.num_requests = None, None
            self.assertEqual(self.client.post(self.url, self.valid, format='json').data['ai_status'], 'ok')
            # Same request again: served from cache, no Gemini call, no quota used
            again = self.client.post(self.url, self.valid, format='json')
            self.assertEqual(again.status_code, 200)
            self.assertEqual(mock_client.models.generate_content.call_count, 1)
            # A different request: the customer's AI quota is used → smart layouts, not an error
            other = self.client.post(self.url, {**self.valid, 'seed': 3}, format='json')
            self.assertEqual(other.status_code, 200)
            self.assertEqual(other.data['ai_status'], 'user_quota')
            self.assertTrue(all(d['source'] == 'engine' for d in other.data['designs']))
            # Staff are exempt
            staff = User.objects.create_user(username='office', password='pw', is_staff=True)
            self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(staff).access_token}')
            self.assertEqual(self.client.post(self.url, {**self.valid, 'seed': 4}, format='json').data['ai_status'], 'ok')
        AILayoutThrottle.rate, AILayoutThrottle.num_requests = None, None

    # ── keep-clear zones ─────────────────────────────────────────────────────
    @patch.dict(os.environ, {'GEMINI_API_KEY': 'your_gemini_api_key_here'})
    def test_customer_zones_are_kept_clear(self):
        from apps.library import layout_engine as E
        zones = ['foreground_center', 'midground_left']
        r = self.client.post(self.url, {**self.valid, 'keep_clear_zones': zones}, format='json')
        self.assertEqual(r.data['zone_scan'], 'customer')
        cells = E.zone_cells(zones)
        for d in r.data['designs']:
            for p in d['plants']:
                if not p.get('is_existing'):
                    self.assertNotIn((int(p['x'] / 0.5), int(p['z'] / 0.6)), cells)

    def test_zone_orientation_front_is_near_the_viewer(self):
        from apps.library import layout_engine as E
        from apps.library.utils import resolve_coordinates
        # z grows towards the viewer in the 3D/AR view, so "foreground" must be the high-z rows
        self.assertGreater(resolve_coordinates('foreground_center', 10, 10)['z'],
                           resolve_coordinates('background_perimeter', 10, 10)['z'])
        self.assertEqual(E.ZONES['background_perimeter']['gz'], (0, 4))

    @patch('apps.library.garden_ai.scan_image_zones')
    @patch.dict(os.environ, {'GEMINI_API_KEY': 'test_api_key'})
    def test_scan_zones_reports_failure_instead_of_hiding_it(self, mock_scan):
        url = '/api/scan-zones/'
        photo = SimpleUploadedFile('yard.jpg', _jpeg_bytes(), content_type='image/jpeg')   # a real photo: non-photos are refused before Gemini
        mock_scan.return_value = ([{'zone': 'foreground_left', 'label': 'path', 'blocks_planting': True,
                                    'is_existing': True}], None)
        r = self.client.post(url, {'image': photo}, format='multipart')
        self.assertEqual(r.data, {'status': 'ok', 'zones': mock_scan.return_value[0]})
        mock_scan.return_value = ([], 'unavailable')
        photo.seek(0)
        r = self.client.post(url, {'image': photo}, format='multipart')
        self.assertEqual(r.data['status'], 'failed')  # the UI asks the customer to mark zones

    # ── the engine's guarantees, on random gardens ───────────────────────────
    def test_engine_rules_hold_on_random_gardens(self):
        import random
        from apps.library import layout_engine as E
        rng = random.Random(42)
        # Growing needs that clash: Fern (high water) vs Rose (low water), Bamboo (aggressive roots),
        # Rose is a low sun-lover next to tall canopy — the engine must keep the companion gaps too
        base = [('1', 'Oak', 1000, 4.0, 'full_sun', 'moderate', False),
                ('2', 'Fern', 500, 1.0, 'full_shade', 'high', False),
                ('3', 'Bamboo', 800, 5.0, 'full_sun', 'moderate', True),
                ('4', 'Rose', 200, 0.5, 'full_sun', 'low', False)]
        for case in range(60):
            specs = [E.PlantSpec(i, n, p, rng.randint(1, 30), h, E.spacing_for(h), sun, water, inv)
                     for i, n, p, h, sun, water, inv in base]
            site = E.Site(rng.uniform(3, 25), rng.uniform(3, 25),
                          E.zone_cells(rng.sample(list(E.ZONES), rng.randint(0, 3))))
            budget = rng.choice([800, 3000, 9000, 30000])
            # Garden features on some cases: a seat or two and a rock group
            seat = E.PlantSpec('s', 'Bench', 300, rng.randint(0, 3), 0.8, E.SEAT_PAD, kind='seat')
            rock = E.PlantSpec('r', 'Rock', 150, rng.randint(0, 9), 0.6, 0.8, kind='rock')
            features = [(seat, rng.randint(1, 2)), (rock, rng.randint(1, 6))] if case % 2 else []
            every = specs + [f for f, _ in features]
            for style in E.STYLES:
                d = E.generate(style, specs, site, budget, seed=case, features=features)
                plan = E.GardenPlan.from_dict(d['garden_plan'])
                self.assertEqual(plan, E.garden_plan(style, site, specs, case))
                self.assertEqual(E.check(d['plants'], every, site, budget, plan), [], (case, style))
            messy = [{'plant_id': rng.choice(['1', '2', '3', '4', '999', 's']), 'x': rng.uniform(-2, 27),
                      'z': rng.uniform(-2, 27), 'rotation': 0} for _ in range(rng.randint(0, 40))]
            fixed, _ = E.repair(messy, specs, site, budget, seed=case, features=features)
            plan = E.garden_plan('Lush / Organic', site, specs, case)
            self.assertEqual(E.check(fixed, every, site, budget, plan), [], (case, 'repair'))

    # ── open lawn: beds around the edges, a usable lawn in the middle ─────────
    def test_designs_keep_an_open_lawn(self):
        from apps.library import layout_engine as E
        specs = [E.PlantSpec('palm', 'Palm', 2500, 60, 3.0, E.spacing_for(7.0), mature=7.0),
                 E.PlantSpec('croton', 'Croton', 300, 200, 0.8, E.spacing_for(1.5), mature=1.5),
                 E.PlantSpec('lantana', 'Lantana', 150, 400, 0.3, E.spacing_for(0.5), 'full_sun', 'low', mature=0.5)]
        site = E.Site(10, 12)
        for style in E.STYLES:
            d = E.generate(style, specs, site, 400000, seed=1)   # far more budget than the beds can take
            plan = E.GardenPlan.from_dict(d['garden_plan'])
            by = {s.id: s for s in specs}
            # Nothing on the lawn, and a real lawn: at least 4 m across at its middle
            self.assertFalse([p for p in d['plants'] if plan.depth_inside(p['x'], p['z']) > 0], style)
            self.assertGreaterEqual(plan.depth_inside(*_lawn_centre(plan)), 2.0, style)
            # Tall plants only in the back half; the palm fits the back bed
            tall = [p for p in d['plants'] if by[p['plant_id']].layer == 'tall']
            self.assertTrue(tall, style)
            self.assertTrue(all(p['z'] <= site.length / 2 for p in tall), style)
            if style != 'Minimalist':
                self.assertIn('lawn', d['budget_note'], style)   # says why the budget isn't all used
        # The house can reach the lawn through the front border
        sym = E.garden_plan('Symmetrical', site, specs)
        self.assertGreater(sym.front, 0)
        self.assertGreater(sym.depth_inside(5, 11.9), 0)        # the entry, at the front edge
        # A small courtyard is planted freely; a narrow side yard keeps a 2 m strip open
        self.assertIsNone(E.garden_plan('Symmetrical', E.Site(3, 3), specs).lawn)
        narrow = E.garden_plan('Lush / Organic', E.Site(3, 15), specs)
        self.assertAlmostEqual(3 - 2 * narrow.side, E.MIN_OPEN, places=3)

    # ── the yard's conditions: sun, care level, lawn ──────────────────────────
    def test_shade_yard_and_low_maintenance_choose_suitable_plants(self):
        from apps.library.models import InventoryItem
        InventoryItem.objects.filter(id=2).update(sunlight='full_shade', water_need='moderate')   # Fern
        InventoryItem.objects.filter(id=4).update(sunlight='full_sun', water_need='high')        # Rose
        r = self.client.post(self.url, {**self.valid, 'preferred_plant_ids': [], 'sun': 'shade'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        for d in r.data['designs']:
            used = {p['plant_id'] for p in d['plants'] if not p.get('is_existing')}
            self.assertNotIn('4', used, d['design_name'])                 # a full-sun rose in a shady yard
        r = self.client.post(self.url, {**self.valid, 'preferred_plant_ids': [], 'maintenance': 'low'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        for d in r.data['designs']:
            used = {p['plant_id'] for p in d['plants'] if not p.get('is_existing')}
            self.assertNotIn('4', used, d['design_name'])                 # thirsty: not for a low-care garden
        # The customer's own picks are kept, with a heads-up
        r = self.client.post(self.url, {**self.valid, 'preferred_plant_ids': ['2', '4'], 'sun': 'shade'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        d = r.data['designs'][0]
        self.assertIn('Rose needs full sun', ' '.join(d['adjustments']))
        self.assertIn('4', {p['plant_id'] for p in d['plants']})
        self.assertEqual(self.client.post(self.url, {**self.valid, 'sun': 'dark'}, format='json').status_code, 400)

    def test_sun_wins_over_upkeep_when_both_cannot_be_met(self):
        from apps.library import layout_engine as E
        from apps.library.garden_ai import suit_conditions
        sunny = E.PlantSpec('1', 'Croton', 300, 9, 0.8, 0.6, 'full_sun', 'low')
        thirsty_shade = E.PlantSpec('2', 'Peace Lily', 250, 9, 0.6, 0.45, 'full_shade', 'high')
        # Shade + low upkeep: no plant meets both. Keep the shade plant (the light is what kills), say why
        specs, notes = suit_conditions([sunny, thirsty_shade], 'shade', 'low', chosen=False)
        self.assertEqual([s.id for s in specs], ['2'])
        self.assertIn('a little more care', notes[0])
        # No plant suits the light at all: the wider catalogue, with a note
        specs, notes = suit_conditions([sunny], 'shade', 'medium', chosen=False)
        self.assertEqual([s.id for s in specs], ['1'])
        self.assertIn('that much shade', notes[0])

    def test_low_maintenance_plants_the_beds_sparingly(self):
        from apps.library import layout_engine as E
        specs = [E.PlantSpec('croton', 'Croton', 300, 400, 0.8, E.spacing_for(0.8)),
                 E.PlantSpec('lantana', 'Lantana', 150, 400, 0.3, E.spacing_for(0.3))]
        site = E.Site(10, 12)
        for style in E.STYLES:
            plan = E.garden_plan(style, site, specs)
            cap = E.low_care_cap(site, plan)
            full = E.generate(style, specs, site, 400000, seed=1)
            easy = E.generate(style, specs, site, 400000, seed=1, max_plants=cap)
            self.assertLessEqual(len(easy['plants']), cap, style)
            if style == 'Minimalist' and len(full['plants']) <= cap:   # already sparser than the cap: unchanged
                self.assertEqual(easy['plants'], full['plants'])
                continue
            self.assertLess(len(easy['plants']), len(full['plants']), style)
            self.assertIn('low-maintenance', easy['budget_note'], style)

    def test_lawn_is_priced_as_turf_and_beds_as_soil_and_mulch(self):
        from apps.library.models import PricingSettings
        from apps.library.pricing import fixed_costs, project_estimate
        ps = PricingSettings.current()
        # No lawn: unchanged (site prep + soil & mulch on the whole area)
        self.assertEqual(fixed_costs(ps, 100), (ps.site_prep_per_sqm + ps.materials_per_sqm) * 100 + ps.mobilization_fee)
        est = project_estimate({}, {}, 100, ps, lawn_sqm=40)
        by = {l['key']: l for l in est['lines']}
        self.assertEqual(by['materials']['amount'], int(ps.materials_per_sqm * 60))     # the beds only
        self.assertEqual(by['turf']['amount'], int(ps.turf_per_sqm * 40))
        self.assertEqual(by['site_prep']['amount'], int(ps.site_prep_per_sqm * 100))
        # Keeping the existing grass: no turf, and the lawn isn't dug up
        keep = {l['key']: l for l in project_estimate({}, {}, 100, ps, lawn_sqm=40, new_lawn=False)['lines']}
        self.assertNotIn('turf', keep)
        self.assertEqual(keep['site_prep']['amount'], int(ps.site_prep_per_sqm * 60))
        # Through the API: every design has a turf line for its own lawn, and stays within budget
        r = self.client.post(self.url, self.valid, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        for d in r.data['designs']:
            turf = next(l for l in d['cost_breakdown']['lines'] if l['key'] == 'turf')
            self.assertAlmostEqual(turf['amount'], float(ps.turf_per_sqm) * d['garden_plan']['lawn_area'], delta=1)
        kept = self.client.post(self.url, {**self.valid, 'lawn': 'keep'}, format='json').data
        for d in kept['designs']:
            self.assertNotIn('turf', [l['key'] for l in d['cost_breakdown']['lines']])
            self.assertLessEqual(d['cost_breakdown']['total'], 80000 + 2)

    def test_repair_moves_ai_plants_off_the_lawn(self):
        from apps.library import layout_engine as E
        specs = [E.PlantSpec('palm', 'Palm', 500, 20, 5.0, E.spacing_for(5.0)),
                 E.PlantSpec('fern', 'Fern', 100, 50, 1.0, E.spacing_for(1.0))]
        site = E.Site(10, 12)
        plan = E.garden_plan('Lush / Organic', site, specs)
        cx, cz = _lawn_centre(plan)
        ai = [{'plant_id': 'fern', 'x': cx, 'z': cz, 'rotation': 0},          # mid-lawn
              {'plant_id': 'palm', 'x': 5, 'z': 11, 'rotation': 0}]           # tall plant at the front
        fixed, notes = E.repair(ai, specs, site, 600)
        self.assertEqual(len(fixed), 2)                                         # moved, not dropped
        self.assertEqual(E.check(fixed, specs, site, 600, plan), [])
        self.assertIn('Moved 2 plants into the beds', ' '.join(notes))

    # ── botanical compatibility ──────────────────────────────────────────────
    def test_companion_gap_rules(self):
        from apps.library import layout_engine as E
        spec = lambda h, sun='full_sun', water='moderate', inv=False: E.PlantSpec('x', 'x', 1, 1, h, 1.0, sun, water, inv)
        self.assertEqual(E.companion_gap(spec(1), spec(1)), 0)
        self.assertEqual(E.companion_gap(spec(1, water='low'), spec(1, water='high')), E.HYDROZONE_GAP)
        self.assertEqual(E.companion_gap(spec(1, water='low'), spec(1, water='moderate')), 0)
        self.assertEqual(E.companion_gap(spec(5, inv=True), spec(1)), E.ROOT_BARRIER_GAP)
        self.assertEqual(E.companion_gap(spec(5, inv=True), spec(5, inv=True)), 0)
        self.assertEqual(E.companion_gap(spec(4), spec(0.4)), E.CANOPY_GAP)
        self.assertEqual(E.companion_gap(spec(4), spec(0.4, sun='full_shade')), 0)  # shade plants belong there
        self.assertEqual(E.min_distance(spec(1, water='low'), spec(1, water='high')), 1.0 + E.HYDROZONE_GAP)

    # ── the customer's plant choice is binding ───────────────────────────────
    @patch.dict(os.environ, {'GEMINI_API_KEY': 'test_api_key'})
    @patch('google.genai.Client')
    def test_designs_use_only_the_chosen_plants(self, mock_client_class):
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        # The AI slips in a palm (3) and a rose (4) the customer didn't choose
        mock_client.models.generate_content.return_value = self._ai_design(
            [('1', 3, 3), ('2', 10, 10), ('3', 15, 3), ('4', 8, 17)])
        r = self.client.post(self.url, {**self.valid, 'preferred_plant_ids': ['1', '2']}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['ai_status'], 'ok')
        prompt = mock_client.models.generate_content.call_args.kwargs['contents']
        self.assertIn("ID '1'", prompt)
        self.assertNotIn("ID '3'", prompt)   # the AI is only shown the chosen plants
        for d in r.data['designs']:
            used = {p['plant_id'] for p in d['plants'] if not p.get('is_existing')}
            self.assertTrue(used, d['design_name'])
            self.assertLessEqual(used, {'1', '2'}, d['design_name'])

    def test_no_choice_means_the_whole_catalog(self):
        r = self.client.post(self.url, {**self.valid, 'preferred_plant_ids': []}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        used = {p['plant_id'] for d in r.data['designs'] for p in d['plants'] if not p.get('is_existing')}
        self.assertGreater(len(used), 2)

    def test_chosen_plants_that_are_sold_out(self):
        from apps.library.models import InventoryItem
        InventoryItem.objects.filter(id=2).update(stock_quantity=0)
        r = self.client.post(self.url, {**self.valid, 'preferred_plant_ids': ['1', '2']}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        for d in r.data['designs']:
            self.assertIn('Left out Fern: not available right now', ' '.join(d['adjustments']), d['design_name'])
            self.assertLessEqual({p['plant_id'] for p in d['plants'] if not p.get('is_existing')}, {'1'})
        InventoryItem.objects.filter(id=1).update(stock_quantity=0)
        r = self.client.post(self.url, {**self.valid, 'preferred_plant_ids': ['1', '2'], 'seed': 2}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('None of the plants you chose are available', r.data['error'])

    # ── garden features (seating, accent rocks) ───────────────────────────────
    def _features_setup(self):
        from apps.library.models import InventoryItem
        InventoryItem.objects.create(id=50, name='Outdoor bench', category='furniture', unit_price=3000,
                                     stock_quantity=4, real_world_size=0.8, model_file='outdoor_bench.glb')
        InventoryItem.objects.create(id=51, name='Volcanic rock', category='hardscape', unit_price=500,
                                     stock_quantity=20, real_world_size=0.6, model_file='volcanic_rock.glb')

    @patch.dict(os.environ, {'GEMINI_API_KEY': 'your_gemini_api_key_here'})
    def test_features_are_placed_priced_and_keep_a_walkway(self):
        from dataclasses import replace
        from apps.library import layout_engine as E
        from apps.library.models import InventoryItem, PricingSettings
        from apps.library.pricing import installed_price, plant_budget_from_all_in
        self._features_setup()
        r = self.client.post(self.url, {**self.valid, 'features': [{'id': 50, 'quantity': 2}, {'id': 51, 'quantity': 3}]},
                             format='json')
        self.assertEqual(r.status_code, 200, r.data)
        ps = PricingSettings.current()
        items = list(InventoryItem.objects.filter(category='plant'))
        feats = list(InventoryItem.objects.filter(id__in=[50, 51]))
        specs = [replace(s, price=installed_price(ps, i)) for s, i in zip(E.specs_from_inventory(items), items)]
        specs += [replace(s, price=installed_price(ps, i)) for s, i in zip(E.feature_specs_from_inventory(feats), feats)]
        site = E.Site(10, 12)
        for d in r.data['designs']:
            plants = [p for p in d['plants'] if not p.get('is_existing')]
            ids = [p['plant_id'] for p in plants]
            self.assertEqual((ids.count('50'), ids.count('51')), (2, 3), d['design_name'])
            self.assertEqual(E.check(plants, specs, site, plant_budget_from_all_in(80000, 120, ps)), [], d['design_name'])
            rows = {row['plant_id']: row for row in d['plant_breakdown']}
            self.assertEqual(rows['50']['quantity'], 2)
            self.assertEqual(rows['50']['category'], 'furniture')
            order = [row['category'] for row in d['plant_breakdown']]
            self.assertEqual(order, sorted(order, key=['plant', 'hardscape', 'furniture'].index))
            self.assertEqual(rows['51']['subtotal'], 1500)
            self.assertLessEqual(d['cost_breakdown']['total'], 80000 + 2)

    def test_feature_requests_are_validated(self):
        self._features_setup()
        for features, msg in [([{'id': 50, 'quantity': 3}], 'at most 2 seats'),
                              ([{'id': 1, 'quantity': 1}], 'no longer available'),   # a plant isn't a feature
                              ([{'id': 51, 'quantity': 11}], 'choose at most 10 boulders'),    # 10 × 12 m: 120 m² → 10
                              ([{'id': 51, 'quantity': 301}], 'between 1 and 300'),
                              ([{'quantity': 1}], 'numeric id')]:
            r = self.client.post(self.url, {**self.valid, 'features': features}, format='json')
            self.assertEqual(r.status_code, 400, features)
            self.assertIn(msg, r.data['error'])

    @patch.dict(os.environ, {'GEMINI_API_KEY': 'your_gemini_api_key_here'})
    def test_small_garden_stones_come_by_the_dozen_in_clusters_and_cost_less_to_place(self):
        from apps.library.models import InventoryItem, PricingSettings
        from apps.library.pricing import installed_price, labour_rate, layer_of
        stone = InventoryItem.objects.create(id=52, name='River stones (small)', category='hardscape', unit_price=40,
                                             stock_quantity=200, real_world_size=0.22, model_file='blueish_river_rock.glb')
        boulder = InventoryItem.objects.create(id=53, name='River rock', category='hardscape', unit_price=500,
                                               stock_quantity=20, real_world_size=0.6, model_file='blueish_river_rock.glb')
        ps = PricingSettings.current()
        self.assertEqual((layer_of(stone), layer_of(boulder)), ('stone', 'other'))
        self.assertEqual(labour_rate(ps, 'stone'), ps.install_other / 5)          # a fifth of setting a boulder
        self.assertEqual(installed_price(ps, stone), int(40 + ps.install_other / 5))
        r = self.client.post(self.url, {**self.valid, 'budget': 150000, 'features': [{'id': 52, 'quantity': 24}]}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        for d in r.data['designs']:
            stones = [p for p in d['plants'] if p['plant_id'] == '52']
            self.assertEqual(len(stones), 24, d['design_name'])
            # in clusters (about seven each), not one long scatter: every stone has a neighbour within 1 m
            for p in stones:
                near = min(((p['x'] - q['x']) ** 2 + (p['z'] - q['z']) ** 2) ** 0.5 for q in stones if q is not p)
                self.assertLess(near, 1.0, (d['design_name'], p))

    def test_every_pick_amount_and_feature_is_in_every_style(self):
        """The customer's brief comes first in all three styles (2026-11-12: 3 bamboo came out as 2 / 1 / 0, a
        picked palm vanished without a word, and the stones went missing)."""
        from apps.library import layout_engine as E
        specs = [E.PlantSpec('bamboo', 'Bamboo', 650, 50, 5.0, E.spacing_for(8.0, 150), mature=8.0, is_invasive=True),
                 E.PlantSpec('palm', 'Christmas Palm', 850, 7, 4.0, E.spacing_for(6.5, 250), mature=6.5),
                 E.PlantSpec('croton', 'Croton', 100, 95, 1.0, E.spacing_for(1.8), mature=1.8),
                 E.PlantSpec('strelitzia', 'Strelitzia', 400, 34, 1.2, E.spacing_for(1.5), mature=1.5),
                 E.PlantSpec('geranium', 'Geranium', 85, 64, 0.4, E.spacing_for(0.5), mature=0.5)]
        bench = E.PlantSpec('bench', 'Bamboo bench', 3500, 5, 0.8, E.SEAT_PAD, kind='seat')
        stones = E.PlantSpec('stones', 'River stones (small)', 40, 400, 0.22, 0.32, kind='rock')
        site = E.Site(10, 12)
        for style in E.STYLES:
            d = E.generate(style, specs, site, 40000, [s.id for s in specs], 7, [(bench, 1), (stones, 12)],
                           quantities={'bamboo': 3})
            ids = [p['plant_id'] for p in d['plants']]
            self.assertEqual(ids.count('bamboo'), 3, style)                          # the amount, exactly
            self.assertEqual((ids.count('bench'), ids.count('stones')), (1, 12), style)   # their features
            for s in specs:
                self.assertIn(s.id, ids, f'{style}: {s.name} missing, notes {d["feature_notes"]}')
            plan = E.GardenPlan.from_dict(d['garden_plan'])
            self.assertEqual(E.check(d['plants'], specs + [bench, stones], site, 40000, plan), [], style)
            # the bamboo is a row along the back fence (a screen), not three trees dotted about
            zs = {p['z'] for p in d['plants'] if p['plant_id'] == 'bamboo'}
            self.assertEqual(len(zs), 1, style)

    def test_plant_amounts_are_met_or_explained(self):
        from apps.library import layout_engine as E
        from apps.library.models import InventoryItem
        r = self.client.post(self.url, {**self.valid, 'preferred_plant_ids': ['1', '2', '4'],
                                        'plant_qty': [{'id': 4, 'quantity': 12}, {'id': 1, 'quantity': 9}]}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        specs = {s.id: s for s in E.specs_from_inventory(InventoryItem.objects.all())}
        site = E.Site(10, 12)
        for d in r.data['designs']:
            ids = [p['plant_id'] for p in d['plants'] if not p.get('is_existing')]
            self.assertEqual(d['source'], 'engine', d['design_name'])                 # amounts: never the AI's guess
            self.assertEqual(ids.count('4'), 12, d['design_name'])                   # roses: exactly as asked
            oaks = ids.count('1')
            self.assertLessEqual(oaks, 9, d['design_name'])                          # never more than asked
            if oaks < 9:                                                             # fewer: said why
                self.assertTrue(any(f'Fitted {oaks} of your 9 Oak Tree' in n for n in d['adjustments']), d['adjustments'])
            self.assertEqual(E.check(d['plants'], list(specs.values()), site, 10 ** 9), [], d['design_name'])

    def test_plant_amounts_are_validated(self):
        for qty, msg in [([{'id': 3, 'quantity': 2}], 'only for plants you picked'),
                         ([{'id': 1, 'quantity': 0}], 'between 1 and 300'),
                         ([{'id': 1}], 'numeric id and quantity')]:
            r = self.client.post(self.url, {**self.valid, 'plant_qty': qty}, format='json')
            self.assertEqual(r.status_code, 400, qty)
            self.assertIn(msg, r.data['error'])

    @patch.dict(os.environ, {'GEMINI_API_KEY': 'your_gemini_api_key_here'})
    def test_rock_limits_follow_the_yard_size(self):
        from apps.library.garden_ai import rock_limits
        self.assertEqual(rock_limits(20), (3, 24))          # a small front yard: the floors
        self.assertEqual(rock_limits(120), (10, 120))
        self.assertEqual(rock_limits(2500), (30, 300))      # a big lot: the ceilings
        self._features_setup()
        InventoryItem = __import__('apps.library.models', fromlist=['InventoryItem']).InventoryItem
        InventoryItem.objects.filter(id=51).update(stock_quantity=40)
        big = {**self.valid, 'lot_width': 20, 'lot_length': 15, 'budget': 400000}   # 300 m² → 25 boulders
        r = self.client.post(self.url, {**big, 'features': [{'id': 51, 'quantity': 15}]}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        for d in r.data['designs']:
            placed = sum(p['plant_id'] == '51' for p in d['plants'])
            notes = ' '.join(d['adjustments'])
            self.assertTrue(placed == 15 or 'Left out' in notes, (d['design_name'], placed, notes))
            self.assertGreater(placed, 9, d['design_name'])   # more than the old fixed limit of 9
        r = self.client.post(self.url, {**self.valid, 'features': [{'id': 51, 'quantity': 15}]}, format='json')
        self.assertEqual(r.status_code, 400)                  # the same 15 in a 120 m² yard is too many

    def test_groundcover_fills_the_beds_and_is_priced_per_m2(self):
        from apps.library.models import InventoryItem, PricingSettings
        from apps.library.pricing import installed_price, labour_rate, layer_of
        sun = InventoryItem.objects.create(id=60, name='Mani-mani', category='plant', is_groundcover=True, unit_price=180,
                                           stock_quantity=300, real_world_size=0.15, sunlight='full_sun')
        shade = InventoryItem.objects.create(id=61, name='Asiatic jasmine', category='plant', is_groundcover=True, unit_price=240,
                                             stock_quantity=300, real_world_size=0.15, sunlight='full_shade')
        ps = PricingSettings.current()
        self.assertEqual(layer_of(sun), 'groundcover')
        self.assertEqual(installed_price(ps, sun), int(180 + labour_rate(ps, 'groundcover')))
        body = {**self.valid, 'preferred_plant_ids': [], 'budget': 150000}
        r = self.client.post(self.url, body, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        for d in r.data['designs']:
            ids = {p['plant_id'] for p in d['plants']}
            self.assertFalse(ids & {'60', '61'}, d['design_name'])            # never placed one by one
            gc = {g['plant_id']: g['area_sqm'] for g in d['groundcover']}
            self.assertIn('60', gc, d['design_name'])                          # the sun one over the beds
            lawn = d['garden_plan']['lawn_area']
            self.assertLessEqual(sum(gc.values()), 10 * 12 - lawn + 0.5)       # never more than the beds
            line = next(l for l in d['plant_breakdown'] if l['plant_id'] == '60')
            self.assertEqual((line['quantity'], line['unit']), (gc['60'], 'm²'))   # priced per m²
            self.assertTrue(any(n.startswith('Groundcover:') for n in d['adjustments']), d['adjustments'])
            self.assertLessEqual(d['total_cost'], 150000 * 1.02, d['design_name'])
        off = self.client.post(self.url, {**body, 'groundcover': 'off'}, format='json')
        for d in off.data['designs']:
            self.assertEqual(d.get('groundcover') or [], [], d['design_name'])
            self.assertFalse(any(l.get('groundcover') for l in d['plant_breakdown']))
        # Picking a groundcover doesn't make it a plant to place, nor a "not available" plant
        r = self.client.post(self.url, {**body, 'preferred_plant_ids': ['2', '61']}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        for d in r.data['designs']:
            self.assertEqual({g['plant_id'] for g in d['groundcover']}, {'61'}, d['design_name'])
            self.assertFalse(any('not available' in n for n in d['adjustments']), d['adjustments'])

    def test_planting_follows_designer_rules(self):
        """Focal specimen, a repeated palette in drifts round the lawn, low plants on the lawn edge, matching edging."""
        import math
        from apps.library import layout_engine as E
        specs = [E.PlantSpec('palm', 'Palm', 2500, 20, 3.0, E.spacing_for(7.0), mature=7.0),
                 E.PlantSpec('plumeria', 'Plumeria', 900, 20, 2.0, E.spacing_for(4.0), mature=4.0),
                 E.PlantSpec('croton', 'Croton', 300, 200, 0.8, E.spacing_for(1.5), mature=1.5),
                 E.PlantSpec('heliconia', 'Heliconia', 350, 200, 1.0, E.spacing_for(1.8), mature=1.8),
                 E.PlantSpec('lily', 'Peace lily', 250, 300, 0.4, E.spacing_for(0.6), mature=0.6),
                 E.PlantSpec('geranium', 'Geranium', 120, 400, 0.3, E.spacing_for(0.5), mature=0.5)]
        by = {s.id: s for s in specs}
        site = E.Site(10, 12)
        d = E.generate('Lush / Organic', specs, site, 120000, seed=2)
        plan = E.GardenPlan.from_dict(d['garden_plan'])
        self.assertEqual(E.check(d['plants'], specs, site, 120000), [])
        # The focal specimen: the dearest tall plant, in the back bed near the lawn's centre line
        palms = [p for p in d['plants'] if p['plant_id'] == 'palm']
        self.assertTrue(any(abs(p['x'] - plan.lawn[1]) < 2.5 and p['z'] < plan.back + 0.5 for p in palms), palms)
        # Low plants frame the lawn: many within a metre of its edge
        lows = [p for p in d['plants'] if by[p['plant_id']].layer == 'low']
        edge = [p for p in lows if -1.0 <= plan.depth_inside(p['x'], p['z']) < 0]
        self.assertGreaterEqual(len(edge), 8, len(edge))
        # Repetition: some mid or low plant appears in groups on opposite sides of the lawn
        sides = {}
        for p in d['plants']:
            if by[p['plant_id']].layer in ('mid', 'low'):
                sides.setdefault(p['plant_id'], set()).add(p['x'] < plan.lawn[1])
        self.assertTrue(any(len(v) == 2 for v in sides.values()), sides)
        # Symmetrical: the low border's plant repeats as edging down both side beds, mirrored
        d = E.generate('Symmetrical', specs, site, 120000, seed=2)
        plan = E.GardenPlan.from_dict(d['garden_plan'])
        x0 = plan.lawn[1]
        edging = [p for p in d['plants'] if by[p['plant_id']].layer == 'low' and abs(p['x'] - x0) < 0.6
                  and plan.back < p['z'] < site.length - plan.front]
        self.assertGreaterEqual(len(edging), 3, edging)
        for p in edging:
            self.assertTrue(any(abs(q['x'] - (10 - p['x'])) < 0.01 and abs(q['z'] - p['z']) < 0.01 and q['plant_id'] == p['plant_id']
                                for q in d['plants']), p)

    def test_features_that_dont_fit_are_reported(self):
        from apps.library import layout_engine as E
        plants = [E.PlantSpec('p', 'Palm', 500, 20, 4.0, E.spacing_for(4.0))]
        seat = E.PlantSpec('s', 'Bench', 300, 5, 0.8, E.SEAT_PAD, kind='seat')
        d = E.generate('Lush / Organic', plants, E.Site(3, 3), 5000, features=[(seat, 2)])
        self.assertEqual(sum(p['plant_id'] == 's' for p in d['plants']), 1)   # a 3 m lot fits one seat
        self.assertIn('Left out 1 garden feature (Bench)', d['feature_notes'][0])

    def test_shade_plants_are_planted_in_shade(self):
        from apps.library import layout_engine as E
        specs = [E.PlantSpec('palm', 'Palm', 500, 20, 4.0, E.spacing_for(4.0)),
                 E.PlantSpec('lily', 'Peace Lily', 250, 40, 0.6, 0.45, 'full_shade', 'high')]
        site = E.Site(10, 12)
        for style in E.STYLES:
            plants = E.generate(style, specs, site, 30000, ['lily'], seed=3)['plants']
            lilies = [p for p in plants if p['plant_id'] == 'lily']
            self.assertTrue(lilies, style)
            # Not in the sunny front border: (nearly) every shade plant sits beside a tall plant
            palms = [p for p in plants if p['plant_id'] == 'palm']
            shaded = [q for q in lilies if any(math.hypot(q['x'] - t['x'], q['z'] - t['z']) <= 3.0 for t in palms)]
            self.assertGreaterEqual(len(shaded), 0.8 * len(lilies), style)

    def test_compatibility_warnings(self):
        from apps.library.compatibility import evaluate_plant_compatibility, format_compatibility_prompt_note
        plants = [{'id': 1, 'name': 'Plumeria', 'water_need': 'low', 'real_world_size': 2.0},
                  {'id': 2, 'name': 'Banana', 'water_need': 'high', 'real_world_size': 3.5},
                  {'id': 3, 'name': 'Bamboo', 'is_invasive': True, 'real_world_size': 5.0},
                  {'id': 4, 'name': 'Geranium', 'water_need': 'low', 'real_world_size': 0.4},
                  {'id': 5, 'name': 'Fern', 'sunlight': 'full_shade', 'real_world_size': 1.0}]
        ids = {w['id'] for w in evaluate_plant_compatibility(plants)}
        self.assertEqual(ids, {'moisture_conflict', 'sunlight_conflict', 'invasive_roots', 'canopy_shading'})
        self.assertEqual(evaluate_plant_compatibility(plants[:1]), [])
        self.assertEqual(evaluate_plant_compatibility([plants[0], plants[3]]), [])  # both low water: fine
        self.assertIn('Banana', format_compatibility_prompt_note(evaluate_plant_compatibility(plants[:2])))

    @patch.dict(os.environ, {'GEMINI_API_KEY': 'your_gemini_api_key_here'})
    def test_conflicting_preferred_plants_still_generate_with_warnings(self):
        from apps.library.models import InventoryItem
        InventoryItem.objects.filter(id=2).update(water_need='high')
        InventoryItem.objects.filter(id=4).update(water_need='low')
        r = self.client.post(self.url, {**self.valid, 'preferred_plant_ids': ['2', '4']}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual([w['id'] for w in r.data['compatibility_warnings']], ['moisture_conflict'])
        self._assert_three_valid_designs(r.data)  # check() includes the watering-zone gap

    @patch.dict(os.environ, {'GEMINI_API_KEY': 'your_gemini_api_key_here'})
    def test_budget_too_small_for_the_site_says_what_it_takes(self):
        r = self.client.post(self.url, {**self.valid, 'budget': 20000}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn("doesn't cover the basics", r.data['error'])
        self.assertGreater(r.data['min_budget'], 20000)
        ok = self.client.post(self.url, {**self.valid, 'budget': r.data['min_budget']}, format='json')
        self.assertEqual(ok.status_code, 200)

    @patch.dict(os.environ, {'GEMINI_API_KEY': 'your_gemini_api_key_here'})
    def test_budget_range_gives_three_styles_for_the_same_budget(self):
        # (2026-11-12: was good / better / best across the range — Minimalist at the low end looked like it ignored
        # the budget. Now every style aims at the top of the range and spends it its own way.)
        r = self.client.post(self.url, {**self.valid, 'budget': None, 'budget_min': 60000, 'budget_max': 120000},
                             format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['budget_range'], {'min': 60000, 'max': 120000})
        for d in r.data['designs']:
            self.assertEqual(d['target_budget'], 120000, d['design_name'])
            self.assertLessEqual(d['cost_breakdown']['total'], d['target_budget'] + 2, d['design_name'])

    @patch.dict(os.environ, {'GEMINI_API_KEY': 'your_gemini_api_key_here'})
    def test_range_low_end_is_lifted_to_a_workable_budget(self):
        r = self.client.post(self.url, {**self.valid, 'budget': None, 'budget_min': 1000, 'budget_max': 90000},
                             format='json')
        self.assertEqual(r.status_code, 200, r.data)
        minimalist = next(d for d in r.data['designs'] if d['design_name'] == 'Minimalist')
        self.assertGreater(minimalist['target_budget'], 1000)
        self.assertTrue([p for p in minimalist['plants'] if not p.get('is_existing')])

    @patch.dict(os.environ, {'GEMINI_API_KEY': 'your_gemini_api_key_here'})
    def test_plants_only_mode_has_no_labour_or_site_work(self):
        r = self.client.post(self.url, {**self.valid, 'budget': None, 'budget_min': 5000, 'budget_max': 10000,
                                        'install': False}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertFalse(r.data['install'])
        for d in r.data['designs']:
            keys = [l['key'] for l in d['cost_breakdown']['lines']]
            self.assertEqual(keys, ['plants', 'delivery'])
            self.assertLessEqual(d['cost_breakdown']['total'], d['target_budget'])
            self.assertEqual(d['cost_breakdown']['low'], d['cost_breakdown']['high'])  # exact catalog prices

    def test_inverted_range_is_rejected(self):
        r = self.client.post(self.url, {**self.valid, 'budget': None, 'budget_min': 90000, 'budget_max': 50000},
                             format='json')
        self.assertEqual(r.status_code, 400)

    def test_budget_suggestion_for_the_yard(self):
        r = self.client.get('/api/pricing/suggest/', {'area_sqm': 120})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['bands'][2]['label'], '₱50K – ₱100K')   # same labels as the Project Hub
        low, high = r.data['suggested']['low'], r.data['suggested']['high']
        self.assertLess(low, high)
        self.assertGreaterEqual(low, 15000)                               # never below the minimum project
        small = self.client.get('/api/pricing/suggest/', {'area_sqm': 120, 'mode': 'plants'}).data
        self.assertLess(small['suggested']['high'], high)                 # plants only costs less
        self.assertEqual(self.client.get('/api/pricing/suggest/', {'area_sqm': 'x'}).status_code, 400)

    def test_engine_explains_unused_budget(self):
        from apps.library import layout_engine as E
        specs = [E.PlantSpec('1', 'Oak', 1000, 2, 4.0, E.spacing_for(4.0))]
        d = E.generate('Minimalist', specs, E.Site(8, 8), 100000)   # room in the back bed for both oaks
        self.assertIn('stock', d['budget_note'].lower())


class CustomerEditReviewTests(APITestCase):
    """AI Designer phase B: designer's notes and "Tidy up" on a design the customer edited in 3D."""

    def _random_case(self, rng, E, case):
        base = [('1', 'Oak', 1000, 4.0, 'full_sun', 'moderate', False),
                ('2', 'Fern', 500, 1.0, 'full_shade', 'high', False),
                ('3', 'Bamboo', 800, 5.0, 'full_sun', 'moderate', True),
                ('4', 'Rose', 200, 0.5, 'full_sun', 'low', False)]
        specs = [E.PlantSpec(i, n, p, rng.randint(5, 30), h, E.spacing_for(h), sun, water, inv)
                 for i, n, p, h, sun, water, inv in base]
        site = E.Site(rng.uniform(4, 20), rng.uniform(4, 20), E.zone_cells(rng.sample(list(E.ZONES), rng.randint(0, 2))))
        seat = E.PlantSpec('s', 'Bench', 300, 2, 0.8, E.SEAT_PAD, kind='seat')
        rock = E.PlantSpec('r', 'Rock', 150, 9, 0.6, 0.8, kind='rock')
        features = [(seat, 1), (rock, 3)] if case % 2 else []
        return specs, site, features, specs + [f for f, _ in features]

    def test_engine_designs_get_no_notes_and_tidy_settles_random_edits(self):
        import random
        from apps.library import layout_engine as E
        rng = random.Random(7)
        for case in range(30):
            specs, site, features, every = self._random_case(rng, E, case)
            for style in E.STYLES:
                d = E.generate(style, specs, site, rng.choice([3000, 9000, 30000]), seed=case, features=features)
                plan = E.GardenPlan.from_dict(d['garden_plan'])
                plants = d['plants']
                # The engine's own design: nothing to say
                self.assertEqual(E.review(plants, every, site, plan), [], (case, style))
                # The customer drags a few things anywhere (seats stay where the engine put them)
                edited = [dict(p) for p in plants]
                movable = [i for i, p in enumerate(edited) if p['plant_id'] != 's']
                for i in rng.sample(movable, min(len(movable), rng.randint(1, 6))):
                    edited[i].update(x=rng.uniform(0.1, site.width - 0.1), z=rng.uniform(0.1, site.length - 0.1))
                dragged = sum(1 for a, b in zip(plants, edited) if (a['x'], a['z']) != (b['x'], b['z']))
                moved, notes = E.tidy(edited, every, site, plan)
                self.assertLessEqual(len(moved), 2 * dragged, (case, style, 'tidy moves only what the edit disturbed'))
                for i, (x, z) in moved.items():
                    self.assertNotEqual(edited[i]['plant_id'], 's', 'seats are never moved')
                    edited[i] = {**edited[i], 'x': x, 'z': z}
                self.assertEqual(len(edited), len(plants), 'tidy never adds or removes')
                if not any('no room' in n for n in notes):
                    self.assertEqual(E.review(edited, every, site, plan), [], (case, style, notes))

    def test_tidy_moves_only_the_plant_dragged_into_a_crowd(self):
        """Browser finding 2026-11-08: one croton dragged among geraniums made tidy move 10 plants."""
        from apps.library import layout_engine as E
        croton = E.PlantSpec('croton', 'Croton', 300, 99, 1.0, E.spacing_for(1.8), mature=1.8)
        geranium = E.PlantSpec('ger', 'Geranium', 80, 99, 0.4, E.spacing_for(0.5), mature=0.5)
        site = E.Site(10, 10)
        plan = E.GardenPlan(lawn=('rect', 2.5, 7.5, 2.5, 7.5))
        border = [{'plant_id': 'ger', 'x': 9.2, 'z': 3.0 + 0.8 * k} for k in range(6)]   # a valid side border
        self.assertEqual(E.review(border, [geranium], site, plan), [])
        edited = border + [{'plant_id': 'croton', 'x': 9.1, 'z': 4.2}]                  # dropped into it
        moved, notes = E.tidy(edited, [croton, geranium], site, plan)
        self.assertEqual(list(moved), [6], notes)
        after = [dict(p) for p in edited]
        after[6].update(x=moved[6][0], z=moved[6][1])
        self.assertEqual(E.review(after, [croton, geranium], site, plan), [])
        same = E.review([{'plant_id': 'ger', 'x': 9.2, 'z': 3.0}, {'plant_id': 'ger', 'x': 9.2, 'z': 3.1}], [geranium], site, plan)
        self.assertTrue(same[0]['message'].startswith('Two Geranium plants are'))

    def test_each_problem_gets_a_plain_note(self):
        from apps.library import layout_engine as E
        palm = E.PlantSpec('palm', 'Christmas Palm', 2500, 9, 4.0, E.spacing_for(6.5), mature=6.5)
        croton = E.PlantSpec('croton', 'Croton', 300, 9, 1.0, E.spacing_for(1.8), mature=1.8)
        bamboo = E.PlantSpec('bamboo', 'Bamboo', 800, 9, 5.0, E.spacing_for(8.0), is_invasive=True, mature=8.0)
        site = E.Site(10, 10, E.zone_cells(['midground_left']))
        plan = E.GardenPlan(lawn=('rect', 3.0, 7.0, 3.0, 7.0))
        kinds = lambda plants: {n['kind'] for n in E.review(plants, [palm, croton, bamboo], site, plan)}  # noqa: E731
        self.assertEqual(kinds([{'plant_id': 'croton', 'x': 5, 'z': 5}]), {'lawn'})
        self.assertIn('forward', kinds([{'plant_id': 'palm', 'x': 1.8, 'z': 9.0}]))
        self.assertEqual(kinds([{'plant_id': 'croton', 'x': 0.1, 'z': 2.0}]), {'edge'})
        self.assertIn('keep_clear', kinds([{'plant_id': 'croton', 'x': 1.5, 'z': 4.0}]))
        notes = E.review([{'plant_id': 'croton', 'x': 9.0, 'z': 1.2}, {'plant_id': 'bamboo', 'x': 9.0, 'z': 2.2}],
                         [croton, bamboo], E.Site(10, 10), plan)
        spacing = [n for n in notes if n['kind'] == 'spacing']
        self.assertEqual(spacing[0]['items'], [0, 1])
        self.assertIn('root barrier', spacing[0]['message'])
        self.assertIn('1 m apart', spacing[0]['message'])

    def test_notes_have_a_short_headline_for_each_plant(self):
        """The plant's details panel shows a few words from that plant's side, then the reason."""
        from apps.library import layout_engine as E
        croton = E.PlantSpec('croton', 'Croton', 300, 9, 1.0, E.spacing_for(1.8), mature=1.8)
        bamboo = E.PlantSpec('bamboo', 'Bamboo', 800, 9, 5.0, E.spacing_for(8.0), is_invasive=True, mature=8.0)
        plan = E.GardenPlan(lawn=('rect', 3.0, 7.0, 3.0, 7.0))
        notes = E.review([{'plant_id': 'croton', 'x': 9.0, 'z': 1.2}, {'plant_id': 'bamboo', 'x': 9.0, 'z': 2.2}],
                         [croton, bamboo], E.Site(10, 10), plan)
        spacing = next(n for n in notes if n['kind'] == 'spacing')
        self.assertTrue(spacing['short'][0].startswith('Too close to the Bamboo: 1 m apart, needs'), spacing['short'])
        self.assertTrue(spacing['short'][1].startswith('Too close to the Croton: 1 m apart, needs'), spacing['short'])
        self.assertIn('root barrier', spacing['advice'])
        self.assertNotIn('apart', spacing['advice'])          # the numbers are in the headline, not repeated
        lawn = E.review([{'plant_id': 'croton', 'x': 5, 'z': 5}], [croton], E.Site(10, 10), plan)
        self.assertEqual(lawn[0]['short'], {0: 'On the open lawn'})
        twins = E.review([{'plant_id': 'croton', 'x': 9, 'z': 1}, {'plant_id': 'croton', 'x': 9, 'z': 1.2}], [croton], E.Site(10, 10), plan)
        self.assertTrue(twins[0]['short'][0].startswith('Too close to another Croton'))

    def test_tidy_one_plant_moves_only_that_plant(self):
        """"Move apart" on one plant: it moves the shortest distance, nothing else does."""
        from apps.library import layout_engine as E
        croton = E.PlantSpec('croton', 'Croton', 300, 99, 1.0, E.spacing_for(1.8), mature=1.8)
        geranium = E.PlantSpec('ger', 'Geranium', 80, 99, 0.4, E.spacing_for(0.5), mature=0.5)
        site = E.Site(10, 10)
        plan = E.GardenPlan(lawn=('rect', 2.5, 7.5, 2.5, 7.5))
        edited = [{'plant_id': 'ger', 'x': 9.2, 'z': 3.0 + 0.8 * k} for k in range(6)] + [{'plant_id': 'croton', 'x': 9.1, 'z': 4.2}]
        specs = [croton, geranium]
        moved, notes = E.tidy(edited, specs, site, plan, only={6})
        self.assertEqual(list(moved), [6])
        self.assertTrue(notes[0].startswith('Moved Croton '), notes)
        after = [dict(p) for p in edited]
        after[6].update(x=moved[6][0], z=moved[6][1])
        self.assertFalse(any(6 in n['items'] for n in E.review(after, specs, site, plan)))
        # A geranium next to the croton: only that geranium may move, the croton stays where they put it
        moved, _ = E.tidy(edited, specs, site, plan, only={1})
        self.assertLessEqual(set(moved), {1})
        # A plant with nothing wrong: nothing moves
        moved, notes = E.tidy(after, specs, site, plan, only={0})
        self.assertEqual(moved, {})
        self.assertEqual(notes, ['It already has room: nothing to move.'])

    def test_tidy_finds_a_tight_spot_the_ring_search_steps_over(self):
        """Browser finding 2026-11-22: in a packed engine design a palm dragged out of place found no spot at all
        (its old one only just fits); tidy now falls back to a fine search of the whole yard."""
        from unittest.mock import patch
        from apps.library import layout_engine as E
        croton = E.PlantSpec('croton', 'Croton', 300, 99, 1.0, E.spacing_for(1.8), mature=1.8)
        geranium = E.PlantSpec('ger', 'Geranium', 80, 99, 0.4, E.spacing_for(0.5), mature=0.5)
        site, plan = E.Site(10, 10), E.GardenPlan(lawn=('rect', 2.5, 7.5, 2.5, 7.5))
        edited = [{'plant_id': 'ger', 'x': 9.2, 'z': 3.0 + 0.8 * k} for k in range(6)] + [{'plant_id': 'croton', 'x': 9.1, 'z': 4.2}]
        with patch.object(E.Layout, 'find_spot', return_value=None):   # the coarse search misses everything
            moved, notes = E.tidy(edited, [croton, geranium], site, plan, only={6})
        self.assertEqual(list(moved), [6], notes)
        after = [dict(p) for p in edited]
        after[6].update(x=moved[6][0], z=moved[6][1])
        self.assertFalse(any(6 in n['items'] for n in E.review(after, [croton, geranium], site, plan)))
        self.assertLess(((moved[6][0] - 9.1) ** 2 + (moved[6][1] - 4.2) ** 2) ** 0.5, 3.0)   # near, not anywhere

    def test_review_endpoint_notes_and_tidy(self):
        from apps.library.models import InventoryItem
        palm = InventoryItem.objects.create(name='Christmas Palm', category='plant', unit_price=900, stock_quantity=5,
                                            real_world_size=4.0, mature_height=6.5)
        croton = InventoryItem.objects.create(name='Croton', category='plant', unit_price=200, stock_quantity=0,
                                              real_world_size=1.0)   # sold out: still checked, it's in their design
        plan = {'lawn': {'shape': 'rect', 'x0': 3.0, 'x1': 7.0, 'z0': 3.0, 'z1': 7.0}, 'entry': None,
                'beds': {'back': 3.0, 'side': 3.0, 'front': 3.0}, 'lawn_area': 16.0}
        body = {'lot_width': 10, 'lot_length': 10, 'garden_plan': plan, 'plants': [
            {'plant_id': palm.id, 'x': 2.5, 'z': 2.5, 'uid': 'p0'},
            {'plant_id': croton.id, 'x': 2.0, 'z': 2.2, 'uid': 'p1'},      # under the palm
            {'plant_id': croton.id, 'x': 5.0, 'z': 5.0, 'uid': 'p2'},      # on the lawn
            {'is_existing': True, 'type': 'structural_wall', 'x': 0, 'z': 0}]}
        r = self.client.post('/api/layout-review/', body, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        by_kind = {n['kind']: n for n in r.data['notes']}
        self.assertEqual(by_kind['spacing']['uids'], ['p0', 'p1'])
        self.assertEqual(by_kind['lawn']['uids'], ['p2'])
        self.assertNotIn('moves', r.data)
        tidy = self.client.post('/api/layout-review/', {**body, 'tidy': True}, format='json')
        self.assertEqual(tidy.status_code, 200, tidy.data)
        self.assertEqual({m['uid'] for m in tidy.data['moves']}, {'p1', 'p2'})   # the palm (tallest, fine) stays
        self.assertEqual(tidy.data['notes'], [])
        self.assertTrue(tidy.data['tidy_notes'][0].startswith('Moved 2 items'))
        # "Move apart" on one plant (its uid): only that one moves; the headlines come per uid
        self.assertEqual(by_kind['spacing']['short_by_uid'], {
            'p0': by_kind['spacing']['short_by_uid']['p0'], 'p1': by_kind['spacing']['short_by_uid']['p1']})
        self.assertTrue(by_kind['spacing']['short_by_uid']['p1'].startswith('Too close to the Christmas Palm'))
        self.assertNotIn('short', by_kind['spacing'])
        one = self.client.post('/api/layout-review/', {**body, 'tidy_uid': 'p1'}, format='json')
        self.assertEqual(one.status_code, 200, one.data)
        self.assertEqual([m['uid'] for m in one.data['moves']], ['p1'])
        self.assertTrue(one.data['tidy_notes'][0].startswith('Moved Croton'))
        self.assertEqual([n['kind'] for n in one.data['notes']], ['lawn'])   # p2 on the lawn was left alone
        gone = self.client.post('/api/layout-review/', {**body, 'tidy_uid': 'nope'}, format='json')
        self.assertEqual((gone.data['moves'], gone.data['tidy_notes']), ([], ['That plant is no longer in this design.']))
        for bad in ({**body, 'plants': 'x'}, {**body, 'lot_width': 0}, {**body, 'plants': [{'plant_id': 'a', 'x': 1, 'z': 1}]},
                    {**body, 'plants': [{'plant_id': palm.id, 'x': 'nan', 'z': 1}]}):
            self.assertEqual(self.client.post('/api/layout-review/', bad, format='json').status_code, 400, bad)


class CustomerEditPlaceTests(APITestCase):
    """AI Designer phase C: an item the customer adds goes where a designer would put it."""

    def test_place_one_follows_the_design_rules(self):
        import random
        from apps.library import layout_engine as E
        rng = random.Random(3)
        palm = E.PlantSpec('palm', 'Palm', 900, 99, 4.0, E.spacing_for(6.5), mature=6.5)
        croton = E.PlantSpec('croton', 'Croton', 300, 99, 1.0, E.spacing_for(1.8), mature=1.8)
        lily = E.PlantSpec('lily', 'Peace Lily', 250, 99, 0.6, 0.45, 'full_shade', 'high', mature=0.7)
        seat = E.PlantSpec('s', 'Bench', 300, 9, 0.8, E.SEAT_PAD, kind='seat')
        specs = [palm, croton, lily, seat]
        for case in range(25):
            site = E.Site(rng.uniform(6, 18), rng.uniform(6, 18))
            for style in E.STYLES:
                d = E.generate(style, specs[:3], site, 15000, seed=case)
                plan = E.GardenPlan.from_dict(d['garden_plan'])
                for spec in specs:
                    spot = E.place_one(d['plants'], specs, site, spec, plan, seed=case)
                    if spot is None:
                        continue
                    after = d['plants'] + [{'plant_id': spec.id, 'x': spot[0], 'z': spot[1]}]
                    new = len(after) - 1
                    self.assertEqual([n for n in E.review(after, specs, site, plan) if new in n['items']], [],
                                     (case, style, spec.name))
                    if spec is palm:
                        self.assertLessEqual(spot[1], E.tall_limit(site, plan) + 1e-6)
        # A full courtyard: no room is an answer, not a crash
        tiny = E.Site(2, 2)
        crowded = [{'plant_id': 'palm', 'x': 1.0, 'z': 1.0}]
        self.assertIsNone(E.place_one(crowded, specs, tiny, palm))

    def test_place_endpoint(self):
        from apps.library.models import InventoryItem
        palm = InventoryItem.objects.create(name='Christmas Palm', category='plant', unit_price=900, stock_quantity=2,
                                            real_world_size=4.0, mature_height=6.5, model_file='palm.glb')
        hidden = InventoryItem.objects.create(name='Old plant', category='plant', unit_price=100, stock_quantity=9,
                                              real_world_size=1.0, model_file='x.glb', is_active=False)
        body = {'lot_width': 10, 'lot_length': 10, 'garden_plan': None, 'plants': [], 'add': palm.id}
        r = self.client.post('/api/layout-place/', body, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['plant_id'], str(palm.id))
        self.assertTrue(0 <= r.data['x'] <= 10 and 0 <= r.data['z'] <= 10)
        two = [{'plant_id': palm.id, 'x': 2, 'z': 2}, {'plant_id': palm.id, 'x': 8, 'z': 2}]
        r = self.client.post('/api/layout-place/', {**body, 'plants': two}, format='json')
        self.assertEqual(r.status_code, 409)
        self.assertIn('in stock', r.data['error'])
        self.assertEqual(self.client.post('/api/layout-place/', {**body, 'add': hidden.id}, format='json').status_code, 400)
        self.assertEqual(self.client.post('/api/layout-place/', {**body, 'add': 'x'}, format='json').status_code, 400)
        r = self.client.post('/api/layout-place/', {**body, 'lot_width': 2, 'lot_length': 2,
                                                    'plants': [{'plant_id': palm.id, 'x': 1, 'z': 1}]}, format='json')
        self.assertEqual(r.status_code, 409)
        self.assertIn('no room', r.data['error'])


class ProductReviewTests(APITestCase):
    def setUp(self):
        from apps.library.models import InventoryItem, Order, OrderItem
        self.user = User.objects.create_user(username='buyer', password='password123')
        self.other = User.objects.create_user(username='other', password='password123')
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(self.user).access_token}')

        self.plant = InventoryItem.objects.create(name="Hibiscus", category="plant", unit_price=300)
        self.order = Order.objects.create(
            user=self.user, customer_name='Buyer', customer_email='b@example.com',
            customer_address='Somewhere', total_price=600, status='Delivered',
        )
        self.line = OrderItem.objects.create(order=self.order, item=self.plant, quantity=2, price_at_booking=300)
        self.url = reverse('product-review-list')

    def _post(self, line=None, rating=5):
        return self.client.post(self.url, {'order_item': (line or self.line).id, 'rating': rating,
                                           'comment': 'Healthy plant'}, format='json')

    def test_review_delivered_item(self):
        response = self._post()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['product'], self.plant.id)
        self.assertEqual(response.data['order_id'], self.order.id)

    def test_duplicate_review_rejected(self):
        self._post()
        response = self._post()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('order_item', response.data)

    def test_undelivered_order_rejected(self):
        self.order.status = 'Shipped'
        self.order.save()
        response = self._post()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_other_users_order_rejected(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(self.other).access_token}')
        response = self._post()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invalid_rating_rejected(self):
        response = self._post(rating=6)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_requires_auth(self):
        self.client.credentials()
        response = self._post()
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_order_and_inventory_expose_review_data(self):
        self._post(rating=4)
        order = self.client.get(reverse('order-detail', args=[self.order.id])).data
        self.assertEqual(order['items'][0]['review']['rating'], 4)

        item = self.client.get(reverse('inventoryitem-detail', args=[self.plant.id])).data
        self.assertEqual(item['avg_rating'], 4.0)
        self.assertEqual(item['review_count'], 1)

        public = self.client.get(reverse('product-review-public'), {'product': self.plant.id})
        self.assertEqual(public.status_code, status.HTTP_200_OK)
        self.assertEqual(len(public.data), 1)
        self.assertEqual(public.data[0]['product_name'], 'Hibiscus')


class ProjectWorkflowTests(APITestCase):
    """Booking → crew → sign-off → billing workflow."""

    SITE = (14.5995, 120.9842)  # Manila

    def setUp(self):
        from apps.library.models import StaffProfile, ServiceBooking, ProjectTracker, GardenDesign
        self.ServiceBooking, self.ProjectTracker = ServiceBooking, ProjectTracker
        self.admin = User.objects.create_user(username='office', password='pw', is_staff=True)
        StaffProfile.objects.create(user=self.admin, role='OFFICE_ADMIN')
        self.crew = User.objects.create_user(username='crew', password='pw', is_staff=True)
        StaffProfile.objects.create(user=self.crew, role='FIELD_CREW')
        self.other_crew = User.objects.create_user(username='crew2', password='pw', is_staff=True)
        StaffProfile.objects.create(user=self.other_crew, role='FIELD_CREW')
        self.customer = User.objects.create_user(username='cust', password='pw')

        self.design = GardenDesign.objects.create(name='D', total_cost=40000, user=self.customer)
        self.booking = ServiceBooking.objects.create(
            user=self.customer, service_type='Softscape', scheduled_date='2026-11-01',
            status='Installing', design=self.design, site_lat=self.SITE[0], site_lng=self.SITE[1],
        )
        self.booking.assigned_crew.add(self.crew)

    def _as(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')

    def _status(self, status_value):
        return self.client.patch(f'/api/bookings/{self.booking.id}/update_status/', {'status': status_value}, format='json')

    # ── sign-off ────────────────────────────────────────────────────────────
    def test_crew_cannot_finish_directly(self):
        self._as(self.crew)
        self.assertEqual(self._status('Finished').status_code, status.HTTP_403_FORBIDDEN)

    def test_crew_submits_for_review_and_admins_are_notified(self):
        from apps.library.models import Notification
        finish_crew_work(self.booking)
        self._as(self.crew)
        self.assertEqual(self._status('For Review').status_code, status.HTTP_200_OK)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, 'For Review')
        self.assertTrue(Notification.objects.filter(user=self.admin, message__contains='awaiting your sign-off').exists())
        self.assertFalse(Notification.objects.filter(user=self.crew).exists())

    def test_unassigned_crew_cannot_touch_booking(self):
        self._as(self.other_crew)
        self.assertEqual(self._status('For Review').status_code, status.HTTP_404_NOT_FOUND)

    def test_admin_sign_off_completes_tracker(self):
        self.booking.status = 'For Review'
        self.booking.save()
        self._as(self.admin)
        self.assertEqual(self._status('Finished').status_code, status.HTTP_200_OK)
        tracker = self.ProjectTracker.objects.get(booking=self.booking)  # created for this legacy booking
        self.assertEqual(tracker.status, 'COMPLETED')
        self.assertEqual(tracker.total_price, 40000)
        self.assertTrue(tracker.history_logs.filter(remarks__contains='Signed off').exists())

    # ── project tracker / billing ───────────────────────────────────────────
    def test_new_booking_gets_tracker_priced_from_design(self):
        self._as(self.customer)
        response = self.client.post('/api/bookings/', {
            'service_type': 'maintenance', 'scheduled_date': '2027-01-15', 'design': self.design.id,
            'address': QUEZON_ADDRESS,
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        tracker = self.ProjectTracker.objects.get(booking_id=response.data['id'])
        self.assertEqual((tracker.total_price, tracker.remaining_balance), (40000, 40000))
        detail = self.client.get(f"/api/bookings/{response.data['id']}/").data
        self.assertEqual(detail['tracker']['amount_paid'], 0.0)

    def test_record_payment(self):
        tracker = self.ProjectTracker.objects.create(booking=self.booking, total_price=40000, remaining_balance=40000)
        url = f'/api/projects/{tracker.id}/record-payment/'
        self._as(self.crew)
        self.assertEqual(self.client.post(url, {'amount': 1000}, format='json').status_code, status.HTTP_403_FORBIDDEN)
        self._as(self.admin)
        self.assertEqual(self.client.post(url, {'amount': 50000}, format='json').status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.client.post(url, {'amount': -5}, format='json').status_code, status.HTTP_400_BAD_REQUEST)
        response = self.client.post(url, {'amount': 12000, 'remarks': 'Downpayment'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tracker.refresh_from_db()
        self.assertEqual(tracker.remaining_balance, 28000)
        self.assertTrue(tracker.history_logs.filter(remarks__contains='Downpayment').exists())

    def test_balance_cannot_exceed_price(self):
        tracker = self.ProjectTracker.objects.create(booking=self.booking, total_price=40000, remaining_balance=40000)
        self._as(self.admin)
        response = self.client.patch(f'/api/projects/{tracker.id}/', {'total_price': 30000}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_completing_tracker_finishes_booking(self):
        tracker = self.ProjectTracker.objects.create(booking=self.booking, total_price=40000, remaining_balance=0)
        self._as(self.admin)
        self.client.patch(f'/api/projects/{tracker.id}/update-status/', {'status': 'COMPLETED'}, format='json')
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, 'Finished')

    # ── clock-in geofence ───────────────────────────────────────────────────
    def _clock_in(self, lat=None, lng=None):
        body = {'booking_id': self.booking.id}
        if lat is not None:
            body.update(latitude=lat, longitude=lng)
        return self.client.post('/api/attendance/clock_in/', body, format='json')

    def test_clock_in_requires_assignment(self):
        self._as(self.other_crew)
        self.assertEqual(self._clock_in(*self.SITE).status_code, status.HTTP_403_FORBIDDEN)

    def test_clock_in_requires_gps_when_site_is_pinned(self):
        self._as(self.crew)
        self.assertEqual(self._clock_in().status_code, status.HTTP_400_BAD_REQUEST)

    def test_clock_in_rejects_far_away(self):
        self._as(self.crew)
        response = self._clock_in(self.SITE[0] + 0.01, self.SITE[1])  # ~1.1 km north
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('m from the job site', response.data['error'])

    def test_clock_in_on_site(self):
        self._as(self.crew)
        self.assertEqual(self._clock_in(self.SITE[0] + 0.0001, self.SITE[1]).status_code, status.HTTP_201_CREATED)

    def test_clock_in_rejected_after_submission(self):
        self.booking.status = 'For Review'
        self.booking.save()
        self._as(self.crew)
        self.assertEqual(self._clock_in(*self.SITE).status_code, status.HTTP_400_BAD_REQUEST)


import io
import shutil
import tempfile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

TEMP_MEDIA = tempfile.mkdtemp(prefix='portfolio-tests-')


def _photo_with_gps(name='site.jpg'):
    """A JPEG carrying EXIF GPS data, like a phone photo."""
    from PIL import Image
    img = Image.new('RGB', (64, 48), (34, 139, 34))
    exif = Image.Exif()
    exif[0x010F] = 'PhoneMaker'                       # Make
    exif[0x8825] = {1: 'N', 2: (14.0, 35.0, 58.0), 3: 'E', 4: (120.0, 59.0, 3.0)}  # GPS IFD
    buf = io.BytesIO()
    img.save(buf, format='JPEG', exif=exif)
    return SimpleUploadedFile(name, buf.getvalue(), content_type='image/jpeg')


@override_settings(MEDIA_ROOT=TEMP_MEDIA)
class ProjectHubTests(APITestCase):
    """Phase 0 crew photos + Phase 1 portfolio workflow."""

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEMP_MEDIA, ignore_errors=True)

    def setUp(self):
        from apps.library.models import StaffProfile, ServiceBooking, GardenDesign, ProjectTracker, ServiceReview
        self.admin = User.objects.create_user(username='office', password='pw', is_staff=True)
        StaffProfile.objects.create(user=self.admin, role='OFFICE_ADMIN')
        self.crew = User.objects.create_user(username='crew', password='pw', is_staff=True, first_name='Jun')
        StaffProfile.objects.create(user=self.crew, role='FIELD_CREW')
        self.other_crew = User.objects.create_user(username='crew2', password='pw', is_staff=True)
        StaffProfile.objects.create(user=self.other_crew, role='FIELD_CREW')
        self.customer = User.objects.create_user(username='cust', password='pw', first_name='Maria', last_name='Santos')

        design = GardenDesign.objects.create(
            name='D', total_cost=85000, user=self.customer, dimensions={'width': 10, 'length': 12},
            plant_breakdown=[{'plant_id': '999', 'name': 'Bird of Paradise', 'quantity': 4}],
        )
        self.booking = ServiceBooking.objects.create(
            user=self.customer, service_type='Softscape', scheduled_date='2026-10-01', status='For Review',
            design=design, service_address='12 Mabini St., Makati, Metro Manila',
        )
        self.booking.assigned_crew.add(self.crew)
        ProjectTracker.objects.create(booking=self.booking, total_price=85000, remaining_balance=0)
        ServiceReview.objects.create(booking=self.booking, user=self.customer, rating=5, comment='Beautiful work!')

    def _as(self, user):
        if user is None:
            self.client.credentials()
        else:
            self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')

    def _upload(self, phase='AFTER'):
        return self.client.post(f'/api/bookings/{self.booking.id}/progress-media/',
                                {'construction_phase': phase, 'file': _photo_with_gps(), 'description': 'Done'},
                                format='multipart')

    def _sign_off(self):
        self._as(self.admin)
        r = self.client.patch(f'/api/bookings/{self.booking.id}/update_status/', {'status': 'Finished'}, format='json')
        self.assertEqual(r.status_code, 200)
        from apps.library.models import PortfolioProject
        return PortfolioProject.objects.get(booking=self.booking)

    # ── Phase 0: crew progress photos ───────────────────────────────────────
    def test_crew_upload_strips_gps_metadata(self):
        from PIL import Image
        from apps.library.models import ProjectProgressMedia
        self._as(self.crew)
        r = self._upload()
        self.assertEqual(r.status_code, 201, r.data)
        stored = ProjectProgressMedia.objects.get(pk=r.data['id'])
        with Image.open(stored.file_path.path) as img:
            self.assertEqual(len(img.getexif()), 0)

    def test_upload_permissions(self):
        self._as(self.other_crew)
        self.assertEqual(self._upload().status_code, 404)   # not assigned → can't see the booking
        self._as(self.customer)
        self.assertEqual(self._upload().status_code, 403)
        self.assertEqual(self.client.get(f'/api/bookings/{self.booking.id}/progress-media/').status_code, 200)

    def test_upload_rejects_non_images(self):
        self._as(self.crew)
        r = self.client.post(f'/api/bookings/{self.booking.id}/progress-media/',
                             {'construction_phase': 'AFTER', 'file': SimpleUploadedFile('x.jpg', b'not an image')},
                             format='multipart')
        self.assertEqual(r.status_code, 400)

    # ── Phase 1: portfolio ──────────────────────────────────────────────────
    def test_sign_off_drafts_portfolio_and_asks_consent(self):
        from apps.library.models import Notification
        self._as(self.crew)
        self._upload('BEFORE')
        self._upload('AFTER')
        project = self._sign_off()
        self.assertEqual(project.status, 'draft')
        self.assertEqual(project.title, 'Softscape / Planting Services in Makati')
        self.assertEqual(project.city, 'Makati')
        self.assertEqual(project.price_band, '₱50K – ₱100K')
        self.assertEqual(project.lot_area_sqm, 120.0)
        self.assertEqual(project.consent.status, 'pending')
        self.assertEqual(project.media.count(), 2)
        self.assertFalse(project.media.filter(is_public=True).exists())
        self.assertEqual(project.cover.kind, 'after')
        self.assertTrue(Notification.objects.filter(user=self.customer, message__contains='feature your garden').exists())

    def test_publish_requires_consent_and_public_photo(self):
        self._as(self.crew)
        self._upload('AFTER')
        project = self._sign_off()
        publish = f'/api/portfolio/{project.id}/publish/'
        r = self.client.post(publish)
        self.assertEqual(r.status_code, 400)
        self.assertIn('consent', r.data['error'])

        self._as(self.customer)
        r = self.client.post(f'/api/portfolio/mine/{project.id}/consent/',
                             {'decision': 'grant', 'name_display': 'initials', 'include_review': True}, format='json')
        self.assertEqual(r.status_code, 200)

        self._as(self.admin)
        self.assertIn('no photos', self.client.post(publish).data['error'])
        media = project.media.first()
        self.client.patch(f'/api/portfolio/{project.id}/media/{media.id}/', {'is_public': True}, format='json')
        self.assertEqual(self.client.post(publish).status_code, 200)

        # Public views: no login, privacy-safe fields only
        self._as(None)
        listing = self.client.get('/api/portfolio/public/').data
        self.assertEqual([p['slug'] for p in listing['results']], [project.slug])
        detail = self.client.get(f'/api/portfolio/public/{project.slug}/').data
        self.assertEqual(detail['client'], 'Maria S.')
        self.assertEqual(detail['testimonial']['rating'], 5)
        self.assertEqual(detail['price_band'], '₱50K – ₱100K')
        self.assertEqual(detail['plants'][0]['name'], 'Bird of Paradise')
        self.assertTrue(detail['verified']['signed_off'])
        body = str(detail)
        for private in ('Mabini', '85000', 'Santos', "'cust'", 'Metro Manila'):
            self.assertNotIn(private, body)

    def test_unpublished_project_is_private(self):
        self._as(self.crew)
        self._upload()
        project = self._sign_off()
        url = f'/api/portfolio/public/{project.slug}/'
        self._as(None)
        self.assertEqual(self.client.get(url).status_code, 404)
        self._as(self.customer)
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data['is_preview'])
        self.assertEqual(r.data['client'], 'Private client')  # no consent yet → anonymous

    def test_withdrawing_consent_unpublishes(self):
        self._as(self.crew)
        self._upload()
        project = self._sign_off()
        project.media.update(is_public=True)
        self._as(self.customer)
        self.client.post(f'/api/portfolio/mine/{project.id}/consent/', {'decision': 'grant'}, format='json')
        self._as(self.admin)
        self.client.post(f'/api/portfolio/{project.id}/publish/')
        self._as(self.customer)
        self.client.post(f'/api/portfolio/mine/{project.id}/consent/', {'decision': 'withdraw'}, format='json')
        project.refresh_from_db()
        self.assertEqual(project.status, 'draft')
        self.assertEqual(project.consent.status, 'withdrawn')

    def test_house_photos_hidden_unless_customer_allows(self):
        self._as(self.crew)
        self._upload('BEFORE')
        self._upload('AFTER')
        project = self._sign_off()
        project.media.update(is_public=True)
        house = project.media.get(kind='after')
        self.client.patch(f'/api/portfolio/{project.id}/media/{house.id}/', {'shows_house': True}, format='json')

        self._as(self.customer)
        self.client.post(f'/api/portfolio/mine/{project.id}/consent/',
                         {'decision': 'grant', 'show_house': 'false'}, format='json')
        self._as(self.admin)
        self.assertEqual(self.client.post(f'/api/portfolio/{project.id}/publish/').status_code, 200)

        self._as(None)
        detail = self.client.get(f'/api/portfolio/public/{project.slug}/').data
        self.assertNotIn(house.id, [m['id'] for m in detail['media']])
        self.assertNotIn(house.image.name.split('/')[-1], detail['cover_url'] or '')

    def test_city_from_address_never_returns_street(self):
        from apps.library.portfolio import city_from_address
        cases = {
            '12 Mabini St., Makati, Metro Manila': 'Makati',
            'Pinag-isa, Cotta, Lucena, 2nd District, Calabarzon, 4301, Philippines': 'Lucena',
            'Brgy. Ibabang Polo, Pagbilao, Quezon, Calabarzon, 4302, Philippines': 'Pagbilao',
            '19 Molave Rd., Cainta, Rizal': 'Cainta',
            'Lot 5 Blk 3, Greenwoods Subd., Pasig': 'Pasig',
            'Lucena City, Quezon': 'Lucena City',
            'Tarlac': 'Tarlac',
            '12 Mabini St., Metro Manila': '',
            '': '',
        }
        for address, city in cases.items():
            self.assertEqual(city_from_address(address), city, address)

    # ── Phase 2: showcase & track record ────────────────────────────────────
    def test_detail_has_3d_layout_and_shop_links_but_no_reference_photo(self):
        from apps.library.models import InventoryItem
        plant = InventoryItem.objects.create(name='Bird of Paradise', category='plant', unit_price=450, stock_quantity=3)
        design = self.booking.design
        design.placed_items = [{'plant_id': str(plant.id), 'x': 1, 'z': 2, 'rotation': 0}]
        design.reference_image_url = 'http://example.com/media/house-front.jpg'
        design.save()
        self._as(self.crew)
        self._upload('AFTER')
        project = self._sign_off()
        project.media.update(is_public=True)
        self._as(self.customer)
        self.client.post(f'/api/portfolio/mine/{project.id}/consent/', {'decision': 'grant'}, format='json')
        self._as(self.admin)
        self.client.post(f'/api/portfolio/{project.id}/publish/')

        self._as(None)
        detail = self.client.get(f'/api/portfolio/public/{project.slug}/').data
        self.assertEqual(detail['design']['width'], 10)
        self.assertEqual(len(detail['design']['placed_items']), 1)
        self.assertEqual(detail['plants'][0]['product_id'], plant.id)  # matched by name
        self.assertEqual(detail['plants'][0]['product']['price'], 450.0)
        self.assertNotIn('house-front', str(detail))

    def test_track_record_counts_real_jobs_and_hides_private_credentials(self):
        import datetime
        from apps.library.models import BusinessCredential
        project = self._sign_off()
        design = project.booking.design
        design.plant_breakdown = design.plant_breakdown + [   # a bench isn't a plant installed
            {'plant_id': '998', 'name': 'Outdoor bench', 'quantity': 2, 'category': 'furniture'}]
        design.save(update_fields=['plant_breakdown'])
        today = datetime.date.today()
        BusinessCredential.objects.create(kind='permit', title="Mayor's Permit", issuer='City of Lucena',
                                          reference_no='MP-123', expires_on=today + datetime.timedelta(days=200))
        BusinessCredential.objects.create(kind='permit', title='Old Permit', expires_on=today - datetime.timedelta(days=1))
        BusinessCredential.objects.create(kind='award', title='Internal note', is_public=False)

        self._as(None)
        data = self.client.get('/api/portfolio/track-record/').data
        stats = data['stats']
        self.assertEqual(stats['projects_completed'], 1)
        self.assertEqual(stats['area_sqm'], 120)
        self.assertEqual(stats['plants_installed'], 4)
        self.assertEqual(stats['average_rating'], 5.0)
        self.assertEqual(stats['repeat_customers'], 0)
        self.assertEqual([c['title'] for c in data['credentials']], ["Mayor's Permit"])
        self.assertEqual(data['credentials'][0]['reference_no'], '')  # hidden unless the admin opts in

    def test_showcase_pairs_follow_privacy_rules(self):
        from apps.library.models import PortfolioMedia
        self._as(self.crew)
        self._upload('BEFORE')
        self._upload('AFTER')
        project = self._sign_off()
        self._as(None)
        self.assertEqual(self.client.get('/api/portfolio/showcase/').data['results'], [])  # draft: nothing public

        project.status = 'published'
        project.save()
        project.media.update(is_public=True)
        rows = self.client.get('/api/portfolio/showcase/').data['results']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['slug'], project.slug)
        self.assertTrue(rows[0]['before_url'] and rows[0]['after_url'])
        self.assertIsNone(rows[0]['render_url'])

        # A "before" shot showing the house is hidden without the customer's consent → no pair left
        project.media.filter(kind='before').update(shows_house=True)
        self.assertEqual(self.client.get('/api/portfolio/showcase/').data['results'], [])
        # ...but a 3D design render paired with the finished photo still makes the showcase
        PortfolioMedia.objects.create(project=project, kind='render', is_public=True,
                                      image=SimpleUploadedFile('render.jpg', _photo_with_gps().read()))
        rows = self.client.get('/api/portfolio/showcase/').data['results']
        self.assertEqual((rows[0]['before_url'], bool(rows[0]['render_url'])), (None, True))

    def test_credentials_admin_only_and_validated(self):
        url = '/api/business-credentials/'
        for user in (None, self.crew, self.customer):
            self._as(user)
            self.assertIn(self.client.get(url).status_code, (401, 403))
        self._as(self.admin)
        bad = self.client.post(url, {'title': 'X', 'document': SimpleUploadedFile('x.exe', b'MZ')}, format='multipart')
        self.assertEqual(bad.status_code, 400)
        r = self.client.post(url, {'title': 'BIR COR', 'kind': 'registration', 'issued_on': '2026-01-10',
                                   'expires_on': '2025-01-01'}, format='json')
        self.assertIn('expires_on', r.data)
        r = self.client.post(url, {'title': 'BIR COR', 'kind': 'registration',
                                   'document': SimpleUploadedFile('cor.pdf', b'%PDF-1.4')}, format='multipart')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertTrue(r.data['document_url'].endswith('.pdf'))

    def test_portfolio_admin_api_is_office_only(self):
        for user in (self.crew, self.customer):
            self._as(user)
            self.assertEqual(self.client.get('/api/portfolio/').status_code, 403)
        self._as(self.admin)
        self.assertEqual(self.client.get('/api/portfolio/').status_code, 200)

    def test_customer_cannot_answer_for_someone_else(self):
        project = self._sign_off()
        self._as(self.other_crew)
        r = self.client.post(f'/api/portfolio/mine/{project.id}/consent/', {'decision': 'grant'}, format='json')
        self.assertEqual(r.status_code, 404)


class IncomeAnalyticsTests(APITestCase):
    def setUp(self):
        from django.utils import timezone
        from apps.library.models import (
            InventoryItem, Order, OrderItem, ServiceBooking, GardenDesign, ProjectTracker,
        )
        self.admin = User.objects.create_superuser(username='admin', password='password123')
        self.customer = User.objects.create_user(username='cust', password='password123')
        self.url = reverse('income-analytics')
        today = timezone.localdate()

        plant = InventoryItem.objects.create(name="Hibiscus", category="plant", unit_price=300)
        order_kwargs = dict(user=self.customer, customer_name='C', customer_email='c@example.com',
                            customer_address='Addr')
        delivered = Order.objects.create(total_price=750, status='Delivered', **order_kwargs)
        OrderItem.objects.create(order=delivered, item=plant, quantity=2, price_at_booking=300)
        Order.objects.create(total_price=1000, status='Pending', **order_kwargs)      # pipeline
        Order.objects.create(total_price=5000, status='Cancelled', **order_kwargs)    # excluded

        design = GardenDesign.objects.create(name='D', total_cost=8000)
        ServiceBooking.objects.create(user=self.customer, service_type='hardscaping',
                                      scheduled_date=today, status='Finished', design=design)
        priced = ServiceBooking.objects.create(user=self.customer, service_type='maintenance',
                                               scheduled_date=today, status='Completed', design=design)
        ProjectTracker.objects.create(booking=priced, total_price=12000)  # tracker price wins over design
        ServiceBooking.objects.create(user=self.customer, service_type='consultation',
                                      scheduled_date=today + timedelta(days=7), status='Pending')  # unpriced pipeline

    def _get(self, **params):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(self.admin).access_token}')
        return self.client.get(self.url, params)

    def test_requires_superuser(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(self.customer).access_token}')
        self.assertEqual(self.client.get(self.url).status_code, status.HTTP_403_FORBIDDEN)

    def test_invalid_range(self):
        self.assertEqual(self._get(range='5y').status_code, status.HTTP_400_BAD_REQUEST)

    def test_revenue_definitions(self):
        data = self._get(range='30d').data
        self.assertEqual(data['ecommerce']['earned'], 750.0)
        self.assertEqual(data['ecommerce']['pipeline'], 1000.0)
        self.assertEqual(data['ecommerce']['cancelled_count'], 1)
        self.assertEqual(data['landscape']['earned'], 20000.0)  # 8000 design + 12000 tracker
        self.assertEqual(data['landscape']['pipeline_count'], 1)  # future-dated booking still counts
        self.assertEqual(data['landscape']['unpriced_count'], 1)
        self.assertEqual(sum(t['landscape'] for t in data['trend']), 20000.0)
        self.assertEqual(sum(t['ecommerce'] for t in data['trend']), 750.0)
        # Category breakdown is item sales, excluding the shipping fee
        self.assertEqual(data['ecommerce_by_category'], [{'label': 'Plants', 'value': 600.0}])
        self.assertEqual(data['landscape_by_service'][0], {'label': 'Maintenance', 'value': 12000.0, 'count': 1})

    def test_completion_rate_leaves_out_work_not_due_yet(self):
        # 2 finished today + 1 pending next week: the pending one isn't due, so 2 of 2 are done
        data = self._get(range='30d').data
        self.assertEqual(data['landscape']['completion_rate'], 100.0)
        self.assertEqual(data['landscape']['open_due_count'], 0)
        self.assertEqual(data['landscape']['pipeline_count'], 1)            # still in the pipeline
        # e-commerce: 1 delivered, 1 pending, 1 cancelled, all placed today → 1 of 3
        self.assertEqual(data['ecommerce']['completion_rate'], 33.3)
        self.assertEqual(data['ecommerce']['open_due_count'], 1)

    def test_pipeline_is_everything_open_now_whatever_the_range(self):
        from django.utils import timezone
        from apps.library.models import ServiceBooking
        ServiceBooking.objects.create(user=self.customer, service_type='maintenance',
                                      scheduled_date=timezone.localdate() - timedelta(days=60), status='Installing')
        short, long = self._get(range='30d').data, self._get(range='12m').data
        self.assertEqual(short['landscape']['pipeline_count'], 2)   # the 60-day-old job is still open now
        self.assertEqual(short['landscape']['pipeline'], long['landscape']['pipeline'])

    def test_orders_count_on_their_manila_day(self):
        import datetime
        from django.utils import timezone
        from apps.library.models import Order
        # 7:30 AM Manila on the 1st of last month = 23:30 UTC on the last day of the month before
        first = (timezone.localdate().replace(day=1) - timedelta(days=1)).replace(day=1)
        manila = timezone.get_current_timezone()
        placed = timezone.make_aware(datetime.datetime.combine(first, datetime.time(7, 30)), manila)
        o = Order.objects.create(user=self.customer, customer_name='C', customer_email='c@example.com',
                                 customer_address='Addr', total_price=999, status='Delivered')
        Order.objects.filter(pk=o.pk).update(created_at=placed)
        trend = {t['period']: t for t in self._get(range='12m').data['trend']}
        self.assertEqual(trend[first.isoformat()]['ecommerce'], 999.0)

    def test_trend_marks_partial_periods(self):
        from django.utils import timezone
        today = timezone.localdate()
        data = self._get(range='12m').data
        first, last = data['trend'][0], data['trend'][-1]
        self.assertEqual(first['from'], data['start'])
        self.assertEqual(last['to'], today.isoformat())
        self.assertEqual(first['partial'], data['start'][8:] != '01')       # starts mid-month → partial
        self.assertEqual(last['partial'], (today + timedelta(days=1)).day != 1)  # month not over → partial
        self.assertTrue(all(not t['partial'] for t in data['trend'][1:-1]))  # whole months in between
        week = self._get(range='30d').data['trend']
        self.assertTrue(all(t['partial'] == (t['from'] != t['period'] or
                                             t['to'] != (datetime_date(t['period']) + timedelta(days=6)).isoformat())
                            for t in week))


def datetime_date(iso):
    import datetime
    return datetime.date.fromisoformat(iso)


class StudioFunnelTests(APITestCase):
    """Anonymous guest Studio funnel events + the office summary."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.url = '/api/funnel-events/'

    def _event(self, name, session='a1b2c3d4-0000-0000-0000-000000000001'):
        return self.client.post(self.url, {'name': name, 'session_id': session}, format='json')

    def test_events_are_anonymous_deduped_and_validated(self):
        from apps.library.models import FunnelEvent
        self.assertEqual(self._event('studio_opened').status_code, 204)
        self.assertEqual(self._event('studio_opened').status_code, 204)  # same tab again: not double-counted
        self.assertEqual(FunnelEvent.objects.count(), 1)
        self.assertEqual(self._event('hacked').status_code, 400)
        self.assertEqual(self._event('studio_opened', session='x').status_code, 400)

    def test_summary_shows_conversion_for_office_only(self):
        for i in range(4):
            self._event('studio_opened', session=f'session-000{i}')
        self._event('ai_teaser_clicked', session='session-0000')
        self._event('account_created', session='session-0000')
        self.assertIn(self.client.get('/api/funnel-events/summary/').status_code, (401, 403))

        admin = User.objects.create_user(username='office', password='pw', is_staff=True)
        from apps.library.models import StaffProfile
        StaffProfile.objects.create(user=admin, role='OFFICE_ADMIN')
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(admin).access_token}')
        data = self.client.get('/api/funnel-events/summary/').data
        steps = {s['name']: s for s in data['steps']}
        self.assertEqual(steps['studio_opened']['count'], 4)
        self.assertEqual(steps['ai_teaser_clicked']['rate'], 25.0)
        self.assertEqual(data['conversion_rate'], 25.0)

    def test_summary_follows_the_visits_opened_in_the_period(self):
        from datetime import timedelta
        from django.utils import timezone
        from apps.library.models import FunnelEvent, StaffProfile
        self._event('studio_opened', session='session-new1')
        self._event('account_created', session='session-new1')
        self._event('signed_in', session='session-new1')          # same visit: one conversion, not two
        self._event('studio_opened', session='session-old1')
        FunnelEvent.objects.filter(session_id='session-old1').update(created_at=timezone.now() - timedelta(days=40))
        self._event('account_created', session='session-old1')   # opened before the period: not in it
        admin = User.objects.create_user(username='office2', password='pw', is_staff=True)
        StaffProfile.objects.create(user=admin, role='OFFICE_ADMIN')
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(admin).access_token}')
        data = self.client.get('/api/funnel-events/summary/', {'days': 30}).data
        steps = {s['name']: s for s in data['steps']}
        self.assertEqual(steps['studio_opened']['count'], 1)
        self.assertEqual(steps['account_created']['count'], 1)   # not 2: the old visit isn't in the cohort
        self.assertEqual(data['converted'], 1)
        self.assertEqual(data['conversion_rate'], 100.0)          # was 300% when events were counted alone


class CustomerReviewTests(APITestCase):
    """Landing page review carousel + office moderation."""

    def setUp(self):
        from apps.library.models import (
            StaffProfile, ServiceBooking, ServiceReview, InventoryItem, Order, OrderItem, ProductReview,
        )
        self.admin = User.objects.create_user(username='office', password='pw', is_staff=True)
        StaffProfile.objects.create(user=self.admin, role='OFFICE_ADMIN')
        self.crew = User.objects.create_user(username='crew', password='pw', is_staff=True, first_name='Jun', last_name='Reyes')
        StaffProfile.objects.create(user=self.crew, role='FIELD_CREW')
        self.maria = User.objects.create_user(username='maria@example.com', password='pw', first_name='Maria', last_name='Santos')
        self.nameless = User.objects.create_user(username='secret_handle', password='pw')

        def booking(user, address='12 Mabini St., Makati, Metro Manila'):
            b = ServiceBooking.objects.create(user=user, service_type='Softscape', scheduled_date='2026-10-01',
                                              status='Finished', service_address=address)
            b.assigned_crew.add(self.crew)
            return b

        self.good = ServiceReview.objects.create(booking=booking(self.maria), user=self.maria, rating=5,
                                                 comment='The crew turned our bare yard into a lovely tropical garden.')
        self.dupe = ServiceReview.objects.create(booking=booking(self.maria), user=self.maria, rating=5,
                                                 comment='The crew turned our bare yard into a lovely tropical garden!')
        self.low = ServiceReview.objects.create(booking=booking(self.nameless), user=self.nameless, rating=2,
                                                comment='Arrived two days late and left some soil on the driveway.')
        self.private = ServiceReview.objects.create(booking=booking(self.nameless), user=self.nameless, rating=5,
                                                    comment='Wonderful work, but please keep this to yourselves.', is_public=False)

        plant = InventoryItem.objects.create(name='Monstera', category='plant', unit_price=850, stock_quantity=5)
        order = Order.objects.create(user=self.maria, customer_name='M', customer_email='m@example.com',
                                     customer_address='Addr', total_price=850, status='Delivered')
        self.product_good = ProductReview.objects.create(
            order_item=OrderItem.objects.create(order=order, item=plant, quantity=1, price_at_booking=850),
            product=plant, user=self.maria, rating=4, comment='Arrived healthy and well packed, already growing new leaves.')
        self.junk = ProductReview.objects.create(
            order_item=OrderItem.objects.create(order=order, item=plant, quantity=1, price_at_booking=850),
            product=plant, user=self.maria, rating=5, comment='drdfdfdfdg')

    def _as(self, user):
        if user is None:
            self.client.credentials()
        else:
            self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')

    def _highlights(self, **params):
        self._as(None)
        r = self.client.get('/api/review-highlights/', params)
        self.assertEqual(r.status_code, 200)
        return r.data

    def test_highlights_mix_sources_and_filter_noise(self):
        data = self._highlights()
        ids = [(c['source'], c['id']) for c in data['results']]
        # The near-duplicate, junk text, the 2-star and the private review are all left out
        self.assertEqual(ids, [('service', self.dupe.id), ('product', self.product_good.id)])
        service = data['results'][0]
        self.assertEqual(service['name'], 'Maria S.')
        self.assertEqual(service['context']['city'], 'Makati')
        self.assertEqual(data['results'][1]['context']['link'], f'/shop?product={self.product_good.product_id}')
        body = str(data)
        for private in ('maria@example.com', 'secret_handle', 'Santos', 'Mabini', 'keep this to yourselves'):
            self.assertNotIn(private, body)

    def test_summary_is_honest(self):
        from apps.library.models import ServiceReview
        ServiceReview.objects.filter(pk=self.low.pk).update(is_hidden=True)
        summary = self._highlights()['summary']['service']
        # 3 public reviews (5, 5, 2): the hidden 2-star still counts, the private one doesn't
        self.assertEqual(summary['count'], 3)
        self.assertEqual(summary['average'], 4.0)
        self.assertEqual(summary['distribution']['2'], 1)

    def test_source_filter(self):
        data = self._highlights(source='product')
        self.assertEqual([c['source'] for c in data['results']], ['product'])
        self._as(None)
        self.assertEqual(self.client.get('/api/review-highlights/', {'source': 'x'}).status_code, 400)

    def test_moderation_is_office_only(self):
        for user in (None, self.crew, self.maria):
            self._as(user)
            self.assertIn(self.client.get('/api/review-moderation/').status_code, (401, 403))
            self.assertIn(self.client.patch(f'/api/review-moderation/service/{self.good.id}/', {'is_hidden': True},
                                            format='json').status_code, (401, 403))
        self._as(self.admin)
        r = self.client.get('/api/review-moderation/', {'status': 'needs_reply'})
        self.assertEqual([(x['source'], x['id']) for x in r.data['results']], [('service', self.low.id)])

    def test_featuring_hiding_and_replies(self):
        from apps.library.models import Notification

        def url(src, pk):
            return f'/api/review-moderation/{src}/{pk}/'

        self._as(self.admin)
        # A private review can never be featured
        r = self.client.patch(url('service', self.private.id), {'is_featured': True}, format='json')
        self.assertEqual(r.status_code, 400)
        # Featuring overrides the automatic 4-star+ / quality picks
        self.assertEqual(self.client.patch(url('service', self.low.id), {'is_featured': True}, format='json').status_code, 200)
        r = self.client.patch(url('service', self.low.id), {'reply': 'Sorry about the delay. We have fixed our scheduling.'}, format='json')
        self.assertIsNotNone(r.data['replied_at'])
        self.assertTrue(Notification.objects.filter(user=self.nameless, message__contains='replied to your review').exists())

        first = self._highlights()['results'][0]
        self.assertEqual(first['id'], self.low.id)
        self.assertEqual(first['name'], 'Verified customer')  # no name on the account, never the username
        self.assertIn('scheduling', first['reply'])

        # Hiding removes it from every public list and unfeatures it
        self._as(self.admin)
        r = self.client.patch(url('service', self.low.id), {'is_hidden': True}, format='json')
        self.assertFalse(r.data['is_featured'])
        self.assertNotIn(self.low.id, [c['id'] for c in self._highlights()['results']])
        self.assertNotIn(self.low.id, [x['id'] for x in self.client.get('/api/reviews/public/').data])

    def test_customer_can_submit_a_private_review(self):
        from apps.library.models import ServiceBooking
        booking = ServiceBooking.objects.create(user=self.maria, service_type='maintenance',
                                                scheduled_date='2026-10-02', status='Finished')
        self._as(self.maria)
        r = self.client.post('/api/reviews/', {'booking': booking.id, 'rating': 5, 'is_public': False,
                                               'comment': 'Great, but I would rather this stayed private.'}, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertFalse(r.data['is_public'])
        self.assertNotIn(r.data['id'], [c['id'] for c in self._highlights(source='service')['results']])

    def test_public_service_reviews_credit_crew_by_first_name(self):
        self._as(None)
        data = self.client.get('/api/reviews/public/').data
        self.assertTrue(data)
        self.assertEqual(data[0]['crew_names'], ['Jun'])
        self.assertNotIn('Reyes', str(data))


class CheckoutSecurityTests(APITestCase):
    """The server prices every order; nothing the browser sends can change what is charged."""

    def setUp(self):
        from apps.library.models import InventoryItem
        self.user = User.objects.create_user(username='buyer', password='pw')
        self.other = User.objects.create_user(username='other', password='pw')
        self.plant = InventoryItem.objects.create(name='Fern', category='plant', unit_price=500, stock_quantity=10)
        self._as(self.user)

    def _as(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')

    def _order(self, items, **extra):
        return self.client.post('/api/checkout/', {
            'customer_name': 'Buyer', 'customer_email': 'b@example.com', 'address': QUEZON_ADDRESS,
            'customer_phone': '09171234567',
            'payment_method': 'cod', 'items': items, **extra}, format='json')

    def test_client_total_is_ignored(self):
        from apps.library.models import Order
        r = self._order([{'id': self.plant.id, 'quantity': 2}], total_price=1)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data['total_price'], 1150.0)  # 2 × ₱500 + ₱150 delivery
        self.assertEqual(Order.objects.get(pk=r.data['order_id']).total_price, 1150)

    def test_bad_quantities_and_empty_carts_are_rejected(self):
        for items in ([], [{'id': self.plant.id, 'quantity': -10}], [{'id': self.plant.id, 'quantity': 0}],
                      [{'id': self.plant.id, 'quantity': 'lots'}], [{'quantity': 1}]):
            self.assertEqual(self._order(items).status_code, 400, items)
        self.plant.refresh_from_db()
        self.assertEqual(self.plant.stock_quantity, 10)  # a negative quantity can't add stock

    def test_duplicate_lines_are_merged_against_stock(self):
        r = self._order([{'id': self.plant.id, 'quantity': 6}, {'id': self.plant.id, 'quantity': 6}])
        self.assertEqual(r.status_code, 400)
        self.assertIn('Insufficient stock', r.data['error'])

    def test_delivery_fee_comes_from_the_rate_card(self):
        from apps.library.models import PricingSettings
        ps = PricingSettings.current()
        ps.shipping_fee = 250
        ps.save()
        self.assertEqual(self._order([{'id': self.plant.id, 'quantity': 1}]).data['total_price'], 750.0)

    def test_phone_must_be_a_ph_mobile_and_is_normalised(self):
        from apps.library.models import Order
        for bad in ('', '09856857', '0917123456789', '0217123456', 'call me'):
            r = self._order([{'id': self.plant.id, 'quantity': 1}], customer_phone=bad)
            self.assertEqual(r.status_code, 400, bad)
            self.assertIn('mobile number', r.data['error'])
        for good in ('0917 123 4567', '+63 917-123-4567', '639171234567', '9171234567'):
            r = self._order([{'id': self.plant.id, 'quantity': 1}], customer_phone=good)
            self.assertEqual(r.status_code, 201, good)
            self.assertEqual(Order.objects.get(pk=r.data['order_id']).customer_phone, '09171234567')

    def test_cod_has_a_cap_from_the_rate_card(self):
        from apps.library.models import PricingSettings
        ps = PricingSettings.current()
        ps.cod_max_amount = 1000
        ps.save()
        r = self._order([{'id': self.plant.id, 'quantity': 2}])  # ₱1,150 > ₱1,000
        self.assertEqual(r.status_code, 400)
        self.assertIn('up to ₱1,000', r.data['error'])
        self.plant.refresh_from_db()
        self.assertEqual(self.plant.stock_quantity, 10)  # rolled back
        self.assertEqual(self._order([{'id': self.plant.id, 'quantity': 2}], payment_method='online').status_code, 201)
        ps.cod_max_amount = 0  # 0 = no limit
        ps.save()
        self.assertEqual(self._order([{'id': self.plant.id, 'quantity': 2}]).status_code, 201)

    def test_guests_cannot_use_cod(self):
        self.client.credentials()
        r = self._order([{'id': self.plant.id, 'quantity': 1}])
        self.assertEqual(r.status_code, 400)
        self.assertIn('log in', r.data['error'])
        self.assertEqual(self._order([{'id': self.plant.id, 'quantity': 1}], payment_method='online').status_code, 201)

    def test_only_the_owner_can_pay_and_never_twice(self):
        from apps.library.models import Order
        order_id = self._order([{'id': self.plant.id, 'quantity': 1}]).data['order_id']
        self._as(self.other)
        self.assertEqual(self.client.post('/api/create-checkout-session/', {'order_id': order_id}, format='json').status_code, 404)
        Order.objects.filter(pk=order_id).update(payment_status='Paid')
        self._as(self.user)
        r = self.client.post('/api/create-checkout-session/', {'order_id': order_id}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('already Paid', r.data['error'])


@override_settings(PAYMONGO_SECRET_KEY='sk_test_x', PAYMONGO_WEBHOOK_SECRET='whsk_test',
                   PAYMONGO_ORDER_TTL_MINUTES=60, FRONTEND_URL='http://front')
class PayMongoTests(APITestCase):
    """GCash/Maya/card checkout through PayMongo; all HTTP calls are mocked."""

    def setUp(self):
        from apps.library.models import InventoryItem
        self.user = User.objects.create_user(username='buyer', password='pw')
        self.plant = InventoryItem.objects.create(name='Fern', category='plant', unit_price=500, stock_quantity=10)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(self.user).access_token}')

    def _order(self, method='online', qty=2):
        r = self.client.post('/api/checkout/', {
            'customer_name': 'Buyer', 'customer_email': 'b@example.com', 'address': QUEZON_ADDRESS,
            'customer_phone': '09171234567',
            'payment_method': method, 'items': [{'id': self.plant.id, 'quantity': qty}]}, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        return r.data

    @staticmethod
    def _session(status='active', paid_amount=None):
        payments = [{'id': 'pay_1', 'attributes': {'status': 'paid', 'amount': paid_amount}}] if paid_amount else []
        return {'id': 'cs_1', 'attributes': {'status': status, 'checkout_url': 'https://pm.test/cs_1',
                                             'payments': payments, 'metadata': {}}}

    def _webhook(self, body, secret='whsk_test'):
        import hashlib, hmac
        raw = json.dumps(body).encode()
        sig = hmac.new(secret.encode(), b'123.' + raw, hashlib.sha256).hexdigest()
        return self.client.generic('POST', '/api/paymongo-webhook/', raw, content_type='application/json',
                                   HTTP_PAYMONGO_SIGNATURE=f't=123,te={sig},li=')

    def test_checkout_session_is_itemised_and_reused(self):
        from apps.library.models import Order
        data = self._order()
        with patch('apps.library.paymongo._request', return_value=self._session()) as req:
            r = self.client.post('/api/create-checkout-session/', {'order_id': data['order_id']}, format='json')
        self.assertEqual(r.data['checkout_url'], 'https://pm.test/cs_1')
        attrs = req.call_args.args[2]['data']['attributes']
        self.assertEqual(sum(li['amount'] * li['quantity'] for li in attrs['line_items']), 115000)  # ₱1,150.00
        self.assertIn('gcash', attrs['payment_method_types'])
        self.assertIn('qrph', attrs['payment_method_types'])
        self.assertIn(f"token={data['receipt_token']}", attrs['success_url'])
        self.assertEqual(Order.objects.get(pk=data['order_id']).paymongo_checkout_id, 'cs_1')
        # "Pay now" again: the open session is reused, no new one is created
        with patch('apps.library.paymongo._request', return_value=self._session()) as req:
            self.client.post('/api/create-checkout-session/', {'order_id': data['order_id']}, format='json')
        self.assertEqual([c.args[0] for c in req.call_args_list], ['GET', 'GET'])

    def test_signed_webhook_marks_order_paid(self):
        from apps.library.models import Order
        data = self._order()
        Order.objects.filter(pk=data['order_id']).update(paymongo_checkout_id='cs_1')
        event = {'data': {'attributes': {'type': 'checkout_session.payment.paid',
                                         'data': self._session(paid_amount=115000)}}}
        self.assertEqual(self._webhook(event, secret='wrong').status_code, 400)
        self.assertEqual(Order.objects.get(pk=data['order_id']).payment_status, 'Pending')
        self.assertEqual(self._webhook(event).status_code, 200)
        order = Order.objects.get(pk=data['order_id'])
        self.assertEqual((order.payment_status, order.status, order.paymongo_payment_id), ('Paid', 'Paid', 'pay_1'))

    def test_short_payment_is_not_accepted(self):
        from apps.library.models import Order
        data = self._order()
        Order.objects.filter(pk=data['order_id']).update(paymongo_checkout_id='cs_1')
        event = {'data': {'attributes': {'type': 'checkout_session.payment.paid',
                                         'data': self._session(paid_amount=100)}}}
        self._webhook(event)
        self.assertEqual(Order.objects.get(pk=data['order_id']).payment_status, 'Pending')

    def test_receipt_needs_token_or_owner_and_syncs_payment(self):
        from apps.library.models import Order
        data = self._order()
        Order.objects.filter(pk=data['order_id']).update(paymongo_checkout_id='cs_1')
        url = f"/api/order-receipt/{data['order_id']}/"
        self.client.credentials()
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.get(url, {'token': 'guess'}).status_code, 404)
        with patch('apps.library.paymongo._request', return_value=self._session(paid_amount=115000)):
            r = self.client.get(url, {'token': data['receipt_token']})
        self.assertEqual(r.data['payment_status'], 'Paid')  # no webhook needed on localhost

    def test_abandoned_online_order_expires_and_returns_stock(self):
        from apps.library.models import Order
        from django.utils import timezone
        data = self._order(qty=3)
        cod = self._order(method='cod', qty=1)
        Order.objects.update(created_at=timezone.now() - timedelta(hours=2))
        self.plant.refresh_from_db()
        self.assertEqual(self.plant.stock_quantity, 6)
        self.client.get('/api/orders/')
        order = Order.objects.get(pk=data['order_id'])
        self.assertEqual((order.status, order.payment_status), ('Cancelled', 'Expired'))
        self.assertEqual(Order.objects.get(pk=cod['order_id']).status, 'Pending')  # COD is never expired
        self.plant.refresh_from_db()
        self.assertEqual(self.plant.stock_quantity, 9)

    def test_customer_cancel_returns_stock_but_paid_orders_stay(self):
        from apps.library.models import Order
        first, second = self._order(qty=2), self._order(qty=2)
        self.assertEqual(self.client.delete(f"/api/orders/{first['order_id']}/").status_code, 204)
        self.plant.refresh_from_db()
        self.assertEqual(self.plant.stock_quantity, 8)
        # Kept as Cancelled, not deleted: it stays in the history and the reports
        self.assertEqual(Order.objects.get(pk=first['order_id']).status, 'Cancelled')
        self.assertEqual(self.client.delete(f"/api/orders/{first['order_id']}/").status_code, 400)   # once only
        self.plant.refresh_from_db()
        self.assertEqual(self.plant.stock_quantity, 8)                     # stock not returned twice
        Order.objects.filter(pk=second['order_id']).update(payment_status='Paid')
        self.assertEqual(self.client.delete(f"/api/orders/{second['order_id']}/").status_code, 400)


@override_settings(PAYMONGO_SECRET_KEY='sk_test_x', PAYMONGO_WEBHOOK_SECRET='whsk_test', FRONTEND_URL='http://front')
class ProjectPaymentRequestTests(APITestCase):
    """Staff request a downpayment; the customer pays through the /pay/<token> link (PayMongo mocked)."""

    def setUp(self):
        from apps.library.models import StaffProfile, ServiceBooking, ProjectTracker
        self.admin = User.objects.create_user(username='office', password='pw', is_staff=True)
        StaffProfile.objects.create(user=self.admin, role='OFFICE_ADMIN')
        self.crew = User.objects.create_user(username='crew', password='pw', is_staff=True)
        StaffProfile.objects.create(user=self.crew, role='FIELD_CREW')
        self.customer = User.objects.create_user(username='cust', password='pw', first_name='Ana',
                                                 email='ana@example.com')
        booking = ServiceBooking.objects.create(user=self.customer, service_type='Softscape',
                                                scheduled_date='2026-11-01')
        self.tracker = ProjectTracker.objects.create(booking=booking, total_price=40000, remaining_balance=40000)
        self.url = f'/api/projects/{self.tracker.id}/payment-requests/'

    def _as(self, user):
        if user is None:
            self.client.credentials()
        else:
            self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')

    def _request(self, amount=20000, label='Downpayment (50%)'):
        self._as(self.admin)
        r = self.client.post(self.url, {'amount': amount, 'label': label}, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        return r.data

    @staticmethod
    def _session(paid_amount=None):
        payments = [{'id': 'pay_9', 'attributes': {'status': 'paid', 'amount': paid_amount}}] if paid_amount else []
        return {'id': 'cs_9', 'attributes': {'status': 'active', 'checkout_url': 'https://pm.test/cs_9',
                                             'payments': payments, 'metadata': {}}}

    def test_only_office_admins_create_requests_within_the_balance(self):
        from apps.library.models import Notification
        self._as(self.crew)
        # Crew who aren't on this job can't even see the project (404); crew on it are refused (403)
        self.assertIn(self.client.post(self.url, {'amount': 1000}, format='json').status_code, (403, 404))
        self._as(self.customer)
        self.assertEqual(self.client.post(self.url, {'amount': 1000}, format='json').status_code, 403)
        data = self._request(30000)
        self.assertTrue(data['pay_url'].startswith('http://front/pay/'))
        self.assertTrue(Notification.objects.filter(user=self.customer, kind='payment_request',
                                                    link='/pay/' + data['pay_url'].rsplit('/', 1)[1]).exists())
        # The open ₱30,000 request counts against the ₱40,000 balance
        self._as(self.admin)
        r = self.client.post(self.url, {'amount': 15000}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('₱10,000.00', r.data['error'])
        self._as(self.customer)  # the customer can see their own requests
        self.assertEqual(len(self.client.get(self.url).data), 1)

    def test_pay_link_checkout_and_sync_lowers_the_balance_once(self):
        token = self._request()['pay_url'].rsplit('/', 1)[1]
        self._as(None)  # the link works without logging in
        self.assertEqual(self.client.get('/api/project-payments/wrong-token/').status_code, 404)
        detail = self.client.get(f'/api/project-payments/{token}/').data
        self.assertEqual((detail['amount'], detail['status'], detail['customer_first_name']), (20000.0, 'PENDING', 'Ana'))
        self.assertNotIn('ana@example.com', str(detail))

        with patch('apps.library.paymongo._request', return_value=self._session()) as req:
            r = self.client.post(f'/api/project-payments/{token}/checkout/')
        self.assertEqual(r.data['checkout_url'], 'https://pm.test/cs_9')
        attrs = req.call_args.args[2]['data']['attributes']
        self.assertEqual(attrs['line_items'][0]['amount'], 2000000)
        self.assertEqual(attrs['metadata']['project_payment_request_id'], str(self.tracker.payment_requests.get().id))

        with patch('apps.library.paymongo._request', return_value=self._session(paid_amount=2000000)):
            self.assertEqual(self.client.get(f'/api/project-payments/{token}/').data['status'], 'PAID')
            self.client.get(f'/api/project-payments/{token}/')  # second sync must not pay twice
            self.assertEqual(self.client.post(f'/api/project-payments/{token}/checkout/').status_code, 400)
        self.tracker.refresh_from_db()
        self.assertEqual(self.tracker.remaining_balance, 20000)
        self.assertEqual(self.tracker.history_logs.filter(remarks__startswith='Payment received online').count(), 1)

    def test_webhook_marks_request_paid(self):
        import hashlib, hmac
        self._request(10000)
        req = self.tracker.payment_requests.get()
        req.paymongo_checkout_id = 'cs_9'
        req.save()
        raw = json.dumps({'data': {'attributes': {'type': 'checkout_session.payment.paid',
                                                  'data': self._session(paid_amount=1000000)}}}).encode()
        sig = hmac.new(b'whsk_test', b'1.' + raw, hashlib.sha256).hexdigest()
        r = self.client.generic('POST', '/api/paymongo-webhook/', raw, content_type='application/json',
                                HTTP_PAYMONGO_SIGNATURE=f't=1,te={sig},li=')
        self.assertEqual(r.status_code, 200)
        req.refresh_from_db()
        self.tracker.refresh_from_db()
        self.assertEqual((req.status, req.paymongo_payment_id), ('PAID', 'pay_9'))
        self.assertEqual(self.tracker.remaining_balance, 30000)

    def test_cancelled_request_cannot_be_paid_and_frees_the_balance(self):
        data = self._request(40000)
        token = data['pay_url'].rsplit('/', 1)[1]
        cancel = f"{self.url}{data['id']}/cancel/"
        self._as(self.crew)
        self.assertEqual(self.client.post(cancel).status_code, 403)
        self._as(self.admin)
        self.assertEqual(self.client.post(cancel).data['status'], 'CANCELLED')
        self.assertEqual(self.client.post(cancel).status_code, 400)
        self._as(None)
        self.assertEqual(self.client.post(f'/api/project-payments/{token}/checkout/').status_code, 400)
        self._request(40000)  # the full balance can be requested again

    def test_new_request_is_emailed_and_can_be_resent(self):
        from django.core import mail
        data = self._request(20000)
        self.assertTrue(data['email_sent'])
        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertEqual(email.to, ['ana@example.com'])
        self.assertIn('₱20,000.00', email.subject)
        self.assertIn(data['pay_url'], email.body)
        self.assertIn(data['pay_url'], email.alternatives[0][0])  # HTML version with the Pay button

        resend = f"{self.url}{data['id']}/send-email/"
        self._as(self.crew)
        self.assertEqual(self.client.post(resend).status_code, 403)
        self._as(self.admin)
        self.assertEqual(self.client.post(resend).status_code, 200)
        self.assertEqual(len(mail.outbox), 2)

    def test_email_problems_never_block_the_request(self):
        from django.core import mail
        with patch('django.core.mail.EmailMultiAlternatives.send', side_effect=OSError('SMTP down')):
            data = self._request(10000)
        self.assertFalse(data['email_sent'])
        self.assertIn('Copy the link', data['email_message'])
        self.customer.email = ''
        self.customer.save()
        data = self._request(10000)
        self.assertFalse(data['email_sent'])
        self.assertIn('no email', data['email_message'])
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(self.tracker.payment_requests.count(), 2)


class PricingTests(APITestCase):
    """Rate card: transparent estimate lines, admin-only edits, sample-rate flag."""

    def setUp(self):
        from apps.library.models import InventoryItem, StaffProfile
        self.palm = InventoryItem.objects.create(name='Palm', category='plant', unit_price=1000, stock_quantity=5, real_world_size=5)
        self.fern = InventoryItem.objects.create(name='Fern', category='plant', unit_price=100, stock_quantity=50, real_world_size=0.5)
        self.bench = InventoryItem.objects.create(name='Bench', category='furniture', unit_price=3000, stock_quantity=3)
        self.admin = User.objects.create_user(username='office', password='pw', is_staff=True)
        StaffProfile.objects.create(user=self.admin, role='OFFICE_ADMIN')

    def test_specimen_size_is_its_own_line(self):
        """A specimen-size plant (bigger stock): its own price and line, labour by its bigger size, installed only."""
        self.fern.specimen_price, self.fern.specimen_height = 400, 1.5
        self.fern.save()
        items = [{'product_id': self.fern.id, 'quantity': 3}, {'product_id': self.fern.id, 'quantity': 1, 'size': 'specimen'}]
        r = self.client.post('/api/pricing/estimate/', {'area_sqm': 50, 'items': items}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        lines = {l['key']: l['amount'] for l in r.data['lines']}
        self.assertEqual(lines['plants'], 3 * 100 + 400)
        self.assertEqual(lines['labour'], 3 * 40 + 120)   # the 1.5 m specimen is planted at the mid rate
        rows = {(row['name'], row.get('size', '')): row for row in r.data['plant_breakdown']}
        self.assertEqual(rows[('Fern', '')]['quantity'], 3)
        self.assertEqual(rows[('Fern — specimen size', 'specimen')]['unit_price'], 400)
        # Plants only: specimens are planted by our crew, so all are the standard size
        r = self.client.post('/api/pricing/estimate/', {'area_sqm': 50, 'items': items, 'install': False}, format='json')
        self.assertEqual(r.data['plant_cost'], 4 * 100)
        self.assertEqual([row['quantity'] for row in r.data['plant_breakdown']], [4])
        # A plant that doesn't offer the size: priced as standard
        r = self.client.post('/api/pricing/estimate/', {'area_sqm': 50, 'items': [
            {'product_id': self.palm.id, 'quantity': 1, 'size': 'specimen'}]}, format='json')
        self.assertEqual(r.data['plant_cost'], 1000)
        self.assertNotIn('size', r.data['plant_breakdown'][0])

    def test_estimate_lines_with_sample_rates(self):
        r = self.client.post('/api/pricing/estimate/', {'area_sqm': 50, 'items': [
            {'product_id': self.palm.id, 'quantity': 2}, {'product_id': self.fern.id, 'quantity': 10},
            {'product_id': self.bench.id, 'quantity': 1}]}, format='json')
        self.assertEqual(r.status_code, 200)
        lines = {l['key']: l['amount'] for l in r.data['lines']}
        self.assertEqual(lines['plants'], 2 * 1000 + 10 * 100 + 3000)
        self.assertEqual(lines['labour'], 2 * 350 + 10 * 40 + 100)   # tall, low, other
        self.assertEqual(lines['site_prep'], 50 * 150)
        self.assertEqual(lines['materials'], 50 * 120)
        self.assertEqual(lines['mobilization'], 1500)
        subtotal = 6000 + 1200 + 7500 + 6000 + 1500
        self.assertEqual(lines['design_fee'], round(subtotal * 0.10))
        self.assertNotIn('vat', lines)                                # not VAT-registered by default
        self.assertEqual(r.data['total'], sum(lines.values()))
        self.assertLess(r.data['low'], r.data['total'])
        self.assertGreater(r.data['high'], r.data['total'])
        self.assertTrue(r.data['is_sample_rates'])

    def test_minimum_charge_and_vat(self):
        from apps.library.models import PricingSettings
        ps = PricingSettings.current()
        ps.vat_registered = True
        ps.save()
        r = self.client.post('/api/pricing/estimate/', {'area_sqm': 1, 'items': [{'product_id': self.fern.id, 'quantity': 1}]}, format='json')
        lines = {l['key']: l['amount'] for l in r.data['lines']}
        self.assertIn('minimum', lines)
        self.assertEqual(lines['vat'], 1800)                          # 12% of the ₱15,000 minimum
        self.assertEqual(r.data['total'], 16800)

    def test_only_office_admins_edit_rates_and_sample_flag_clears(self):
        self.assertEqual(self.client.get('/api/pricing/').status_code, 200)
        self.assertIn(self.client.patch('/api/pricing/', {'planting_tall': 1}, format='json').status_code, (401, 403))
        customer = User.objects.create_user(username='cust', password='pw')
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(customer).access_token}')
        self.assertEqual(self.client.patch('/api/pricing/', {'planting_tall': 1}, format='json').status_code, 403)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(self.admin).access_token}')
        bad = self.client.patch('/api/pricing/', {'design_fee_percent': 250, 'site_prep_per_sqm': -5}, format='json')
        self.assertEqual(set(bad.data), {'design_fee_percent', 'site_prep_per_sqm'})
        r = self.client.patch('/api/pricing/', {'planting_tall': '500', 'vat_registered': True}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['planting_tall'], 500.0)
        self.assertTrue(r.data['vat_registered'])
        self.assertFalse(r.data['is_sample'])

    def test_estimate_prices_plants_only_and_existing_garden_like_the_ai(self):
        """An edited AI design is re-priced on the same terms it was designed with."""
        items = [{'product_id': self.palm.id, 'quantity': 2}, {'product_id': self.fern.id, 'quantity': 10}]
        post = lambda **extra: self.client.post('/api/pricing/estimate/', {'area_sqm': 50, 'items': items, **extra}, format='json')  # noqa: E731
        plants_only = post(install=False)
        self.assertEqual(plants_only.status_code, 200)
        self.assertEqual({l['key'] for l in plants_only.data['lines']}, {'plants', 'delivery'})
        self.assertEqual(plants_only.data['plant_cost'], 2 * 1000 + 10 * 100)
        existing = post(scope='existing')
        keys = {l['key'] for l in existing.data['lines']}
        self.assertIn('compost', keys)
        self.assertNotIn('site_prep', keys)
        self.assertEqual(existing.data['scope'], 'existing')
        self.assertEqual(post(scope='makeover').data['total'], post().data['total'])
        self.assertEqual(post(scope='everything').status_code, 400)

    def test_estimate_validates_input(self):
        for body in ({'items': 'x'}, {'items': [{'product_id': 'a'}]}, {'items': [{'product_id': self.fern.id, 'quantity': -1}]},
                     {'items': [], 'area_sqm': -3}):
            self.assertEqual(self.client.post('/api/pricing/estimate/', body, format='json').status_code, 400, body)

    def test_quick_estimate_scales_with_yard_and_fullness(self):
        def quick(**params):
            r = self.client.get('/api/pricing/quick/', {'area_sqm': 100, 'fullness': 'balanced', **params})
            self.assertEqual(r.status_code, 200, r.data)
            return r.data
        balanced = quick()
        self.assertEqual(balanced['plant_count'], 40)                          # 0.4 plants/m²
        self.assertEqual({l['key'] for l in balanced['lines']} >= {'plants', 'labour', 'site_prep'}, True)
        self.assertNotIn(str(self.bench.id), {p['plant_id'] for p in balanced['plant_breakdown']})  # plants only
        self.assertLess(quick(fullness='light')['total'], balanced['total'])
        self.assertGreater(quick(fullness='full')['total'], balanced['total'])
        self.assertGreater(quick(area_sqm=200)['total'], balanced['total'])
        plants_only = quick(mode='plants')
        self.assertEqual({l['key'] for l in plants_only['lines']}, {'plants', 'delivery'})
        self.assertLess(plants_only['total'], balanced['total'])

    def test_quick_estimate_light_uses_cheaper_plants_and_skips_out_of_stock(self):
        from apps.library.models import InventoryItem
        InventoryItem.objects.create(name='Cheap fern', category='plant', unit_price=20, stock_quantity=9, real_world_size=0.4)
        gone = InventoryItem.objects.create(name='Sold out', category='plant', unit_price=50, stock_quantity=0, real_world_size=0.4)
        light = self.client.get('/api/pricing/quick/', {'area_sqm': 40, 'fullness': 'light'}).data
        names = {p['name'] for p in light['plant_breakdown']}
        self.assertIn('Cheap fern', names)
        self.assertNotIn('Fern', names)                                        # the dearer low plant is left out
        self.assertNotIn(gone.name, names)

    def test_quick_estimate_validates_input(self):
        for params in ({'area_sqm': 0}, {'area_sqm': 5000}, {'area_sqm': 'x'}, {'area_sqm': 50, 'fullness': 'huge'}):
            self.assertEqual(self.client.get('/api/pricing/quick/', params).status_code, 400, params)
        from apps.library.models import InventoryItem
        InventoryItem.objects.filter(category='plant').update(stock_quantity=0)
        self.assertEqual(self.client.get('/api/pricing/quick/', {'area_sqm': 50}).status_code, 404)


class StaffWorkQueueTests(APITestCase):
    """Order status rules, paged/filtered staff queues and their badge counts."""

    def setUp(self):
        from apps.library.models import StaffProfile, InventoryItem
        self.office = User.objects.create_user(username='office', password='pw', is_staff=True)
        StaffProfile.objects.create(user=self.office, role='OFFICE_ADMIN')
        self.crew = User.objects.create_user(username='crew', password='pw', is_staff=True)
        StaffProfile.objects.create(user=self.crew, role='FIELD_CREW')
        self.customer = User.objects.create_user(username='cust', password='pw', first_name='Maria')
        self.plant = InventoryItem.objects.create(name='Fern', category='plant', unit_price=500, stock_quantity=10)
        self._as(self.office)

    def _as(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')

    def _order(self, method='cod', status='Pending', payment_status='Pending', name='Buyer', qty=0):
        from apps.library.models import Order, OrderItem
        order = Order.objects.create(user=self.customer, customer_name=name, customer_email='b@example.com',
                                     customer_address='Lucena', total_price=500, payment_method=method,
                                     status=status, payment_status=payment_status)
        if qty:
            OrderItem.objects.create(order=order, item=self.plant, quantity=qty, price_at_booking=500)
        return order

    def _set(self, order, **data):
        return self.client.patch(f'/api/orders/{order.id}/update_status/', data, format='json')

    # ── order status rules ──────────────────────────────────────────────────
    def test_unpaid_online_order_cannot_be_shipped(self):
        order = self._order(method='online')
        r = self._set(order, status='Shipped')
        self.assertEqual(r.status_code, 400)
        self.assertIn('not been paid', r.data['error'])
        self.assertEqual(self.client.get('/api/orders/').data[0]['next_statuses'], ['Cancelled'])
        paid = self._order(method='online', status='Paid', payment_status='Paid')
        self.assertEqual(self._set(paid, status='Shipped').status_code, 200)

    def test_cod_moves_one_step_at_a_time_and_is_paid_on_delivery(self):
        order = self._order()
        self.assertEqual(self._set(order, status='Delivered').status_code, 400)  # skipped shipping
        for step in ('Shipped', 'Out for Delivery', 'Delivered'):
            self.assertEqual(self._set(order, status=step).status_code, 200, step)
        order.refresh_from_db()
        self.assertEqual((order.status, order.payment_status), ('Delivered', 'Paid'))
        self.assertEqual(self._set(order, status='Cancelled').status_code, 400)  # final

    def test_cancelling_a_paid_online_order_flags_a_refund(self):
        order = self._order(method='online', status='Paid', payment_status='Paid', qty=2)
        self.assertEqual(self._set(order, payment_status='Refunded').status_code, 400)  # not cancelled yet
        r = self._set(order, status='Cancelled')
        self.assertTrue(r.data['needs_refund'])
        self.plant.refresh_from_db()
        self.assertEqual(self.plant.stock_quantity, 12)
        self.assertEqual(self._set(order, status='Cancelled').status_code, 200)  # repeat: no double restock
        self.plant.refresh_from_db()
        self.assertEqual(self.plant.stock_quantity, 12)
        self.assertEqual(self.client.get('/api/orders/counts/').data['needs_refund'], 1)
        r = self._set(order, payment_status='Refunded')
        self.assertFalse(r.data['needs_refund'])
        self.assertEqual(self.client.get('/api/orders/counts/').data['needs_refund'], 0)

    def test_payment_status_is_not_set_by_hand(self):
        order = self._order(method='online')
        self.assertEqual(self._set(order, payment_status='Paid').status_code, 400)

    def test_field_crew_only_handle_the_delivery_run(self):
        order = self._order(status='Shipped')
        self._as(self.crew)
        self.assertEqual(self._set(order, status='Delivered').status_code, 404)  # not given to them
        order.delivery_crew = self.crew
        order.save()
        self.assertEqual(self._set(order, status='Cancelled').status_code, 400)
        self.assertEqual(self._set(order, status='Out for Delivery').status_code, 200)
        self.assertEqual(self._set(order, status='Delivered').status_code, 200)

    # ── order queues ────────────────────────────────────────────────────────
    def test_order_queues_paging_and_search(self):
        from apps.library.models import Order
        from django.utils import timezone
        old = self._order(name='Old Cod')
        Order.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=3))
        self._order(method='online', status='Paid', payment_status='Paid', name='Paid Online')
        self._order(method='online', name='Unpaid Online')
        self._order(status='Shipped', name='On The Truck')
        for i in range(30):
            self._order(status='Delivered', payment_status='Paid', name=f'Done {i}')

        self.assertIsInstance(self.client.get('/api/orders/').data, list)  # unpaged callers unchanged
        r = self.client.get('/api/orders/', {'queue': 'to_pack', 'page': 1})
        self.assertEqual([o['customer_name'] for o in r.data['results']], ['Old Cod', 'Paid Online'])  # oldest first
        r = self.client.get('/api/orders/', {'queue': 'history', 'page': 2})
        self.assertEqual((r.data['count'], r.data['pages'], r.data['page'], len(r.data['results'])), (30, 2, 2, 5))
        r = self.client.get('/api/orders/', {'q': f'#{old.id}', 'page': 1})
        self.assertEqual([o['id'] for o in r.data['results']], [old.id])
        r = self.client.get('/api/orders/', {'q': 'truck', 'payment': 'cod', 'page': 1})
        self.assertEqual(r.data['count'], 1)

        counts = self.client.get('/api/orders/counts/').data
        self.assertEqual((counts['to_pack'], counts['awaiting_payment'], counts['in_transit'],
                          counts['cod_to_collect'], counts['history']), (2, 1, 1, 1, 30))
        self._as(self.customer)
        self.assertEqual(self.client.get('/api/orders/counts/').status_code, 403)

    def test_counts_report_new_arrivals(self):
        first = self._order()
        seen = self.client.get('/api/orders/counts/').data['latest_id']
        self.assertEqual(seen, first.id)
        self.assertEqual(self.client.get('/api/orders/counts/', {'since': seen}).data['new'], 0)
        self._order()
        self._order()
        self.assertEqual(self.client.get('/api/orders/counts/', {'since': seen}).data['new'], 2)
        self.assertNotIn('new', self.client.get('/api/orders/counts/', {'since': 'x'}).data)

    def test_csv_exports_follow_filters_and_defuse_formulas(self):
        from apps.library.models import ServiceBooking
        self._order(name='=HYPERLINK("http://evil")', qty=2)
        self._order(method='online', status='Paid', payment_status='Paid', name='Online Ana')
        r = self.client.get('/api/orders/export/', {'payment': 'cod'})
        self.assertEqual(r.status_code, 200)
        self.assertIn('attachment; filename="orders-', r['Content-Disposition'])
        text = b''.join(r.streaming_content).decode('utf-8-sig')
        lines = text.strip().splitlines()
        self.assertEqual(len(lines), 2)  # header + the one COD order
        self.assertIn('\'=HYPERLINK', lines[1])  # neutralised, shows as text in Excel
        self.assertIn('2x Fern', lines[1])

        b = ServiceBooking.objects.create(user=self.customer, service_type='Softscape', status='Pending',
                                          scheduled_date='2026-11-03', contact_number='09171234567')
        b.assigned_crew.add(self.crew)
        r = self.client.get('/api/bookings/export/', {'queue': 'awaiting'})
        rows = b''.join(r.streaming_content).decode('utf-8-sig').strip().splitlines()
        self.assertEqual(rows[0].split(',')[:3], ['Booking #', 'Service', 'Visit date'])
        self.assertTrue(rows[1].startswith(f'{b.id},Softscape / Planting Services,2026-11-03'))
        self.assertIn('09171234567', rows[1])

        self._as(self.crew)
        self.assertEqual(self.client.get('/api/orders/export/').status_code, 403)
        self.assertEqual(self.client.get('/api/bookings/export/').status_code, 403)

    def test_crew_delivery_run(self):
        from apps.library.models import Notification, StaffProfile
        other = User.objects.create_user(username='crew2', password='pw', is_staff=True)
        StaffProfile.objects.create(user=other, role='FIELD_CREW')
        cod = self._order(name='Cod Ana')
        paid = self._order(method='online', status='Paid', payment_status='Paid', name='Paid Ben')
        not_mine = self._order(name='Other Crew')
        unpaid = self._order(method='online', name='Unpaid')

        # Office ships two orders with this crew member in one go; they get told what to collect
        r = self.client.post('/api/orders/bulk_status/', {'ids': [cod.id, paid.id], 'status': 'Shipped', 'delivery_crew': self.crew.id},
                             format='json')
        self.assertEqual(r.data['updated'], [cod.id, paid.id])
        self.assertIn('Collect ₱500.00 cash', Notification.objects.get(user=self.crew, message__contains=f'#{cod.id} ').message)
        self.client.post(f'/api/orders/{not_mine.id}/assign_delivery/', {'crew_id': other.id}, format='json')
        self.assertEqual(self.client.post(f'/api/orders/{unpaid.id}/assign_delivery/', {'crew_id': self.office.id},
                                          format='json').status_code, 400)  # not crew

        self._as(self.crew)
        self.assertEqual({o['id'] for o in self.client.get('/api/orders/').data}, {cod.id, paid.id})  # only their own
        run = self.client.get('/api/orders/my_deliveries/').data
        self.assertEqual([o['id'] for o in run['active']], [cod.id, paid.id])
        # Can't touch someone else's delivery, alone or in bulk
        self.assertEqual(self._set(not_mine, status='Out for Delivery').status_code, 404)
        r = self.client.post('/api/orders/bulk_status/', {'ids': [not_mine.id], 'status': 'Out for Delivery'}, format='json')
        self.assertEqual(r.data['updated'], [])
        self.assertEqual(self.client.post('/api/orders/bulk_status/', {'ids': [cod.id], 'status': 'Delivered', 'delivery_crew': other.id},
                                          format='json').status_code, 403)

        self._set(cod, status='Out for Delivery')
        self._set(cod, status='Delivered')
        cod.refresh_from_db()
        self.assertEqual((cod.payment_status, cod.delivered_by_id), ('Paid', self.crew.id))
        self.assertIsNotNone(cod.delivered_at)
        run = self.client.get('/api/orders/my_deliveries/').data
        self.assertEqual(([o['id'] for o in run['active']], [o['id'] for o in run['delivered_today']], run['cash_today']),
                         ([paid.id], [cod.id], '500.00'))

        # Customer not home: the office hears about it, the order stays on the run
        self.assertEqual(self.client.post(f'/api/orders/{paid.id}/report_problem/', {}, format='json').status_code, 400)
        self.client.post(f'/api/orders/{paid.id}/report_problem/', {'reason': 'Customer not home', 'note': 'Gate locked'}, format='json')
        msg = Notification.objects.filter(user=self.office).latest('id').message
        self.assertIn(f'order #{paid.id}', msg)
        self.assertIn('Customer not home', msg)
        paid.refresh_from_db()
        self.assertEqual(paid.status, 'Shipped')

    def test_bulk_ship_follows_the_same_rules(self):
        ready = [self._order(), self._order(method='online', status='Paid', payment_status='Paid')]
        unpaid = self._order(method='online')
        r = self.client.post('/api/orders/bulk_status/', {'ids': [o.id for o in ready] + [unpaid.id, 99999], 'status': 'Shipped'},
                             format='json')
        self.assertEqual(r.data['updated'], [o.id for o in ready])
        self.assertEqual({f['id'] for f in r.data['failed']}, {unpaid.id, 99999})
        unpaid.refresh_from_db()
        self.assertEqual(unpaid.status, 'Pending')
        self.assertEqual(self.client.post('/api/orders/bulk_status/', {'ids': 'x', 'status': 'Shipped'}, format='json').status_code, 400)
        self.assertEqual(self.client.post('/api/orders/bulk_status/', {'ids': [1], 'status': 'Teleported'}, format='json').status_code, 400)
        self._as(self.customer)
        self.assertEqual(self.client.post('/api/orders/bulk_status/', {'ids': [ready[0].id], 'status': 'Delivered'},
                                          format='json').status_code, 403)

    def test_pick_list_totals_items_across_orders(self):
        from apps.library.models import InventoryItem, OrderItem
        rock = InventoryItem.objects.create(name='Rock', category='hardscape', unit_price=100, stock_quantity=50)
        a = self._order(qty=2)
        b = self._order(method='online', status='Paid', payment_status='Paid', qty=3)
        OrderItem.objects.create(order=b, item=rock, quantity=4, price_at_booking=100)
        self._order(method='online', qty=9)  # unpaid: not in "to pack"

        data = self.client.get('/api/orders/pick_list/').data  # default: everything to pack
        self.assertEqual(data['order_count'], 2)
        self.assertEqual({i['name']: (i['quantity'], i['orders']) for i in data['items']},
                         {'Fern': (5, [a.id, b.id]), 'Rock': (4, [b.id])})
        self.assertEqual([o['id'] for o in data['orders']], [a.id, b.id])
        only_b = self.client.get('/api/orders/pick_list/', {'ids': str(b.id)}).data
        self.assertEqual(only_b['order_count'], 1)
        # Order rows show the product picture the shop uses
        self.plant.image_url = 'https://img.test/fern.jpg'
        self.plant.save()
        row = self.client.get('/api/orders/', {'queue': 'to_pack', 'page': 1}).data['results'][0]
        self.assertEqual(row['items'][0]['item_image'], 'https://img.test/fern.jpg')

    # ── booking queues ──────────────────────────────────────────────────────
    def test_booking_queues_counts_and_dates(self):
        from apps.library.models import ServiceBooking
        import datetime
        today = datetime.date.today()

        def make(status, days=0):
            return ServiceBooking.objects.create(
                user=self.customer, service_type='Softscape', status=status,
                scheduled_date=today + datetime.timedelta(days=days), service_address='Lucena')

        make('Pending', 5)
        make('Pending', 1)
        unassigned = make('Preparing')
        make('Installing').assigned_crew.add(self.crew)
        make('For Review', -1)
        make('Finished', -10)

        counts = self.client.get('/api/bookings/counts/').data
        self.assertEqual({k: counts[k] for k in ('awaiting', 'dispatch', 'active', 'review', 'completed', 'today', 'all')},
                         {'awaiting': 2, 'dispatch': 1, 'active': 1, 'review': 1, 'completed': 1, 'today': 2, 'all': 6})
        r = self.client.get('/api/bookings/', {'queue': 'awaiting', 'page': 1})
        self.assertEqual([b['scheduled_date'] for b in r.data['results']],
                         [str(today + datetime.timedelta(days=1)), str(today + datetime.timedelta(days=5))])  # soonest first
        r = self.client.get('/api/bookings/', {'queue': 'dispatch', 'page': 1})
        self.assertEqual([b['id'] for b in r.data['results']], [unassigned.id])
        r = self.client.get('/api/bookings/', {'from': str(today), 'to': str(today), 'page': 1})
        self.assertEqual(r.data['count'], 2)
        self.assertEqual(self.client.get('/api/bookings/', {'q': 'maria', 'page': 1}).data['count'], 6)
        self._as(self.crew)  # crew counts only cover their own jobs
        self.assertEqual(self.client.get('/api/bookings/counts/').data['all'], 1)
        self.assertIsInstance(self.client.get('/api/bookings/').data, list)

    def test_startable_lists_only_jobs_that_can_be_clocked_in_to(self):
        from django.utils import timezone
        from apps.library.models import ServiceBooking
        today = timezone.localdate()

        def make(status, days=0):
            return ServiceBooking.objects.create(user=self.customer, service_type='Softscape', status=status,
                                                 scheduled_date=today + timedelta(days=days))
        due = make('Preparing')
        late = make('Preparing', -2)
        going = make('Installing', 3)
        for status, days in (('Preparing', 1), ('Pending', 0), ('For Review', 0), ('Finished', 0), ('Cancelled', 0)):
            make(status, days)
        ids = {b['id'] for b in self.client.get('/api/bookings/', {'startable': '1'}).data}
        self.assertEqual(ids, {due.id, late.id, going.id})      # the same rule as field_work.start_job_error

    def test_dispatch_schedule_shows_who_is_where(self):
        from apps.library.models import ServiceBooking
        import datetime
        monday = datetime.date(2026, 11, 2)
        job = ServiceBooking.objects.create(user=self.customer, service_type='Softscape', status='Preparing',
                                            scheduled_date=monday, preferred_time='morning', service_address='Lucena')
        job.assigned_crew.add(self.crew)
        ServiceBooking.objects.create(user=self.customer, service_type='Softscape', status='Pending',
                                      scheduled_date=monday + datetime.timedelta(days=2))
        ServiceBooking.objects.create(user=self.customer, service_type='Softscape', status='Cancelled', scheduled_date=monday)
        ServiceBooking.objects.create(user=self.customer, service_type='Softscape', status='Pending',
                                      scheduled_date=monday + datetime.timedelta(days=9))  # next week

        week = {'from': '2026-11-02', 'to': '2026-11-08'}
        data = self.client.get('/api/bookings/schedule/', week).data
        self.assertEqual([c['id'] for c in data['crew']], [self.crew.id])
        self.assertEqual([(b['id'], b['crew']) for b in data['bookings']][0], (job.id, [self.crew.id]))
        self.assertEqual(len(data['bookings']), 2)  # cancelled and next week left out
        self.assertEqual(self.client.get('/api/bookings/schedule/', {'from': '2026-11-08', 'to': '2026-11-02'}).status_code, 400)
        self.assertEqual(self.client.get('/api/bookings/schedule/', {'from': '2026-01-01', 'to': '2026-12-31'}).status_code, 400)
        self._as(self.crew)
        self.assertEqual(self.client.get('/api/bookings/schedule/', week).status_code, 403)


@override_settings(MEDIA_ROOT=TEMP_MEDIA)
class InventoryUploadTests(APITestCase):
    """Product photos uploaded from the Inventory page; 3D models are picked from the bundled catalog by name."""

    def setUp(self):
        from apps.library.models import InventoryItem, StaffProfile
        self.office = User.objects.create_user(username='office', password='pw', is_staff=True)
        StaffProfile.objects.create(user=self.office, role='OFFICE_ADMIN')
        self.customer = User.objects.create_user(username='cust', password='pw')
        self.item = InventoryItem.objects.create(name='Fern', category='plant', unit_price=500, stock_quantity=10)
        self.url = f'/api/inventory/{self.item.id}/'

    def _as(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')

    def test_photo_is_saved_and_model_is_picked_by_name(self):
        from PIL import Image
        self._as(self.office)
        r = self.client.patch(self.url, {'thumbnail': _photo_with_gps('fern.jpg'), 'model_file': 'Geranium.glb'}, format='multipart')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(r.data['thumbnail'].startswith('http://testserver/media/thumbnails/'))
        self.assertEqual(r.data['model_file'], 'geranium.glb')                 # a bundled model's name
        self.item.refresh_from_db()
        with Image.open(self.item.thumbnail.path) as img:
            self.assertEqual(img.format, 'JPEG')
            self.assertNotIn(0x8825, img.getexif())  # phone GPS stripped
        # An edit keeps the photo and model, and can no longer overwrite stock
        r = self.client.patch(self.url, {'stock_quantity': 4, 'name': 'Fern'}, format='multipart')
        self.item.refresh_from_db()
        self.assertTrue(self.item.thumbnail and self.item.model_file)
        self.assertEqual(self.item.stock_quantity, 10)

    def test_growth_heights_are_checked(self):
        self._as(self.office)
        self.item.real_world_size = 3.5
        self.item.save()
        ok = self.client.patch(self.url, {'planting_height': '1.2', 'mature_height': '4'}, format='multipart')
        self.assertEqual(ok.status_code, 200, ok.data)
        self.assertEqual((ok.data['planting_height'], ok.data['mature_height']), (1.2, 4.0))
        # Empty = unknown
        cleared = self.client.patch(self.url, {'planting_height': ''}, format='multipart')
        self.assertEqual(cleared.status_code, 200, cleared.data)
        self.assertIsNone(cleared.data['planting_height'])
        # At planting can't be taller than at 2 years; fully grown can't be shorter
        bad = self.client.patch(self.url, {'planting_height': 5, 'mature_height': 2}, format='json')
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(set(bad.data), {'planting_height', 'mature_height'})
        self.assertEqual(self.client.patch(self.url, {'mature_height': 99}, format='json').status_code, 400)
        # Not a plant: growth heights don't apply
        r = self.client.patch(self.url, {'category': 'furniture', 'mature_height': 6}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIsNone(r.data['mature_height'])

    def test_specimen_size_is_checked(self):
        """Specimen price and planted height: both or neither, dearer than standard, between planting and grown."""
        self._as(self.office)
        self.item.planting_height, self.item.real_world_size, self.item.mature_height = 0.4, 1.0, 2.0
        self.item.save()
        patch = lambda data: self.client.patch(self.url, data, format='json')  # noqa: E731
        ok = patch({'specimen_price': '1200', 'specimen_height': 1.2})
        self.assertEqual(ok.status_code, 200, ok.data)
        self.assertEqual((float(ok.data['specimen_price']), ok.data['specimen_height']), (1200.0, 1.2))
        self.assertIn('specimen_price', patch({'specimen_price': '400', 'specimen_height': 1.2}).data)    # cheaper than ₱500
        self.assertIn('specimen_height', patch({'specimen_price': '900', 'specimen_height': 0.3}).data)   # not bigger
        self.assertIn('specimen_height', patch({'specimen_price': '900', 'specimen_height': 2.5}).data)   # past grown
        self.assertIn('specimen_height', patch({'specimen_price': '900', 'specimen_height': None}).data)  # price alone
        cleared = patch({'specimen_price': None, 'specimen_height': None})
        self.assertEqual(cleared.status_code, 200, cleared.data)
        self.assertIsNone(cleared.data['specimen_price'])
        r = patch({'category': 'furniture', 'specimen_price': '900', 'specimen_height': 1.2})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIsNone(r.data['specimen_price'])

    def test_years_to_mature_is_checked(self):
        """Years after planting until fully grown (the 3D "Fully grown" card): 1–60, empty = unknown, plants only."""
        self._as(self.office)
        ok = self.client.patch(self.url, {'years_to_mature': '12'}, format='multipart')
        self.assertEqual(ok.status_code, 200, ok.data)
        self.assertEqual(ok.data['years_to_mature'], 12)
        cleared = self.client.patch(self.url, {'years_to_mature': ''}, format='multipart')
        self.assertEqual(cleared.status_code, 200, cleared.data)
        self.assertIsNone(cleared.data['years_to_mature'])
        self.assertEqual(self.client.patch(self.url, {'years_to_mature': 0}, format='json').status_code, 400)
        self.assertEqual(self.client.patch(self.url, {'years_to_mature': 61}, format='json').status_code, 400)
        r = self.client.patch(self.url, {'category': 'furniture', 'years_to_mature': 5}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIsNone(r.data['years_to_mature'])

    def test_growth_spreads_are_checked(self):
        """Widths at planting / ~2 years / fully grown (the 3D growth view widens plants at their own pace)."""
        self._as(self.office)
        ok = self.client.patch(self.url, {'planting_spread': '0.6', 'established_spread': '1.5', 'mature_spread': '3'},
                               format='multipart')
        self.assertEqual(ok.status_code, 200, ok.data)
        self.assertEqual((ok.data['planting_spread'], ok.data['established_spread'], ok.data['mature_spread']), (0.6, 1.5, 3.0))
        # Must read small ≤ medium ≤ large
        bad = self.client.patch(self.url, {'planting_spread': 2, 'mature_spread': 1}, format='json')
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(set(bad.data), {'planting_spread', 'mature_spread'})
        self.assertEqual(self.client.patch(self.url, {'mature_spread': 99}, format='json').status_code, 400)
        # The 3D view grows from the 2-year width, so the others need it
        r = self.client.patch(self.url, {'established_spread': ''}, format='multipart')
        self.assertEqual(r.status_code, 400)
        self.assertIn('established_spread', r.data)
        cleared = self.client.patch(self.url, {'planting_spread': '', 'established_spread': '', 'mature_spread': ''}, format='multipart')
        self.assertEqual(cleared.status_code, 200, cleared.data)
        self.assertIsNone(cleared.data['established_spread'])
        # Not a plant: widths don't apply
        r = self.client.patch(self.url, {'category': 'hardscape', 'established_spread': 2}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIsNone(r.data['established_spread'])

    def test_bad_models_and_files_are_rejected(self):
        self._as(self.office)
        glb = SimpleUploadedFile('rock.glb', b'glTF' + b'\x00' * 32, content_type='model/gltf-binary')
        self.assertEqual(self.client.patch(self.url, {'model_file': glb}, format='multipart').status_code, 400)   # no uploads any more
        for bad in ('../../etc/passwd', 'rock.exe', 'https://evil.example/x.glb'):
            self.assertEqual(self.client.patch(self.url, {'model_file': bad}, format='json').status_code, 400, bad)
        self.assertEqual(self.client.patch(self.url, {'model_file': ''}, format='json').status_code, 200)       # no 3D model
        notimg = SimpleUploadedFile('x.jpg', b'hello', content_type='image/jpeg')
        self.assertEqual(self.client.patch(self.url, {'thumbnail': notimg}, format='multipart').status_code, 400)

    def test_customers_cannot_upload(self):
        self._as(self.customer)
        self.assertEqual(self.client.patch(self.url, {'thumbnail': _photo_with_gps()}, format='multipart').status_code, 403)


from django.test import override_settings


@override_settings(NOTIFY_EMAIL_ASYNC=False, EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class NotificationTests(APITestCase):
    """Who hears about what (notifications.py), crew emails, links, grouping and the bell API."""

    def setUp(self):
        from apps.library.models import InventoryItem, ServiceBooking, StaffProfile
        self.office = User.objects.create_user(username='office', password='pw', is_staff=True, email='office@example.com')
        StaffProfile.objects.create(user=self.office, role='OFFICE_ADMIN')
        self.crew = User.objects.create_user(username='crew', password='pw', is_staff=True, first_name='Jun', email='jun@example.com')
        StaffProfile.objects.create(user=self.crew, role='FIELD_CREW')
        self.crew2 = User.objects.create_user(username='crew2', password='pw', is_staff=True, first_name='Ben', email='ben@example.com')
        StaffProfile.objects.create(user=self.crew2, role='FIELD_CREW')
        self.customer = User.objects.create_user(username='maria', password='pw', first_name='Maria', last_name='Cruz')
        self.plant = InventoryItem.objects.create(name='Fern', category='plant', unit_price=500, stock_quantity=20)
        self.booking = ServiceBooking.objects.create(user=self.customer, service_type='Softscape',
                                                     scheduled_date='2026-11-20', service_address='Lucena City')

    def _as(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')

    def _notes(self, user, kind=None):
        from apps.library.models import Notification
        qs = Notification.objects.filter(user=user)
        return list(qs.filter(kind=kind) if kind else qs)

    def _cod_order(self):
        self._as(self.customer)
        with self.captureOnCommitCallbacks(execute=True):
            r = self.client.post('/api/checkout/', {
                'customer_name': 'Maria Cruz', 'customer_email': 'm@example.com', 'address': QUEZON_ADDRESS,
                'customer_phone': '09171234567', 'payment_method': 'cod', 'items': [{'id': self.plant.id, 'quantity': 1}]},
                format='json')
        self.assertEqual(r.status_code, 201, r.data)
        return r.data['order_id']

    def test_new_cod_orders_reach_the_office_grouped(self):
        first = self._cod_order()
        [n] = self._notes(self.office, 'order_new')
        self.assertIn(f'#{first}', n.message)
        self.assertEqual(n.link, f'/staff-dashboard?tab=orders&queue=all&q={first}&open={first}')
        second = self._cod_order()
        [n] = self._notes(self.office, 'order_new')        # still one line
        self.assertEqual(n.count, 2)
        self.assertIn('2 new orders', n.message)
        self.assertIn(f'#{second}', n.message)
        # The buyer gets their own confirmation: in the bell and by email to the checkout address
        self.assertEqual(len(self._notes(self.customer, 'order_placed')), 2)

    def test_online_order_reaches_the_office_only_once_paid(self):
        from apps.library import paymongo
        from apps.library.models import Order
        order = Order.objects.create(user=self.customer, customer_name='Maria', customer_email='m@example.com',
                                     total_price=650, payment_method='online')
        self.assertEqual(self._notes(self.office), [])
        paymongo.mark_order_paid(order, {'id': 'pay_1', 'attributes': {'amount': 65000}})
        paymongo.mark_order_paid(order, {'id': 'pay_1', 'attributes': {'amount': 65000}})   # webhook + sync
        [n] = self._notes(self.office, 'order_new')
        self.assertEqual(n.count, 1)

    def test_new_booking_reaches_the_office(self):
        self._as(self.customer)
        r = self.client.post('/api/bookings/', {'service_type': 'maintenance', 'scheduled_date': '2026-12-01',
                                                'address': QUEZON_ADDRESS}, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        [n] = self._notes(self.office, 'booking_new')
        self.assertIn('Maria Cruz', n.message)
        self.assertTrue(n.link.endswith(f"&open={r.data['id']}"))

    def test_crew_assignment_notifies_and_emails_crew_and_tells_the_customer(self):
        from django.core import mail
        self._as(self.office)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(f'/api/bookings/{self.booking.id}/assign_crew/', {'crew_ids': [self.crew.id]}, format='json')
        [n] = self._notes(self.crew, 'job_assigned')
        self.assertEqual(n.link, f'/staff-dashboard?tab=projects&job={self.booking.id}')
        self.assertIn('Lucena City', n.message)
        self.assertEqual([m.to for m in mail.outbox], [['jun@example.com']])
        self.assertIn(f'/staff-dashboard?tab=projects&job={self.booking.id}', mail.outbox[0].body)
        [c] = self._notes(self.customer, 'crew_assigned')
        self.assertIn('Jun', c.message)
        # Swap Jun for Ben: Ben is assigned, Jun is told he can stay home
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(f'/api/bookings/{self.booking.id}/assign_crew/', {'crew_ids': [self.crew2.id]}, format='json')
        self.assertEqual(len(self._notes(self.crew2, 'job_assigned')), 1)
        self.assertEqual(len(self._notes(self.crew, 'job_unassigned')), 1)
        self.assertEqual(len(self._notes(self.crew, 'job_assigned')), 1)   # not re-sent

    def test_sign_off_round_trip(self):
        from django.core import mail
        self.booking.status = 'Installing'
        self.booking.save()
        self.booking.assigned_crew.add(self.crew)
        finish_crew_work(self.booking)
        self._as(self.crew)
        self.client.patch(f'/api/bookings/{self.booking.id}/update_status/', {'status': 'For Review'}, format='json')
        self.assertEqual(len(self._notes(self.office, 'job_review')), 1)
        self._as(self.office)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.patch(f'/api/bookings/{self.booking.id}/update_status/',
                              {'status': 'Installing', 'note': 'Mulch the front bed'}, format='json')
        [n] = self._notes(self.crew, 'job_sent_back')
        self.assertIn('Mulch the front bed', n.message)
        self.assertEqual(len(mail.outbox), 1)

    def test_customer_cancel_keeps_the_booking_and_tells_office_and_crew(self):
        from apps.library.models import ServiceBooking
        self.booking.assigned_crew.add(self.crew)
        self._as(self.customer)
        with self.captureOnCommitCallbacks(execute=True):
            r = self.client.delete(f'/api/bookings/{self.booking.id}/')
        self.assertEqual(r.status_code, 204)
        self.assertEqual(ServiceBooking.objects.get(pk=self.booking.id).status, 'Cancelled')   # kept, not deleted
        self.assertEqual(len(self._notes(self.office, 'booking_cancelled')), 1)
        self.assertEqual(len(self._notes(self.crew, 'job_cancelled')), 1)
        self.assertEqual(self.client.delete(f'/api/bookings/{self.booking.id}/').status_code, 400)

    def test_customer_cannot_change_status_but_reschedule_reaches_the_crew(self):
        from apps.library.models import ServiceBooking
        self.booking.assigned_crew.add(self.crew)
        self._as(self.customer)
        with self.captureOnCommitCallbacks(execute=True):
            r = self.client.patch(f'/api/bookings/{self.booking.id}/',
                                  {'status': 'Finished', 'scheduled_date': '2026-11-27'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        b = ServiceBooking.objects.get(pk=self.booking.id)
        self.assertEqual((b.status, str(b.scheduled_date)), ('Pending', '2026-11-27'))
        [n] = self._notes(self.crew, 'job_rescheduled')
        self.assertIn('Nov 20', n.message)
        self.assertIn('Nov 27', n.message)

    def test_customer_hears_about_delivery_and_drivers_about_reassignment(self):
        from apps.library.models import Order
        order = Order.objects.create(user=self.customer, customer_name='Maria', customer_email='m@example.com',
                                     total_price=650, payment_method='cod', status='Shipped')
        self._as(self.office)
        self.client.post(f'/api/orders/{order.id}/assign_delivery/', {'crew_id': self.crew.id}, format='json')
        self.client.post(f'/api/orders/{order.id}/assign_delivery/', {'crew_id': self.crew2.id}, format='json')
        self.assertEqual(len(self._notes(self.crew, 'delivery_assigned')), 1)
        self.assertEqual(len(self._notes(self.crew, 'delivery_unassigned')), 1)
        self.client.patch(f'/api/orders/{order.id}/update_status/', {'status': 'Out for Delivery'}, format='json')
        [n] = self._notes(self.customer, 'order_on_the_way')
        self.assertIn('650.00 cash', n.message)
        self.assertEqual(n.link, '/my-orders')

    # ── The customer side: every step of an order or booking reaches the buyer ──

    def test_cod_order_confirms_to_the_buyer_by_bell_and_email(self):
        from django.core import mail
        from apps.library.models import Order
        order_id = self._cod_order()
        [n] = self._notes(self.customer, 'order_placed')
        self.assertIn(f'#{order_id}', n.message)
        self.assertIn('cash on delivery', n.message)
        self.assertEqual(n.link, '/my-orders')
        [email] = [m for m in mail.outbox if m.to == ['m@example.com']]   # the address typed at checkout
        self.assertIn(f'Order received (order #{order_id})', email.subject)
        token = Order.objects.get(pk=order_id).receipt_token
        self.assertIn(f'/order-success?order_id={order_id}&token={token}', email.body)

    def test_paid_online_order_confirms_to_a_guest_by_email(self):
        from django.core import mail
        from apps.library import paymongo
        from apps.library.models import Order
        order = Order.objects.create(customer_name='Guest', customer_email='guest@example.com', total_price=650,
                                     payment_method='online', receipt_token='tok123')
        with self.captureOnCommitCallbacks(execute=True):
            paymongo.mark_order_paid(order, {'id': 'pay_1', 'attributes': {'amount': 65000}})
            paymongo.mark_order_paid(order, {'id': 'pay_1', 'attributes': {'amount': 65000}})   # webhook + sync
        guest_mail = [m for m in mail.outbox if m.to == ['guest@example.com']]
        self.assertEqual(len(guest_mail), 1)
        self.assertIn('Payment received', guest_mail[0].subject)
        self.assertIn(f'order_id={order.id}&token=tok123', guest_mail[0].body)

    def test_online_order_paid_after_expiry_warns_the_office(self):
        from apps.library import paymongo
        from apps.library.models import Order
        order = Order.objects.create(user=self.customer, customer_name='Maria', customer_email='m@example.com',
                                     total_price=650, payment_method='online', status='Cancelled', payment_status='Expired')
        paymongo.mark_order_paid(order, {'id': 'pay_1', 'attributes': {'amount': 65000}})
        [n] = self._notes(self.office, 'order_late_payment')
        self.assertIn('Check the stock', n.message)
        self.assertEqual(len(self._notes(self.customer, 'order_paid')), 1)

    def test_expired_online_order_tells_the_buyer(self):
        import datetime
        from django.utils import timezone
        from apps.library import paymongo
        from apps.library.models import Order
        order = Order.objects.create(user=self.customer, customer_name='Maria', customer_email='m@example.com',
                                     total_price=650, payment_method='online')
        Order.objects.filter(pk=order.pk).update(created_at=timezone.now() - datetime.timedelta(days=1))
        self.assertEqual(paymongo.expire_unpaid_online_orders(), 1)
        [n] = self._notes(self.customer, 'order_expired')
        self.assertIn('not charged', n.message)

    def test_every_order_step_reaches_the_buyer_and_refund_is_confirmed(self):
        from django.core import mail
        from apps.library.models import Order
        order = Order.objects.create(user=self.customer, customer_name='Maria', customer_email='m@example.com',
                                     total_price=650, payment_method='online', status='Paid', payment_status='Paid')
        self._as(self.office)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.patch(f'/api/orders/{order.id}/update_status/', {'status': 'Shipped'}, format='json')
        self.assertEqual(len(self._notes(self.customer, 'order_packed')), 1)
        self.assertEqual(mail.outbox, [])                                  # packed: the bell only
        with self.captureOnCommitCallbacks(execute=True):
            self.client.patch(f'/api/orders/{order.id}/update_status/', {'status': 'Cancelled'}, format='json')
            self.client.patch(f'/api/orders/{order.id}/update_status/', {'payment_status': 'Refunded'}, format='json')
        [c] = self._notes(self.customer, 'order_cancelled')
        self.assertIn('will be refunded', c.message)
        [r] = self._notes(self.customer, 'order_refunded')
        self.assertIn('650.00', r.message)
        self.assertEqual([m.subject.split(':', 1)[1].strip() for m in mail.outbox],
                         [f'Order cancelled (order #{order.id})', f'Refund sent (order #{order.id})'])

    def test_customer_cancelling_a_cod_order_keeps_it_and_tells_the_office(self):
        from apps.library.models import ActivityLog, Order
        order_id = self._cod_order()
        self._as(self.customer)
        self.assertEqual(self.client.delete(f'/api/orders/{order_id}/').status_code, 204)
        self.assertEqual(Order.objects.get(pk=order_id).status, 'Cancelled')
        [n] = self._notes(self.office, 'order_withdrawn')
        self.assertIn(f'#{order_id}', n.message)
        self.assertIn("Don't pack it", n.message)
        self.assertTrue(n.link.endswith(f'&open={order_id}'))              # the order still exists to open
        log = ActivityLog.objects.get(action='order_cancelled')
        self.assertEqual((log.actor, log.details['by_customer']), (self.customer, True))

    def test_new_order_is_emailed_to_the_office_with_what_packing_needs(self):
        from django.core import mail
        order_id = self._cod_order()
        [email] = [m for m in mail.outbox if m.to == ['office@example.com']]
        self.assertIn(f'New order #{order_id}', email.subject)
        self.assertIn('cash on delivery', email.subject)
        self.assertIn('1 × Fern', email.body)
        self.assertIn('Phone: 09171234567', email.body)
        self.assertIn(f'/staff-dashboard?tab=orders&queue=all&q={order_id}&open={order_id}', email.body)
        self.assertFalse([m for m in mail.outbox if m.to == ['jun@example.com']])   # crew are not emailed

    def test_online_order_paid_after_the_customer_cancelled_warns_the_office(self):
        from apps.library import paymongo
        from apps.library.models import Order
        order = Order.objects.create(user=self.customer, customer_name='Maria', customer_email='m@example.com',
                                     total_price=650, payment_method='online')
        self._as(self.customer)
        self.assertEqual(self.client.delete(f'/api/orders/{order.id}/').status_code, 204)
        self.assertEqual(self._notes(self.office, 'order_withdrawn'), [])   # unpaid online: the office never had it
        paymongo.mark_order_paid(order, {'id': 'pay_1', 'attributes': {'amount': 65000}})
        self.assertEqual(len(self._notes(self.office, 'order_late_payment')), 1)

    def test_booking_request_is_acknowledged_to_the_customer(self):
        self._as(self.customer)
        r = self.client.post('/api/bookings/', {'service_type': 'maintenance', 'scheduled_date': '2026-12-01',
                                                'address': QUEZON_ADDRESS}, format='json')
        [n] = self._notes(self.customer, 'booking_received')
        self.assertIn(f"#{r.data['id']}", n.message)

    def test_customer_reschedule_and_address_change_reach_the_office_not_the_customer(self):
        self._as(self.customer)
        self.client.patch(f'/api/bookings/{self.booking.id}/', {'scheduled_date': '2026-11-27'}, format='json')
        [n] = self._notes(self.office, 'booking_changed')
        self.assertIn('Nov 27', n.message)
        self.assertEqual(self._notes(self.customer, 'booking_rescheduled'), [])   # they made the change
        self.client.patch(f'/api/bookings/{self.booking.id}/', {'address': QUEZON_ADDRESS}, format='json')
        self.assertEqual(len(self._notes(self.office, 'booking_changed')), 2)

    def test_sign_off_tells_the_customer_the_project_is_complete(self):
        self.booking.status = 'For Review'
        self.booking.save()
        self._as(self.office)
        self.client.patch(f'/api/bookings/{self.booking.id}/update_status/', {'status': 'Finished'}, format='json')
        [n] = self._notes(self.customer, 'booking_finished')
        self.assertIn('is complete', n.message)

    def test_project_payments_give_the_customer_a_receipt(self):
        from apps.library import paymongo
        from apps.library.models import ProjectPaymentRequest, ProjectTracker
        tracker = ProjectTracker.objects.create(booking=self.booking, total_price=50000, remaining_balance=50000)
        self._as(self.office)
        r = self.client.post(f'/api/projects/{tracker.id}/record-payment/', {'amount': '10000', 'method': 'cash'},
                             format='json')
        self.assertEqual(r.status_code, 200, r.data)
        [n] = self._notes(self.customer, 'payment_receipt')
        self.assertIn('₱10,000.00', n.message)
        self.assertIn('Remaining balance: ₱40,000.00', n.message)
        req = ProjectPaymentRequest.objects.create(project=tracker, label='Downpayment', amount=40000, token='t1')
        paymongo.mark_project_request_paid(req, {'id': 'pay_2', 'attributes': {'amount': 4000000}})
        latest = self._notes(self.customer, 'payment_receipt')[0]
        self.assertIn('fully paid', latest.message)
        req2 = ProjectPaymentRequest.objects.create(project=tracker, label='Extra', amount=100, token='t2')
        self.client.post(f'/api/projects/{tracker.id}/payment-requests/{req2.id}/cancel/')
        self.assertEqual(len(self._notes(self.customer, 'payment_request_withdrawn')), 1)

    def test_bell_api_is_paged_private_and_counts_unread(self):
        from apps.library.models import Notification
        for i in range(25):
            Notification.objects.create(user=self.customer, message=f'n{i}')
        Notification.objects.create(user=self.office, message='not yours')
        self._as(self.customer)
        page1 = self.client.get('/api/notifications/').data
        self.assertEqual((len(page1['results']), page1['has_more'], page1['unread']), (20, True, 25))
        self.assertEqual(page1['results'][0]['message'], 'n24')                     # newest first
        page2 = self.client.get('/api/notifications/?page=2').data
        self.assertEqual((len(page2['results']), page2['has_more']), (5, False))
        first = page1['results'][0]['id']
        self.client.patch(f'/api/notifications/{first}/mark_read/')
        self.assertEqual(self.client.get('/api/notifications/unread_count/').data['unread'], 24)
        not_mine = Notification.objects.get(user=self.office).id
        self.assertEqual(self.client.patch(f'/api/notifications/{not_mine}/mark_read/').status_code, 404)
        self.client.post('/api/notifications/mark_all_read/')
        self.assertEqual(self.client.get('/api/notifications/unread_count/').data['unread'], 0)


class AfterCommitTests(APITestCase):
    """Emails and pushes must actually run on PythonAnywhere, whose uWSGI workers never run app threads."""

    def setUp(self):
        from apps.library import after_commit
        after_commit._THREADS = None
        self.addCleanup(setattr, after_commit, '_THREADS', None)

    def _fake_uwsgi(self, **opt):
        import sys, types
        mod = types.ModuleType('uwsgi')
        mod.opt = opt
        return patch.dict(sys.modules, {'uwsgi': mod})

    def test_threads_are_detected_per_server(self):
        from apps.library.after_commit import threads_run_here
        self.assertTrue(threads_run_here())                                   # no uWSGI: gunicorn / runserver
        with self._fake_uwsgi(socket=b'/tmp/x'):
            self.assertFalse(threads_run_here())                              # PythonAnywhere
        with self._fake_uwsgi(**{'enable-threads': True}):
            self.assertTrue(threads_run_here())
        with self._fake_uwsgi(**{'threads': b'4'}):
            self.assertTrue(threads_run_here())

    @override_settings(NOTIFY_EMAIL_ASYNC=True)
    def test_without_threads_the_work_runs_right_after_the_commit(self):
        from apps.library.after_commit import run_after_commit
        ran = []
        with self._fake_uwsgi(socket=b'/tmp/x'), patch('threading.Thread') as thread:
            with self.captureOnCommitCallbacks(execute=True):
                run_after_commit(lambda: ran.append('sent'))
        self.assertEqual(ran, ['sent'])
        thread.assert_not_called()

    @override_settings(NOTIFY_EMAIL_ASYNC=True)
    def test_with_threads_the_work_goes_to_a_background_thread(self):
        from apps.library.after_commit import run_after_commit
        with patch('threading.Thread') as thread:
            with self.captureOnCommitCallbacks(execute=True):
                run_after_commit(lambda: None)
        thread.assert_called_once()
        thread.return_value.start.assert_called_once()

    @override_settings(NOTIFY_EMAIL_ASYNC=False, EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_one_email_per_recipient_and_a_bad_address_does_not_stop_the_rest(self):
        from django.core import mail
        from apps.library import notifications
        real_send = mail.EmailMessage.send

        def send(msg, *a, **k):
            if msg.to == ['bad@example.com']:
                raise OSError('rejected')
            return real_send(msg, *a, **k)
        with patch('django.core.mail.EmailMessage.send', send), self.captureOnCommitCallbacks(execute=True):
            notifications._email_addresses(['a@example.com', 'bad@example.com', 'b@example.com', 'a@example.com'],
                                           'Hello', 'Body', '/x')
        self.assertEqual([m.to for m in mail.outbox], [['a@example.com'], ['b@example.com']])


TEST_VAPID = dict(VAPID_PUBLIC_KEY='BPublicKeyForTests', VAPID_PRIVATE_KEY='private-key-for-tests', VAPID_SUBJECT='mailto:test@example.com')


@override_settings(NOTIFY_EMAIL_ASYNC=False, EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend', **TEST_VAPID)
class PushTests(APITestCase):
    """Web Push: subscriptions, crew-only alerts, dead-subscription cleanup, test alert, off switch."""

    def setUp(self):
        from apps.library.models import ServiceBooking, StaffProfile
        self.office = User.objects.create_user(username='office', password='pw', is_staff=True)
        StaffProfile.objects.create(user=self.office, role='OFFICE_ADMIN')
        self.crew = User.objects.create_user(username='crew', password='pw', is_staff=True, first_name='Jun')
        StaffProfile.objects.create(user=self.crew, role='FIELD_CREW')
        self.customer = User.objects.create_user(username='maria', password='pw')
        self.booking = ServiceBooking.objects.create(user=self.customer, service_type='Softscape',
                                                     scheduled_date='2026-11-20', service_address='Lucena City')

    def _as(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')

    def _subscribe(self, user, endpoint='https://push.example.com/abc'):
        self._as(user)
        return self.client.post('/api/push/subscribe/', {'endpoint': endpoint, 'keys': {'p256dh': 'BKey', 'auth': 'authsecret'}}, format='json')

    def test_subscribe_validates_and_one_device_has_one_owner(self):
        from apps.library.models import PushSubscription
        self._as(self.crew)
        self.assertTrue(self.client.get('/api/push/config/').data['enabled'])
        bad = self.client.post('/api/push/subscribe/', {'endpoint': 'http://insecure', 'keys': {'p256dh': 'x', 'auth': 'y'}}, format='json')
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self._subscribe(self.crew).data['devices'], 1)
        self._subscribe(self.crew)                                    # same phone again: still one row
        self.assertEqual(PushSubscription.objects.count(), 1)
        self._subscribe(self.office)                                  # someone else signs in on that phone
        self.assertEqual(PushSubscription.objects.get().user, self.office)

    def test_crew_assignment_pushes_to_the_crew_phone(self):
        self._subscribe(self.crew)
        self._subscribe(self.customer, 'https://push.example.com/customer')
        self._as(self.office)
        with patch('pywebpush.webpush') as wp, self.captureOnCommitCallbacks(execute=True):
            self.client.post(f'/api/bookings/{self.booking.id}/assign_crew/', {'crew_ids': [self.crew.id]}, format='json')
        self.assertEqual(wp.call_count, 1)                            # the customer's "crew assigned" is not pushed
        kwargs = wp.call_args.kwargs
        self.assertEqual(kwargs['subscription_info']['endpoint'], 'https://push.example.com/abc')
        payload = json.loads(kwargs['data'])
        self.assertIn(f'booking #{self.booking.id}', payload['title'])
        self.assertIn('Lucena City', payload['body'])
        self.assertEqual(payload['url'], f'/staff-dashboard?tab=projects&job={self.booking.id}')
        self.assertEqual(kwargs['vapid_private_key'], 'private-key-for-tests')

    def test_gone_subscriptions_are_removed(self):
        from pywebpush import WebPushException
        from apps.library.models import PushSubscription
        self._subscribe(self.crew)
        gone = WebPushException('Gone', response=MagicMock(status_code=410))
        self._as(self.crew)
        with patch('pywebpush.webpush', side_effect=gone):
            r = self.client.post('/api/push/test/')
        self.assertEqual((r.data['sent'], r.data['failed']), (0, 1))
        self.assertFalse(PushSubscription.objects.exists())

    def test_test_alert_and_unsubscribe(self):
        from apps.library.models import PushSubscription
        self._subscribe(self.crew)
        self._as(self.crew)
        with patch('pywebpush.webpush') as wp:
            r = self.client.post('/api/push/test/')
        self.assertEqual((r.data['sent'], wp.call_count), (1, 1))
        self._as(self.office)                                         # can't remove someone else's phone
        self.client.post('/api/push/unsubscribe/', {'endpoint': 'https://push.example.com/abc'}, format='json')
        self.assertTrue(PushSubscription.objects.exists())
        self._as(self.crew)
        self.client.post('/api/push/unsubscribe/', {'endpoint': 'https://push.example.com/abc'}, format='json')
        self.assertFalse(PushSubscription.objects.exists())

    @override_settings(VAPID_PRIVATE_KEY='')
    def test_without_keys_push_is_simply_off(self):
        self._as(self.crew)
        self.assertEqual(self.client.get('/api/push/config/').data, {'enabled': False, 'public_key': ''})
        self.assertEqual(self._subscribe(self.crew).status_code, 503)
        self._as(self.office)
        with patch('pywebpush.webpush') as wp, self.captureOnCommitCallbacks(execute=True):
            self.client.post(f'/api/bookings/{self.booking.id}/assign_crew/', {'crew_ids': [self.crew.id]}, format='json')
        wp.assert_not_called()


def finish_crew_work(booking, with_after_photo=True):
    """Test helper: every crew stage done (+ an After photo), i.e. a job that may be submitted."""
    from django.core.files.base import ContentFile
    from apps.library.models import ProjectMilestone, ProjectProgressMedia, ProjectTracker
    for phase in ('site_prep', 'installation', 'final_inspection'):
        ProjectMilestone.objects.update_or_create(booking=booking, phase=phase, defaults={'completion_pct': 100})
    if with_after_photo:
        tracker, _ = ProjectTracker.objects.get_or_create(booking=booking, defaults={'total_price': 0, 'remaining_balance': 0})
        media = ProjectProgressMedia(project=tracker, construction_phase='AFTER')
        with override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix='fieldwork-photo-')):   # never the real media folder
            media.file_path.save('after.jpg', ContentFile(b'\xff\xd8\xff\xd9'), save=False)
        media.save()


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix='fieldwork-tests-'))
class FieldWorkTests(APITestCase):
    """Crew job rules (field_work.py): when a job can start, who updates which stage, the sign-off gate."""

    def test_saved_plant_list_counts_specimens_apart(self):
        """A Studio save counts specimen-size plants as their own row, at the specimen price."""
        from apps.library import field_work
        from apps.library.models import InventoryItem
        hib = InventoryItem.objects.create(name='Hibiscus', category='plant', unit_price=150, stock_quantity=9,
                                           specimen_price=450, specimen_height=1.2)
        rows = field_work.plant_breakdown_for([
            {'productId': hib.id, 'position': {'x': 0, 'z': 0}},
            {'productId': hib.id, 'position': {'x': 1, 'z': 0}, 'size': 'specimen'},
            {'productId': hib.id, 'position': {'x': 2, 'z': 0}, 'size': 'specimen'},
        ])
        by = {(r['name'], r.get('size', '')): r for r in rows}
        self.assertEqual(by[('Hibiscus', '')]['quantity'], 1)
        self.assertEqual((by[('Hibiscus — specimen size', 'specimen')]['quantity'],
                          by[('Hibiscus — specimen size', 'specimen')]['unit_price']), (2, 450.0))

    def setUp(self):
        from django.utils import timezone
        from apps.library.models import ServiceBooking, StaffProfile
        self.today = timezone.localdate()
        self.office = User.objects.create_user(username='office', password='pw', is_staff=True)
        StaffProfile.objects.create(user=self.office, role='OFFICE_ADMIN')
        self.crew = User.objects.create_user(username='crew', password='pw', is_staff=True)
        StaffProfile.objects.create(user=self.crew, role='FIELD_CREW')
        self.customer = User.objects.create_user(username='cust', password='pw')
        self.booking = ServiceBooking.objects.create(user=self.customer, service_type='Softscape',
                                                     scheduled_date=self.today, status='Preparing')
        self.booking.assigned_crew.add(self.crew)

    def _as(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')

    def _start(self):
        self._as(self.crew)
        return self.client.post('/api/attendance/clock_in/', {'booking_id': self.booking.id}, format='json')

    def _stage(self, phase, pct=100, proof='http://testserver/media/p.jpg'):
        return self.client.patch(f'/api/bookings/{self.booking.id}/update_milestone/',
                                 {'phase': phase, 'completion_pct': pct, 'proof_photo_url': proof}, format='json')

    def test_a_job_starts_only_once_confirmed_and_due(self):
        from datetime import timedelta
        self.booking.status = 'Pending'
        self.booking.save()
        self.assertIn('not confirmed', self._start().data['error'])
        self.booking.status, self.booking.scheduled_date = 'Preparing', self.today + timedelta(days=3)
        self.booking.save()
        self.assertIn('You can start it that day', self._start().data['error'])
        self.booking.scheduled_date = self.today
        self.booking.save()
        self.assertEqual(self._start().status_code, 201)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, 'Installing')

    def test_multi_day_jobs_continue_on_later_days(self):
        from datetime import timedelta
        self.booking.status, self.booking.scheduled_date = 'Installing', self.today - timedelta(days=2)
        self.booking.save()
        self.assertEqual(self._start().status_code, 201)

    def test_crew_stage_rules(self):
        self._as(self.crew)
        self.assertEqual(self._stage('site_prep').status_code, 400)                  # not on the job yet
        self._start()
        self.assertEqual(self._stage('design_contract').status_code, 403)            # the office's stage
        self.assertIn('photo', self._stage('site_prep', proof='').data['error'])     # needs a photo
        self.assertEqual(self._stage('site_prep').status_code, 200)
        self.assertEqual(self._stage('site_prep', pct=0, proof='').status_code, 200)  # undo is fine
        self._as(self.office)
        self.assertEqual(self._stage('design_contract', proof='').status_code, 200)  # office ticks it

    def test_sign_off_gate(self):
        self._start()
        r = self.client.patch(f'/api/bookings/{self.booking.id}/update_status/', {'status': 'For Review'}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertEqual(len(r.data['blockers']), 2)                                 # stages + after photo
        self.assertIn('Site prep, Installation, Final inspection', r.data['blockers'][0])
        finish_crew_work(self.booking, with_after_photo=False)
        listed = self.client.get('/api/bookings/').data
        self.assertEqual(listed[0]['sign_off_blockers'], ['Add at least one After photo of the finished garden.'])
        r = self.client.patch(f'/api/bookings/{self.booking.id}/update_status/', {'status': 'For Review'}, format='json')
        self.assertEqual(r.data['blockers'], ['Add at least one After photo of the finished garden.'])
        finish_crew_work(self.booking)
        r = self.client.patch(f'/api/bookings/{self.booking.id}/update_status/', {'status': 'For Review'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)


@override_settings(NOTIFY_EMAIL_ASYNC=False, EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class CrewFieldExtrasTests(APITestCase):
    """Crew field extras: "On my way", job problem reports, and the What to bring list."""

    def setUp(self):
        from django.utils import timezone
        from apps.library.models import GardenDesign, InventoryItem, ServiceBooking, StaffProfile
        self.office = User.objects.create_user(username='office', password='pw', is_staff=True)
        StaffProfile.objects.create(user=self.office, role='OFFICE_ADMIN')
        self.crew = User.objects.create_user(username='crew', password='pw', is_staff=True, first_name='Jun')
        StaffProfile.objects.create(user=self.crew, role='FIELD_CREW')
        self.other = User.objects.create_user(username='crew2', password='pw', is_staff=True)
        StaffProfile.objects.create(user=self.other, role='FIELD_CREW')
        self.customer = User.objects.create_user(username='maria', password='pw', email='maria@example.com')
        self.palm = InventoryItem.objects.create(name='Palm', category='plant', unit_price=500, stock_quantity=9)
        self.fern = InventoryItem.objects.create(name='Fern', category='plant', unit_price=80, stock_quantity=50)
        self.bench = InventoryItem.objects.create(name='Bench', category='furniture', unit_price=3000, stock_quantity=3)
        design = GardenDesign.objects.create(name='D', user=self.customer, placed_items=[
            {'plant_id': str(self.palm.id), 'x': 1, 'z': 1}, {'plant_id': str(self.palm.id), 'x': 2, 'z': 1},
            {'plant_id': str(self.fern.id), 'x': 3, 'z': 1},
            {'productId': self.bench.id, 'position': {'x': 0, 'z': 0}},                     # Studio format
            {'plant_id': str(self.palm.id), 'x': 5, 'z': 5, 'is_existing': True},           # already on site
        ])
        self.booking = ServiceBooking.objects.create(user=self.customer, service_type='Softscape', status='Preparing',
                                                     scheduled_date=timezone.localdate(), service_address='12 Rizal St',
                                                     design=design)
        self.booking.assigned_crew.add(self.crew)

    def _as(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')

    def _on_my_way(self, eta=30):
        return self.client.post(f'/api/bookings/{self.booking.id}/on_my_way/', {'eta_minutes': eta}, format='json')

    def test_on_my_way_tells_the_customer_once(self):
        from django.core import mail
        from apps.library.models import Notification
        self._as(self.crew)
        self.assertEqual(self._on_my_way(eta=25).status_code, 400)                    # not one of the choices
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self._on_my_way().status_code, 200)
        n = Notification.objects.get(user=self.customer, kind='crew_on_the_way')
        self.assertIn('Jun from Garden Studio is on the way to 12 Rizal St', n.message)
        self.assertIn('about 30 minutes', n.message)
        self.assertEqual([m.to for m in mail.outbox], [['maria@example.com']])
        again = self._on_my_way()
        self.assertEqual(again.status_code, 400)
        self.assertIn('already told', again.data['error'])
        self._as(self.other)                                                             # not their job
        self.assertEqual(self._on_my_way().status_code, 404)

    def test_on_my_way_only_for_jobs_that_can_start(self):
        self.booking.status = 'Pending'
        self.booking.save()
        self._as(self.crew)
        self.assertIn('not confirmed', self._on_my_way().data['error'])

    def test_job_problem_reaches_the_office_and_the_history(self):
        from apps.library.models import Notification, ProjectHistoryLog, ProjectTracker
        ProjectTracker.objects.create(booking=self.booking, total_price=0, remaining_balance=0)
        self._as(self.crew)
        url = f'/api/bookings/{self.booking.id}/report_problem/'
        self.assertEqual(self.client.post(url, {'reason': ''}, format='json').status_code, 400)
        r = self.client.post(url, {'reason': 'Customer not home', 'note': 'Gate locked, no answer'}, format='json')
        self.assertEqual(r.status_code, 200)
        n = Notification.objects.get(user=self.office, kind='job_problem')
        self.assertIn('Customer not home', n.message)
        self.assertIn('Gate locked', n.message)
        self.assertTrue(n.link.endswith(f'open={self.booking.id}'))
        self.assertTrue(ProjectHistoryLog.objects.filter(remarks__contains='Customer not home').exists())

    def test_what_to_bring_counts_what_is_placed(self):
        self._as(self.crew)
        [job] = self.client.get('/api/bookings/').data
        self.assertEqual([(m['name'], m['quantity']) for m in job['materials']], [('Palm', 2), ('Fern', 1), ('Bench', 1)])


class InventoryStockTests(APITestCase):
    """Stock moves only through recorded adjustments; archived items leave the shop but keep their history."""

    def setUp(self):
        from apps.library.models import InventoryItem, StaffProfile
        self.office = User.objects.create_user(username='office', password='pw', is_staff=True)
        StaffProfile.objects.create(user=self.office, role='OFFICE_ADMIN')
        self.customer = User.objects.create_user(username='cust', password='pw')
        self.item = InventoryItem.objects.create(name='Fern', category='plant', unit_price=100, stock_quantity=10, reorder_level=5)

    def _as(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')

    def _adjust(self, reason, quantity, note=''):
        return self.client.post(f'/api/inventory/{self.item.id}/adjust_stock/', {'reason': reason, 'quantity': quantity, 'note': note}, format='json')

    def test_restock_damage_and_count_are_recorded(self):
        self._as(self.office)
        r = self._adjust('restock', 6, 'Supplier delivery')
        self.assertEqual((r.status_code, r.data['item']['stock_quantity'], r.data['movement']['change']), (200, 16, 6))
        self.assertIn('more than', self._adjust('damaged', 99).data['error'].replace('at most', 'more than'))
        self.assertEqual(self._adjust('damaged', 4, 'Dead in transit').data['item']['stock_quantity'], 12)
        r = self._adjust('count', 3)
        self.assertEqual((r.data['movement']['change'], r.data['item']['stock_quantity'], r.data['item']['is_low_stock']), (-9, 3, True))
        self.assertIn('Nothing to change', self._adjust('count', 3).data['error'])
        self.assertEqual(self._adjust('sale', 1).status_code, 400)                    # orders record sales themselves
        history = self.client.get(f'/api/inventory/{self.item.id}/stock_history/').data
        self.assertEqual([(h['reason'], h['change'], h['quantity_after']) for h in history],
                         [('count', -9, 3), ('damaged', -4, 12), ('restock', 6, 16)])
        self.assertEqual(history[1]['note'], 'Dead in transit')

    def test_orders_record_sales_and_returns(self):
        from apps.library.models import Order, StockMovement
        self._as(self.customer)
        r = self.client.post('/api/checkout/', {
            'customer_name': 'Cust', 'customer_email': 'c@example.com', 'address': QUEZON_ADDRESS,
            'customer_phone': '09171234567', 'payment_method': 'cod', 'items': [{'id': self.item.id, 'quantity': 3}]}, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        sale = StockMovement.objects.get(item=self.item, reason='sale')
        self.assertEqual((sale.change, sale.quantity_after, sale.note), (-3, 7, f"Order #{r.data['order_id']}"))
        self._as(self.office)
        self.client.patch(f"/api/orders/{r.data['order_id']}/update_status/", {'status': 'Cancelled'}, format='json')
        back = StockMovement.objects.get(item=self.item, reason='released')
        self.assertEqual((back.change, back.quantity_after), (3, 10))
        self.assertEqual(Order.objects.get(pk=r.data['order_id']).status, 'Cancelled')

    def test_archive_instead_of_delete(self):
        from apps.library.models import InventoryItem
        self._as(self.office)
        self.assertEqual(self.client.delete(f'/api/inventory/{self.item.id}/').status_code, 200)
        self.assertFalse(InventoryItem.objects.get(pk=self.item.id).is_active)      # still there
        self.client.credentials()
        self.assertEqual(self.client.get('/api/inventory/').data, [])               # gone from the shop / Studio
        self._as(self.customer)
        r = self.client.post('/api/checkout/', {
            'customer_name': 'Cust', 'customer_email': 'c@example.com', 'address': QUEZON_ADDRESS,
            'customer_phone': '09171234567', 'payment_method': 'cod', 'items': [{'id': self.item.id, 'quantity': 1}]}, format='json')
        self.assertIn('no longer sold', r.data['error'])
        self._as(self.office)
        self.assertEqual(len(self.client.get('/api/inventory/?archived=1').data), 1)
        self.client.post(f'/api/inventory/{self.item.id}/restore/')
        self.assertTrue(InventoryItem.objects.get(pk=self.item.id).is_active)

    def test_customers_cannot_change_stock(self):
        self._as(self.customer)
        self.assertEqual(self._adjust('restock', 5).status_code, 403)
        self.assertEqual(self.client.get('/api/inventory/?archived=1').status_code, 200)   # just the public list


class BusinessOverviewTests(APITestCase):
    """Owner overview: today's numbers and the attention list (same rules as the staff queues)."""

    def setUp(self):
        from django.utils import timezone
        from apps.library.models import (
            Attendance, InventoryItem, Order, ProjectTracker, ServiceBooking, StaffProfile,
        )
        self.owner = User.objects.create_superuser(username='owner', password='pw', email='o@example.com')
        self.crew = User.objects.create_user(username='crew', password='pw', is_staff=True)
        StaffProfile.objects.create(user=self.crew, role='FIELD_CREW')
        self.customer = User.objects.create_user(username='cust', password='pw')
        now = timezone.now()
        Order.objects.create(user=self.customer, customer_name='A', customer_email='a@x.com', total_price=1000,
                             payment_method='cod', status='Pending')                                  # to pack
        Order.objects.create(user=self.customer, customer_name='B', customer_email='b@x.com', total_price=2500,
                             payment_method='online', payment_status='Paid', status='Paid', paid_at=now)   # to pack, paid today
        Order.objects.create(user=self.customer, customer_name='C', customer_email='c@x.com', total_price=700,
                             payment_method='cod', status='Delivered', payment_status='Paid', delivered_at=now)   # cash today
        ServiceBooking.objects.create(user=self.customer, service_type='Softscape', status='Pending',
                                      scheduled_date=timezone.localdate())                            # to confirm, today
        done = ServiceBooking.objects.create(user=self.customer, service_type='Softscape', status='Finished', scheduled_date='2026-10-01')
        ProjectTracker.objects.create(booking=done, total_price=40000, remaining_balance=15000)        # unpaid balance
        InventoryItem.objects.create(name='Fern', category='plant', unit_price=50, stock_quantity=3, reorder_level=5)
        InventoryItem.objects.create(name='Palm', category='plant', unit_price=500, stock_quantity=3, reorder_level=5, is_active=False)
        Attendance.objects.create(staff=self.crew, clock_in_time=now)

    def _as(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')

    def test_today_and_attention(self):
        self._as(self.owner)
        r = self.client.get('/api/admin/overview/')
        self.assertEqual(r.status_code, 200)
        t = r.data['today']
        self.assertEqual((t['orders'], t['orders_value'], t['visits'], t['crew_on_shift']), (3, 4200.0, 1, 1))
        self.assertEqual(t['money_received'], 3200.0)                         # ₱2,500 online + ₱700 cash
        attention = {a['key']: a for a in r.data['attention']}
        self.assertEqual(attention['to_pack']['count'], 2)
        self.assertEqual(attention['to_pack']['link'], '/staff-dashboard?tab=orders&queue=to_pack')
        self.assertEqual(attention['awaiting']['count'], 1)
        self.assertEqual(attention['low_stock']['count'], 1)                  # archived items don't count
        self.assertIn('15,000', attention['unpaid']['hint'])
        self.assertNotIn('needs_refund', attention)                          # empty queues are left out

    def test_office_only(self):
        self._as(self.crew)
        self.assertEqual(self.client.get('/api/admin/overview/').status_code, 403)
        self._as(self.customer)
        self.assertEqual(self.client.get('/api/admin/overview/').status_code, 403)

    def test_owner_notifications_open_the_item(self):
        from apps.library import notifications
        from apps.library.models import Notification, ServiceBooking
        b = ServiceBooking.objects.filter(status='Pending').first()
        notifications.booking_new(b)
        self.assertEqual(Notification.objects.get(user=self.owner).link, f'/staff-dashboard?tab=bookings&queue=all&open={b.id}')


class MoneyControlBase(APITestCase):
    def setUp(self):
        from apps.library.models import StaffProfile
        self.owner = User.objects.create_superuser(username='owner', password='pw', email='o@example.com')
        self.office = User.objects.create_user(username='office', password='pw', is_staff=True)
        StaffProfile.objects.create(user=self.office, role='OFFICE_ADMIN')
        self.crew = User.objects.create_user(username='crew', password='pw', is_staff=True, first_name='Eddie')
        StaffProfile.objects.create(user=self.crew, role='FIELD_CREW')
        self.customer = User.objects.create_user(username='cust', password='pw')

    def _as(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')

    def _order(self, total, method='cod', delivered_by=None, **extra):
        from django.utils import timezone
        from apps.library.models import Order
        return Order.objects.create(user=self.customer, customer_name='Ana', customer_email='a@x.com', total_price=total,
                                    payment_method=method, status='Delivered', payment_status='Paid',
                                    delivered_by=delivered_by, delivered_at=timezone.now(), **extra)


class CashHandoverTests(MoneyControlBase):
    """COD cash: with the crew until the office counts it in."""

    def setUp(self):
        super().setUp()
        self.a = self._order(700, delivered_by=self.crew)
        self.b = self._order(1300, delivered_by=self.crew)
        self._order(900, method='online', delivered_by=self.crew)   # paid online: no cash
        self._order(400, delivered_by=self.office)                   # the office delivered it: no handover

    def test_cash_with_crew_and_exact_handover(self):
        from apps.library.models import ActivityLog, Notification
        self._as(self.office)
        r = self.client.get('/api/cash/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.data['crew']), 1)
        row = r.data['crew'][0]
        self.assertEqual((row['crew_id'], row['total'], len(row['orders'])), (self.crew.id, '2000.00', 2))

        self._as(self.crew)
        mine = self.client.get('/api/orders/my_deliveries/').data
        self.assertEqual((mine['cash_to_hand_in'], mine['cash_orders']), ('2000.00', 2))

        self._as(self.office)
        r = self.client.post('/api/cash/receive/', {'crew': self.crew.id, 'orders': [self.a.id, self.b.id], 'counted': '2000'},
                             format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data['difference'], '0.00')
        self.a.refresh_from_db()
        self.assertIsNotNone(self.a.cash_handover_id)
        self.assertEqual(self.client.get('/api/cash/').data['crew'], [])
        self.assertTrue(Notification.objects.filter(user=self.crew, kind='cash_received').exists())
        self.assertTrue(ActivityLog.objects.filter(action='cash_received', actor=self.office).exists())
        self._as(self.owner)
        keys = [a['key'] for a in self.client.get('/api/admin/overview/').data['attention']]
        self.assertNotIn('cash', keys)

    def test_short_count_needs_a_note_and_is_kept(self):
        self._as(self.office)
        body = {'crew': self.crew.id, 'orders': [self.a.id, self.b.id], 'counted': '1900'}
        r = self.client.post('/api/cash/receive/', body, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('note', r.data['error'])
        r = self.client.post('/api/cash/receive/', {**body, 'note': 'Customer paid ₱100 less, will settle'}, format='json')
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.data['difference'], '-100.00')

    def test_same_cash_cannot_be_received_twice(self):
        self._as(self.office)
        body = {'crew': self.crew.id, 'orders': [self.a.id], 'counted': '700'}
        self.assertEqual(self.client.post('/api/cash/receive/', body, format='json').status_code, 201)
        self.assertEqual(self.client.post('/api/cash/receive/', body, format='json').status_code, 400)

    def test_overview_shows_cash_and_crew_cannot_record(self):
        self._as(self.owner)
        cash = {a['key']: a for a in self.client.get('/api/admin/overview/').data['attention']}['cash']
        self.assertEqual(cash['count'], 2)
        self.assertIn('2,000', cash['hint'])
        self._as(self.crew)
        self.assertEqual(self.client.get('/api/cash/').status_code, 403)
        self.assertEqual(self.client.post('/api/cash/receive/', {'crew': self.crew.id, 'orders': [self.a.id], 'counted': 700},
                                          format='json').status_code, 403)


class BusinessProfileTests(MoneyControlBase):
    def test_public_read_owner_write(self):
        from apps.library.models import ActivityLog
        r = self.client.get('/api/business/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['name'], 'Garden Studio')
        self.assertNotIn('updated_by', r.data)

        self._as(self.office)
        self.assertEqual(self.client.patch('/api/business/', {'phone': '+63 912 345 6789'}, format='json').status_code, 403)

        self._as(self.owner)
        r = self.client.patch('/api/business/', {'phone': '+63 912 345 6789', 'facebook': 'https://www.facebook.com/gardenstudio',
                                                 'founded_year': '2019'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual((r.data['phone'], r.data['founded_year'], r.data['social']['facebook']),
                         ('+63 912 345 6789', 2019, 'https://www.facebook.com/gardenstudio'))
        self.assertTrue(ActivityLog.objects.filter(action='business_profile_changed').exists())

    def test_bad_values_are_refused(self):
        self._as(self.owner)
        r = self.client.patch('/api/business/', {'email': 'not-an-email', 'instagram': 'https://evil.example.com/x',
                                                 'name': '', 'founded_year': '1800'}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertEqual(set(r.data), {'email', 'instagram', 'name', 'founded_year'})

    def test_emails_use_the_business_name(self):
        from apps.library import notifications
        from apps.library.models import BusinessProfile
        bp = BusinessProfile.current()
        bp.name = 'Halaman Gardens'
        bp.save()
        self.assertEqual(notifications.business_name(), 'Halaman Gardens')


class ActivityLogTests(MoneyControlBase):
    def test_owner_cannot_lock_themselves_out(self):
        self._as(self.owner)
        me = f'/api/users/{self.owner.id}/'
        for body in ({'is_active': False}, {'role': 'OFFICE_ADMIN'}, {'input_role': 'customer'}):
            r = self.client.patch(me, body, format='json')
            self.assertEqual(r.status_code, 400, body)
            self.assertIn('your own account', str(r.data))
        self.assertEqual(self.client.delete(me).status_code, 400)
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.is_active and self.owner.is_superuser)
        # Other accounts are managed as before
        self.assertEqual(self.client.patch(f'/api/users/{self.crew.id}/', {'is_active': False}, format='json').status_code, 200)

    def test_prices_roles_and_refunds_are_logged(self):
        from apps.library.models import ActivityLog, InventoryItem, Order
        item = InventoryItem.objects.create(name='Fern', category='plant', unit_price=50, stock_quantity=3)
        self._as(self.office)
        self.assertEqual(self.client.patch(f'/api/inventory/{item.id}/', {'unit_price': '65'}, format='json').status_code, 200)
        order = Order.objects.create(user=self.customer, customer_name='Ana', customer_email='a@x.com', total_price=900,
                                     payment_method='online', status='Paid', payment_status='Paid')
        self.assertEqual(self.client.patch(f'/api/orders/{order.id}/update_status/', {'status': 'Cancelled'},
                                           format='json').status_code, 200)
        r = self.client.patch(f'/api/orders/{order.id}/update_status/', {'payment_status': 'Refunded'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self._as(self.owner)
        self.assertEqual(self.client.patch(f'/api/users/{self.crew.id}/', {'role': 'OFFICE_ADMIN'}, format='json').status_code, 200)

        actions = list(ActivityLog.objects.values_list('action', flat=True))
        for a in ('price_changed', 'order_cancelled', 'order_refunded', 'role_changed'):
            self.assertIn(a, actions)
        price = ActivityLog.objects.get(action='price_changed')
        self.assertEqual(price.actor, self.office)
        self.assertIn('₱50.00 → ₱65.00', price.summary)
        self.assertIn('Field crew to Office admin', ActivityLog.objects.get(action='role_changed').summary)

        r = self.client.get('/api/admin/activity/?area=stock')
        self.assertEqual(r.status_code, 200)
        self.assertEqual([x['action'] for x in r.data['results']], ['price_changed'])
        self.assertEqual(r.data['results'][0]['actor'], 'office')

    def test_owner_only(self):
        self._as(self.office)
        self.assertEqual(self.client.get('/api/admin/activity/').status_code, 403)


class ReportsTests(MoneyControlBase):
    """Owner reports: money actually received, balances owed, crew output."""

    def setUp(self):
        super().setUp()
        from datetime import timedelta
        from django.utils import timezone
        from apps.library.models import Attendance, ProjectTracker, ServiceBooking
        now = timezone.now()
        self.today = timezone.localdate()
        self._order(1000, method='online', paid_at=now)                         # shop online
        self._order(700, delivered_by=self.crew)                                 # shop cash (crew delivered)
        old = self.today - timedelta(days=45)
        self.job = ServiceBooking.objects.create(user=self.customer, service_type='Softscape', status='Finished', scheduled_date=old)
        self.job.assigned_crew.add(self.crew)
        self.tracker = ProjectTracker.objects.create(booking=self.job, total_price=40000, remaining_balance=40000)
        open_job = ServiceBooking.objects.create(user=self.customer, service_type='Softscape', status='Installing',
                                                 scheduled_date=self.today)
        open_job.assigned_crew.add(self.crew)
        ProjectTracker.objects.create(booking=open_job, total_price=9000, remaining_balance=9000)
        Attendance.objects.create(staff=self.crew, clock_in_time=now - timedelta(hours=8), clock_out_time=now)

    def test_office_payment_needs_a_method_and_becomes_a_record(self):
        from apps.library.models import ProjectPayment
        self._as(self.office)
        url = f'/api/projects/{self.tracker.id}/record-payment/'
        self.assertEqual(self.client.post(url, {'amount': '5000', 'method': 'online'}, format='json').status_code, 400)
        self.assertEqual(self.client.post(url, {'amount': '99999', 'method': 'bank'}, format='json').status_code, 400)
        r = self.client.post(url, {'amount': '15000', 'method': 'bank', 'remarks': 'BPI ref 123'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        p = ProjectPayment.objects.get()
        self.assertEqual((p.amount, p.method, p.received_by, p.note), (15000, 'bank', self.office, 'BPI ref 123'))
        self.tracker.refresh_from_db()
        self.assertEqual(self.tracker.remaining_balance, 25000)

    def test_online_payment_request_becomes_a_record(self):
        import secrets
        from apps.library import paymongo
        from apps.library.models import ProjectPayment, ProjectPaymentRequest
        req = ProjectPaymentRequest.objects.create(project=self.tracker, label='Downpayment', amount=10000,
                                                   token=secrets.token_urlsafe(32))
        paymongo.mark_project_request_paid(req, {'id': 'pay_1', 'attributes': {'amount': 1000000}})
        paymongo.mark_project_request_paid(req, {'id': 'pay_1', 'attributes': {'amount': 1000000}})  # webhook retry
        p = ProjectPayment.objects.get()
        self.assertEqual((p.method, p.amount, p.request_id), ('online', 10000, req.id))

    def test_money_in_by_channel_with_refunds(self):
        from apps.library.models import Order
        self._as(self.office)
        self.client.post(f'/api/projects/{self.tracker.id}/record-payment/', {'amount': '5000', 'method': 'cash'}, format='json')
        refunded = Order.objects.create(user=self.customer, customer_name='B', customer_email='b@x.com', total_price=300,
                                        payment_method='online', status='Cancelled', payment_status='Paid')
        self.client.patch(f'/api/orders/{refunded.id}/update_status/', {'payment_status': 'Refunded'}, format='json')
        refunded.refresh_from_db()
        self.assertIsNotNone(refunded.refunded_at)

        self.assertEqual(self.client.get('/api/reports/money/').status_code, 403)   # owner only
        self._as(self.owner)
        r = self.client.get('/api/reports/money/')
        self.assertEqual(r.status_code, 200)
        ch = {c['key']: c for c in r.data['channels']}
        self.assertEqual((ch['shop_online']['amount'], ch['shop_cash']['amount'], ch['project_office']['amount']), (1000, 700, 5000))
        self.assertEqual((r.data['received'], r.data['refunds']['amount'], r.data['net']), (6700, 300, 6400))
        self.assertEqual(r.data['days'][-1]['total'], 6700)
        self.assertEqual(r.data['office_methods'][0]['label'], 'Cash')
        # Overview "money received today" now includes office-recorded project payments
        self.assertEqual(self.client.get('/api/admin/overview/').data['today']['money_received'], 6700.0)

        csv_r = self.client.get('/api/reports/money/?export=csv')
        self.assertEqual(csv_r['Content-Type'], 'text/csv; charset=utf-8')
        body = csv_r.content.decode('utf-8-sig')
        self.assertIn('Projects · recorded by the office', body)
        self.assertIn('-300.00', body)

    def test_period_is_checked(self):
        self._as(self.owner)
        self.assertEqual(self.client.get('/api/reports/money/?from=2026-10-31&to=2026-10-01').status_code, 400)
        self.assertEqual(self.client.get('/api/reports/money/?from=2024-01-01&to=2026-10-01').status_code, 400)
        self.assertEqual(self.client.get('/api/reports/money/?from=oops').status_code, 400)

    def test_balances_are_aged(self):
        self._as(self.owner)
        r = self.client.get('/api/reports/balances/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.data['owed_total'], r.data['owed_count'], r.data['open_total']), (40000, 1, 9000))
        aging = {a['label']: a['amount'] for a in r.data['aging']}
        self.assertEqual(aging['31–60 days'], 40000)
        self.assertEqual(r.data['owed'][0]['days_since_job'], 45)
        self.assertEqual(r.data['cash_with_crew']['amount'], 700)
        self.assertIn(f'#{self.job.id}', self.client.get('/api/reports/balances/?export=csv').content.decode('utf-8-sig'))

    def test_crew_output(self):
        from datetime import timedelta
        self._as(self.owner)
        start = (self.today - timedelta(days=60)).isoformat()
        r = self.client.get(f'/api/reports/crew/?from={start}&to={self.today.isoformat()}')
        self.assertEqual(r.status_code, 200)
        row = r.data['crew'][0]
        self.assertEqual((row['name'], row['jobs_finished'], row['jobs_value'], row['jobs_open'], row['deliveries'],
                          row['cash_collected'], row['days_worked']), ('Eddie', 1, 40000, 1, 1, 700, 1))
        self.assertAlmostEqual(row['hours'], 8.0, places=1)


class SecurityTests(MoneyControlBase):
    """Pre-deploy hardening: password reset by emailed link, rate limits, uploads, crew data scope."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()  # rate-limit counters
        super().setUp()
        self.customer.email = 'cust@example.com'
        self.customer.save()

    def test_old_username_email_reset_is_gone(self):
        r = self.client.post('/api/reset-password/', {'username': 'owner', 'email': 'o@example.com', 'new_password': 'hijacked-123'})
        self.assertEqual(r.status_code, 404)
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.check_password('pw'))

    def test_reset_by_emailed_link_once(self):
        import re
        from django.core import mail
        r = self.client.post('/api/password-reset/', {'email': 'CUST@example.com'})
        self.assertEqual(r.status_code, 200)
        same = self.client.post('/api/password-reset/', {'email': 'nobody@example.com'})
        self.assertEqual(same.data['message'], r.data['message'])           # no account enumeration
        self.assertEqual(len(mail.outbox), 1)
        uid, token = re.search(r'reset-password\?uid=([\w-]+)&token=([\w-]+)', mail.outbox[0].body).groups()

        weak = self.client.post('/api/password-reset/confirm/', {'uid': uid, 'token': token, 'new_password': '12345678'})
        self.assertEqual(weak.status_code, 400)
        ok = self.client.post('/api/password-reset/confirm/', {'uid': uid, 'token': token, 'new_password': 'Ferns&Pebbles-2026'})
        self.assertEqual(ok.status_code, 200, ok.data)
        self.customer.refresh_from_db()
        self.assertTrue(self.customer.check_password('Ferns&Pebbles-2026'))
        again = self.client.post('/api/password-reset/confirm/', {'uid': uid, 'token': token, 'new_password': 'Another-pass-99'})
        self.assertEqual(again.status_code, 400)                              # single use
        forged = self.client.post('/api/password-reset/confirm/', {'uid': uid, 'token': 'abc-123', 'new_password': 'Another-pass-99'})
        self.assertEqual(forged.status_code, 400)

    def test_login_is_rate_limited(self):
        codes = [self.client.post('/api/token/', {'username': 'owner', 'password': f'guess-{i}'}).status_code for i in range(12)]
        self.assertEqual(codes[0], 401)
        self.assertEqual(codes[-1], 429)

    def test_sign_up_needs_a_real_password(self):
        weak = self.client.post('/api/register/', {'username': 'newbie', 'email': 'n@example.com', 'password': '1234',
                                                   'accept_privacy': True})
        self.assertEqual(weak.status_code, 400)
        self.assertIn('password', weak.data)
        ok = self.client.post('/api/register/', {'username': 'newbie', 'email': 'n@example.com', 'password': 'Garden-Path-77',
                                                 'accept_privacy': True})
        self.assertEqual(ok.status_code, 201)
        self.assertFalse(User.objects.get(username='newbie').is_staff)

    def test_sign_up_records_privacy_consent(self):
        from apps.library.models import PrivacyConsent
        from apps.library.serializers import PRIVACY_POLICY_VERSION
        form = {'username': 'maria', 'email': 'm@example.com', 'password': 'Garden-Path-77'}
        for refused in ({}, {'accept_privacy': False}):
            r = self.client.post('/api/register/', {**form, **refused})
            self.assertEqual(r.status_code, 400)
            self.assertIn('accept_privacy', r.data)
        self.assertFalse(User.objects.filter(username='maria').exists())       # no account without consent
        self.assertEqual(self.client.post('/api/register/', {**form, 'accept_privacy': True}).status_code, 201)
        consent = PrivacyConsent.objects.get(user__username='maria')
        self.assertEqual(consent.policy_version, PRIVACY_POLICY_VERSION)

    def test_design_image_upload_only_takes_images(self):
        import io
        from django.core.files.uploadedfile import SimpleUploadedFile
        from PIL import Image
        self._as(self.customer)
        html = SimpleUploadedFile('evil.html', b'<script>alert(1)</script>', content_type='text/html')
        self.assertEqual(self.client.post('/api/upload-design-image/', {'image': html}).status_code, 400)
        buf = io.BytesIO()
        Image.new('RGB', (40, 30), 'green').save(buf, 'PNG')
        with self.settings(MEDIA_ROOT=__import__('tempfile').mkdtemp()):
            r = self.client.post('/api/upload-design-image/', {'image': SimpleUploadedFile('yard.png', buf.getvalue(), content_type='image/png')})
        self.assertEqual(r.status_code, 201)
        self.assertTrue(r.data['url'].endswith('.jpg'))

    def test_crew_only_see_their_own_jobs_projects_and_designs(self):
        from apps.library.models import GardenDesign, ProjectTracker, ServiceBooking
        mine_design = GardenDesign.objects.create(user=self.customer, name='Mine')
        other_design = GardenDesign.objects.create(user=self.customer, name='Other')
        mine = ServiceBooking.objects.create(user=self.customer, service_type='Softscape', status='Installing',
                                             scheduled_date='2026-10-22', design=mine_design)
        mine.assigned_crew.add(self.crew)
        other = ServiceBooking.objects.create(user=self.customer, service_type='Softscape', status='Installing',
                                              scheduled_date='2026-10-22', design=other_design)
        t1 = ProjectTracker.objects.create(booking=mine, total_price=1000, remaining_balance=1000)
        ProjectTracker.objects.create(booking=other, total_price=9000, remaining_balance=9000)
        self._as(self.crew)
        projects = self.client.get('/api/projects/').data
        ids = [p['id'] for p in (projects['results'] if isinstance(projects, dict) else projects)]
        self.assertEqual(ids, [t1.id])
        self.assertEqual(self.client.get(f'/api/designs/{mine_design.id}/').status_code, 200)
        self.assertEqual(self.client.get(f'/api/designs/{other_design.id}/').status_code, 404)
        self._as(self.office)
        projects = self.client.get('/api/projects/').data
        self.assertEqual(len(projects['results'] if isinstance(projects, dict) else projects), 2)

    def test_owner_can_add_a_user_without_a_password(self):
        self._as(self.owner)
        r = self.client.post('/api/users/', {'username': 'newcrew', 'email': 'c@example.com', 'role': 'FIELD_CREW'}, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertTrue(User.objects.get(username='newcrew').has_usable_password())

    def test_production_settings_refuse_unsafe_values(self):
        import importlib
        import os
        import sys
        from unittest import mock
        from django.core.exceptions import ImproperlyConfigured

        def load_prod(env):
            # Fresh copies of base + prod under these variables; the running settings are untouched
            with mock.patch.dict(os.environ, env):
                for name in ('config.settings.prod', 'config.settings.base'):
                    sys.modules.pop(name, None)
                try:
                    return importlib.import_module('config.settings.prod')
                finally:
                    for name in ('config.settings.prod', 'config.settings.base'):
                        sys.modules.pop(name, None)

        with self.assertRaises(ImproperlyConfigured):
            load_prod({'DJANGO_SECRET_KEY': '', 'ALLOWED_HOSTS': 'x.example.com'})
        with self.assertRaises(ImproperlyConfigured):
            load_prod({'DJANGO_SECRET_KEY': 'k' * 60, 'ALLOWED_HOSTS': ''})
        prod = load_prod({'DJANGO_SECRET_KEY': 'k' * 60, 'ALLOWED_HOSTS': 'x.example.com',
                          'FRONTEND_URL': 'https://shop.example.com', 'DJANGO_DEBUG': ''})
        self.assertFalse(prod.DEBUG)
        self.assertFalse(prod.CORS_ALLOW_ALL_ORIGINS)
        self.assertEqual(prod.CORS_ALLOWED_ORIGINS, ['https://shop.example.com'])
        self.assertEqual(prod.ALLOWED_HOSTS, ['x.example.com'])


class ModelSizeTests(MoneyControlBase):
    """real_world_size: metres at established size (height for plants/furniture, longest side for hardscape)."""

    def test_size_is_editable_and_checked(self):
        from apps.library.models import InventoryItem
        item = InventoryItem.objects.create(name='Heliconia', category='plant', unit_price=400, stock_quantity=5)
        self._as(self.office)
        url = f'/api/inventory/{item.id}/'
        self.assertEqual(self.client.patch(url, {'real_world_size': 0}, format='json').status_code, 400)
        self.assertEqual(self.client.patch(url, {'real_world_size': 80}, format='json').status_code, 400)
        r = self.client.patch(url, {'real_world_size': '1.8'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        item.refresh_from_db()
        self.assertEqual(item.real_world_size, 1.8)
        # The backend reads it as plant height: 1.8 m is a mid-layer plant
        from apps.library.pricing import layer_of
        self.assertEqual(layer_of(item), 'mid')


class OfflineCrewActionTests(MoneyControlBase):
    """Crew actions sent late from a phone that was offline: real times, and never done twice."""

    def setUp(self):
        super().setUp()
        from django.utils import timezone
        from apps.library.models import ServiceBooking
        self.job = ServiceBooking.objects.create(user=self.customer, service_type='Softscape', status='Preparing',
                                                 scheduled_date=timezone.localdate())
        self.job.assigned_crew.add(self.crew)

    def _post(self, url, body=None, key=None, at=None):
        extra = {}
        if key:
            extra['HTTP_IDEMPOTENCY_KEY'] = key
        if at:
            extra['HTTP_X_ACTION_TIME'] = at.isoformat()
        return self.client.post(url, body or {}, format='json', **extra)

    def test_clock_in_and_out_keep_the_real_times(self):
        from datetime import timedelta
        from django.utils import timezone
        from apps.library.models import Attendance
        self._as(self.crew)
        started = timezone.now() - timedelta(minutes=40)
        r = self._post('/api/attendance/clock_in/', {'booking_id': self.job.id}, key='k' * 20, at=started)
        self.assertEqual(r.status_code, 201, r.data)
        a = Attendance.objects.get()
        self.assertLess(abs((a.clock_in_time - started).total_seconds()), 1)
        r = self._post('/api/attendance/clock_out/', {}, key='o' * 20, at=started + timedelta(minutes=30))
        self.assertEqual(r.status_code, 200, r.data)
        a.refresh_from_db()
        self.assertEqual(round(a.total_hours, 2), 0.5)

    def test_unbelievable_times_fall_back_to_now(self):
        from datetime import timedelta
        from django.utils import timezone
        from apps.library.models import Attendance
        self._as(self.crew)
        self._post('/api/attendance/clock_in/', {'booking_id': self.job.id}, at=timezone.now() + timedelta(hours=3))
        self.assertLess(abs((Attendance.objects.get().clock_in_time - timezone.now()).total_seconds()), 5)
        Attendance.objects.all().delete()
        self._post('/api/attendance/clock_in/', {'booking_id': self.job.id}, at=timezone.now() - timedelta(days=3))
        self.assertLess(abs((Attendance.objects.get().clock_in_time - timezone.now()).total_seconds()), 5)

    def test_same_action_sent_twice_is_done_once(self):
        from apps.library.models import Attendance
        self._as(self.crew)
        first = self._post('/api/attendance/clock_in/', {'booking_id': self.job.id}, key='retry-key-123456')
        again = self._post('/api/attendance/clock_in/', {'booking_id': self.job.id}, key='retry-key-123456')
        self.assertEqual((first.status_code, again.status_code), (201, 201))       # not "already clocked in"
        self.assertEqual(again['Idempotent-Replayed'], 'true')
        self.assertEqual(again.json()['id'], first.json()['id'])
        self.assertEqual(Attendance.objects.count(), 1)
        # Without a key the normal rule applies
        self.assertEqual(self._post('/api/attendance/clock_in/', {'booking_id': self.job.id}).status_code, 400)
        # Someone else can't replay this crew member's answer
        self._as(self.office)
        self.assertEqual(self._post('/api/attendance/clock_in/', {'booking_id': self.job.id}, key='retry-key-123456').status_code, 422)

    def test_delivery_marked_offline_keeps_its_time_and_problem_says_when(self):
        from datetime import timedelta
        from django.utils import timezone
        from apps.library.models import Notification, Order
        order = Order.objects.create(user=self.customer, customer_name='Ana', customer_email='a@x.com', total_price=800,
                                     payment_method='cod', status='Out for Delivery', delivery_crew=self.crew)
        self._as(self.crew)
        handed = timezone.now() - timedelta(hours=2)
        r = self.client.patch(f'/api/orders/{order.id}/update_status/', {'status': 'Delivered'}, format='json',
                              HTTP_IDEMPOTENCY_KEY='deliver-key-12345', HTTP_X_ACTION_TIME=handed.isoformat())
        self.assertEqual(r.status_code, 200, r.data)
        order.refresh_from_db()
        self.assertLess(abs((order.delivered_at - handed).total_seconds()), 1)
        self._post(f'/api/bookings/{self.job.id}/report_problem/', {'reason': 'Customer not home'},
                   key='problem-key-12345', at=timezone.now() - timedelta(minutes=50))
        self.assertTrue(Notification.objects.filter(kind='job_problem', message__contains='sent when the signal came back').exists())

    def test_stage_photo_taken_offline_counts_after_clock_out(self):
        from datetime import timedelta
        from django.utils import timezone
        from apps.library.models import Attendance, ProjectMilestone, ProjectProgressMedia
        self._as(self.crew)
        start = timezone.now() - timedelta(hours=2)
        Attendance.objects.create(staff=self.crew, booking=self.job, clock_in_time=start,
                                  clock_out_time=start + timedelta(hours=1))
        taken = start + timedelta(minutes=45)
        with self.settings(MEDIA_ROOT=TEMP_MEDIA):
            def upload():
                return self.client.post(f'/api/bookings/{self.job.id}/progress-media/',
                                        {'construction_phase': 'DURING', 'file': _photo_with_gps()}, format='multipart',
                                        HTTP_IDEMPOTENCY_KEY='photo-key-123456', HTTP_X_ACTION_TIME=taken.isoformat())
            first, again = upload(), upload()
        self.assertEqual((first.status_code, again.status_code), (201, 201), first.data)
        self.assertEqual(ProjectProgressMedia.objects.count(), 1)              # the resend didn't upload it twice

        def finish(at, key):
            return self.client.patch(f'/api/bookings/{self.job.id}/update_milestone/',
                                     {'phase': 'site_prep', 'completion_pct': 100, 'proof_photo_url': first.json()['file_url']},
                                     format='json', HTTP_IDEMPOTENCY_KEY=key, HTTP_X_ACTION_TIME=at.isoformat())
        # Taken after the shift ended (or with no time at all): not on the job
        self.assertEqual(finish(start + timedelta(minutes=90), 'stage-key-late-1').status_code, 400)
        self.assertEqual(self.client.patch(f'/api/bookings/{self.job.id}/update_milestone/',
                                           {'phase': 'site_prep', 'completion_pct': 100, 'proof_photo_url': 'x'},
                                           format='json').status_code, 400)
        r = finish(taken, 'stage-key-123456')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(ProjectMilestone.objects.get(booking=self.job, phase='site_prep').completion_pct, 100)

    def test_attendance_list_by_period(self):
        from datetime import timedelta
        from django.utils import timezone
        from apps.library.models import Attendance
        now = timezone.now()
        old = Attendance.objects.create(staff=self.crew, clock_in_time=now - timedelta(days=20), clock_out_time=now - timedelta(days=20, hours=-8))
        recent = Attendance.objects.create(staff=self.crew, clock_in_time=now - timedelta(hours=3))
        other = Attendance.objects.create(staff=self.office, clock_in_time=now - timedelta(hours=2))
        week_ago = str(timezone.localdate() - timedelta(days=7))
        self._as(self.owner)
        self.assertEqual({a['id'] for a in self.client.get('/api/attendance/').data}, {old.id, recent.id, other.id})
        r = self.client.get('/api/attendance/', {'from': week_ago, 'to': str(timezone.localdate())})
        self.assertEqual({a['id'] for a in r.data}, {recent.id, other.id})
        self.assertEqual(self.client.get('/api/attendance/', {'from': 'last week'}).status_code, 400)
        self._as(self.crew)   # crew still see only their own shifts
        self.assertEqual({a['id'] for a in self.client.get('/api/attendance/', {'from': week_ago}).data}, {recent.id})

    def test_phone_may_send_the_headers_cross_site(self):
        r = self.client.options('/api/attendance/clock_in/', HTTP_ORIGIN='https://shimereyslandscaping.vercel.app',
                                HTTP_ACCESS_CONTROL_REQUEST_METHOD='POST',
                                HTTP_ACCESS_CONTROL_REQUEST_HEADERS='authorization,content-type,idempotency-key,x-action-time')
        allowed = r.get('Access-Control-Allow-Headers', '')
        self.assertIn('idempotency-key', allowed)
        self.assertIn('x-action-time', allowed)


from django.test import TransactionTestCase  # noqa: E402


class BackupTests(TransactionTestCase):
    """manage.py backup_db / daily_maintenance: a compressed, checked copy; only the newest kept.
    (No wrapping transaction: SQLite can't snapshot a database another transaction holds locked.)"""

    def setUp(self):
        from apps.library.models import StaffProfile
        User.objects.create_superuser(username='owner', password='pw', email='o@example.com')
        self.crew = User.objects.create_user(username='crew', password='pw', is_staff=True)
        StaffProfile.objects.create(user=self.crew, role='FIELD_CREW')

    def test_backup_is_a_working_copy_and_old_ones_are_pruned(self):
        from django.db import connection
        if connection.vendor != 'sqlite':
            self.skipTest('backup_db copies the SQLite file; on Postgres the database host keeps backups')
        import gzip
        import sqlite3
        import tempfile
        from pathlib import Path
        from apps.library.backups import backup_database
        dest = Path(tempfile.mkdtemp())
        for day in ('2026-10-01', '2026-10-02', '2026-10-03'):
            (dest / f'db-{day}.sqlite3.gz').write_bytes(b'old')
        path = backup_database(dest, keep=2)
        self.assertTrue(path.name.startswith('db-') and path.name.endswith('.sqlite3.gz'))
        self.assertEqual(sorted(p.name for p in dest.glob('db-*.gz')), sorted([path.name, 'db-2026-10-03.sqlite3.gz']))
        restored = dest / 'restored.sqlite3'
        restored.write_bytes(gzip.decompress(path.read_bytes()))
        db = sqlite3.connect(restored)
        users = db.execute('select username from auth_user order by username').fetchall()
        db.close()
        self.assertIn(('owner',), users)                     # the data really is in the copy

    def test_daily_maintenance_runs_every_step(self):
        import io
        import tempfile
        from datetime import timedelta
        from unittest import mock
        from django.core.management import call_command
        from django.utils import timezone
        from apps.library.models import IdempotentRequest
        old = IdempotentRequest.objects.create(key='old-key-1234567890', user=self.crew, method='POST', path='/api/x/', status_code=200)
        IdempotentRequest.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=5))
        IdempotentRequest.objects.create(key='new-key-1234567890', user=self.crew, method='POST', path='/api/x/', status_code=200)
        out = io.StringIO()
        with mock.patch.dict('os.environ', {'BACKUP_DIR': tempfile.mkdtemp()}):
            call_command('daily_maintenance', stdout=out)
        text = out.getvalue()
        for step in ('Backup:', 'Unpaid orders:', 'Retry keys: 1 removed'):
            self.assertIn(step, text)
        self.assertEqual(IdempotentRequest.objects.count(), 1)


class AddressTests(APITestCase):
    """Quezon-only addresses: PSGC town + barangay, street, landmark, a required map pin; address book."""

    def setUp(self):
        from apps.library.models import InventoryItem
        self.user = User.objects.create_user(username='buyer', password='pw12345!', email='b@example.com')
        self.client.force_authenticate(self.user)
        self.item = InventoryItem.objects.create(name='Fern', category='plant', unit_price=500, stock_quantity=10)

    def _order(self, address, method='cod'):
        body = {'customer_name': 'Buyer', 'customer_email': 'b@example.com', 'customer_phone': '09171234567',
                'payment_method': method, 'items': [{'id': self.item.id, 'quantity': 1}]}
        if address is not None:
            body['address'] = address
        return self.client.post('/api/checkout/', body, format='json')

    def test_areas_are_quezon_only_and_complete(self):
        r = self.client.get('/api/address-areas/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['province'], 'Quezon')
        names = {c['name'] for c in r.data['cities']}
        self.assertEqual(len(r.data['cities']), 41)                       # 39 municipalities + Tayabas + Lucena
        self.assertTrue({'Lucena City', 'Tayabas City', 'Sariaya', 'Polillo'} <= names)
        self.assertEqual(sum(len(c['barangays']) for c in r.data['cities']), 1242)
        self.assertIn('max-age', r['Cache-Control'])

    def test_order_saves_the_pin_and_a_readable_address(self):
        r = self._order(QUEZON_ADDRESS)
        self.assertEqual(r.status_code, 201, r.data)
        from apps.library.models import Order
        o = Order.objects.get()
        self.assertEqual((o.delivery_lat, o.delivery_lng), (13.9358, 121.6129))
        self.assertEqual(o.customer_address,
                         '12 Quezon Ave, Brgy. Barangay 1 (Pob.), Lucena City, Quezon · near beside the chapel')
        self.assertEqual(o.address_detail['barangay_code'], '045624002')

    def test_bad_addresses_are_refused_with_a_reason(self):
        cases = [
            (None, 'pin your location'),                                                  # nothing structured
            ({**QUEZON_ADDRESS, 'city_code': '137404000'}, 'within Quezon'),              # a Metro Manila city
            ({**QUEZON_ADDRESS, 'barangay_code': '045647001'}, 'barangay in Lucena'),     # a Tayabas barangay
            ({**QUEZON_ADDRESS, 'street': ''}, 'house number'),
            ({**QUEZON_ADDRESS, 'lat': None}, 'Drop the pin'),
            ({**QUEZON_ADDRESS, 'lat': 14.5995, 'lng': 120.9842}, 'outside Lucena City'),  # a pin in Manila
        ]
        for address, msg in cases:
            r = self._order(address)
            self.assertEqual(r.status_code, 400, address)
            self.assertIn(msg, r.data['error'], address)
        r = self.client.post('/api/checkout/', {'customer_name': 'B', 'customer_email': 'b@example.com',
                                                'customer_phone': '09171234567', 'customer_address': 'Lucena',
                                                'items': [{'id': self.item.id, 'quantity': 1}]}, format='json')
        self.assertEqual(r.status_code, 400)                                              # free text alone

    def test_booking_site_pin_drives_the_crew_check_in(self):
        r = self.client.post('/api/bookings/', {'service_type': 'maintenance', 'scheduled_date': '2027-02-01',
                                                'address': QUEZON_ADDRESS}, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual((r.data['site_lat'], r.data['site_lng']), (13.9358, 121.6129))
        self.assertIn('Lucena City', r.data['service_address'])
        booking_id = r.data['id']
        # The text and the pin can't be changed apart from each other
        self.client.patch(f'/api/bookings/{booking_id}/', {'service_address': 'Manila', 'site_lat': 1, 'site_lng': 1},
                          format='json')
        again = self.client.get(f'/api/bookings/{booking_id}/').data
        self.assertEqual((again['site_lat'], again['service_address']), (13.9358, r.data['service_address']))
        # A new pinned address is fine before work starts, refused after
        moved = {**QUEZON_ADDRESS, 'street': '14 Quezon Ave', 'lat': 13.9361}
        self.assertEqual(self.client.patch(f'/api/bookings/{booking_id}/', {'address': moved}, format='json').status_code, 200)
        from apps.library.models import ServiceBooking
        ServiceBooking.objects.filter(id=booking_id).update(status='Installing')
        r = self.client.patch(f'/api/bookings/{booking_id}/', {'address': QUEZON_ADDRESS}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('contact us', str(r.data))
        self.assertEqual(self.client.post('/api/bookings/', {'service_type': 'maintenance', 'scheduled_date': '2027-02-01'},
                                          format='json').status_code, 400)

    def test_address_book(self):
        url = '/api/addresses/'
        first = self.client.post(url, {**QUEZON_ADDRESS, 'label': 'Home', 'contact_phone': '0917 123 4567'}, format='json')
        self.assertEqual(first.status_code, 201, first.data)
        self.assertTrue(first.data['is_default'])                                         # the first is the default
        self.assertEqual(first.data['contact_phone'], '09171234567')
        self.assertEqual(first.data['city'], 'Lucena City')
        work = self.client.post(url, {**QUEZON_ADDRESS, 'label': 'Work', 'street': '1 Rizal St'}, format='json').data
        self.assertFalse(work['is_default'])
        self.client.post(f"{url}{work['id']}/make_default/")
        self.assertEqual([a['label'] for a in self.client.get(url).data], ['Work', 'Home'])   # default first
        self.assertEqual(self.client.patch(f"{url}{first.data['id']}/", {'lat': 14.5995, 'lng': 120.9842},
                                           format='json').status_code, 400)               # the pin left town
        # Private: another customer sees none of it
        other = User.objects.create_user(username='other', password='pw12345!')
        self.client.force_authenticate(other)
        self.assertEqual(self.client.get(url).data, [])
        self.assertEqual(self.client.get(f"{url}{work['id']}/").status_code, 404)
        self.client.force_authenticate(self.user)
        self.client.delete(f"{url}{work['id']}/")
        self.assertTrue(self.client.get(url).data[0]['is_default'])                       # Home takes over


class GisPhase3Tests(APITestCase):
    """Crew base pin (staff only) and the owner's Areas report (orders / bookings per Quezon town)."""

    def setUp(self):
        from apps.library.models import StaffProfile
        self.owner = User.objects.create_superuser(username='owner', password='pw12345!', email='o@example.com')
        self.crew = User.objects.create_user(username='crew', password='pw12345!', is_staff=True)
        StaffProfile.objects.create(user=self.crew, role='FIELD_CREW')
        self.customer = User.objects.create_user(username='cust', password='pw12345!', email='c@example.com')

    def test_crew_base_is_staff_only_and_inside_quezon(self):
        self.client.force_authenticate(self.owner)
        self.assertEqual(self.client.patch('/api/business/', {'base': {'lat': 14.5995, 'lng': 120.9842}},
                                           format='json').status_code, 400)                  # Manila: outside Quezon
        r = self.client.patch('/api/business/', {'base': {'lat': 13.9358, 'lng': 121.6129}}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['base'], {'lat': 13.9358, 'lng': 121.6129})
        self.client.force_authenticate(self.crew)
        self.assertEqual(self.client.get('/api/business/').data['base']['lat'], 13.9358)     # crew route start
        self.assertEqual(self.client.patch('/api/business/', {'base': None}, format='json').status_code, 403)
        self.client.force_authenticate(None)
        self.assertNotIn('base', self.client.get('/api/business/').data)                      # never public
        self.client.force_authenticate(self.customer)
        self.assertNotIn('base', self.client.get('/api/business/').data)

    def test_areas_report_counts_by_town(self):
        import datetime
        from django.utils import timezone
        from apps.library.models import Order, ServiceBooking
        from apps.library import addresses
        tayabas = {**QUEZON_ADDRESS, 'city_code': '045647000', 'barangay_code': '045647001', 'lat': 14.03, 'lng': 121.59}
        pinned = Order(customer_name='A', customer_email='a@example.com', total_price=1000, payment_method='cod', user=self.customer)
        addresses.apply_to_order(pinned, addresses.clean_address(QUEZON_ADDRESS))
        pinned.save()
        Order.objects.create(customer_name='B', customer_email='b@example.com', total_price=500, payment_method='cod',
                             customer_address='Purok 2, Brgy. Alitao, Tayabas, Quezon')                # older, text only
        Order.objects.create(customer_name='C', customer_email='c2@example.com', total_price=700, payment_method='cod',
                             customer_address='Somewhere, Quezon')                                   # no town: not guessed
        Order.objects.create(customer_name='D', customer_email='d@example.com', total_price=900, payment_method='cod',
                             status='Cancelled', customer_address='Lucena')                          # cancelled: left out
        b = ServiceBooking(user=self.customer, service_type='maintenance', scheduled_date=timezone.localdate())
        addresses.apply_to_booking(b, addresses.clean_address(tayabas))
        b.save()
        today = timezone.localdate()
        self.client.force_authenticate(self.owner)
        r = self.client.get('/api/reports/areas/', {'from': str(today - datetime.timedelta(days=7)), 'to': str(today)})
        self.assertEqual(r.status_code, 200, r.data)
        by = {t['name']: t for t in r.data['towns']}
        self.assertEqual((by['Lucena City']['orders'], by['Lucena City']['order_value']), (1, 1000.0))
        self.assertEqual((by['Tayabas City']['orders'], by['Tayabas City']['bookings']), (1, 1))
        self.assertEqual(by['Tayabas City']['customers'], 2)       # the text-only buyer and the booking customer
        self.assertNotIn('Quezon', by)                              # the province word never counts as the town 'Quezon'
        self.assertEqual(r.data['not_mapped'], {'orders': 1, 'bookings': 0})
        self.assertEqual(r.data['totals'], {'orders': 2, 'bookings': 1, 'towns_served': 2})
        csv = self.client.get('/api/reports/areas/', {'export': 'csv'})
        self.assertEqual(csv.status_code, 200)
        self.assertIn('Town / city', csv.content.decode('utf-8-sig'))
        self.client.force_authenticate(self.crew)
        self.assertEqual(self.client.get('/api/reports/areas/').status_code, 403)              # owner only


class CrewTrackingTests(APITestCase):
    """Live crew positions (on shift only, office only, 30 days) and arrival at the customer's pin."""

    def setUp(self):
        from apps.library.models import StaffProfile
        self.crew = User.objects.create_user(username='rider', password='pw12345!', first_name='Jun', is_staff=True)
        StaffProfile.objects.create(user=self.crew, role='FIELD_CREW')
        self.other_crew = User.objects.create_user(username='rider2', password='pw12345!', is_staff=True)
        StaffProfile.objects.create(user=self.other_crew, role='FIELD_CREW')
        self.office = User.objects.create_superuser(username='boss', password='pw12345!', email='boss@example.com')
        self.customer = User.objects.create_user(username='cust', password='pw12345!', email='c@example.com')
        self.here = {'lat': 13.9358, 'lng': 121.6129, 'accuracy': 12}

    def _clock_in(self, booking=None):
        from django.utils import timezone
        from apps.library.models import Attendance
        return Attendance.objects.create(staff=self.crew, booking=booking, clock_in_time=timezone.now())

    def _booking(self):
        from apps.library import addresses
        from apps.library.models import ServiceBooking
        from django.utils import timezone
        b = ServiceBooking(user=self.customer, service_type='maintenance', scheduled_date=timezone.localdate(), status='Installing')
        addresses.apply_to_booking(b, addresses.clean_address(QUEZON_ADDRESS))
        b.save()
        b.assigned_crew.set([self.crew])
        return b

    def test_positions_only_on_shift(self):
        url = '/api/crew-location/'
        self.client.force_authenticate(self.crew)
        r = self.client.post(url, self.here, format='json')
        self.assertEqual(r.status_code, 409)                              # not clocked in, no delivery out
        self.assertFalse(r.data['tracking'])
        self._clock_in()
        self.assertEqual(self.client.post(url, self.here, format='json').status_code, 201)
        again = self.client.post(url, self.here, format='json')
        self.assertEqual((again.status_code, again.data['stored']), (200, False))   # 20 s spacing
        self.assertEqual(self.client.post(url, {'lat': 51.5, 'lng': -0.12}, format='json').status_code, 400)  # London
        self.client.force_authenticate(self.customer)
        self.assertEqual(self.client.post(url, self.here, format='json').status_code, 403)

    def test_a_rider_out_on_a_delivery_is_on_shift(self):
        from apps.library.models import Order
        Order.objects.create(customer_name='A', customer_email='a@example.com', total_price=500, payment_method='cod',
                             customer_address='x', delivery_crew=self.crew, status='Out for Delivery')
        self.client.force_authenticate(self.crew)
        self.assertEqual(self.client.post('/api/crew-location/', self.here, format='json').status_code, 201)

    def test_office_live_map_and_retention(self):
        import datetime
        from django.utils import timezone
        from apps.library.models import CrewLocation
        b = self._booking()
        self._clock_in(b)
        CrewLocation.objects.create(user=self.crew, lat=13.93, lng=121.61, accuracy_m=10)
        stale = CrewLocation.objects.create(user=self.other_crew, lat=13.9, lng=121.6)
        CrewLocation.objects.filter(pk=stale.pk).update(recorded_at=timezone.now() - datetime.timedelta(minutes=40))
        ancient = CrewLocation.objects.create(user=self.other_crew, lat=13.9, lng=121.6)
        CrewLocation.objects.filter(pk=ancient.pk).update(recorded_at=timezone.now() - datetime.timedelta(days=31))
        self.client.force_authenticate(self.office)
        r = self.client.get('/api/crew-location/live/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual([c['name'] for c in r.data['crew']], ['Jun'])        # the 40-min-old position isn't live
        self.assertEqual(r.data['crew'][0]['job']['id'], b.id)
        self.assertFalse(CrewLocation.objects.filter(pk=ancient.pk).exists())  # 30-day retention
        self.client.force_authenticate(self.crew)
        self.assertEqual(self.client.get('/api/crew-location/live/').status_code, 403)

    def test_arrival_is_checked_against_the_pin_and_tells_the_customer_once(self):
        from apps.library.models import Notification
        b = self._booking()
        self.client.force_authenticate(self.crew)
        url = '/api/crew-location/arrived/'
        far = self.client.post(url, {'kind': 'job', 'id': b.id, 'lat': 13.95, 'lng': 121.6129, 'accuracy': 10}, format='json')
        self.assertEqual(far.status_code, 400)                                 # ~1.6 km away
        self.assertGreater(far.data['distance_m'], 1000)
        at_gate = {'kind': 'job', 'id': b.id, 'lat': 13.9366, 'lng': 121.6129, 'accuracy': 15}   # ~90 m
        r = self.client.post(url, at_gate, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(r.data['notified'])
        self.assertFalse(self.client.post(url, at_gate, format='json').data['notified'])       # told once
        self.assertEqual(Notification.objects.filter(user=self.customer, kind='crew_arrived').count(), 1)
        self.client.force_authenticate(self.other_crew)
        self.assertEqual(self.client.post(url, at_gate, format='json').status_code, 404)        # not their job

    def test_rider_arrival_reminds_cod_customers_to_have_cash(self):
        from apps.library import addresses
        from apps.library.models import Notification, Order
        o = Order(customer_name='A', customer_email='c@example.com', total_price=500, payment_method='cod',
                  user=self.customer, delivery_crew=self.crew, status='Out for Delivery')
        addresses.apply_to_order(o, addresses.clean_address(QUEZON_ADDRESS))
        o.save()
        self.client.force_authenticate(self.crew)
        r = self.client.post('/api/crew-location/arrived/', {'kind': 'delivery', 'id': o.id, **self.here}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        note = Notification.objects.get(user=self.customer, kind='rider_arrived')
        self.assertIn('have the cash ready', note.message)


class StaffPhaseCTests(APITestCase):
    """Forgotten shifts (owner closes them; no live tracking, not "on shift"), and paged user accounts."""

    def setUp(self):
        from apps.library.models import StaffProfile
        self.owner = User.objects.create_superuser(username='owner', password='pw12345!', email='o@example.com')
        self.office = User.objects.create_user(username='office', password='pw12345!', is_staff=True)
        StaffProfile.objects.create(user=self.office, role='OFFICE_ADMIN')
        self.crew = User.objects.create_user(username='crew', password='pw12345!', first_name='Ana', is_staff=True)
        StaffProfile.objects.create(user=self.crew, role='FIELD_CREW')

    def _shift(self, hours_ago):
        import datetime
        from django.utils import timezone
        from apps.library.models import Attendance
        return Attendance.objects.create(staff=self.crew, clock_in_time=timezone.now() - datetime.timedelta(hours=hours_ago))

    def test_owner_closes_a_forgotten_shift_with_the_real_end_time(self):
        import datetime
        from apps.library.models import ActivityLog
        old = self._shift(40)                     # clocked in two days ago, never out
        self.client.force_authenticate(self.owner)
        listed = self.client.get('/api/attendance/', {'forgotten': '1'}).data
        self.assertEqual([s['id'] for s in listed], [old.id])
        url = f'/api/attendance/{old.id}/close_shift/'
        too_long = (old.clock_in_time + datetime.timedelta(hours=20)).isoformat()
        self.assertIn('at most 16 hours', self.client.post(url, {'clock_out_time': too_long}, format='json').data['error'])
        before = (old.clock_in_time - datetime.timedelta(hours=1)).isoformat()
        self.assertEqual(self.client.post(url, {'clock_out_time': before}, format='json').status_code, 400)
        end = (old.clock_in_time + datetime.timedelta(hours=8, minutes=30)).isoformat()
        r = self.client.post(url, {'clock_out_time': end, 'note': 'Left the site at 4:30'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['total_hours'], 8.5)
        self.assertEqual((r.data['closed_by_name'], r.data['close_note']), ('owner', 'Left the site at 4:30'))
        self.assertTrue(ActivityLog.objects.filter(action='shift_closed').exists())
        self.assertEqual(self.client.post(url, {'clock_out_time': end}, format='json').status_code, 400)  # already closed
        self.assertEqual(self.client.get('/api/attendance/', {'forgotten': '1'}).data, [])
        other = self._shift(30)
        self.client.force_authenticate(self.office)
        self.assertIn(self.client.post(f'/api/attendance/{other.id}/close_shift/', {'clock_out_time': end}, format='json').status_code,
                      (403, 404))                                          # owner only

    def test_overview_flags_forgotten_shifts_for_the_owner_only(self):
        self._shift(30)                           # forgotten
        self.client.force_authenticate(self.owner)
        data = self.client.get('/api/admin/overview/').data
        keys = [a['key'] for a in data['attention']]
        self.assertIn('open_shifts', keys)
        self.assertEqual(data['today']['crew_on_shift'], 0)   # a forgotten shift isn't someone working now
        self._shift(2)                            # a real shift today
        self.assertEqual(self.client.get('/api/admin/overview/').data['today']['crew_on_shift'], 1)
        self.client.force_authenticate(self.office)
        self.assertNotIn('open_shifts', [a['key'] for a in self.client.get('/api/admin/overview/').data['attention']])

    def test_a_forgotten_shift_does_not_keep_live_tracking_on(self):
        self._shift(30)
        self.client.force_authenticate(self.crew)
        self.assertEqual(self.client.post('/api/crew-location/', {'lat': 13.93, 'lng': 121.61}, format='json').status_code, 409)
        self._shift(1)
        self.assertEqual(self.client.post('/api/crew-location/', {'lat': 13.93, 'lng': 121.61}, format='json').status_code, 201)

    def test_user_accounts_are_paged_on_the_server(self):
        for i in range(35):
            User.objects.create_user(username=f'buyer{i:02d}', password='pw12345!', email=f'b{i}@example.com')
        self.client.force_authenticate(self.owner)
        staff = self.client.get('/api/users/', {'page': 1}).data
        self.assertEqual(staff['group'], 'staff')
        self.assertEqual(staff['counts'], {'staff': 3, 'customers': 35, 'all': 38, 'closed': 0})
        self.assertEqual(len(staff['results']), 3)
        first = self.client.get('/api/users/', {'page': 1, 'group': 'customers'}).data
        self.assertEqual((len(first['results']), first['has_more']), (30, True))
        second = self.client.get('/api/users/', {'page': 2, 'group': 'customers'}).data
        self.assertEqual((len(second['results']), second['has_more']), (5, False))
        found = self.client.get('/api/users/', {'page': 1, 'group': 'all', 'q': 'buyer07'}).data
        self.assertEqual(([u['username'] for u in found['results']], found['counts']['all']), (['buyer07'], 1))
        self.assertEqual(len(self.client.get('/api/users/').data), 38)        # the plain list still works


class BudgetUseTests(APITestCase):
    """A design far under its budget because the customer's few, cheap picks fill the beds says so."""

    def setUp(self):
        from apps.library.models import InventoryItem
        self.user = User.objects.create_user(username='buyer', password='pw12345!')
        self.client.force_authenticate(self.user)
        self.cheap = InventoryItem.objects.create(name='Strelitzia', category='plant', unit_price=80, stock_quantity=200,
                                                  real_world_size=1.2)

    @patch.dict(os.environ, {'GOOGLE_API_KEY': '', 'GEMINI_API_KEY': ''})
    def test_cheap_picks_that_fill_the_beds_explain_the_unused_budget(self):
        r = self.client.post('/api/generate-layouts/', {
            'budget_min': 10000, 'budget_max': 25000, 'lot_width': 10, 'lot_length': 10, 'install': False,
            'preferred_plant_ids': [str(self.cheap.id)]}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        for d in r.data['designs']:
            if d['design_name'] == 'Minimalist':
                continue                              # sparse by design: its own note
            self.assertLess(d['budget_use'], 0.6, d['design_name'])
            self.assertIn('Your chosen plants (Strelitzia) fill the planting beds', d['budget_note'])
            self.assertIn(f"₱{d['total_cost']:,.0f}", d['budget_note'])


class InstallScopeAndCapacityTests(APITestCase):
    """Planting into an existing garden (no ground work), variety in designs, and what a yard can hold."""

    def setUp(self):
        from apps.library.models import InventoryItem
        self.user = User.objects.create_user(username='buyer', password='pw12345!')
        self.client.force_authenticate(self.user)
        keys = patch.dict(os.environ, {'GOOGLE_API_KEY': '', 'GEMINI_API_KEY': ''})
        keys.start()
        self.addCleanup(keys.stop)
        for name, price, size, mature in [('Christmas Palm', 1200, 4.0, 6.5), ('Croton', 150, 1.0, 1.8),
                                          ('Strelitzia', 550, 1.2, 1.5), ('Geranium', 120, 0.4, 0.5),
                                          ('Peace Lily', 250, 0.6, 0.7)]:
            InventoryItem.objects.create(name=name, category='plant', unit_price=price, stock_quantity=200,
                                         real_world_size=size, mature_height=mature)
        self.body = {'budget_min': 50000, 'budget_max': 70000, 'lot_width': 10, 'lot_length': 10, 'install': True}

    def _lines(self, design):
        return {l['key']: l['amount'] for l in design['cost_breakdown']['lines']}

    def test_planting_into_an_existing_garden_has_no_ground_work(self):
        from apps.library.models import PricingSettings
        ps = PricingSettings.current()
        makeover = self.client.post('/api/generate-layouts/', self.body, format='json').data
        existing = self.client.post('/api/generate-layouts/', {**self.body, 'scope': 'existing'}, format='json').data
        self.assertEqual(existing['scope'], 'existing')
        for m, e in zip(makeover['designs'], existing['designs']):
            ml, el = self._lines(m), self._lines(e)
            self.assertIn('site_prep', ml)
            for key in ('site_prep', 'materials', 'turf'):
                self.assertNotIn(key, el, e['design_name'])
            plants = sum(r['quantity'] for r in e['plant_breakdown'] if r['category'] == 'plant')
            self.assertEqual(el['compost'], int(ps.compost_per_plant) * plants)
            self.assertLessEqual(e['cost_breakdown']['total'], e['target_budget'] + 2)
        sym = lambda d: next(x for x in d['designs'] if x['design_name'] == 'Symmetrical')
        self.assertLess(sym(existing)['total_cost'], sym(makeover)['total_cost'])
        tiny = self.client.post('/api/generate-layouts/', {**self.body, 'budget_min': 1000, 'budget_max': 1500,
                                                           'scope': 'existing'}, format='json')
        self.assertEqual(tiny.status_code, 400)
        self.assertIn('crew mobilisation alone', tiny.data['error'])
        self.assertEqual(self.client.post('/api/generate-layouts/', {**self.body, 'scope': 'repaint'},
                                          format='json').status_code, 400)

    def test_no_plant_takes_over_a_design(self):
        from apps.library import layout_engine as E
        specs = [E.PlantSpec('g', 'Geranium', 677, 300, 0.4, E.spacing_for(0.5), mature=0.5),
                 E.PlantSpec('l', 'Peace Lily', 250, 300, 0.6, E.spacing_for(0.7), 'full_shade', mature=0.7),
                 E.PlantSpec('c', 'Croton', 35, 300, 1.0, E.spacing_for(1.8), mature=1.8),
                 E.PlantSpec('p', 'Palm', 500, 300, 4.0, 2.5, mature=6.5)]
        for seed in range(4):
            d = E.generate('Lush / Organic', specs, E.Site(10, 10), 400000, seed=seed)
            counts = {}
            for p in d['plants']:
                counts[p['plant_id']] = counts.get(p['plant_id'], 0) + 1
            n = sum(counts.values())
            self.assertGreaterEqual(n, 10)
            layer = {s.id: s.layer for s in specs}
            for pid, k in counts.items():   # trees and shrubs 40%, the low border may be massed to 50%
                share = E.MAX_SHARE_LOW if layer[pid] == 'low' else E.MAX_SHARE
                self.assertLessEqual(k, share * n + 1, (seed, counts))
        # A customer who chose two plants still gets full beds of them
        two = E.generate('Lush / Organic', specs[:2], E.Site(10, 10), 400000, seed=1)
        self.assertGreater(len(two['plants']), 20)

    def test_the_budget_picker_knows_what_the_yard_can_hold(self):
        r = self.client.get('/api/pricing/suggest/', {'area_sqm': 100, 'width': 10, 'length': 10, 'mode': 'plants'})
        self.assertEqual(r.status_code, 200)
        cap = r.data['capacity']
        self.assertGreater(cap['plants'], 10)
        self.assertGreater(cap['total'], cap['plant_cost'])                     # + delivery
        self.assertLessEqual(r.data['suggested']['high'], cap['total'] + 5000)    # never suggests more than fits
        makeover = self.client.get('/api/pricing/suggest/', {'area_sqm': 100, 'width': 10, 'length': 10, 'mode': 'install'}).data
        existing = self.client.get('/api/pricing/suggest/', {'area_sqm': 100, 'width': 10, 'length': 10, 'mode': 'install',
                                                             'scope': 'existing'}).data
        self.assertLess(existing['capacity']['total'], makeover['capacity']['total'])
        self.assertIsNone(self.client.get('/api/pricing/suggest/', {'area_sqm': 100}).data['capacity'])   # no size: none

    def test_sample_prices_command(self):
        from io import StringIO
        from django.core.management import call_command
        from apps.library.models import InventoryItem
        InventoryItem.objects.filter(name='Geranium').update(unit_price=677)
        out = StringIO()
        call_command('load_sample_prices', '--dry-run', stdout=out)
        self.assertEqual(InventoryItem.objects.get(name='Geranium').unit_price, 677)       # dry run: unchanged
        self.assertIn('Would change', out.getvalue())
        InventoryItem.objects.create(name='Volcanic rock', category='hardscape', unit_price=800, stock_quantity=5,
                                     real_world_size=0.6, model_file='volcanic_rock.glb')
        call_command('load_sample_prices', stdout=StringIO())
        self.assertEqual(InventoryItem.objects.get(name='Geranium').unit_price, 85)
        self.assertEqual(InventoryItem.objects.get(name='Croton').unit_price, 150)          # not in the list: unchanged
        # Small garden stones are added once, sharing the boulder's model, at ~20 cm and ₱30 each
        stones = InventoryItem.objects.get(name='Volcanic stones (small)')
        self.assertEqual((stones.unit_price, stones.model_file, stones.category), (30, 'volcanic_rock.glb', 'hardscape'))
        self.assertLess(stones.real_world_size, 0.35)
        self.assertFalse(InventoryItem.objects.filter(name='River stones (small)').exists())   # its boulder isn't in this catalog
        call_command('load_sample_prices', stdout=StringIO())
        self.assertEqual(InventoryItem.objects.filter(name='Volcanic stones (small)').count(), 1)   # not twice

    def test_sample_products_for_shipped_models(self):
        """Catalog models without a product (hibiscus, livistona, coral stone) get one, once, sized for the layout."""
        from io import StringIO
        from django.core.management import call_command
        from apps.library import layout_engine as E
        from apps.library.models import InventoryItem
        # The owner already made a product with the livistona model under their own name: left alone
        InventoryItem.objects.create(name='Anahaw', category='plant', unit_price=1200, stock_quantity=3,
                                     real_world_size=2.0, model_file='livistona.glb')
        call_command('load_sample_prices', stdout=StringIO())
        call_command('load_sample_prices', stdout=StringIO())
        hibiscus = InventoryItem.objects.get(name='Hibiscus')
        self.assertEqual((hibiscus.model_file, hibiscus.category, hibiscus.unit_price), ('hibiscus.glb', 'plant', 150))
        self.assertFalse(InventoryItem.objects.filter(name='Livistona palm').exists())
        self.assertEqual(InventoryItem.objects.filter(model_file='coral_stone.glb', category='hardscape').count(), 1)
        # A pruned shrub: the middle of the bed, not the tall back row
        spec = next(s for s in E.specs_from_inventory([hibiscus]) if s.id == str(hibiscus.id))
        self.assertEqual(spec.layer, 'mid')


class SavedCartTests(APITestCase):
    """The shop cart follows a customer between devices; prices and stock stay the catalog's."""

    def setUp(self):
        from apps.library.models import InventoryItem
        self.user = User.objects.create_user(username='cartuser', password='pw')
        self.other = User.objects.create_user(username='cartother', password='pw')
        self.fern = InventoryItem.objects.create(name='Fern', category='plant', unit_price=500, stock_quantity=10)
        self.bench = InventoryItem.objects.create(name='Bench', category='furniture', unit_price=3000, stock_quantity=2)
        self._as(self.user)

    def _as(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')

    def _put(self, items):
        return self.client.put('/api/cart/', {'items': items}, format='json')

    def test_needs_sign_in(self):
        self.client.credentials()
        self.assertEqual(self.client.get('/api/cart/').status_code, 401)

    def test_empty_cart_before_anything_is_saved(self):
        r = self.client.get('/api/cart/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['items'], [])

    def test_save_and_read_back_with_live_product_data(self):
        r = self._put([{'id': self.fern.id, 'quantity': 2}, {'id': self.bench.id, 'quantity': 1}])
        self.assertEqual(r.status_code, 200, r.data)
        self.fern.unit_price = 450
        self.fern.save()
        r = self.client.get('/api/cart/')
        lines = {l['product']['name']: l for l in r.data['items']}
        self.assertEqual(lines['Fern']['quantity'], 2)
        self.assertEqual(float(lines['Fern']['product']['unit_price']), 450.0)   # the catalog's price, not a copy

    def test_lines_are_merged_capped_and_unknown_ids_dropped(self):
        r = self._put([{'id': self.fern.id, 'quantity': 600}, {'id': self.fern.id, 'quantity': 600},
                       {'id': 999999, 'quantity': 1}, {'id': self.bench.id, 'quantity': 0}])
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual([(l['product']['id'], l['quantity']) for l in r.data['items']], [(self.fern.id, 999)])

    def test_malformed_requests_are_refused(self):
        self.assertEqual(self._put('nope').status_code, 400)
        self.assertEqual(self._put([{'id': 'x', 'quantity': 1}]).status_code, 400)
        self.assertEqual(self._put([{'id': self.fern.id, 'quantity': 1}] * 61).status_code, 400)

    def test_archived_items_are_left_out(self):
        self._put([{'id': self.fern.id, 'quantity': 1}, {'id': self.bench.id, 'quantity': 1}])
        self.bench.is_active = False
        self.bench.save()
        names = [l['product']['name'] for l in self.client.get('/api/cart/').data['items']]
        self.assertEqual(names, ['Fern'])

    def test_each_customer_sees_only_their_own_cart(self):
        self._put([{'id': self.fern.id, 'quantity': 3}])
        self._as(self.other)
        self.assertEqual(self.client.get('/api/cart/').data['items'], [])

    def test_placing_an_order_empties_the_saved_cart(self):
        self._put([{'id': self.fern.id, 'quantity': 2}])
        r = self.client.post('/api/checkout/', {
            'customer_name': 'Buyer', 'customer_email': 'b@example.com', 'address': QUEZON_ADDRESS,
            'customer_phone': '09171234567', 'payment_method': 'cod',
            'items': [{'id': self.fern.id, 'quantity': 2}]}, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(self.client.get('/api/cart/').data['items'], [])


class DesignShareTests(APITestCase):
    """Share links: only the owner makes or stops one; the public copy shows the garden, not private details."""

    def setUp(self):
        from apps.library.models import GardenDesign
        self.owner = User.objects.create_user(username='shareowner', password='pw', first_name='Maria')
        self.other = User.objects.create_user(username='shareother', password='pw')
        self.staff = User.objects.create_user(username='sharestaff', password='pw', is_staff=True)
        self.design = GardenDesign.objects.create(
            user=self.owner, name='Lush Tropical',
            placed_items=[{'plant_id': '24', 'x': 3.3, 'z': 5, 'rotation': 70, 'uid': 'p1', 'secret_note': 'gate code 1234'}],
            plant_breakdown=[{'plant_id': '24', 'name': 'Christmas Palm', 'quantity': 1, 'unit_price': 500, 'subtotal': 500}],
            dimensions={'width': 8, 'length': 12, 'photoCorners': [{'u': 0.1, 'v': 0.5}],
                        'gardenPlan': {'lawn': {'x': 0}}, 'customerEdits': {'notes': ['private']}},
            reference_image_url='/media/designs/yard.jpg',
            cost_estimate={'lines': [{'key': 'plants', 'label': 'Plants', 'amount': 500}]},
            total_cost=500,
        )
        self.url = f'/api/designs/{self.design.id}/share/'

    def _as(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')

    def test_owner_gets_one_link_and_asking_again_returns_the_same(self):
        self._as(self.owner)
        r1 = self.client.post(self.url)
        self.assertEqual(r1.status_code, 201, r1.data)
        self.assertEqual(r1.data['path'], f"/d/{r1.data['token']}")
        self.assertGreaterEqual(len(r1.data['token']), 12)
        r2 = self.client.post(self.url)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.data['token'], r1.data['token'])

    def test_nobody_else_can_share_it_not_even_the_office(self):
        for user in (self.other, self.staff):
            self._as(user)
            self.assertEqual(self.client.post(self.url).status_code, 404)
        self.client.credentials()
        self.assertEqual(self.client.post(self.url).status_code, 401)

    def test_anyone_with_the_link_sees_the_garden_without_private_details(self):
        self._as(self.owner)
        token = self.client.post(self.url).data['token']
        self.client.credentials()
        r = self.client.get(f'/api/shared-designs/{token}/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r['X-Robots-Tag'], 'noindex')
        self.assertEqual(r.data['name'], 'Lush Tropical')
        self.assertEqual(r.data['lot'], {'width': 8, 'length': 12})
        self.assertEqual(r.data['plants'], [{'uid': 'p1', 'plant_id': '24', 'x': 3.3, 'z': 5, 'rotation': 70}])
        self.assertEqual(r.data['garden_plan'], {'lawn': {'x': 0}})
        self.assertEqual(r.data['plant_breakdown'][0]['name'], 'Christmas Palm')
        self.assertEqual(r.data['estimate_lines'], [{'label': 'Plants', 'amount': 500}])
        self.assertFalse(r.data['is_owner'])
        body = str(r.data)
        for private in ('yard.jpg', 'photoCorners', 'gate code', 'customerEdits', 'shareowner', 'Maria'):
            self.assertNotIn(private, body)

    def test_the_shared_copy_rebuilds_the_yard_without_what_the_customer_typed(self):
        self.design.placed_items = [{'plant_id': 'existing', 'x': 2, 'z': 3, 'r': 1.5, 'is_existing': True, 'label': "Lola's mango"}]
        self.design.dimensions = {**self.design.dimensions, 'siteSurvey': {
            'items': [{'kind': 'entry', 'x': 8, 'z': 6, 'edge': 'right', 'label': 'Gate by Aling Nena'},
                      {'kind': 'clear', 'x0': 0, 'z0': 9, 'x1': 2, 'z1': 12, 'label': 'Driveway'},
                      {'kind': 'clear', 'x0': 5, 'z0': 0, 'x1': 6, 'z1': 1, 'label': 'Dog house of Bantay'},
                      {'kind': 'keep', 'x': 2, 'z': 3, 'r': 1.5, 'label': "Lola's mango"}],
            'sun': {'facing': 135, 'house': 'two'}}}
        self.design.save()
        self._as(self.owner)
        token = self.client.post(self.url).data['token']
        self.client.credentials()
        r = self.client.get(f'/api/shared-designs/{token}/')
        self.assertEqual(r.data['plants'], [{'plant_id': 'existing', 'x': 2, 'z': 3, 'r': 1.5, 'is_existing': True}])
        self.assertEqual(r.data['site'], {
            'items': [{'kind': 'entry', 'x': 8, 'z': 6, 'edge': 'right'},
                      {'kind': 'clear', 'x0': 0, 'z0': 9, 'x1': 2, 'z1': 12, 'label': 'Driveway'},
                      {'kind': 'clear', 'x0': 5, 'z0': 0, 'x1': 6, 'z1': 1, 'label': 'Other'}],
            'outline': None, 'sun': {'house': 'two'}})
        body = str(r.data)
        for private in ('Nena', 'Bantay', 'Lola', 'facing', '135'):
            self.assertNotIn(private, body)

    def test_views_count_visitors_but_not_the_owner(self):
        from apps.library.models import DesignShare
        self._as(self.owner)
        token = self.client.post(self.url).data['token']
        self.assertTrue(self.client.get(f'/api/shared-designs/{token}/').data['is_owner'])
        self.client.credentials()
        self.client.get(f'/api/shared-designs/{token}/')
        self.client.get(f'/api/shared-designs/{token}/')
        self.assertEqual(DesignShare.objects.get(token=token).view_count, 2)

    def test_stopping_the_share_breaks_the_link_and_a_new_share_gets_a_new_link(self):
        self._as(self.owner)
        token = self.client.post(self.url).data['token']
        self.assertEqual(self.client.delete(self.url).status_code, 204)
        self.client.credentials()
        self.assertEqual(self.client.get(f'/api/shared-designs/{token}/').status_code, 404)
        self._as(self.owner)
        r = self.client.post(self.url)
        self.assertEqual(r.status_code, 201)
        self.assertNotEqual(r.data['token'], token)

    def test_unknown_link(self):
        self.assertEqual(self.client.get('/api/shared-designs/nope-not-real/').status_code, 404)


class DesignProposalTests(APITestCase):
    """The PDF proposal: owner only, a real 3-page PDF, pictures checked and stripped of GPS."""

    def setUp(self):
        from apps.library.models import GardenDesign, InventoryItem
        self.owner = User.objects.create_user(username='propowner', password='pw', first_name='Ana', last_name='Cruz')
        self.other = User.objects.create_user(username='propother', password='pw')
        self.palm = InventoryItem.objects.create(name='Christmas Palm', category='plant', unit_price=500, stock_quantity=5,
                                                 planting_height=1.2, mature_height=5, mature_spread=2, established_spread=1.5,
                                                 years_to_mature=8)
        self.bench = InventoryItem.objects.create(name='Bench', category='furniture', unit_price=3000, stock_quantity=2, real_world_size=1.6)
        self.design = GardenDesign.objects.create(
            user=self.owner, name='Lush <Tropical> & Co',
            placed_items=[{'plant_id': str(self.palm.id), 'x': 2, 'z': 3}, {'plant_id': str(self.bench.id), 'x': 5, 'z': 9},
                          {'plant_id': 'background_perimeter', 'x': 5, 'z': 1, 'is_existing': True}],
            plant_breakdown=[{'plant_id': str(self.palm.id), 'name': 'Christmas Palm', 'quantity': 1, 'unit_price': 500, 'subtotal': 500},
                             {'plant_id': str(self.bench.id), 'name': 'Bench', 'quantity': 1, 'unit_price': 3000, 'subtotal': 3000}],
            dimensions={'width': 8, 'length': 12, 'gardenPlan': {'lawn': {'shape': 'ellipse', 'cx': 4, 'cz': 6, 'rx': 2, 'rz': 3},
                                                                 'entry': {'x0': 3.4, 'x1': 4.6, 'z0': 9, 'z1': 12}}},
            cost_estimate={'lines': [{'key': 'plants', 'label': 'Plants', 'amount': 3500}, {'key': 'labour', 'label': 'Labour', 'amount': 900}]},
            total_cost=4400,
        )
        self.url = f'/api/designs/{self.design.id}/proposal/'

    def _as(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')

    def _jpeg(self, gps=False, size=(400, 300)):
        import io
        from PIL import Image
        img = Image.new('RGB', size, (90, 140, 60))
        out = io.BytesIO()
        if gps:
            exif = Image.Exif()
            exif[0x8825] = {1: 'N', 2: (14.0, 5.0, 0.0)}   # GPSInfo
            img.save(out, 'JPEG', exif=exif)
        else:
            img.save(out, 'JPEG')
        out.seek(0)
        out.name = 'pic.jpg'
        return out

    def test_owner_gets_a_three_page_pdf(self):
        self._as(self.owner)
        r = self.client.post(self.url, {'render': self._jpeg()}, format='multipart')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r['Content-Type'], 'application/pdf')
        self.assertIn('-proposal.pdf', r['Content-Disposition'])
        body = r.content
        self.assertTrue(body.startswith(b'%PDF'))
        self.assertEqual(body.count(b'/Type /Page\n') + body.count(b'/Type /Page '), 3)

    def test_works_without_pictures_and_with_before_after(self):
        self._as(self.owner)
        self.assertEqual(self.client.post(self.url, {}, format='multipart').status_code, 200)
        r = self.client.post(self.url, {'photo_before': self._jpeg(), 'photo_after': self._jpeg()}, format='multipart')
        self.assertEqual(r.status_code, 200)

    def test_only_the_owner(self):
        self._as(self.other)
        self.assertEqual(self.client.post(self.url, {}, format='multipart').status_code, 404)
        self.client.credentials()
        self.assertEqual(self.client.post(self.url, {}, format='multipart').status_code, 401)

    def test_rejects_a_file_that_is_not_a_picture(self):
        import io
        self._as(self.owner)
        bad = io.BytesIO(b'not an image at all')
        bad.name = 'x.jpg'
        r = self.client.post(self.url, {'render': bad}, format='multipart')
        self.assertEqual(r.status_code, 400)

    def test_pictures_lose_their_gps_and_are_resized(self):
        from PIL import Image
        import io
        from apps.library.proposal import clean_picture
        src = self._jpeg(gps=True, size=(4000, 3000))
        src.size = len(src.getvalue())
        self.assertIn(0x8825, Image.open(io.BytesIO(src.getvalue())).getexif())
        data, w, h = clean_picture(src)
        self.assertEqual((w, h), (1800, 1350))
        self.assertNotIn(0x8825, Image.open(io.BytesIO(data)).getexif())

    def test_expired_prices_still_build(self):
        from datetime import timedelta
        from django.utils import timezone
        from apps.library.proposal import build_proposal
        pdf = build_proposal(self.design, self.owner, {}, now=timezone.now() + timedelta(days=60))
        self.assertTrue(pdf.startswith(b'%PDF'))


class WorkOrderPdfTests(APITestCase):
    """The crew work order prints prices with the peso sign, and still builds without a peso font."""

    def setUp(self):
        from datetime import date
        from apps.library.models import GardenDesign, InventoryItem, ServiceBooking
        self.admin = User.objects.create_user(username='woadmin', password='pw', is_staff=True, is_superuser=True)
        customer = User.objects.create_user(username='wocustomer', password='pw')
        palm = InventoryItem.objects.create(name='Palm', category='plant', unit_price=500, stock_quantity=5)
        design = GardenDesign.objects.create(
            user=customer, name='WO design', placed_items=[{'plant_id': str(palm.id), 'x': 2, 'z': 3}],
            plant_breakdown=[{'plant_id': str(palm.id), 'name': 'Palm', 'quantity': 2, 'unit_price': 500, 'subtotal': 1000}],
            dimensions={'width': 10, 'length': 10}, total_cost=1000)
        self.booking = ServiceBooking.objects.create(user=customer, service_type='consultation', scheduled_date=date.today(), design=design)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(self.admin).access_token}')

    def _get(self):
        return self.client.get(f'/api/bookings/{self.booking.id}/generate_pdf/')

    def test_builds_with_the_peso_font(self):
        r = self._get()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.content.startswith(b'%PDF'))

    def test_builds_on_a_server_without_a_peso_font(self):
        from unittest import mock
        from apps.library import proposal
        with mock.patch.object(proposal, '_fonts', ('Helvetica', 'Helvetica-Bold', 'PHP ')):
            r = self._get()
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.content.startswith(b'%PDF'))
            pdf = proposal.build_proposal(self.booking.design, self.admin, {})
            self.assertTrue(pdf.startswith(b'%PDF'))


class BookedDesignLockTests(APITestCase):
    """design_lock.py: a booked design is the crew's build reference, so it stays the version that was booked."""

    def setUp(self):
        from apps.library.models import GardenDesign, StaffProfile
        self.customer = User.objects.create_user(username='lockcust', password='pw')
        self.other = User.objects.create_user(username='lockother', password='pw')
        self.office = User.objects.create_user(username='lockoffice', password='pw', is_staff=True)
        StaffProfile.objects.create(user=self.office, role='OFFICE_ADMIN')
        self.crew = User.objects.create_user(username='lockcrew', password='pw', is_staff=True)
        StaffProfile.objects.create(user=self.crew, role='FIELD_CREW')
        self.design = GardenDesign.objects.create(
            user=self.customer, name='Tropical Retreat', total_cost=60000,
            placed_items=[{'id': 1, 'plant_id': '24', 'x': 2, 'z': 3, 'position': {'x': 0, 'y': 0, 'z': 0}}],
            plant_breakdown=[{'plant_id': '24', 'name': 'Christmas Palm', 'quantity': 1, 'unit_price': 500, 'subtotal': 500}],
            cost_estimate={'lines': []}, reference_image_url='/media/designs/yard.jpg',
            dimensions={'width': 8, 'length': 12})
        self.url = f'/api/designs/{self.design.id}/'

    def _as(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')

    def _book(self, design_id=None, user=None):
        self._as(user or self.customer)
        return self.client.post('/api/bookings/', {
            'service_type': 'consultation', 'scheduled_date': '2027-02-01',
            'design': design_id or self.design.id, 'address': QUEZON_ADDRESS,
        }, format='json')

    def test_customer_edits_and_deletes_freely_until_booked(self):
        self._as(self.customer)
        self.assertFalse(self.client.get(self.url).data['locked'])
        r = self.client.patch(self.url, {'name': 'Renamed'}, format='json')
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)

    def test_booking_locks_the_design_for_the_customer(self):
        booking = self._book()
        self.assertEqual(booking.status_code, status.HTTP_201_CREATED, booking.data)
        detail = self.client.get(self.url).data
        self.assertTrue(detail['locked'])
        self.assertEqual(detail['locked_for']['booking'], booking.data['id'])
        self.assertTrue(next(d for d in self.client.get('/api/designs/').data if d['id'] == self.design.id)['locked'])

        r = self.client.patch(self.url, {'name': 'Sneaky change'}, format='json')
        self.assertEqual(r.status_code, status.HTTP_409_CONFLICT)
        self.assertTrue(r.data['locked'])
        self.assertIn('Save it as a new design', r.data['error'])
        r = self.client.patch(f'{self.url}items/1/', {'rotation_y': 90}, format='json')
        self.assertEqual(r.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(self.client.delete(self.url).status_code, status.HTTP_409_CONFLICT)
        self.design.refresh_from_db()
        self.assertEqual(self.design.name, 'Tropical Retreat')

        # The way out: an unbooked copy with everything the original had
        copy = self.client.post(f'{self.url}duplicate/')
        self.assertEqual(copy.status_code, status.HTTP_201_CREATED)
        self.assertFalse(copy.data['locked'])
        self.assertEqual(copy.data['plant_breakdown'], self.design.plant_breakdown)
        self.assertEqual(copy.data['reference_image_url'], self.design.reference_image_url)
        self.assertEqual(self.client.patch(f"/api/designs/{copy.data['id']}/", {'name': 'Mine'}, format='json').status_code,
                         status.HTTP_200_OK)

    def test_cancelling_the_booking_unlocks_it(self):
        booking_id = self._book().data['id']
        self.assertEqual(self.client.delete(f'/api/bookings/{booking_id}/').status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(self.client.get(self.url).data['locked'])
        self.assertEqual(self.client.patch(self.url, {'name': 'Free again'}, format='json').status_code, status.HTTP_200_OK)

    def test_office_can_adjust_a_booked_design_and_it_is_logged_but_not_delete_it(self):
        from apps.library.models import ActivityLog
        booking_id = self._book().data['id']
        self._as(self.office)
        r = self.client.patch(self.url, {'name': 'Adjusted after site visit'}, format='json')
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.assertTrue(ActivityLog.objects.filter(action='booked_design_changed', details__booking=booking_id).exists())
        self.assertEqual(self.client.delete(self.url).status_code, status.HTTP_409_CONFLICT)

    def test_crew_can_view_but_never_change_a_job_design(self):
        from apps.library.models import ServiceBooking
        booking = ServiceBooking.objects.get(id=self._book().data['id'])
        booking.assigned_crew.add(self.crew)
        self._as(self.crew)
        self.assertEqual(self.client.get(self.url).status_code, status.HTTP_200_OK)
        self.assertEqual(self.client.patch(self.url, {'name': 'x'}, format='json').status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.client.delete(self.url).status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.client.patch(f'{self.url}items/1/', {'rotation_y': 90}, format='json').status_code,
                         status.HTTP_403_FORBIDDEN)

    def test_customer_cannot_book_someone_elses_design(self):
        r = self._book(user=self.other)
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('design', r.data)
        # A design id that doesn't exist is an error too (it used to book quietly without a design)
        self.assertEqual(self._book(design_id=999999).status_code, status.HTTP_400_BAD_REQUEST)

    def test_customer_can_switch_design_only_while_pending(self):
        from apps.library.models import GardenDesign, ServiceBooking
        second = GardenDesign.objects.create(user=self.customer, name='Zen Garden')
        booking_id = self._book().data['id']
        url = f'/api/bookings/{booking_id}/'
        self.assertEqual(self.client.patch(url, {'design': second.id}, format='json').status_code, status.HTTP_200_OK)
        self.assertFalse(self.client.get(self.url).data['locked'])   # the first one is free again
        ServiceBooking.objects.filter(id=booking_id).update(status='Preparing')
        r = self.client.patch(url, {'design': self.design.id}, format='json')
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('design', r.data)


class DesignReviewTests(APITestCase):
    """design_review.py: the office sends the adjusted design, the customer approves that exact version."""

    def setUp(self):
        from apps.library.models import GardenDesign, InventoryItem, ServiceBooking, StaffProfile, ProjectTracker
        self.customer = User.objects.create_user(username='revcust', password='pw')
        self.other = User.objects.create_user(username='revother', password='pw')
        self.office = User.objects.create_user(username='revoffice', password='pw', is_staff=True)
        StaffProfile.objects.create(user=self.office, role='OFFICE_ADMIN')
        self.crew = User.objects.create_user(username='revcrew', password='pw', is_staff=True)
        StaffProfile.objects.create(user=self.crew, role='FIELD_CREW')
        self.palm = InventoryItem.objects.create(name='Christmas Palm', category='plant', unit_price=500, stock_quantity=10)
        self.fern = InventoryItem.objects.create(name='Boston Fern', category='plant', unit_price=150, stock_quantity=10)
        self.design = GardenDesign.objects.create(
            user=self.customer, name='Tropical Retreat', total_cost=60000,
            placed_items=[{'plant_id': str(self.palm.id), 'x': 2, 'z': 3}, {'plant_id': str(self.palm.id), 'x': 5, 'z': 3}],
            plant_breakdown=[{'plant_id': str(self.palm.id), 'name': 'Christmas Palm', 'quantity': 2, 'unit_price': 500, 'subtotal': 1000}],
            dimensions={'width': 8, 'length': 12})
        self.booking = ServiceBooking.objects.create(user=self.customer, service_type='consultation',
                                                     scheduled_date='2027-02-01', design=self.design)
        self.booking.assigned_crew.add(self.crew)
        ProjectTracker.objects.create(booking=self.booking, total_price=60000, remaining_balance=60000)
        self.url = f'/api/bookings/{self.booking.id}/design-review/'

    def _as(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')

    def _review(self):
        return self.client.get(f'/api/bookings/{self.booking.id}/').data['design_review']

    def _office_edit(self, **data):
        self._as(self.office)
        r = self.client.patch(f'/api/designs/{self.design.id}/', data, format='json')
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        return r

    def test_full_round_send_changes_adjust_resend_approve(self):
        from apps.library.models import Notification, ProjectTracker
        self._as(self.customer)
        self.assertEqual(self._review()['state'], 'draft')

        # Office adjusts after the site visit (a Studio save: placed items only) and sends it
        self._office_edit(placed_items=[{'productId': self.palm.id, 'position': {'x': 0, 'z': 0}},
                                        {'productId': self.fern.id, 'position': {'x': 1, 'z': 1}}])
        self.design.refresh_from_db()
        self.assertEqual(self.design.revision, 2)
        self.assertEqual({r['name']: r['quantity'] for r in self.design.plant_breakdown},
                         {'Christmas Palm': 1, 'Boston Fern': 1})   # recounted, not the old list
        r = self.client.post(self.url, {'step': 'send', 'note': 'Moved the palm away from the water pipe.'}, format='json')
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.assertEqual(r.data['design_review']['state'], 'sent')
        self.assertTrue(Notification.objects.filter(user=self.customer, kind='design_sent',
                                                    link=f'/my-bookings?review={self.booking.id}').exists())
        self.assertEqual(self.client.post(self.url, {'step': 'send'}, format='json').status_code, 400)  # already waiting

        # Customer asks for changes
        self._as(self.customer)
        self.assertEqual(self.client.post(self.url, {'step': 'changes', 'note': ''}, format='json').status_code, 400)
        r = self.client.post(self.url, {'step': 'changes', 'note': 'Can we keep two palms?'}, format='json')
        self.assertEqual(r.data['design_review']['state'], 'changes')
        self.assertTrue(Notification.objects.filter(user=self.office, kind='design_changes').exists())

        # Office adjusts again and resends; customer approves exactly that version
        self._office_edit(placed_items=[{'productId': self.palm.id, 'position': {'x': 0, 'z': 0}},
                                        {'productId': self.palm.id, 'position': {'x': 2, 'z': 0}}])
        self.assertEqual(self.client.post(self.url, {'step': 'send'}, format='json').status_code, 200)
        self._as(self.customer)
        review = self._review()
        self.assertEqual(review['state'], 'sent')
        r = self.client.post(self.url, {'step': 'approve', 'revision': review['revision']}, format='json')
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.assertEqual(r.data['design_review']['state'], 'approved')
        self.assertEqual(r.data['design_review']['approved_revision'], 3)
        self.assertEqual(ProjectTracker.objects.get(booking=self.booking).status, 'DESIGN_APPROVED')
        self.assertTrue(Notification.objects.filter(user=self.crew, kind='design_approved').exists())

    def test_approving_a_version_that_changed_since_it_was_opened_fails(self):
        self._as(self.office)
        self.client.post(self.url, {'step': 'send'}, format='json')
        self._as(self.customer)
        seen = self._review()['revision']
        self._office_edit(dimensions={'width': 9, 'length': 12})
        self._as(self.customer)
        self.assertEqual(self._review()['state'], 'changed')
        r = self.client.post(self.url, {'step': 'approve', 'revision': seen}, format='json')
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_changing_an_approved_design_needs_approval_again_but_a_rename_does_not(self):
        self._as(self.office)
        self.client.post(self.url, {'step': 'approved_in_person', 'note': 'Signed at the site visit'}, format='json')
        self._as(self.customer)
        self.assertEqual(self._review()['state'], 'approved')
        self.assertTrue(self._review()['approved_in_person'])
        self._office_edit(name='Tropical Retreat (final)')
        self._as(self.customer)
        self.assertEqual(self._review()['state'], 'approved')
        self._office_edit(total_cost=65000)
        self._as(self.customer)
        self.assertEqual(self._review()['state'], 'changed')

    def test_who_may_do_what(self):
        self._as(self.customer)
        self.assertEqual(self.client.post(self.url, {'step': 'send'}, format='json').status_code, 403)
        self.assertEqual(self.client.post(self.url, {'step': 'approved_in_person', 'note': 'x'}, format='json').status_code, 403)
        self.assertEqual(self.client.post(self.url, {'step': 'approve', 'revision': 1}, format='json').status_code, 400)  # nothing sent
        self._as(self.crew)
        self.assertEqual(self.client.post(self.url, {'step': 'send'}, format='json').status_code, 403)
        self.assertEqual(self.client.post(self.url, {'step': 'approve', 'revision': 1}, format='json').status_code, 403)
        self._as(self.other)
        self.assertEqual(self.client.post(self.url, {'step': 'approve', 'revision': 1}, format='json').status_code, 404)
        self._as(self.office)
        self.assertEqual(self.client.post(self.url, {'step': 'approved_in_person', 'note': ''}, format='json').status_code, 400)

    def test_design_and_contract_stage_waits_for_approval(self):
        self._as(self.office)
        url = f'/api/bookings/{self.booking.id}/update_milestone/'
        r = self.client.patch(url, {'phase': 'design_contract', 'completion_pct': 100}, format='json')
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("hasn't approved", r.data['error'])
        self.client.post(self.url, {'step': 'approved_in_person', 'note': 'Agreed on site'}, format='json')
        r = self.client.patch(url, {'phase': 'design_contract', 'completion_pct': 100}, format='json')
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)


class DesignItemsTests(APITestCase):
    """design_items.lot_items: Studio items (centred, radians) come out in lot metres and degrees like AI items."""

    def test_studio_and_ai_items_share_one_shape(self):
        import math
        from apps.library.design_items import lot_items
        from apps.library.models import GardenDesign
        design = GardenDesign(dimensions={'width': 8, 'length': 12}, placed_items=[
            {'plant_id': '5', 'x': 2, 'z': 3, 'rotation': 45, 'uid': 'a'},
            {'is_existing': True, 'x': 1, 'z': 1},
            {'product_id': 7, 'position': {'x': -4, 'y': 0, 'z': 6}, 'rotation': {'x': 0, 'y': math.pi / 2, 'z': 0}},
            {'productId': 9, 'position': {'x': 0, 'y': 0, 'z': 0}},
            'junk',
        ])
        items = lot_items(design)
        self.assertEqual(items[0], {'plant_id': '5', 'x': 2, 'z': 3, 'rotation': 45, 'uid': 'a'})
        self.assertTrue(items[1]['is_existing'])
        self.assertEqual(items[2], {'plant_id': '7', 'x': 0.0, 'z': 12.0, 'rotation': 90.0})
        self.assertEqual(items[3], {'plant_id': '9', 'x': 4.0, 'z': 6.0, 'rotation': 0.0})
        self.assertEqual(len(items), 4)


class SettingOutTests(APITestCase):
    """setting_out.py: labels and tape measurements the crew sets the garden out with."""

    def setUp(self):
        from apps.library.models import GardenDesign, InventoryItem, ServiceBooking, StaffProfile
        self.palm = InventoryItem.objects.create(name='Christmas Palm', category='plant', unit_price=500, stock_quantity=9)
        self.bench = InventoryItem.objects.create(name='Garden Bench', category='furniture', unit_price=4000, stock_quantity=2)
        self.customer = User.objects.create_user(username='socust', password='pw')
        self.crew = User.objects.create_user(username='socrew', password='pw', is_staff=True)
        StaffProfile.objects.create(user=self.crew, role='FIELD_CREW')
        # 8 m wide, 12 m deep. Mixed formats: an office Studio edit leaves Studio items next to AI ones.
        self.design = GardenDesign.objects.create(
            user=self.customer, name='Mixed', dimensions={'width': 8, 'length': 12},
            plant_breakdown=[{'plant_id': str(self.bench.id), 'name': 'Garden Bench', 'quantity': 1},
                             {'plant_id': str(self.palm.id), 'name': 'Christmas Palm', 'quantity': 2}],
            placed_items=[
                {'plant_id': str(self.palm.id), 'x': 1.0, 'z': 2.0},                  # back left
                {'product_id': self.palm.id, 'position': {'x': 2.5, 'y': 0, 'z': 4.5}},  # = lot (6.5, 10.5)
                {'plant_id': str(self.bench.id), 'x': 4.0, 'z': 11.0},
                {'is_existing': True, 'x': 7.0, 'z': 1.0},
            ])
        self.booking = ServiceBooking.objects.create(user=self.customer, service_type='Softscape',
                                                     scheduled_date='2027-03-01', status='Preparing', design=self.design)
        self.booking.assigned_crew.add(self.crew)

    def test_labels_follow_the_plant_list_and_measure_from_house_and_nearer_side(self):
        from apps.library.setting_out import setting_out
        so = setting_out(self.design)
        self.assertEqual([(t['no'], t['name'], t['quantity']) for t in so['types']],
                         [(1, 'Garden Bench', 1), (2, 'Christmas Palm', 2)])
        rows = {r['label']: r for r in so['items']}
        self.assertEqual(set(rows), {'1.1', '2.1', '2.2', 'E1'})
        # 2.1 is the palm nearest the house: the Studio one at lot (6.5, 10.5)
        self.assertEqual((rows['2.1']['from_house'], rows['2.1']['side'], rows['2.1']['from_side']), (1.5, 'right', 1.5))
        self.assertEqual((rows['2.2']['from_house'], rows['2.2']['side'], rows['2.2']['from_side']), (10.0, 'left', 1.0))
        self.assertEqual((rows['1.1']['from_house'], rows['1.1']['from_side']), (1.0, 4.0))
        self.assertTrue(rows['E1']['existing'])
        self.assertEqual((rows['E1']['from_house'], rows['E1']['side'], rows['E1']['from_side']), (11.0, 'right', 1.0))
        self.assertFalse(any(r['outside'] for r in so['items']))

    def test_crew_get_it_with_the_job_and_the_work_order_has_the_plan(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(self.crew).access_token}')
        data = self.client.get(f'/api/bookings/{self.booking.id}/').data
        self.assertEqual(len(data['design_details']['setting_out']['items']), 4)
        r = self.client.get(f'/api/bookings/{self.booking.id}/generate_pdf/')
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        pdf = b''.join(r.streaming_content) if getattr(r, 'streaming', False) else r.content
        self.assertTrue(pdf.startswith(b'%PDF'))

    def test_design_without_items_has_no_plan(self):
        from apps.library.models import GardenDesign
        from apps.library.setting_out import setting_out
        self.assertIsNone(setting_out(GardenDesign(dimensions={'width': 5, 'length': 5}, placed_items=[])))
        self.assertIsNone(setting_out(None))

    def test_work_order_prints_an_approved_design(self):
        from apps.library import design_review
        office = User.objects.create_user(username="sooffice", password="pw", is_staff=True)
        design_review.approve_in_person(self.booking, office, "Signed at the site visit")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(self.crew).access_token}")
        r = self.client.get(f"/api/bookings/{self.booking.id}/generate_pdf/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)


class InventorySpacingTests(APITestCase):
    """Planting distance (spacing_cm) from the Inventory form: optional, 10–400 cm."""

    def setUp(self):
        from apps.library.models import InventoryItem, StaffProfile
        self.office = User.objects.create_user(username='spoffice', password='pw', is_staff=True)
        StaffProfile.objects.create(user=self.office, role='OFFICE_ADMIN')
        self.item = InventoryItem.objects.create(name='Fern', category='plant', unit_price=150, stock_quantity=3)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(self.office).access_token}')
        self.url = f'/api/inventory/{self.item.id}/'

    def test_set_clear_and_reject_out_of_range(self):
        self.assertEqual(self.client.patch(self.url, {'spacing_cm': '90'}, format='multipart').status_code, 200)
        self.item.refresh_from_db()
        self.assertEqual(self.item.spacing_cm, 90)
        self.assertEqual(self.client.patch(self.url, {'spacing_cm': ''}, format='multipart').status_code, 200)
        self.item.refresh_from_db()
        self.assertIsNone(self.item.spacing_cm)
        r = self.client.patch(self.url, {'spacing_cm': '5000'}, format='multipart')
        self.assertEqual(r.status_code, 400)
        self.assertIn('spacing_cm', r.data)


class MyAccountTests(APITestCase):
    """account.py: people read and correct their own details; email and password need the current password."""

    def setUp(self):
        self.user = User.objects.create_user(username='maria', password='Garden-pass-2026', email='maria@example.com',
                                             first_name='Maria')
        self.other = User.objects.create_user(username='jose', password='Garden-pass-2026', email='jose@example.com')
        self._as(self.user)

    def _as(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')

    def test_read_and_correct_name_and_mobile(self):
        r = self.client.get('/api/me/')
        self.assertEqual((r.data['username'], r.data['first_name'], r.data['phone'], r.data['role']), ('maria', 'Maria', '', 'customer'))
        r = self.client.patch('/api/me/', {'first_name': '  Maria  Clara ', 'last_name': 'Santos', 'phone': '+63 917-123-4567'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual((r.data['first_name'], r.data['last_name'], r.data['phone']), ('Maria Clara', 'Santos', '09171234567'))
        # Fresh tokens carry the new name, so the app's header changes at once
        from rest_framework_simplejwt.tokens import AccessToken
        self.assertEqual(AccessToken(r.data['tokens']['access'])['first_name'], 'Maria Clara')
        # The username is the sign-in name: it can't be changed here
        self.client.patch('/api/me/', {'username': 'hacker'}, format='json')
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, 'maria')

    def test_bad_values_are_refused_and_the_number_can_be_cleared(self):
        self.assertEqual(self.client.patch('/api/me/', {'phone': '12345'}, format='json').status_code, 400)
        self.assertEqual(self.client.patch('/api/me/', {'first_name': '   '}, format='json').status_code, 400)
        self.client.patch('/api/me/', {'phone': '09171234567'}, format='json')
        r = self.client.patch('/api/me/', {'phone': ''}, format='json')
        self.assertEqual(r.data['phone'], '')

    def test_change_password_needs_the_current_one(self):
        url = '/api/me/password/'
        r = self.client.post(url, {'current_password': 'wrong', 'new_password': 'Another-pass-2026'}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('current_password', r.data)
        self.assertEqual(self.client.post(url, {'current_password': 'Garden-pass-2026', 'new_password': '123'}, format='json').status_code, 400)
        r = self.client.post(url, {'current_password': 'Garden-pass-2026', 'new_password': 'Another-pass-2026'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn('access', r.data['tokens'])
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('Another-pass-2026'))

    def test_change_email_needs_the_password_and_must_be_free(self):
        url = '/api/me/email/'
        self.assertEqual(self.client.post(url, {'email': 'new@example.com', 'current_password': 'nope'}, format='json').status_code, 400)
        r = self.client.post(url, {'email': 'JOSE@example.com', 'current_password': 'Garden-pass-2026'}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('Another account', r.data['email'])
        self.assertEqual(self.client.post(url, {'email': 'not-an-email', 'current_password': 'Garden-pass-2026'}, format='json').status_code, 400)
        r = self.client.post(url, {'email': 'maria.santos@example.com', 'current_password': 'Garden-pass-2026'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['email'], 'maria.santos@example.com')

    def test_sign_up_refuses_an_email_already_in_use(self):
        self.client.credentials()
        r = self.client.post('/api/register/', {'username': 'maria2', 'email': 'Maria@Example.com', 'password': 'Garden-pass-2026',
                                                'accept_privacy': True}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('email', r.data)

    def test_needs_sign_in(self):
        self.client.credentials()
        self.assertEqual(self.client.get('/api/me/').status_code, 401)


class AccountPrivacyTests(APITestCase):
    """account_privacy.py: consent record, a copy of your data, closing the account (anonymised, records kept)."""

    def setUp(self):
        from apps.library.models import (AccountProfile, GardenDesign, InventoryItem, Order, PrivacyConsent,
                                         SavedAddress, ServiceBooking, ServiceReview)
        self.user = User.objects.create_user(username='lina', password='Garden-pass-2026', email='lina@example.com',
                                             first_name='Lina', last_name='Cruz')
        AccountProfile.objects.create(user=self.user, phone='09171234567')
        PrivacyConsent.objects.create(user=self.user, policy_version='2026-01-01')
        SavedAddress.objects.create(user=self.user, city_code='045624000', barangay_code='045624002', street='12 Quezon Ave',
                                    lat=13.93, lng=121.61, contact_phone='09171234567')
        self.draft = GardenDesign.objects.create(user=self.user, name='Just trying')
        self.booked = GardenDesign.objects.create(user=self.user, name='The one we built')
        self.booking = ServiceBooking.objects.create(user=self.user, service_type='Softscape', scheduled_date='2026-10-01',
                                                     status='Finished', design=self.booked, contact_number='09171234567',
                                                     site_lat=13.93, site_lng=121.61)
        ServiceReview.objects.create(booking=self.booking, user=self.user, rating=5, comment='Lovely work', is_public=True)
        item = InventoryItem.objects.create(name='Fern', category='plant', unit_price=150, stock_quantity=5)
        self.order = Order.objects.create(user=self.user, customer_name='Lina Cruz', customer_email='lina@example.com',
                                          customer_phone='09171234567', customer_address='12 Quezon Ave, Lucena',
                                          total_price=300, status='Delivered', delivery_lat=13.9, delivery_lng=121.6)
        self.order.items.create(item=item, quantity=2, price_at_booking=150)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(self.user).access_token}')

    def test_consent_shows_and_the_updated_notice_can_be_accepted(self):
        r = self.client.get('/api/me/privacy/')
        self.assertEqual(r.data['consent']['version'], '2026-01-01')
        self.assertFalse(r.data['up_to_date'])
        self.assertTrue(r.data['can_close'])
        r = self.client.post('/api/me/privacy/accept/')
        self.assertTrue(r.data['up_to_date'])

    def test_download_my_data(self):
        import json
        r = self.client.get('/api/me/export/')
        self.assertEqual(r.status_code, 200)
        self.assertIn('attachment', r['Content-Disposition'])
        data = json.loads(r.content)
        self.assertEqual(data['account']['phone'], '09171234567')
        self.assertEqual(len(data['addresses']), 1)
        self.assertEqual(data['orders'][0]['items'][0]['quantity'], 2)
        self.assertEqual(data['service_reviews'][0]['comment'], 'Lovely work')

    def test_open_work_blocks_closing(self):
        from apps.library.models import ProjectTracker, ServiceBooking
        open_booking = ServiceBooking.objects.create(user=self.user, service_type='maintenance', scheduled_date='2027-01-05', status='Pending')
        ProjectTracker.objects.create(booking=self.booking, total_price=1000, remaining_balance=400)
        blockers = self.client.get('/api/me/privacy/').data['blockers']
        self.assertTrue(any(f'#{open_booking.id}' in b and 'Cancel it' in b for b in blockers))
        self.assertTrue(any('unpaid balance' in b for b in blockers))
        r = self.client.post('/api/me/close/', {'current_password': 'Garden-pass-2026', 'confirm': 'CLOSE'}, format='json')
        self.assertEqual(r.status_code, 409)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)

    def test_close_needs_password_and_the_word(self):
        self.assertEqual(self.client.post('/api/me/close/', {'current_password': 'nope', 'confirm': 'CLOSE'}, format='json').status_code, 400)
        self.assertEqual(self.client.post('/api/me/close/', {'current_password': 'Garden-pass-2026', 'confirm': 'yes'}, format='json').status_code, 400)

    def test_closing_erases_the_person_and_keeps_the_records(self):
        from apps.library.models import (ActivityLog, GardenDesign, Order, SavedAddress, ServiceBooking, ServiceReview)
        from apps.library.serializers import public_reviewer_name
        r = self.client.post('/api/me/close/', {'current_password': 'Garden-pass-2026', 'confirm': 'close'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)
        self.assertTrue(self.user.username.startswith(f'deleted-{self.user.id}-'))
        self.assertEqual((self.user.email, self.user.first_name, self.user.last_name), ('', '', ''))
        self.assertFalse(self.user.has_usable_password())
        self.assertEqual(self.user.account_profile.phone, '')
        self.assertFalse(SavedAddress.objects.filter(user=self.user).exists())
        self.assertFalse(GardenDesign.objects.filter(pk=self.draft.pk).exists())       # never booked: gone
        self.assertTrue(GardenDesign.objects.filter(pk=self.booked.pk).exists())       # the built design stays
        order = Order.objects.get(pk=self.order.pk)                                    # the sale stays, minus phone and pin
        self.assertEqual((order.customer_phone, order.delivery_lat, str(order.total_price)), ('', None, '300.00'))
        booking = ServiceBooking.objects.get(pk=self.booking.pk)
        self.assertEqual((booking.contact_number, booking.site_lat), ('', None))
        review = ServiceReview.objects.get(booking=self.booking)
        self.assertEqual(public_reviewer_name(review.user), 'Verified customer')
        self.assertTrue(ActivityLog.objects.filter(action='account_closed').exists())
        # The old sign-in no longer works
        self.client.credentials()
        r = self.client.post('/api/token/', {'username': 'lina', 'password': 'Garden-pass-2026'}, format='json')
        self.assertEqual(r.status_code, 401)

    def test_staff_close_through_the_owner(self):
        from apps.library.models import StaffProfile
        staff = User.objects.create_user(username='office2', password='Garden-pass-2026', is_staff=True)
        StaffProfile.objects.create(user=staff, role='OFFICE_ADMIN')
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(staff).access_token}')
        self.assertFalse(self.client.get('/api/me/privacy/').data['can_close'])
        r = self.client.post('/api/me/close/', {'current_password': 'Garden-pass-2026', 'confirm': 'CLOSE'}, format='json')
        self.assertEqual(r.status_code, 409)


class ClosedAccountsTests(APITestCase):
    """User accounts → Closed: the owner sees closed accounts and what was kept; they can't be revived or deleted."""

    def setUp(self):
        from apps.library.models import Order, ServiceBooking
        self.owner = User.objects.create_superuser(username='owner1', password='Garden-pass-2026', email='o@example.com')
        self.customer = User.objects.create_user(username='rosa', password='Garden-pass-2026', email='rosa@example.com')
        self.other = User.objects.create_user(username='ben', password='Garden-pass-2026')
        Order.objects.create(user=self.customer, customer_name='Rosa', customer_email='rosa@example.com', customer_address='Lucena',
                             total_price=500, status='Delivered', payment_status='Paid')
        ServiceBooking.objects.create(user=self.customer, service_type='maintenance', scheduled_date='2026-09-01', status='Finished')
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(self.customer).access_token}')
        r = self.client.post('/api/me/close/', {'current_password': 'Garden-pass-2026', 'confirm': 'CLOSE'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(self.owner).access_token}')

    def test_closed_tab_shows_kept_records_and_nothing_personal(self):
        r = self.client.get('/api/users/', {'page': 1, 'group': 'closed'})
        self.assertEqual(r.data['counts']['closed'], 1)
        row = r.data['results'][0]
        self.assertEqual((row['id'], row['orders'], row['bookings'], row['paid_total']), (self.customer.id, 1, 1, 500.0))
        self.assertIsNotNone(row['closed_at'])
        self.assertNotIn('username', row)
        self.assertNotIn('email', row)
        # Not mixed in with the open accounts
        customers = self.client.get('/api/users/', {'page': 1, 'group': 'customers'}).data
        self.assertEqual([u['id'] for u in customers['results']], [self.other.id])
        self.assertEqual(customers['counts']['all'], 2)   # owner + ben

    def test_closed_account_cant_be_revived_or_deleted(self):
        url = f'/api/users/{self.customer.id}/'
        self.assertEqual(self.client.patch(url, {'is_active': True}, format='json').status_code, 400)
        self.assertEqual(self.client.delete(url).status_code, 400)
        self.customer.refresh_from_db()
        self.assertFalse(self.customer.is_active)

    def test_an_account_with_orders_or_bookings_is_never_hard_deleted(self):
        from apps.library.models import Order
        Order.objects.create(user=self.other, customer_name='Ben', customer_email='b@example.com', customer_address='Tayabas',
                             total_price=100, status='Delivered')
        r = self.client.delete(f'/api/users/{self.other.id}/')
        self.assertEqual(r.status_code, 400)
        self.assertIn('Deactivate it instead', r.data['error'])
        empty = User.objects.create_user(username='nobody', password='Garden-pass-2026')
        self.assertEqual(self.client.delete(f'/api/users/{empty.id}/').status_code, 204)


class SignOutEverywhereTests(APITestCase):
    """jwt_session.py / signout.py: raising the session version ends every token issued before, at once."""

    def setUp(self):
        self.user = User.objects.create_user(username='tess', password='Garden-pass-2026', email='tess@example.com')

    def _login(self):
        r = self.client.post('/api/token/', {'username': 'tess', 'password': 'Garden-pass-2026'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        return r.data

    def _me(self, access):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {access}')
        return self.client.get('/api/me/')

    def test_other_devices_are_signed_out_and_this_one_stays(self):
        phone, laptop = self._login(), self._login()
        self.assertEqual(self._me(phone['access']).status_code, 200)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {laptop["access"]}')
        r = self.client.post('/api/me/sign-out-everywhere/')
        self.assertEqual(r.status_code, 200)
        self.assertIsNotNone(r.data['signed_out_everywhere_at'])
        fresh = r.data['tokens']
        # The phone's access token stops working at once (not after it expires), and so does its refresh
        r = self._me(phone['access'])
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.data['code'], 'session_ended')
        self.client.credentials()
        self.assertEqual(self.client.post('/api/token/refresh/', {'refresh': phone['refresh']}, format='json').status_code, 401)
        self.assertEqual(self._me(laptop['access']).status_code, 401)   # the laptop's old token too
        # The device that asked carries on with its fresh tokens, and can refresh them
        self.assertEqual(self._me(fresh['access']).status_code, 200)
        self.client.credentials()
        self.assertEqual(self.client.post('/api/token/refresh/', {'refresh': fresh['refresh']}, format='json').status_code, 200)
        # Signing in again works as usual
        self.assertEqual(self._me(self._login()['access']).status_code, 200)

    def test_changing_the_password_signs_out_the_other_devices(self):
        other, mine = self._login(), self._login()
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {mine["access"]}')
        r = self.client.post('/api/me/password/', {'current_password': 'Garden-pass-2026', 'new_password': 'Fresh-garden-2027'}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._me(other['access']).status_code, 401)
        self.assertEqual(self._me(r.data['tokens']['access']).status_code, 200)

    def test_tokens_from_before_still_work_until_someone_signs_out(self):
        # Tokens without the "sv" claim (issued before this feature) count as version 0
        legacy = str(RefreshToken.for_user(self.user).access_token)
        self.assertEqual(self._me(legacy).status_code, 200)
        self.client.post('/api/me/sign-out-everywhere/')
        self.assertEqual(self._me(legacy).status_code, 401)


class BarangayLookupTests(APITestCase):
    """geo_lookup.py: map pin → town and barangay from the official boundaries (no outside service)."""

    def _areas(self):
        import json, os
        from django.conf import settings
        with open(os.path.join(settings.BASE_DIR, 'apps', 'library', 'data', 'quezon_areas.json'), encoding='utf-8') as f:
            return json.load(f)['cities']

    def test_every_barangay_has_a_boundary(self):
        from apps.library.geo_lookup import _bounds
        codes = {row['code'] for row in _bounds()}
        ours = {b['code'] for t in self._areas() for b in t['barangays']}
        self.assertEqual(codes, ours)
        self.assertEqual(len(codes), 1242)

    def test_known_places_land_in_their_own_barangay(self):
        """Barangay centres from OpenStreetMap (an independent source): most must land in their own barangay,
        and the town must be right far more often (a centre near a border can sit in the next barangay)."""
        from apps.library.geo_lookup import locate
        same_brgy = same_town = total = 0
        for town in self._areas():
            for b in town['barangays']:
                if not b.get('lat'):
                    continue
                total += 1
                found = locate(b['lat'], b['lng'])
                same_brgy += bool(found and found['barangay_code'] == b['code'])
                same_town += bool(found and found['city_code'] == town['code'])
        self.assertGreaterEqual(same_brgy / total, 0.90, f'{same_brgy}/{total} in their own barangay')
        self.assertGreaterEqual(same_town / total, 0.97, f'{same_town}/{total} in their own town')

    def test_lucena_and_tayabas_codes_line_up(self):
        """The two cities whose codes needed care (Lucena has its own code since 2023)."""
        from apps.library.geo_lookup import locate
        for name in ('Lucena City', 'Tayabas City'):
            town = next(t for t in self._areas() if t['name'] == name)
            hits = [locate(b['lat'], b['lng']) for b in town['barangays'] if b.get('lat')]
            self.assertTrue(any(h and h['city_code'] == town['code'] and h['city_name'] == name for h in hits), name)

    def test_outside_quezon_is_no_match(self):
        from apps.library.geo_lookup import locate
        self.assertIsNone(locate(14.5995, 120.9842))   # Manila
        self.assertIsNone(locate(12.0, 124.5))         # open sea, far south-east
        self.assertIsNone(locate('x', None))

    def test_inside_near_and_far_on_a_known_shape(self):
        from unittest import mock
        from apps.library import geo_lookup
        square = [{'code': '045601001', 'city': '045601000', 'bbox': [121.0, 14.0, 121.01, 14.01],
                   'rings': [[(121.0, 14.0), (121.01, 14.0), (121.01, 14.01), (121.0, 14.01), (121.0, 14.0)]]}]
        with mock.patch.object(geo_lookup, '_bounds', return_value=square):
            self.assertEqual(geo_lookup.locate(14.005, 121.005)['match'], 'inside')
            near = geo_lookup.locate(14.005, 121.0108)        # ~85 m east of the edge
            self.assertEqual((near['match'], near['barangay_code']), ('near', '045601001'))
            self.assertTrue(60 <= near['distance_m'] <= 110, near['distance_m'])
            self.assertIsNone(geo_lookup.locate(14.005, 121.013))   # ~320 m out: too far to guess

    def test_endpoint(self):
        town = next(t for t in self._areas() if t['name'] == 'Lucena City')
        b = next(x for x in town['barangays'] if x.get('lat'))
        r = self.client.get('/api/address-locate/', {'lat': b['lat'], 'lng': b['lng']})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['city_code'], town['code'])
        self.assertIn(r.data['match'], ('inside', 'near'))
        self.assertTrue(r.data['barangay_name'])
        self.assertEqual(self.client.get('/api/address-locate/', {'lat': 14.5995, 'lng': 120.9842}).data, {'match': None})
        self.assertEqual(self.client.get('/api/address-locate/', {'lat': 'abc', 'lng': 1}).status_code, 400)
        self.assertEqual(self.client.get('/api/address-locate/', {'lat': 95, 'lng': 1}).status_code, 400)


class SiteSurveyTests(APITestCase):
    """site_survey.py: what the customer marked on their yard (photo or plan) shapes every design."""

    setUp = GenerateLayoutsViewTests.setUp   # same catalogue and no real Gemini; not its tests

    def test_parse_cleans_and_clamps(self):
        from apps.library import site_survey as S
        s = S.parse({'items': [
            {'kind': 'clear', 'x0': 8, 'z0': -3, 'x1': 30, 'z1': 2, 'label': 'Driveway'},   # clamped into 10 × 12
            {'kind': 'clear', 'x0': 1, 'z0': 1, 'x1': 1.05, 'z1': 1.05},                    # a tap, not an area: dropped
            {'kind': 'keep', 'x': 5, 'z': 4, 'r': 99},                                      # canopy capped
            {'kind': 'entry', 'x': 5, 'z': 12, 'edge': 'front'},
            {'kind': 'screen', 'edge': 'left', 'a': 9, 'b': 2},
        ]}, 10, 12)
        kinds = [i['kind'] for i in s['items']]
        self.assertEqual(kinds, ['clear', 'keep', 'entry', 'screen'])
        self.assertEqual((s['items'][0]['x1'], s['items'][0]['z0']), (10.0, 0.0))
        self.assertEqual(s['items'][1]['r'], 8.0)
        self.assertEqual((s['items'][3]['a'], s['items'][3]['b']), (2.0, 9.0))
        for bad in ('nope', [{'kind': 'volcano'}], [{'kind': 'entry', 'x': 1, 'z': 1, 'edge': 'up'}],
                    [{'kind': 'keep', 'x': 'a', 'z': 1, 'r': 1}]):
            with self.assertRaises(S.SurveyError):
                S.parse(bad, 10, 12)
        self.assertEqual(S.parse(None, 10, 12), {'items': [], 'sun': None, 'outline': None})
        # Sun setup: compass degrees wrapped to 0–360, unknown house height → one storey
        self.assertEqual(S.parse({'sun': {'facing': -90, 'house': 'tower'}}, 10, 12)['sun'], {'facing': 270.0, 'house': 'one'})

    def test_designs_keep_marked_areas_and_trees_clear(self):
        from apps.library import layout_engine as E, site_survey as S
        survey = {'items': [
            {'kind': 'clear', 'x0': 0, 'z0': 6, 'x1': 3, 'z1': 12, 'label': 'Driveway'},   # front-left strip
            {'kind': 'keep', 'x': 8, 'z': 2.5, 'r': 1.5, 'label': 'Mango tree'},          # back-right tree
        ]}
        r = self.client.post(self.url, {**self.valid, 'site_survey': survey}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        site = E.Site(10, 12, rules=S.rules(S.parse(survey, 10, 12)))
        specs = {s.id: s for s in E.specs_from_inventory(__import__('apps.library.models', fromlist=['x']).InventoryItem.objects.all())}
        for d in r.data['designs']:
            planted = [p for p in d['plants'] if not p.get('is_existing')]
            self.assertTrue(planted)
            for p in planted:
                self.assertTrue(site.is_clear(specs[str(p['plant_id'])], p['x'], p['z']), (d['design_name'], p))
                self.assertFalse(0 <= p['x'] <= 3 and 6 <= p['z'] <= 12, (d['design_name'], p))      # not on the driveway
                self.assertGreater(((p['x'] - 8) ** 2 + (p['z'] - 2.5) ** 2) ** 0.5, 1.2, (d['design_name'], p))
            kept = [p for p in d['plants'] if p.get('is_existing')]
            self.assertEqual([(k['label'], k['x'], k['z'], k['r']) for k in kept], [('Mango tree', 8.0, 2.5, 1.5)])
            self.assertTrue(any('kept clear the 1 area' in n and '1 tree' in n for n in d['adjustments']), d['adjustments'])
        self.assertEqual(len(r.data['site_survey']['items']), 2)

    def test_a_bad_survey_is_refused_and_edits_respect_the_marks(self):
        r = self.client.post(self.url, {**self.valid, 'site_survey': 'not json'}, format='json')
        self.assertEqual(r.status_code, 400)
        survey = {'items': [{'kind': 'clear', 'x0': 0, 'z0': 0, 'x1': 10, 'z1': 3}]}   # the whole back strip
        r = self.client.post('/api/layout-review/', {
            'plants': [{'plant_id': 2, 'x': 5, 'z': 1.5}], 'lot_width': 10, 'lot_length': 12, 'site_survey': survey,
        }, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(any(n['kind'] == 'keep_clear' for n in r.data['notes']), r.data['notes'])


class PhotoBasisTests(APITestCase):
    """The yard photo shapes the design: marks on it, what its reading found (accepted), its size or shape and its
    grass; the results and the proposal say so ("Based on your yard photo, we …")."""

    setUp = GenerateLayoutsViewTests.setUp

    PHOTO_SURVEY = {
        'items': [
            {'kind': 'clear', 'x0': 0, 'z0': 8, 'x1': 3, 'z1': 12, 'label': 'Patio', 'src': 'found'},
            {'kind': 'clear', 'x0': 6, 'z0': 10, 'x1': 7, 'z1': 12, 'label': 'Path', 'src': 'found'},
            {'kind': 'keep', 'x': 8, 'z': 2.5, 'r': 1.5, 'label': 'Mango tree', 'src': 'photo'},
            {'kind': 'entry', 'x': 5, 'z': 12, 'edge': 'front'},                      # marked on the plan
        ],
        'photo': {'lined_up': True, 'sized': True, 'shaped': False, 'grass': 0.72},
    }

    def test_parse_keeps_where_marks_came_from(self):
        from apps.library import site_survey as S
        s = S.parse(self.PHOTO_SURVEY, 10, 12)
        self.assertEqual([i.get('src') for i in s['items']], ['found', 'found', 'photo', None])
        self.assertEqual(s['photo'], {'lined_up': True, 'sized': True, 'shaped': False, 'grass': 0.72})
        # Unknown sources dropped; a photo never lined up counts as no photo; odd grass values cleaned
        odd = S.parse({'items': [{'kind': 'keep', 'x': 1, 'z': 1, 'r': 1, 'src': 'gemini'}],
                       'photo': {'lined_up': 'yes', 'grass': 3}}, 10, 12)
        self.assertNotIn('src', odd['items'][0])
        self.assertNotIn('photo', odd)
        self.assertEqual(S.parse({'photo': {'lined_up': True, 'grass': 'lots'}}, 10, 12)['photo']['grass'], None)
        self.assertEqual(S.parse({'photo': {'lined_up': True, 'grass': 7}}, 10, 12)['photo']['grass'], 1.0)

    def test_photo_facts_say_what_the_photo_did(self):
        from apps.library import site_survey as S
        s = S.parse(self.PHOTO_SURVEY, 10, 12)
        facts = S.photo_facts(s, new_lawn=False)
        self.assertEqual(facts, [
            'used the yard size your photo measured',
            'kept clear the patio and path we found in it',
            'planted around the 1 tree or plant you marked on it',
            'kept your existing lawn (about 72% of your yard looks like grass)',
        ])
        # New grass laid: the lawn isn't claimed; no photo: nothing at all
        self.assertNotIn('lawn', ' '.join(S.photo_facts(s, new_lawn=True)))
        self.assertEqual(S.photo_facts(S.parse({'items': self.PHOTO_SURVEY['items']}, 10, 12)), [])
        self.assertIsNone(S.photo_note(S.parse(None, 10, 12)))
        self.assertTrue(S.photo_note(s, new_lawn=False).startswith('Based on your yard photo, we used the yard size'))

    def test_designs_report_their_photo_basis(self):
        r = self.client.post(self.url, {**self.valid, 'site_survey': self.PHOTO_SURVEY, 'lawn': 'keep'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn('kept clear the patio and path we found in it', r.data['photo_basis'])
        self.assertTrue(any('existing lawn' in f for f in r.data['photo_basis']), r.data['photo_basis'])
        # The found patio is kept clear like any marked area
        for d in r.data['designs']:
            for p in d['plants']:
                if not p.get('is_existing'):
                    self.assertFalse(0 <= p['x'] <= 3 and 8 <= p['z'] <= 12, (d['design_name'], p))
        # No photo: no basis
        plain = self.client.post(self.url, self.valid, format='json')
        self.assertEqual(plain.data['photo_basis'], [])

    def test_proposal_says_what_came_from_the_photo(self):
        from apps.library.proposal import _photo_note
        dims = {'siteSurvey': self.PHOTO_SURVEY, 'gardenPlan': {'newLawn': False}}
        note = _photo_note(dims, 10, 12)
        self.assertTrue(note.startswith('Based on your yard photo, we'), note)
        self.assertIn('existing lawn', note)
        self.assertNotIn('existing lawn', _photo_note({'siteSurvey': self.PHOTO_SURVEY}, 10, 12))   # new grass
        self.assertIsNone(_photo_note({}, 10, 12))
        self.assertIsNone(_photo_note({'siteSurvey': 'not json'}, 10, 12))   # never breaks the proposal


class PhotoObjectsTests(APITestCase):
    """POST /api/photo-objects/: the AI's look at the yard photo (boxes on the picture), cached per photo, within
    the daily photo-scan limit, and never a dead end when the AI is out."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        keys = patch.dict(os.environ, {'GOOGLE_API_KEY': '', 'GEMINI_API_KEY': 'test_api_key', 'AI_MODE': 'auto'})
        keys.start()
        self.addCleanup(keys.stop)
        self.user = User.objects.create_user(username='photouser', password='pw')
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(self.user).access_token}')
        self.url = reverse('photo-objects')

    def _jpeg(self, colour=(90, 140, 60)):
        import io
        from PIL import Image
        out = io.BytesIO()
        Image.new('RGB', (320, 240), colour).save(out, 'JPEG')
        out.seek(0)
        out.name = 'yard.jpg'
        return out

    def _answer(self, text):
        response = MagicMock()
        response.text = text
        return response

    def test_clean_objects_keeps_only_good_entries(self):
        from apps.library.utils import clean_objects
        objects = clean_objects([
            {'kind': 'tree', 'label': 'mango   tree', 'box_2d': [100, 600, 700, 900]},
            {'kind': 'paving', 'label': 'Patio', 'box_2d': [650, 200, 900, 1200]},    # clamped to the picture
            {'kind': 'house', 'label': 'House', 'box_2d': [0, 0, 500, 500]},          # not a kind we plan around
            {'kind': 'shrub', 'label': 'Bush', 'box_2d': [10, 10, 12, 400]},          # too thin to be real
            {'kind': 'utility', 'label': 'Meter', 'box_2d': 'nowhere'},
            'junk',
        ])
        self.assertEqual(objects, [
            {'kind': 'tree', 'label': 'Mango tree', 'box': [100, 600, 700, 900]},
            {'kind': 'paving', 'label': 'Patio', 'box': [650, 200, 900, 1000]},
        ])

    @patch('google.genai.Client')
    def test_scan_returns_boxes_and_the_same_photo_is_free(self, client_class):
        client_class.return_value.models.generate_content.return_value = self._answer(
            '[{"kind": "tree", "label": "Mango tree", "box_2d": [100, 600, 700, 900]}]')
        r = self.client.post(self.url, {'image': self._jpeg()}, format='multipart')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data, {'status': 'ok', 'objects': [{'kind': 'tree', 'label': 'Mango tree', 'box': [100, 600, 700, 900]}]})
        again = self.client.post(self.url, {'image': self._jpeg()}, format='multipart')
        self.assertEqual(again.data['objects'], r.data['objects'])
        self.assertEqual(client_class.return_value.models.generate_content.call_count, 1)   # cached: no second call

    @patch('google.genai.Client')
    def test_out_of_quota_or_no_ai_is_never_an_error(self, client_class):
        from apps.library.garden_ai import ZoneScanThrottle, breaker_open
        client_class.return_value.models.generate_content.side_effect = Exception('429 RESOURCE_EXHAUSTED PerDay')
        r = self.client.post(self.url, {'image': self._jpeg()}, format='multipart')
        self.assertEqual(r.data, {'status': 'quota', 'objects': []})
        self.assertTrue(breaker_open())                     # paused: the next photo doesn't wait on Gemini
        r = self.client.post(self.url, {'image': self._jpeg((10, 10, 10))}, format='multipart')
        self.assertEqual(r.data['status'], 'unavailable')
        # The customer's daily photo scans used up
        from django.core.cache import cache
        cache.clear()
        with patch.object(ZoneScanThrottle, 'get_rate', return_value='1/day'):
            ZoneScanThrottle.rate, ZoneScanThrottle.num_requests = None, None
            client_class.return_value.models.generate_content.side_effect = None
            client_class.return_value.models.generate_content.return_value = self._answer('[]')
            self.assertEqual(self.client.post(self.url, {'image': self._jpeg((1, 2, 3))}, format='multipart').data['status'], 'ok')
            self.assertEqual(self.client.post(self.url, {'image': self._jpeg((4, 5, 6))}, format='multipart').data['status'], 'quota')
        ZoneScanThrottle.rate, ZoneScanThrottle.num_requests = None, None
        with patch.dict(os.environ, {'AI_MODE': 'engine_only'}):
            self.assertEqual(self.client.post(self.url, {'image': self._jpeg((7, 8, 9))}, format='multipart').data['status'], 'unavailable')

    def test_needs_a_signed_in_customer_and_a_real_photo(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        bad = self.client.post(self.url, {'image': SimpleUploadedFile('x.jpg', b'%PDF-1.4 not a photo', content_type='image/jpeg')},
                               format='multipart')
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self.client.post(self.url, {}, format='multipart').status_code, 400)
        self.client.credentials()
        self.assertEqual(self.client.post(self.url, {'image': self._jpeg()}, format='multipart').status_code, 401)

    def test_trees_found_in_the_photo_are_reported(self):
        from apps.library import site_survey as S
        s = S.parse({'items': [{'kind': 'keep', 'x': 8, 'z': 2.5, 'r': 2, 'label': 'Mango tree', 'src': 'found'},
                               {'kind': 'clear', 'x0': 0, 'z0': 8, 'x1': 3, 'z1': 12, 'label': 'Shed', 'src': 'found'}],
                     'photo': {'lined_up': True}}, 10, 12)
        self.assertEqual(S.photo_facts(s), ['kept clear the shed we found in it', 'planted around the mango tree we found in it'])


class SiteSurveyEngineTests(APITestCase):
    """Phase 2: the engine plans around the marks: paths from gates, screens, low views, the house setback."""

    setUp = GenerateLayoutsViewTests.setUp

    def _designs(self, items):
        r = self.client.post(self.url, {**self.valid, 'preferred_plant_ids': [], 'site_survey': {'items': items}}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        return r.data['designs']

    def _specs(self):
        from apps.library import layout_engine as E
        from apps.library.models import InventoryItem
        return {s.id: s for s in E.specs_from_inventory(InventoryItem.objects.all())}

    def test_a_side_gate_gets_a_clear_path_to_the_lawn(self):
        from apps.library import layout_engine as E
        specs = self._specs()
        for d in self._designs([{'kind': 'entry', 'x': 10, 'z': 9, 'edge': 'right'}]):
            plan = E.GardenPlan.from_dict(d['garden_plan'])
            self.assertTrue(plan.paths, d['design_name'])
            x0, x1, z0, z1 = plan.paths[0]
            self.assertAlmostEqual(x1, 10.0)                       # starts at the gate's fence…
            self.assertTrue(z0 <= 9 <= z1)                         # …at the gate
            self.assertTrue(plan.depth_inside(x0 + 0.01, 9) > -0.01 or len(plan.paths) == 2)   # and reaches the lawn
            for p in d['plants']:
                if not p.get('is_existing'):
                    self.assertFalse(plan.on_walk(specs[str(p['plant_id'])], p['x'], p['z']), (d['design_name'], p))
            self.assertTrue(any('path from your gate' in n for n in d['adjustments']), d['adjustments'])

    def test_a_front_door_moves_the_entry(self):
        from apps.library import layout_engine as E
        d = self._designs([{'kind': 'entry', 'x': 2.5, 'z': 12, 'edge': 'front'}])[0]   # Symmetrical
        plan = E.GardenPlan.from_dict(d['garden_plan'])
        self.assertIsNone(plan.entry)                               # not the default middle gap…
        x0, x1, z0, z1 = plan.paths[0]
        self.assertTrue(x0 <= 2.5 <= x1 and z1 == 12.0)            # …a path from the door

    def test_a_screen_plants_a_tall_row_along_the_back_fence(self):
        specs = self._specs()
        for d in self._designs([{'kind': 'screen', 'edge': 'back', 'a': 1, 'b': 9}]):
            row = [p for p in d['plants'] if not p.get('is_existing') and p['z'] <= 2.2
                   and specs[str(p['plant_id'])].layer in ('tall', 'mid') and 1 <= p['x'] <= 9]
            self.assertGreaterEqual(len(row), 2, d['design_name'])
            self.assertTrue(any('Screened the back fence' in n for n in d['adjustments']), d['adjustments'])

    def test_a_view_keeps_only_low_plants_and_nothing_crowds_the_house(self):
        from apps.library import layout_engine as E
        specs = self._specs()
        view = (3, 5, 7, 9)   # x0, z0, x1, z1
        for d in self._designs([{'kind': 'view', 'x0': view[0], 'z0': view[1], 'x1': view[2], 'z1': view[3]}]):
            for p in d['plants']:
                if p.get('is_existing'):
                    continue
                spec = specs[str(p['plant_id'])]
                if view[0] <= p['x'] <= view[2] and view[1] <= p['z'] <= view[3]:
                    self.assertEqual(spec.layer, 'low', (d['design_name'], p))
                self.assertLessEqual(p['z'], 12 - E.HOUSE_SETBACK + 1e-6, (d['design_name'], p))

    def test_review_explains_the_new_rules(self):
        from apps.library import layout_engine as E
        specs = list(self._specs().values())
        palm = next(s for s in specs if s.layer == 'tall')
        site = E.Site(10, 12, rules=E.SiteRules(views=((0, 0, 10, 12),)))
        kinds = {n['kind'] for n in E.review([{'plant_id': palm.id, 'x': 5, 'z': 11.8}], specs, site)}
        self.assertTrue({'house', 'view'} <= kinds, kinds)


class SunMapTests(APITestCase):
    """Phase 3: sun and shade per spot, from which way the yard faces, the house, walls and kept trees."""

    setUp = GenerateLayoutsViewTests.setUp

    def test_the_house_shades_the_yard_only_when_the_sun_is_behind_it(self):
        from apps.library import sun_map as S
        # Back of the yard faces north: the house is south of it, between the yard and the (mostly southern) sun
        north = S.hours_grid(10.0, 10.0, 0.0, S.HOUSE_HEIGHTS['two'])
        south = S.hours_grid(10.0, 10.0, 180.0, S.HOUSE_HEIGHTS['two'])
        by_house = lambda g: sum(g[-1]) / len(g[-1])   # noqa: E731  the row next to the house
        self.assertLess(by_house(north), S.SHADE_H)
        self.assertGreater(by_house(south), S.FULL_SUN_H)
        # No house: nothing on that side shades the yard
        self.assertGreater(by_house(S.hours_grid(10.0, 10.0, 0.0, 0.0)), by_house(north) + 2)
        # A kept tree shades the ground under it
        tree = S.hours_grid(10.0, 10.0, 180.0, 0.0, ((5.0, 5.0, 2.5),))
        self.assertLess(tree[10][10], S.hours_grid(10.0, 10.0, 180.0, 0.0)[10][10] - 1)

    def test_the_sun_map_endpoint(self):
        self.client.credentials()   # works before signing in
        r = self.client.post('/api/sun-map/', {'lot_width': 8, 'lot_length': 10,
                                               'site_survey': {'items': [], 'sun': {'facing': 0, 'house': 'one'}}}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(len(r.data['hours']), r.data['grid'])
        self.assertAlmostEqual(sum(r.data['summary'].values()), 1.0, places=1)
        r = self.client.post('/api/sun-map/', {'lot_width': 8, 'lot_length': 10, 'site_survey': {'items': []}}, format='json')
        self.assertEqual(r.status_code, 400)

    def test_designs_put_each_plant_in_the_light_it_needs(self):
        from apps.library import layout_engine as E, site_survey as S
        from apps.library.models import InventoryItem
        survey = {'items': [], 'sun': {'facing': 0, 'house': 'two'}}   # deep shade along the house
        r = self.client.post(self.url, {**self.valid, 'preferred_plant_ids': [], 'site_survey': survey}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        lot_w, lot_l = float(self.valid['lot_width']), float(self.valid['lot_length'])
        site = E.Site(lot_w, lot_l, rules=S.rules(S.parse(survey, lot_w, lot_l), lot_w, lot_l))
        specs = {s.id: s for s in E.specs_from_inventory(InventoryItem.objects.all())}
        for d in r.data['designs']:
            for p in d['plants']:
                if not p.get('is_existing'):
                    self.assertTrue(E.sun_suits(specs[str(p['plant_id'])], p['x'], p['z'], site, d['plants'], specs),
                                    (d['design_name'], p, site.sun_hours(p['x'], p['z'])))
            self.assertTrue(any('Sun and shade worked out' in n for n in d['adjustments']), d['adjustments'])

    def test_review_says_when_a_plant_is_in_the_wrong_light(self):
        from apps.library import layout_engine as E, sun_map as S
        from apps.library.models import InventoryItem
        sunny = next(s for s in E.specs_from_inventory(InventoryItem.objects.all()) if s.is_plant and s.sunlight == 'full_sun')
        site = E.Site(10, 10, rules=E.SiteRules(sun=S.hours_grid(10.0, 10.0, 0.0, S.HOUSE_HEIGHTS['two'])))
        notes = E.review([{'plant_id': sunny.id, 'x': 5, 'z': 9.2}], [sunny], site)
        self.assertIn('sun', {n['kind'] for n in notes}, notes)


class YardShapeTests(APITestCase):
    """Part 1 of shaped yards: the customer's own outline (L-shape, narrower at the back, cut corner…)."""

    setUp = GenerateLayoutsViewTests.setUp
    # 10 × 12 lot; the house takes the front-right corner (4 m × 5 m): the yard is an L around it
    L_SHAPE = [{'x': 0, 'z': 0}, {'x': 10, 'z': 0}, {'x': 10, 'z': 7, 'house': True}, {'x': 6, 'z': 7, 'house': True},
               {'x': 6, 'z': 12, 'house': True}, {'x': 0, 'z': 12}]

    def test_outlines_are_checked(self):
        from apps.library import lot_shape as S
        self.assertIsNone(S.clean(None, 10, 12))
        self.assertIsNone(S.clean([[0, 0], [10, 0], [10, 12], [0, 12]], 10, 12))       # just the box
        pts, house = S.clean([[0, 0], [10, 0], [7, 12], [3, 12]], 10, 12)               # narrower at the house
        self.assertEqual(house, (False, False, True, False))                          # z = L side by default
        with self.assertRaises(S.ShapeError):
            S.clean([[0, 0], [10, 12], [10, 0], [0, 12]], 10, 12)                      # sides cross
        with self.assertRaises(S.ShapeError):
            S.clean([[0, 0], [1, 0], [0, 1]], 10, 12)                                  # too small
        self.assertEqual(S.area([(0, 0), (10, 0), (10, 7), (6, 7), (6, 12), (0, 12)]), 100.0)

    def test_designs_stay_inside_the_shape_and_off_the_house_walls(self):
        from apps.library import layout_engine as E, site_survey as SS
        from apps.library.models import InventoryItem
        survey = {'items': [], 'outline': self.L_SHAPE}
        r = self.client.post(self.url, {**self.valid, 'preferred_plant_ids': [], 'site_survey': survey}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        site = E.Site(10, 12, rules=SS.rules(SS.parse(survey, 10, 12), 10, 12))
        self.assertEqual(site.area, 100.0)
        specs = {s.id: s for s in E.specs_from_inventory(InventoryItem.objects.all())}
        for d in r.data['designs']:
            plants = [p for p in d['plants'] if not p.get('is_existing')]
            self.assertTrue(plants, d['design_name'])
            for p in plants:
                spec = specs[str(p['plant_id'])]
                self.assertTrue(site.in_bounds(spec, p['x'], p['z']), (d['design_name'], p))
                self.assertFalse(site.near_house(spec, p['x'], p['z']), (d['design_name'], p))
            plan = E.GardenPlan.from_dict(d['garden_plan'])
            self.assertEqual(len(plan.outline), 6)                                    # the views get the shape
            if plan.lawn:
                for x, z in ((plan.lawn[1], plan.lawn[3]), (plan.lawn[2], plan.lawn[4])) if plan.lawn[0] == 'rect' else ():
                    self.assertTrue(site.inside(x, z), (d['design_name'], plan.lawn))
            self.assertTrue(any("your yard's own shape" in n for n in d['adjustments']), d['adjustments'])

    def test_price_uses_the_real_area(self):
        from unittest import mock
        from apps.library import garden_ai
        with mock.patch.object(garden_ai, 'project_estimate', wraps=garden_ai.project_estimate) as est:
            r = self.client.post(self.url, {**self.valid, 'preferred_plant_ids': [],
                                            'site_survey': {'items': [], 'outline': self.L_SHAPE}}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(est.called)
        self.assertEqual({c.args[2] for c in est.call_args_list}, {100.0})     # not the 10 × 12 = 120 m² box

    def test_sun_map_leaves_out_what_is_outside(self):
        from apps.library import sun_map as M
        rows = M.hours_grid(10.0, 12.0, 180.0, 6.5, (), 20, ((0, 0), (10, 0), (10, 7), (6, 7), (6, 12), (0, 12)),
                            (((10, 7), (6, 7)), ((6, 7), (6, 12))))
        self.assertIsNone(rows[19][19])          # front right: the house
        self.assertIsNotNone(rows[19][0])
        # The house's back wall (z = 7) shades the yard behind it when the sun comes from the house side
        # (back of the yard faces north: the sun is mostly in the south, behind the house)
        shape = ((0, 0), (10, 0), (10, 7), (6, 7), (6, 12), (0, 12))
        walls = (((10, 7), (6, 7)), ((6, 7), (6, 12)))
        tall = M.hours_grid(10.0, 12.0, 0.0, 6.5, (), 20, shape, walls)
        none = M.hours_grid(10.0, 12.0, 0.0, 0.0, (), 20, shape, walls)
        self.assertLess(tall[10][16], none[10][16] - 1)    # x 8.25, z 6.3: just behind the house


from apps.library.models import InventoryItem, UploadedModel  # noqa: E402


def _glb(gltf=None, total_pad=0):
    """A minimal binary glTF: header + JSON chunk (+ optional padding bytes in a BIN chunk)."""
    import json as _json
    import struct as _struct
    body = _json.dumps(gltf if gltf is not None else
                       {'asset': {'version': '2.0'}, 'meshes': [{'primitives': []}]}).encode()
    body += b' ' * (-len(body) % 4)
    chunks = _struct.pack('<II', len(body), 0x4E4F534A) + body
    if total_pad:
        chunks += _struct.pack('<II', total_pad, 0x004E4942) + b'\0' * total_pad
    return b'glTF' + _struct.pack('<II', 2, 12 + len(chunks)) + chunks


# Server-disk mode, whatever R2 keys the developer's .env holds
@override_settings(MEDIA_ROOT=TEMP_MEDIA, R2_ENABLED=False, MAX_MODEL_UPLOAD_BYTES=5 * 1024 * 1024,
                   MAX_TOTAL_MODEL_BYTES=150 * 1024 * 1024)
class ModelUploadTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(username='modeladmin', password='password123')
        self.customer = User.objects.create_user(username='modelcustomer', password='password123')

    def upload(self, data, name='Fern.glb', label='Boston Fern', category='plant'):
        from django.core.files.uploadedfile import SimpleUploadedFile
        return self.client.post('/api/inventory-models/', {
            'file': SimpleUploadedFile(name, data, content_type='model/gltf-binary'),
            'label': label, 'category': category}, format='multipart')

    def test_office_uploads_and_file_is_served_with_cors(self):
        self.client.force_authenticate(self.admin)
        res = self.upload(_glb(total_pad=64))
        self.assertEqual(res.status_code, 201, res.data)
        name = res.data['file_name']
        self.assertRegex(name, r'^boston-fern-[0-9a-f]{6}\.glb$')
        self.client.force_authenticate(None)
        got = self.client.get(f'/api/model-files/{name}', HTTP_ORIGIN='https://example.vercel.app')
        self.assertEqual(got.status_code, 200)
        self.assertEqual(b''.join(got.streaming_content)[:4], b'glTF')
        self.assertEqual(got['Content-Type'], 'model/gltf-binary')
        self.assertIn('access-control-allow-origin', {k.lower() for k in got.headers})

    def test_item_can_use_uploaded_model_and_blocks_its_delete(self):
        self.client.force_authenticate(self.admin)
        res = self.upload(_glb())
        name, pk = res.data['file_name'], res.data['id']
        InventoryItem.objects.create(name='Fern', category='plant', unit_price=100, model_file=name)
        self.assertEqual(self.client.delete(f'/api/inventory-models/{pk}/').status_code, 409)
        InventoryItem.objects.filter(model_file=name).update(model_file='')
        self.assertEqual(self.client.delete(f'/api/inventory-models/{pk}/').status_code, 204)
        self.assertEqual(self.client.get(f'/api/model-files/{name}').status_code, 404)

    def test_rejects_non_glb_and_unsupported_models(self):
        self.client.force_authenticate(self.admin)
        self.assertIn('not a .glb', self.upload(b'PK\x03\x04' + b'\0' * 40).data['file'])
        self.assertIn('separate files', self.upload(_glb({'asset': {'version': '2.0'}, 'meshes': [{}],
                                                          'images': [{'uri': 'leaf.png'}]})).data['file'])
        self.assertIn('Draco', self.upload(_glb({'asset': {'version': '2.0'}, 'meshes': [{}],
                                                 'extensionsRequired': ['KHR_draco_mesh_compression']})).data['file'])
        self.assertIn('no 3D shapes', self.upload(_glb({'asset': {'version': '2.0'}})).data['file'])
        self.assertEqual(self.upload(b'glTF' + b'\0' * 40, name='a.txt').status_code, 400)
        self.assertEqual(UploadedModel.objects.count(), 0)

    def test_size_limits(self):
        self.client.force_authenticate(self.admin)
        with self.settings(MAX_MODEL_UPLOAD_BYTES=100):
            self.assertIn('the limit is', self.upload(_glb(total_pad=200)).data['file'])
        with self.settings(MAX_TOTAL_MODEL_BYTES=100):
            self.assertIn('Not enough space', self.upload(_glb(total_pad=200)).data['file'])

    def test_customers_cannot_upload_or_list(self):
        self.client.force_authenticate(self.customer)
        self.assertEqual(self.upload(_glb()).status_code, 403)
        self.assertEqual(self.client.get('/api/inventory-models/').status_code, 403)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get('/api/model-files/../../db.sqlite3').status_code, 404)


R2_TEST = dict(R2_ENABLED=True, R2_ACCOUNT_ID='acc123', R2_ACCESS_KEY_ID='AKID', R2_SECRET_ACCESS_KEY='secret',
               R2_BUCKET='models-bucket', R2_PUBLIC_URL='https://pub-test.r2.dev')


class R2SignerTests(APITestCase):
    def test_matches_aws_reference_signature(self):
        # AWS's published SigV4 query-string example (S3 docs: "Authenticating Requests: Using Query Parameters")
        from datetime import datetime, timezone
        from apps.library.r2 import presign_url
        url = presign_url('GET', 'examplebucket.s3.amazonaws.com', '/test.txt', 'AKIAIOSFODNN7EXAMPLE',
                          'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY', region='us-east-1', expires=86400,
                          now=datetime(2013, 5, 24, tzinfo=timezone.utc))
        self.assertTrue(url.endswith('X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404'))


@override_settings(MEDIA_ROOT=TEMP_MEDIA, **R2_TEST)
class R2ModelUploadTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(username='r2admin', password='password123')
        self.client.force_authenticate(self.admin)

    def start(self, size=1000, label='Areca Palm'):
        return self.client.post('/api/inventory-models/upload-url/',
                                {'label': label, 'category': 'plant', 'size': size, 'file_name': 'areca.glb'}, format='json')

    def fake_get(self, body, code=200):
        res = MagicMock(status_code=code, content=body, text='')
        return patch('apps.library.model_uploads.requests.get', return_value=res)

    def test_direct_upload_flow(self):
        self.assertEqual(self.client.get('/api/inventory-models/').data['storage'], 'r2')
        start = self.start(size=len(_glb()))
        self.assertEqual(start.status_code, 200, start.data)
        self.assertTrue(start.data['upload_url'].startswith('https://acc123.r2.cloudflarestorage.com/models-bucket/models/areca-palm-'))
        self.assertIn('X-Amz-Signature=', start.data['upload_url'])
        with self.fake_get(_glb()):
            done = self.client.post('/api/inventory-models/confirm/', {'token': start.data['token']}, format='json')
        self.assertEqual(done.status_code, 201, done.data)
        self.assertEqual(done.data['storage'], 'r2')
        name = done.data['file_name']
        self.client.force_authenticate(None)
        got = self.client.get(f'/api/model-files/{name}', HTTP_ORIGIN='https://example.vercel.app')
        self.assertEqual(got.status_code, 302)
        self.assertEqual(got['Location'], f'https://pub-test.r2.dev/models/{name}')
        self.assertIn('access-control-allow-origin', {k.lower() for k in got.headers})

    def test_bad_file_in_r2_is_deleted_and_refused(self):
        start = self.start(size=40)
        with self.fake_get(b'PK' + bytes(38)), patch('apps.library.model_uploads.requests.delete') as delete:
            done = self.client.post('/api/inventory-models/confirm/', {'token': start.data['token']}, format='json')
        self.assertEqual(done.status_code, 400)
        self.assertIn('not a .glb', done.data['file'])
        delete.assert_called_once()
        self.assertEqual(UploadedModel.objects.count(), 0)

    def test_unreachable_r2_trusts_browser_checks(self):
        import requests as _requests
        start = self.start(size=1234)
        with patch('apps.library.model_uploads.requests.get', side_effect=_requests.ConnectionError('proxy')):
            done = self.client.post('/api/inventory-models/confirm/', {'token': start.data['token']}, format='json')
        self.assertEqual(done.status_code, 201)
        self.assertEqual(done.data['size_bytes'], 1234)

    def test_missing_upload_and_forged_token(self):
        start = self.start()
        with self.fake_get(b'', code=404):
            self.assertIn('didn’t arrive', self.client.post('/api/inventory-models/confirm/',
                                                            {'token': start.data['token']}, format='json').data['file'])
        self.assertEqual(self.client.post('/api/inventory-models/confirm/', {'token': 'x:y:z'}, format='json').status_code, 400)

    def test_limits_and_server_upload_blocked(self):
        with self.settings(MAX_MODEL_UPLOAD_BYTES=500):
            self.assertIn('the limit is', self.start(size=600).data['file'])
        from django.core.files.uploadedfile import SimpleUploadedFile
        res = self.client.post('/api/inventory-models/', {'file': SimpleUploadedFile('a.glb', _glb()), 'label': 'A'}, format='multipart')
        self.assertEqual(res.status_code, 400)

    def test_delete_removes_from_r2(self):
        m = UploadedModel.objects.create(label='Old', category='plant', file_name='old-abc123.glb', size_bytes=10, storage='r2')
        with patch('apps.library.model_uploads.requests.delete', return_value=MagicMock(status_code=204)) as delete:
            self.assertEqual(self.client.delete(f'/api/inventory-models/{m.id}/').status_code, 204)
        self.assertIn('/models-bucket/models/old-abc123.glb?', delete.call_args[0][0])


def _jpeg_bytes():
    import io as _io
    from PIL import Image as _Image
    buf = _io.BytesIO()
    _Image.new('RGB', (8, 8), (40, 160, 90)).save(buf, format='JPEG')
    return buf.getvalue()


@override_settings(MEDIA_ROOT=TEMP_MEDIA)
class FileCheckTests(APITestCase):
    """Uploads are judged by their bytes, never by file name or the type the browser claims."""

    def setUp(self):
        from django.utils import timezone
        from apps.library.models import ServiceBooking, StaffProfile
        self.crew = User.objects.create_user(username='fc-crew', password='pw', is_staff=True)
        StaffProfile.objects.create(user=self.crew, role='FIELD_CREW')
        self.admin = User.objects.create_superuser(username='fc-admin', password='pw')
        customer = User.objects.create_user(username='fc-cust', password='pw')
        self.booking = ServiceBooking.objects.create(user=customer, service_type='Softscape',
                                                     scheduled_date=timezone.localdate(), status='Preparing')
        self.booking.assigned_crew.add(self.crew)

    def clock_in(self, photo):
        self.client.force_authenticate(self.crew)
        return self.client.post('/api/attendance/clock_in/', {'booking_id': self.booking.id, 'photo': photo}, format='json')

    def test_attendance_photo_cannot_smuggle_html(self):
        import base64 as _b64
        from apps.library.models import Attendance
        page = _b64.b64encode(b'<script>alert(1)</script>').decode()
        res = self.clock_in(f'data:image/html;base64,{page}')
        self.assertEqual(res.status_code, 400)
        self.assertIn('isn’t a photo', res.data['error'])
        self.assertFalse(Attendance.objects.exists())   # a refused photo leaves no half clock-in behind

    def test_attendance_photo_saved_as_clean_jpeg(self):
        import base64 as _b64
        from apps.library.models import Attendance
        res = self.clock_in('data:image/png;base64,' + _b64.b64encode(_jpeg_bytes()).decode())
        self.assertEqual(res.status_code, 201, res.data)
        self.assertTrue(Attendance.objects.get().clock_in_photo_url.name.endswith('.jpg'))

    def test_garden_photo_endpoints_refuse_renamed_files(self):
        self.client.force_authenticate(self.admin)
        fake = SimpleUploadedFile('yard.jpg', b'%PDF-1.4 not a photo', content_type='image/jpeg')
        res = self.client.post('/api/generate-depth/', {'image': fake}, format='multipart')
        self.assertEqual(res.status_code, 400)
        self.assertIn('PDF', res.json()['error'])
        heic = SimpleUploadedFile('yard.jpg', b'\x00\x00\x00\x18ftypheic' + b'\x00' * 40, content_type='image/jpeg')
        res = self.client.post('/api/scan-zones/', {'image': heic}, format='multipart')
        self.assertEqual(res.status_code, 400)
        self.assertIn('HEIC', res.data['error'])

    def test_credentials_checked_by_content(self):
        self.client.force_authenticate(self.admin)
        url = '/api/business-credentials/'
        for name, body, ok in (('permit.pdf', b'MZ\x90\x00 exe renamed', False),
                               ('permit.pdf', _jpeg_bytes(), False),
                               ('permit.jpg', _jpeg_bytes(), True),
                               ('permit.pdf', b'%PDF-1.7 real', True)):
            res = self.client.post(url, {'title': 'Permit', 'kind': 'registration',
                                         'document': SimpleUploadedFile(name, body)}, format='multipart')
            self.assertEqual(res.status_code == 201, ok, (name, res.data))

    def test_product_photo_explains_heic(self):
        self.client.force_authenticate(self.admin)
        heic = SimpleUploadedFile('fern.jpg', b'\x00\x00\x00\x18ftypheic' + b'\x00' * 40, content_type='image/jpeg')
        res = self.client.post('/api/inventory/', {'name': 'Fern', 'category': 'plant', 'unit_price': '100',
                                                   'thumbnail': heic}, format='multipart')
        self.assertEqual(res.status_code, 400)
        self.assertIn('HEIC', str(res.data['thumbnail']))


class PlantLooksTests(APITestCase):
    """Looks (flower colour, leaf colour, shape, design role): the AI Designer combines plants for a pleasing design.
    One flower-colour family per design, contrasting shapes side by side, each plant in its job; soft preferences
    that never drop the customer's own picks and change nothing for plants with no looks set."""

    def _specs(self):
        from apps.library import layout_engine as E
        S = lambda i, n, p, h, m, **kw: E.PlantSpec(i, n, p, 200, h, E.spacing_for(m), mature=m, **kw)
        return [
            S('fan', 'Fan palm', 900, 2.0, 6.0, flower='none', form='palm', role='specimen'),
            S('bamboo', 'Bamboo', 1200, 5.0, 8.0, flower='none', form='upright', role='structure'),
            S('red', 'Red shrub', 150, 1.2, 1.8, flower='red', form='mounding', role='filler'),
            S('orange', 'Orange bold', 300, 1.2, 1.5, flower='orange', form='bold', role='accent'),
            S('pink', 'Pink shrub', 150, 1.2, 1.8, flower='pink', form='mounding', role='filler'),
            S('edge', 'Red edging', 80, 0.4, 0.5, flower='red', form='mounding', role='edging'),
            S('lilac', 'Purple edging', 80, 0.4, 0.5, flower='purple', form='grassy', role='edging'),
        ]

    def test_one_flower_colour_family_per_design(self):
        from apps.library import layout_engine as E
        specs, by = self._specs(), None
        by = {s.id: s for s in specs}
        for seed in range(6):
            for style in E.STYLES:
                d = E.generate(style, specs, E.Site(10, 12), 60000, seed=seed)
                families = {E.flower_family(by[p['plant_id']]) for p in d['plants']} - {None}
                self.assertLessEqual(len(families), 1, (style, seed, families))
                self.assertEqual(E.check(d['plants'], specs, E.Site(10, 12), 60000,
                                         E.GardenPlan.from_dict(d['garden_plan'])), [], (style, seed))

    def test_customer_picks_are_never_filtered_out(self):
        from apps.library import layout_engine as E
        specs = self._specs()
        for style in E.STYLES:
            # Picked a hot and a cool flower: both appear, the design doesn't pick a side
            d = E.generate(style, specs, E.Site(10, 12), 60000, preferred_ids=['red', 'pink'], seed=2)
            used = {p['plant_id'] for p in d['plants']}
            self.assertTrue({'red', 'pink'} <= used, (style, used))

    def test_roles(self):
        from apps.library import layout_engine as E
        specs = self._specs()
        lay = E.Layout(E.Site(10, 12), specs)
        # The specimen is the plant meant as one, not the dearer bamboo screen
        self.assertEqual(E._specimen(specs, set(), lay).id, 'fan')
        # Screens are made of structure plants; edging plants run along the border
        self.assertEqual(E._screen_species(specs, 'tall', set(), lay).id, 'bamboo')
        import random
        pick = E._pick(specs, 'low', set(), lay, random.Random(0), role='edging')
        self.assertIn(pick.id, {'edge', 'lilac'})
        # No looks set anywhere: chosen exactly as before (the dearest tall plant is the specimen)
        plain = [E.PlantSpec(s.id, s.name, s.price, s.stock, s.height, s.spacing, mature=s.mature) for s in specs]
        self.assertEqual(E._specimen(plain, set(), E.Layout(E.Site(10, 12), plain)).id, 'bamboo')

    def test_palette_contrasts_shapes(self):
        import random
        from apps.library import layout_engine as E
        # (no pink: a pink first pick would rightly stand alone, the cool family having no other mid plant)
        specs = [s for s in self._specs() if s.id != 'pink']
        for seed in range(10):
            lay = E.Layout(E.Site(10, 12), specs)
            mids = E._palette(specs, 'mid', set(), random.Random(seed), lay, role='filler')
            self.assertEqual(len(mids), 2)
            self.assertNotEqual(mids[0].form, mids[1].form, seed)   # a mound and a bold plant, not two mounds

    def test_inventory_api_and_sample_command(self):
        from io import StringIO
        from django.core.management import call_command
        from apps.library.models import InventoryItem, StaffProfile
        office = User.objects.create_user(username='looksoffice', password='pw', is_staff=True)
        StaffProfile.objects.create(user=office, role='OFFICE_ADMIN')
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(office).access_token}')
        plant = InventoryItem.objects.create(name='Hibiscus', category='plant', unit_price=150, stock_quantity=3)
        url = f'/api/inventory/{plant.id}/'
        r = self.client.patch(url, {'flower_color': 'pink', 'plant_form': 'mounding'}, format='multipart')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual((r.data['flower_color'], r.data['plant_form'], r.data['design_role']), ('pink', 'mounding', ''))
        self.assertEqual(self.client.patch(url, {'flower_color': 'blue'}, format='multipart').status_code, 400)
        # The sample command fills only what's empty: the owner's pink hibiscus stays pink
        call_command('load_sample_prices', stdout=StringIO())
        plant.refresh_from_db()
        self.assertEqual((plant.flower_color, plant.foliage_color, plant.plant_form, plant.design_role),
                         ('pink', 'dark_green', 'mounding', 'filler'))
        # …and the years until fully grown, only where empty: the owner's own value stays
        self.assertEqual(plant.years_to_mature, 3)
        plant.years_to_mature = 5
        plant.save(update_fields=['years_to_mature'])
        call_command('load_sample_prices', stdout=StringIO())
        plant.refresh_from_db()
        self.assertEqual(plant.years_to_mature, 5)
        # …and a sample specimen size, also only where empty
        self.assertEqual((int(plant.specimen_price), plant.specimen_height), (450, 1.2))
        # Groundcovers get theirs too: the years until they close into a carpet (fast mani-mani, slow mondo)
        years = dict(InventoryItem.objects.filter(is_groundcover=True).values_list('name', 'years_to_mature'))
        self.assertEqual(years.get('Mani-mani (Arachis pintoi)'), 1)
        self.assertEqual(years.get('Mondo grass'), 3)
        # Hardscape never keeps looks
        rock = InventoryItem.objects.create(name='Rock', category='hardscape', unit_price=400, stock_quantity=3)
        r = self.client.patch(f'/api/inventory/{rock.id}/', {'flower_color': 'red'}, format='multipart')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['flower_color'], '')

    def test_ai_prompt_lists_looks(self):
        from apps.library import garden_ai, layout_engine as E
        spec = self._specs()[3]
        self.assertEqual(garden_ai._looks(spec), ', flowers orange, bold, role accent')
        self.assertEqual(garden_ai._looks(E.PlantSpec('x', 'X', 1, 1, 1.0, 1.0)), '')



class FeatureTypeTests(APITestCase):
    """Pots, garden lights and water features (InventoryItem.feature_type): placed the way a designer places them —
    a water feature where the eye lands across the lawn, pots framing the way in and the seating, lights along the
    walkways and round the lawn — inside every layout rule, with limits by yard size and a power-point note."""

    def _specs(self):
        from apps.library import layout_engine as E
        S = lambda i, n, p, h, m: E.PlantSpec(i, n, p, 300, h, E.spacing_for(m), mature=m)
        plants = [S('palm', 'Palm', 900, 2.0, 6.0), S('shrub', 'Shrub', 150, 1.2, 1.8), S('edge', 'Edging', 80, 0.4, 0.5)]
        F = lambda i, n, k, size: E.PlantSpec(i, n, 500, 60, size, E.feature_spacing(k, size), kind=k)
        feats = {k: F(k, n, k, size) for k, n, size in (('seat', 'Bench', 0.9), ('pot', 'Urn', 0.7),
                                                       ('light', 'Path light', 0.5), ('water', 'Fountain', 1.2),
                                                       ('rock', 'Rock', 0.6))}
        return plants, feats

    def test_feature_kind_by_type_else_category(self):
        from types import SimpleNamespace as N
        from apps.library import layout_engine as E
        self.assertEqual(E.feature_kind(N(category='furniture', feature_type='')), 'seat')
        self.assertEqual(E.feature_kind(N(category='hardscape', feature_type='')), 'rock')
        self.assertEqual(E.feature_kind(N(category='furniture', feature_type='pot')), 'pot')
        self.assertEqual(E.feature_kind(N(category='hardscape', feature_type='water')), 'water')

    def test_every_style_places_them_within_the_rules(self):
        from apps.library import layout_engine as E
        plants, f = self._specs()
        feats = [(f['seat'], 1), (f['pot'], 4), (f['light'], 8), (f['water'], 1), (f['rock'], 3)]
        every = plants + [s for s, _ in feats]
        for site in (E.Site(10, 12), E.Site(16, 20), E.Site(3.5, 3.5)):
            for style in E.STYLES:
                for seed in (0, 1):
                    d = E.generate(style, plants, site, 80000, seed=seed, features=feats)
                    plan = E.GardenPlan.from_dict(d['garden_plan'])
                    self.assertEqual(E.check(d['plants'], every, site, 80000, plan), [], (site.width, style, seed))
                    if site.width >= 10:
                        ids = [p['plant_id'] for p in d['plants']]
                        self.assertEqual((ids.count('water'), ids.count('pot'), ids.count('light')), (1, 4, 8),
                                         (site.width, style, seed))
                        self.assertTrue(any('power point' in n for n in d['feature_notes']), d['feature_notes'])

    def test_where_they_go(self):
        from apps.library import layout_engine as E
        plants, f = self._specs()
        site = E.Site(16, 20)
        d = E.generate('Symmetrical', plants, site, 80000, seed=1, features=[(f['water'], 1), (f['pot'], 2), (f['light'], 6)])
        plan = E.GardenPlan.from_dict(d['garden_plan'])
        cx = (plan.lawn[1] + plan.lawn[2]) / 2
        water = [p for p in d['plants'] if p['plant_id'] == 'water'][0]
        # The fountain heads the formal axis, in the back bed seen across the lawn
        self.assertAlmostEqual(water['x'], cx, delta=0.3)
        self.assertLess(water['z'], plan.lawn[3])
        # A matching pair of specimens frames it (the axis is taken), mirrored about the axis
        palms = sorted((p for p in d['plants'] if p['plant_id'] == 'palm'), key=lambda p: p['x'])
        self.assertGreaterEqual(len(palms), 2)
        self.assertAlmostEqual(palms[0]['x'] + palms[-1]['x'], 2 * cx, delta=0.05)
        # The pair of pots frames the way in from the house, either side of the gap in the front border
        x0, x1, z0, z1 = plan.entry
        pots = sorted((p for p in d['plants'] if p['plant_id'] == 'pot'), key=lambda p: p['x'])
        self.assertLess(pots[0]['x'], x0)
        self.assertGreater(pots[1]['x'], x1)
        self.assertTrue(all(z0 - 1.3 <= p['z'] <= z1 + 1.3 for p in pots), pots)
        # Lights: the first ones line the way in, then round the lawn (in the beds, never on it)
        lights = [p for p in d['plants'] if p['plant_id'] == 'light']
        self.assertEqual(len(lights), 6)
        self.assertTrue(any(z0 - 0.5 <= p['z'] <= z1 + 0.5 and abs(p['x'] - (x0 + x1) / 2) < 2.0 for p in lights))
        self.assertFalse([p for p in lights if plan.depth_inside(p['x'], p['z']) > 0])

    def test_lights_and_rocks_leave_room_for_tall_plants(self):
        """A plant may spread over a light, a pot or a boulder at its foot: the tall layer survives them."""
        from apps.library import layout_engine as E
        plants, f = self._specs()
        for style in E.STYLES:
            d = E.generate(style, plants, E.Site(10, 12), 80000, seed=1, features=[(f['light'], 10), (f['rock'], 3)])
            self.assertIn('palm', [p['plant_id'] for p in d['plants']], style)
        self.assertLess(E.min_distance(f['light'], plants[0]), 0.6)
        self.assertEqual(E.min_distance(f['water'], plants[0]), (f['water'].spacing + plants[0].spacing) / 2)

    def test_api_limits_and_seat_count_by_kind(self):
        from apps.library.models import InventoryItem
        from apps.library.garden_ai import feature_limits
        self.assertEqual(feature_limits(120), {'pot': 12, 'light': 20, 'water': 1})
        self.assertEqual(feature_limits(20), {'pot': 2, 'light': 4, 'water': 1})
        self.assertEqual(feature_limits(200)['water'], 2)
        user = User.objects.create_user(username='featuser', password='pw')
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')
        InventoryItem.objects.create(id=1, name='Palm', category='plant', unit_price=800, stock_quantity=10, real_world_size=4.0)
        InventoryItem.objects.create(id=2, name='Rose', category='plant', unit_price=200, stock_quantity=60, real_world_size=0.5)
        InventoryItem.objects.create(id=60, name='Bench', category='furniture', unit_price=3000, stock_quantity=4,
                                     real_world_size=0.8, model_file='outdoor_bench.glb')
        InventoryItem.objects.create(id=61, name='Glazed urn', category='furniture', feature_type='pot', unit_price=900,
                                     stock_quantity=10, real_world_size=0.7, model_file='urn.glb')
        InventoryItem.objects.create(id=62, name='Path light', category='hardscape', feature_type='light', unit_price=400,
                                     stock_quantity=40, real_world_size=0.3, model_file='path_light.glb')
        InventoryItem.objects.create(id=63, name='Fountain', category='hardscape', feature_type='water', unit_price=8000,
                                     stock_quantity=3, real_world_size=1.2, model_file='fountain.glb')
        url = reverse('generate-layouts')
        base = {'budget': 120000, 'lot_width': 10, 'lot_length': 12}
        # Two seats and four pots (furniture, but pots): not "too many seats"
        r = self.client.post(url, {**base, 'features': [{'id': 60, 'quantity': 2}, {'id': 61, 'quantity': 4},
                                                         {'id': 62, 'quantity': 6}, {'id': 63, 'quantity': 1}]}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        for d in r.data['designs']:
            ids = [p['plant_id'] for p in d['plants']]
            self.assertEqual((ids.count('60'), ids.count('61'), ids.count('62'), ids.count('63')), (2, 4, 6, 1), d['design_name'])
            self.assertTrue(any('power point' in n for n in d['adjustments']), d['adjustments'])
        for features, msg in [([{'id': 63, 'quantity': 2}], 'at most 1 water features'),
                              ([{'id': 62, 'quantity': 21}], 'at most 20 garden lights'),
                              ([{'id': 61, 'quantity': 13}], 'at most 12 pots')]:
            r = self.client.post(url, {**base, 'features': features}, format='json')
            self.assertEqual(r.status_code, 400, features)
            self.assertIn(msg, r.data['error'])

    def test_inventory_field_and_pricing(self):
        from apps.library.models import InventoryItem, StaffProfile
        from apps.library.pricing import is_small_stone, layer_of
        office = User.objects.create_user(username='featoffice', password='pw', is_staff=True)
        StaffProfile.objects.create(user=office, role='OFFICE_ADMIN')
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(office).access_token}')
        light = InventoryItem.objects.create(name='Solar light', category='hardscape', unit_price=400, stock_quantity=5,
                                             real_world_size=0.3)
        self.assertTrue(is_small_stone(light))   # no type yet: a small hardscape item is a stone, as before
        r = self.client.patch(f'/api/inventory/{light.id}/', {'feature_type': 'light'}, format='multipart')
        self.assertEqual(r.status_code, 200, r.data)
        light.refresh_from_db()
        self.assertEqual(light.feature_type, 'light')
        self.assertFalse(is_small_stone(light))   # a small light is not a stone: placed and priced as a light
        self.assertEqual(layer_of(light), 'other')
        self.assertEqual(self.client.patch(f'/api/inventory/{light.id}/', {'feature_type': 'lamp'},
                                           format='multipart').status_code, 400)
        plant = InventoryItem.objects.create(name='Fern', category='plant', unit_price=150, stock_quantity=3)
        r = self.client.patch(f'/api/inventory/{plant.id}/', {'feature_type': 'pot'}, format='multipart')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['feature_type'], '')   # plants are placed as plants


# ─── Move to Render: /media redirect, link rewrite, media copy, row counts ───────────────────────

class RenderMoveTests(APITestCase):
    """Helpers for moving off PythonAnywhere: uploads in R2, old links kept working, data checked."""

    OLD = 'https://old.pythonanywhere.com/media/'

    def test_media_redirect_points_at_storage_and_refuses_odd_paths(self):
        from django.http import Http404
        from django.test import RequestFactory
        from apps.library import media_redirect as mr
        request = RequestFactory().get('/media/thumbnails/a.jpg')
        with patch.object(mr.default_storage, 'url', return_value='https://pub-media.r2.dev/thumbnails/a.jpg') as url:
            r = mr.media_redirect(request, 'thumbnails/a.jpg')
        url.assert_called_once_with('thumbnails/a.jpg')
        self.assertEqual((r.status_code, r['Location']), (302, 'https://pub-media.r2.dev/thumbnails/a.jpg'))
        for bad in ('../db.sqlite3', 'a/../../b', '/etc/passwd', 'a//b', r'a\b', 'thumbnails/'):
            with self.assertRaises(Http404, msg=bad):
                mr.media_redirect(request, bad)

    def test_design_image_upload_returns_the_storages_address(self):
        user = User.objects.create_user(username='refup', password='pw')
        self.client.force_authenticate(user)
        from PIL import Image
        buf = io.BytesIO()
        Image.new('RGB', (40, 30), 'green').save(buf, format='JPEG')
        upload = SimpleUploadedFile('yard.jpg', buf.getvalue(), content_type='image/jpeg')
        with self.settings(MEDIA_ROOT=tempfile.mkdtemp(prefix='design-ref-')):
            r = self.client.post('/api/upload-design-image/', {'image': upload}, format='multipart')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertRegex(r.data['url'], r'^http://testserver/media/design_refs/ref_\d+_[0-9a-f]{8}\.jpg$')

    @override_settings(R2_MEDIA_PUBLIC_URL='https://pub-media.r2.dev')
    def test_rewrite_old_media_urls_dry_run_then_apply(self):
        from django.core.management import call_command
        from apps.library.models import GardenDesign
        design = GardenDesign.objects.create(
            name='D', total_cost=1, reference_image_url=self.OLD + 'design_refs/ref_1.jpg',
            depth_data={'depth_map_url': '/media/depth_maps/d.png', 'photo': self.OLD + 'gardens/g.jpg'})
        other = GardenDesign.objects.create(name='Untouched', total_cost=1, reference_image_url='/media/x.jpg')
        out = io.StringIO()
        call_command('rewrite_old_media_urls', old=self.OLD, stdout=out)
        design.refresh_from_db()
        self.assertTrue(design.reference_image_url.startswith(self.OLD))          # dry run changes nothing
        self.assertIn('1 row(s) would change', out.getvalue())
        call_command('rewrite_old_media_urls', old=self.OLD, apply=True, stdout=io.StringIO())
        design.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(design.reference_image_url, 'https://pub-media.r2.dev/design_refs/ref_1.jpg')
        self.assertEqual(design.depth_data, {'depth_map_url': '/media/depth_maps/d.png',   # short links: redirected
                                             'photo': 'https://pub-media.r2.dev/gardens/g.jpg'})
        self.assertEqual(other.reference_image_url, '/media/x.jpg')

    def test_rewrite_old_media_urls_needs_a_full_media_address(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command('rewrite_old_media_urls', old='pythonanywhere.com', stdout=io.StringIO())

    def test_copy_media_to_r2_copies_photos_and_moves_server_models(self):
        from pathlib import Path
        from django.core.files.storage import FileSystemStorage
        from django.core.management import call_command
        from apps.library.management.commands import copy_media_to_r2 as cmd
        src = Path(tempfile.mkdtemp(prefix='old-media-'))
        (src / 'thumbnails').mkdir()
        (src / 'thumbnails' / 'fern.jpg').write_bytes(b'jpg')
        (src / 'models').mkdir()
        (src / 'models' / 'fern.glb').write_bytes(b'glTF-fern')
        admin = User.objects.create_superuser(username='mover', password='pw')
        server_model = UploadedModel.objects.create(label='Fern', category='plant', file_name='fern.glb',
                                                    size_bytes=9, storage='server', uploaded_by=admin)
        bucket = FileSystemStorage(location=tempfile.mkdtemp(prefix='fake-media-bucket-'))
        ok = MagicMock(status_code=200)
        with override_settings(MEDIA_ON_R2=True, R2_MEDIA_BUCKET='media-bucket', **R2_TEST), \
                patch.object(cmd, 'default_storage', bucket), patch.object(cmd.requests, 'put', return_value=ok) as put:
            call_command('copy_media_to_r2', source=str(src), stdout=io.StringIO())   # dry run
            self.assertFalse(bucket.exists('thumbnails/fern.jpg'))
            put.assert_not_called()
            call_command('copy_media_to_r2', source=str(src), apply=True, stdout=io.StringIO())
            call_command('copy_media_to_r2', source=str(src), apply=True, stdout=io.StringIO())   # again: no-op
        self.assertEqual(bucket.open('thumbnails/fern.jpg').read(), b'jpg')
        self.assertFalse(bucket.exists('models/fern.glb'))          # models go to the models bucket, not here
        self.assertEqual(put.call_count, 1)
        self.assertIn('/models-bucket/models/fern.glb?', put.call_args.args[0])
        self.assertEqual(put.call_args.kwargs['data'], b'glTF-fern')
        server_model.refresh_from_db()
        self.assertEqual(server_model.storage, 'r2')

    def test_copy_media_to_r2_refuses_without_r2(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError
        with override_settings(MEDIA_ON_R2=False), self.assertRaises(CommandError):
            call_command('copy_media_to_r2', source=tempfile.mkdtemp(), stdout=io.StringIO())


# ─── When a job was finished (ServiceBooking.finished_at) ─────────────────────────────────────────

class FinishedAtTests(APITestCase):
    """finished_at: stamped on sign-off (every path), cleared on reopen, never re-dated by later edits;
    analytics and reports date finished work by it, falling back to the scheduled date."""

    def setUp(self):
        from django.utils import timezone
        from apps.library.models import ServiceBooking
        self.admin = User.objects.create_superuser(username='fa-owner', password='pw')
        self.customer = User.objects.create_user(username='fa-cust', password='pw')
        self.today = timezone.localdate()
        self.booking = ServiceBooking.objects.create(user=self.customer, service_type='landscaping',
                                                     scheduled_date=self.today - timedelta(days=60), status='Installing')
        self.client.force_authenticate(self.admin)

    def test_sign_off_stamps_and_reopen_clears(self):
        r = self.client.patch(f'/api/bookings/{self.booking.id}/update_status/', {'status': 'Finished'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.booking.refresh_from_db()
        self.assertIsNotNone(self.booking.finished_at)
        self.assertEqual(self.booking.finish_date(), self.today)          # the sign-off day, not 60 days ago
        detail = self.client.get(f'/api/bookings/{self.booking.id}/').data
        self.assertIsNotNone(detail['finished_at'])                        # in the booking API (read-only)
        self.client.patch(f'/api/bookings/{self.booking.id}/', {'finished_at': '2020-01-01T00:00:00Z'}, format='json')
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.finish_date(), self.today)          # clients can't rewrite it
        self.client.patch(f'/api/bookings/{self.booking.id}/update_status/', {'status': 'Installing'}, format='json')
        self.booking.refresh_from_db()
        self.assertIsNone(self.booking.finished_at)                       # reopened: not finished any more

    def test_tracker_completion_stamps_it_too(self):
        from apps.library.models import ProjectTracker
        tracker = ProjectTracker.objects.create(booking=self.booking, total_price=5000, remaining_balance=0)
        self.client.patch(f'/api/projects/{tracker.id}/update-status/', {'status': 'COMPLETED'}, format='json')
        self.booking.refresh_from_db()
        self.assertEqual((self.booking.status, self.booking.finish_date()), ('Finished', self.today))

    def test_editing_a_finished_job_keeps_its_date_and_update_fields_saves_it(self):
        import datetime
        from django.utils import timezone
        from apps.library.models import ServiceBooking
        self.booking.status = 'Finished'
        self.booking.save(update_fields=['status'])                       # finished_at must be saved too
        stamped = ServiceBooking.objects.get(pk=self.booking.pk).finished_at
        self.assertIsNotNone(stamped)
        earlier = timezone.now() - datetime.timedelta(days=10)
        ServiceBooking.objects.filter(pk=self.booking.pk).update(finished_at=earlier)
        b = ServiceBooking.objects.get(pk=self.booking.pk)
        b.staff_notes = 'touch-up'
        b.save()
        self.assertEqual(ServiceBooking.objects.get(pk=b.pk).finished_at, earlier)   # not re-dated

    def test_created_as_finished_gets_no_made_up_date(self):
        from apps.library.models import ServiceBooking
        b = ServiceBooking.objects.create(user=self.customer, service_type='maintenance',
                                          scheduled_date=self.today - timedelta(days=5), status='Finished')
        self.assertIsNone(b.finished_at)
        self.assertEqual(b.finish_date(), self.today - timedelta(days=5))  # falls back to the scheduled day

    def test_migration_copies_first_sign_off_from_the_history(self):
        import datetime
        from importlib import import_module
        from django.apps import apps as registry
        from django.utils import timezone
        from apps.library.models import ProjectHistoryLog, ProjectTracker, ServiceBooking
        ServiceBooking.objects.filter(pk=self.booking.pk).update(status='Finished', finished_at=None)
        tracker = ProjectTracker.objects.create(booking=self.booking, total_price=5000, remaining_balance=0)
        first = ProjectHistoryLog.objects.create(project=tracker, status_reached='COMPLETED')
        ProjectHistoryLog.objects.create(project=tracker, status_reached='CUSTOMER_FEEDBACK')
        when = timezone.now() - datetime.timedelta(days=20)
        ProjectHistoryLog.objects.filter(pk=first.pk).update(timestamp=when)
        import_module('apps.library.migrations.0084_booking_finished_at').copy_sign_off_dates(registry, None)
        self.assertEqual(ServiceBooking.objects.get(pk=self.booking.pk).finished_at, when)   # the first one

    def test_income_counts_on_the_sign_off_day(self):
        from apps.library.models import ServiceBooking
        from django.utils import timezone
        ServiceBooking.objects.filter(pk=self.booking.pk).update(status='Finished', finished_at=timezone.now())
        ServiceBooking.objects.create(user=self.customer, service_type='maintenance',   # no sign-off date
                                      scheduled_date=self.today - timedelta(days=3), status='Finished')
        data = self.client.get(reverse('income-analytics'), {'range': '30d'}).data
        self.assertEqual(data['landscape']['earned_count'], 2)   # scheduled 60 days ago, finished today: in
        self.assertEqual(data['landscape']['finish_date_estimated_count'], 1)

    def test_balance_ages_from_the_finish_day(self):
        from apps.library.models import ProjectTracker, ServiceBooking
        from django.utils import timezone
        ServiceBooking.objects.filter(pk=self.booking.pk).update(
            status='Finished', finished_at=timezone.now() - timedelta(days=5))
        ProjectTracker.objects.create(booking=self.booking, total_price=5000, remaining_balance=2000)
        data = self.client.get('/api/reports/balances/').data
        row = data['owed'][0]
        self.assertEqual((row['days_since_job'], row['job_date_estimated']), (5, False))   # not 60
        self.assertEqual(data['aging'][0]['count'], 1)                                     # 0–30 days

    def test_crew_output_counts_finished_jobs_in_the_month_they_were_finished(self):
        from apps.library.models import ServiceBooking, StaffProfile
        from django.utils import timezone
        crew = User.objects.create_user(username='fa-crew', password='pw', is_staff=True)
        StaffProfile.objects.create(user=crew, role='FIELD_CREW')
        self.booking.assigned_crew.add(crew)
        ServiceBooking.objects.filter(pk=self.booking.pk).update(status='Finished', finished_at=timezone.now())
        start = (self.today - timedelta(days=6)).isoformat()
        data = self.client.get('/api/reports/crew/', {'from': start, 'to': self.today.isoformat()}).data
        row = next(r for r in data['crew'] if r['name'] == 'fa-crew')
        self.assertEqual(row['jobs_finished'], 1)    # scheduled 60 days ago, but finished this week


class ProjectsBoardTests(APITestCase):
    """GET /api/projects/board/: the office's view of every project (project_board.py)."""

    def setUp(self):
        from apps.library.models import StaffProfile
        from django.utils import timezone
        self.today = timezone.localdate()
        self.admin = User.objects.create_user(username='pb-admin', password='pw', is_staff=True)
        StaffProfile.objects.create(user=self.admin, role='OFFICE_ADMIN')
        self.crew = User.objects.create_user(username='pb-crew', password='pw', is_staff=True, first_name='Cris')
        StaffProfile.objects.create(user=self.crew, role='FIELD_CREW')
        self.customer = User.objects.create_user(username='pb-cust', password='pw', first_name='Ana')
        self.client.force_authenticate(self.admin)

    def job(self, status, days, end_in=None, crew=False, owed=1000, service='Softscape'):
        from apps.library.models import ProjectTracker, ServiceBooking
        b = ServiceBooking.objects.create(user=self.customer, service_type=service, status=status,
                                          scheduled_date=self.today + timedelta(days=days))
        ProjectTracker.objects.create(booking=b, total_price=5000, remaining_balance=owed, status='PLANTING',
                                      estimated_end_date=None if end_in is None else self.today + timedelta(days=end_in))
        if crew:
            b.assigned_crew.add(self.crew)
        return b

    def board(self, **params):
        res = self.client.get('/api/projects/board/', params)
        self.assertEqual(res.status_code, 200, res.content)
        return res.data

    def row(self, data, booking):
        return next(r for r in data['results'] if r['id'] == booking.id)

    def test_only_the_office_and_admins_can_open_it(self):
        for user in (self.crew, self.customer):
            self.client.force_authenticate(user)
            self.assertEqual(self.client.get('/api/projects/board/').status_code, 403)
            self.assertEqual(self.client.get('/api/projects/board/export/').status_code, 403)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get('/api/projects/board/').status_code, 401)

    def test_on_track_job_shows_both_progress_measures(self):
        from apps.library.models import ProjectMilestone
        b = self.job('Installing', -1, end_in=5, crew=True)
        ProjectMilestone.objects.create(booking=b, phase='site_prep', completion_pct=100)
        r = self.row(self.board(), b)
        self.assertEqual(r['delays'], [])
        self.assertEqual((r['office_stage'], r['office_pct'], r['crew_stages_done'], r['crew_stages_total']),
                         ('PLANTING', 70, 1, 3))
        self.assertEqual(r['crew'][0]['name'], 'Cris')
        self.assertEqual((r['total_price'], r['paid'], r['balance']), (5000.0, 4000.0, 1000.0))

    def test_overdue_uses_target_end_date_then_visit_date(self):
        late = self.job('Preparing', -10, end_in=-4)
        not_yet = self.job('Preparing', -10, end_in=3)       # visit passed, target not reached
        no_target = self.job('Pending', -2)
        data = self.board()
        self.assertEqual((self.row(data, late)['delays'], self.row(data, late)['days_late']), (['overdue'], 4))
        self.assertEqual(self.row(data, not_yet)['delays'], [])
        self.assertEqual(self.row(data, no_target)['delays'], ['overdue'])

    def test_stalled_when_the_crew_goes_quiet(self):
        from apps.library.models import Attendance
        from apps.library.project_board import STALL_DAYS
        from django.utils import timezone
        quiet = self.job('Installing', -6, end_in=10, crew=True)
        Attendance.objects.create(staff=self.crew, booking=quiet, clock_in_time=timezone.now() - timedelta(days=4))
        busy = self.job('Installing', -6, end_in=10, crew=True)
        Attendance.objects.create(staff=self.crew, booking=busy, clock_in_time=timezone.now() - timedelta(hours=3))
        never_started = self.job('Preparing', -STALL_DAYS, end_in=10, crew=True)
        just_due = self.job('Preparing', -(STALL_DAYS - 1), end_in=10, crew=True)
        no_crew = self.job('Preparing', -10, end_in=10)       # that is "needs crew", not stalled
        data = self.board()
        self.assertEqual(self.row(data, quiet)['delays'], ['stalled'])
        self.assertEqual(self.row(data, busy)['delays'], [])
        self.assertEqual(self.row(data, never_started)['delays'], ['stalled'])
        self.assertEqual(self.row(data, just_due)['delays'], [])
        self.assertEqual(self.row(data, no_crew)['delays'], [])
        self.assertEqual(data['totals']['needs_crew'], 1)

    def test_waiting_for_sign_off(self):
        from apps.library.models import ProjectMilestone
        from apps.library.project_board import REVIEW_DAYS
        old = self.job('For Review', -8, end_in=10, crew=True)
        m = ProjectMilestone.objects.create(booking=old, phase='final_inspection', completion_pct=100)
        ProjectMilestone.objects.filter(pk=m.pk).update(updated_at=m.updated_at - timedelta(days=REVIEW_DAYS + 1))
        fresh = self.job('For Review', -8, end_in=10, crew=True)
        ProjectMilestone.objects.create(booking=fresh, phase='final_inspection', completion_pct=100)
        data = self.board()
        self.assertEqual(self.row(data, old)['delays'], ['review'])
        self.assertEqual(self.row(data, fresh)['delays'], [])
        self.assertEqual(data['results'][0]['id'], old.id)    # delayed first

    def test_finished_and_cancelled_are_never_delayed_and_stay_out_of_open(self):
        done = self.job('Finished', -30, end_in=-20)
        gone = self.job('Cancelled', -30, end_in=-20)
        self.assertEqual(self.board()['count'], 0)
        self.assertEqual(self.row(self.board(scope='finished'), done)['delays'], [])
        self.assertEqual(self.row(self.board(scope='cancelled'), gone)['delays'], [])
        self.assertEqual(self.board(scope='all')['count'], 2)

    def test_filters_and_totals(self):
        a = self.job('Installing', -1, end_in=5, crew=True, service='hardscaping')
        b = self.job('Preparing', -9, end_in=-1, owed=0)
        self.job('Cancelled', -1, owed=9999)                  # cancelled balances are not owed
        self.job('Finished', -40, owed=500)
        self.assertEqual([r['id'] for r in self.board(crew=self.crew.id)['results']], [a.id])
        self.assertEqual([r['id'] for r in self.board(service='hardscaping')['results']], [a.id])
        self.assertEqual([r['id'] for r in self.board(delayed='1')['results']], [b.id])
        self.assertEqual([r['id'] for r in self.board(owing='1')['results']], [a.id])
        self.assertEqual(self.board(q=str(b.id))['results'][0]['id'], b.id)
        t = self.board(scope='finished')['totals']           # totals ignore the filters
        self.assertEqual((t['open'], t['delayed'], t['overdue'], t['in_field'], t['balance_owed']),
                         (2, 1, 1, 1, 1500.0))

    def test_paging_and_query_count_do_not_grow_with_rows(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        for i in range(30):
            self.job('Preparing', -i, end_in=1, crew=i % 2 == 0)
        data = self.board(page_size=10, page=3)
        self.assertEqual((data['count'], data['pages'], data['page'], len(data['results'])), (30, 3, 3, 10))
        with CaptureQueriesContext(connection) as small:
            self.board(scope='all', page_size=5)
        with CaptureQueriesContext(connection) as big:
            self.board(scope='all', page_size=30)
        self.assertEqual(len(small), len(big))

    def test_booking_without_tracker_still_shows(self):
        from apps.library.models import ServiceBooking
        b = ServiceBooking.objects.create(user=self.customer, service_type='maintenance', status='Pending',
                                          scheduled_date=self.today + timedelta(days=2))
        r = self.row(self.board(), b)
        self.assertEqual((r['tracker_id'], r['office_stage'], r['balance'], r['delays']), (None, None, None, []))

    def test_csv_export(self):
        self.job('Preparing', -9, end_in=-2)
        self.job('Preparing', 2, end_in=5)
        res = self.client.get('/api/projects/board/export/', {'delayed': '1'})
        self.assertEqual(res.status_code, 200)
        lines = b''.join(res.streaming_content).decode('utf-8-sig').strip().splitlines()
        self.assertEqual(len(lines), 2)                       # header + the one delayed project
        self.assertIn('Overdue', lines[1])


class OverviewMonitoringTests(APITestCase):
    """Business overview additions: collected vs earned, owed now, demand, and delayed projects."""

    def setUp(self):
        from apps.library.models import Order, ProjectPayment, ProjectTracker, ServiceBooking, StaffProfile
        from django.utils import timezone
        self.today = timezone.localdate()
        now = timezone.now()
        self.owner = User.objects.create_superuser(username='mon-owner', password='pw')
        self.customer = User.objects.create_user(username='mon-cust', password='pw')
        self.client.force_authenticate(self.owner)
        o = dict(user=self.customer, customer_name='C', customer_email='c@example.com')
        # Shop: one paid online today, one cash on delivery today, one refunded today (paid 40 days ago)
        Order.objects.create(total_price=1000, payment_method='online', payment_status='Paid', status='Delivered',
                             paid_at=now, **o)
        Order.objects.create(total_price=300, payment_method='cod', payment_status='Paid', status='Delivered',
                             delivered_at=now, **o)
        Order.objects.create(total_price=200, payment_method='online', payment_status='Refunded', status='Cancelled',
                             paid_at=now - timedelta(days=40), refunded_at=now, **o)
        # Landscape: finished 50,000 job, 20,000 paid today → 30,000 owed; an open job owing 9,000
        done = ServiceBooking.objects.create(user=self.customer, service_type='Softscape', status='Finished',
                                             scheduled_date=self.today - timedelta(days=3), finished_at=now)
        t = ProjectTracker.objects.create(booking=done, total_price=50000, remaining_balance=30000)
        ProjectPayment.objects.create(project=t, amount=20000, method='cash', paid_at=now)
        open_ = ServiceBooking.objects.create(user=self.customer, service_type='hardscaping', status='Preparing',
                                              scheduled_date=self.today - timedelta(days=8))
        ProjectTracker.objects.create(booking=open_, total_price=9000, remaining_balance=9000,
                                      estimated_end_date=self.today - timedelta(days=2))   # overdue
        gone = ServiceBooking.objects.create(user=self.customer, service_type='maintenance', status='Cancelled',
                                             scheduled_date=self.today)
        ProjectTracker.objects.create(booking=gone, total_price=7000, remaining_balance=7000)   # not owed
        ServiceBooking.objects.create(user=self.customer, service_type='consultation', status='Pending',
                                      scheduled_date=self.today + timedelta(days=5))
        StaffProfile.objects.create(user=User.objects.create_user(username='mon-office', password='pw', is_staff=True),
                                    role='OFFICE_ADMIN')

    def analytics(self, rng='30d'):
        res = self.client.get('/api/analytics/income/', {'range': rng})
        self.assertEqual(res.status_code, 200, res.content)
        return res.data

    def test_collected_matches_the_money_report_to_the_peso(self):
        d = self.analytics()
        start = self.today - timedelta(days=29)
        money = self.client.get('/api/reports/money/', {'from': start.isoformat(), 'to': self.today.isoformat()}).data
        self.assertEqual(d['collected']['total'], money['net'])
        self.assertEqual(d['collected']['landscape']['amount'], 20000.0)
        self.assertEqual(d['collected']['ecommerce']['amount'], 1100.0)          # 1000 + 300 − 200 refund
        self.assertEqual(sum(t['landscape_collected'] + t['ecommerce_collected'] for t in d['trend']), 21100.0)
        self.assertEqual(d['landscape']['earned'], 50000.0)                      # earned ≠ collected

    def test_owed_splits_finished_from_open_and_skips_cancelled(self):
        self.assertEqual(self.analytics()['owed'],
                         {'finished': 30000.0, 'finished_count': 1, 'open': 9000.0, 'open_count': 1})

    def test_demand_counts_and_request_outcomes(self):
        from apps.library.models import ServiceBooking
        from django.utils import timezone
        future = ServiceBooking.objects.create(user=self.customer, service_type='maintenance', status='Pending',
                                               scheduled_date=self.today + timedelta(days=60))
        ServiceBooking.objects.filter(pk=future.pk).update(created_at=timezone.now() + timedelta(days=20))  # bad data
        old = ServiceBooking.objects.create(user=self.customer, service_type='maintenance', status='Finished',
                                            scheduled_date=self.today - timedelta(days=70))
        ServiceBooking.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=45))
        d = self.analytics()['demand']
        # 4 requests made in the last 30 days (finished, preparing, cancelled, pending); the future-dated
        # one and the 45-day-old one are out
        self.assertEqual((d['requests'], d['confirmed'], d['waiting'], d['cancelled']), (4, 2, 1, 1))
        self.assertEqual((d['confirmed_pct'], d['cancelled_pct']), (50.0, 25.0))
        self.assertEqual(d['orders'], 2)                                          # the refunded order is cancelled
        self.assertEqual(d['requests_change_pct'], 300.0)                        # 1 in the previous 30 days
        self.assertEqual(sum(t['requests'] for t in self.analytics()['trend']), 4)

    def test_overview_lists_late_projects_with_the_projects_page_link(self):
        from apps.library.project_board import totals
        attention = {a['key']: a for a in self.client.get('/api/admin/overview/').data['attention']}
        late = attention['delayed_projects']
        self.assertEqual(late['count'], totals(self.today)['delayed'])
        self.assertEqual(late['count'], 1)            # the overdue job; the pending one isn't due yet
        self.assertEqual(late['link'], '/manage-projects?delayed=1')
        self.assertIn('overdue', late['hint'])
        self.assertEqual(attention['unpaid']['link'], '/manage-projects?scope=finished&owing=1')

    def test_office_sees_the_late_projects_item_too(self):
        self.client.force_authenticate(User.objects.get(username='mon-office'))
        keys = [a['key'] for a in self.client.get('/api/admin/overview/').data['attention']]
        self.assertIn('delayed_projects', keys)


class SeedDemoBusinessTests(APITestCase):
    """manage.py seed_demo_business: consistent, complete demo history that --clear removes entirely."""

    @classmethod
    def setUpTestData(cls):
        from io import StringIO
        from django.core.management import call_command
        from apps.library.models import InventoryItem, StaffProfile
        cls.owner = User.objects.create_superuser(username='seed-owner', password='pw', email='owner@example.com')
        office = User.objects.create_user(username='seed-office', password='pw', is_staff=True)
        StaffProfile.objects.create(user=office, role='OFFICE_ADMIN')
        cls.office = office
        cls.real_customer = User.objects.create_user(username='seed-real', password='pw', email='real@example.com')
        for name, cat, price in (('Fern', 'plant', 120), ('Bench', 'furniture', 4500), ('Pebbles', 'hardscape', 300)):
            InventoryItem.objects.create(name=name, category=cat, unit_price=price, stock_quantity=50)
        call_command('seed_demo_business', '--months', '3', stdout=StringIO())

    def demo(self, model, path):
        return model.objects.filter(**{f'{path}__email__iendswith': '@demo.gardenstudio.test'})

    def test_nothing_is_dated_in_the_future(self):
        from django.utils import timezone
        from apps.library.models import (Attendance, FunnelEvent, Order, ProjectHistoryLog, ProjectMilestone,
                                         ProjectPayment, ServiceBooking)
        now = timezone.now()
        checks = [
            (ServiceBooking.objects.all(), 'created_at'), (ServiceBooking.objects.all(), 'finished_at'),
            (Order.objects.all(), 'created_at'), (Order.objects.all(), 'paid_at'),
            (Order.objects.all(), 'delivered_at'), (Order.objects.all(), 'refunded_at'),
            (ProjectPayment.objects.all(), 'paid_at'), (ProjectHistoryLog.objects.all(), 'timestamp'),
            (ProjectMilestone.objects.all(), 'updated_at'), (Attendance.objects.all(), 'clock_in_time'),
            (Attendance.objects.all(), 'clock_out_time'), (FunnelEvent.objects.all(), 'created_at'),
        ]
        for qs, field in checks:
            self.assertFalse(qs.filter(**{f'{field}__gt': now}).exists(), f'{qs.model.__name__}.{field} in the future')

    def test_dates_and_people_are_consistent(self):
        from apps.library.models import Order, ServiceBooking
        bookings = ServiceBooking.objects.all()
        self.assertTrue(bookings.exists())
        self.assertFalse(bookings.filter(status='Finished', finished_at__isnull=True).exists())
        self.assertFalse(bookings.exclude(status='Finished').filter(finished_at__isnull=False).exists())
        for b in bookings.filter(status='Finished'):
            self.assertGreaterEqual(b.finished_at, b.created_at)
        orders = Order.objects.all()
        self.assertFalse(orders.filter(payment_method='online', payment_status__in=('Paid', 'Refunded'),
                                       paid_at__isnull=True).exists())
        self.assertFalse(orders.filter(status='Delivered', delivered_at__isnull=True).exists())
        self.assertFalse(orders.filter(status='Delivered', delivered_by__isnull=True).exists())
        self.assertFalse(orders.filter(payment_status='Refunded', refunded_at__isnull=True).exists())
        # Customers live in the service area, with a pin
        self.assertFalse(bookings.exclude(service_address__contains='Quezon').exists())
        self.assertFalse(bookings.filter(site_lat__isnull=True).exists())
        # Confirmed jobs mostly have a crew; finished work was clocked
        under_way = bookings.filter(status__in=('Installing', 'For Review', 'Finished'))
        self.assertFalse(under_way.filter(assigned_crew__isnull=True).exists())

    def test_project_payments_add_up_to_what_was_paid(self):
        from django.db.models import Sum
        from apps.library.models import ProjectTracker
        trackers = ProjectTracker.objects.annotate(paid_rows=Sum('payments__amount'))
        self.assertTrue(trackers.exists())
        for t in trackers:
            self.assertEqual(t.paid_rows or 0, t.total_price - t.remaining_balance, f'tracker {t.id}')

    def test_no_forgotten_shifts_and_stages_follow_status(self):
        from apps.library import field_work
        from apps.library.models import ProjectMilestone
        self.assertEqual(field_work.forgotten_shifts().count(), 0)
        # Nobody is on two jobs at once, and no shift is longer than a working day
        from apps.library.models import Attendance
        shifts = {}
        for a in Attendance.objects.order_by('staff_id', 'clock_in_time'):
            end = a.clock_out_time or a.clock_in_time
            self.assertLessEqual((end - a.clock_in_time).total_seconds(), 11 * 3600)
            previous = shifts.get(a.staff_id)
            if previous:
                self.assertGreaterEqual(a.clock_in_time, previous, f'overlapping shifts for crew {a.staff_id}')
            shifts[a.staff_id] = end
        self.assertFalse(ProjectMilestone.objects.filter(booking__status__in=('Pending', 'Cancelled')).exists())
        for_review = ProjectMilestone.objects.filter(booking__status='For Review', phase__in=field_work.CREW_STAGES)
        self.assertEqual(for_review.count(), 3 * for_review.values('booking').distinct().count())
        # The office stage agrees with the booking: awaiting sign-off = final inspection, finished = completed
        from apps.library.models import ProjectTracker
        self.assertFalse(ProjectTracker.objects.filter(booking__status='For Review').exclude(status='FINAL_INSPECTION').exists())
        self.assertFalse(ProjectTracker.objects.filter(booking__status='Finished').exclude(status='COMPLETED').exists())

    def test_collected_shows_up_and_matches_reports(self):
        from django.utils import timezone
        self.client.force_authenticate(self.owner)
        data = self.client.get('/api/analytics/income/', {'range': '30d'}).data
        self.assertGreater(data['collected']['total'], 0)
        self.assertGreater(data['collected']['landscape']['amount'], 0)
        today = timezone.localdate()
        money = self.client.get('/api/reports/money/', {'from': (today - timedelta(days=29)).isoformat(),
                                                        'to': today.isoformat()}).data
        self.assertEqual(data['collected']['total'], money['net'])
        self.assertEqual(data['landscape']['finish_date_estimated_count'], 0)
        self.assertGreater(self.client.get('/api/funnel-events/summary/').status_code, 0)

    def test_reset_is_reproducible_and_clear_removes_everything_demo(self):
        from io import StringIO
        from django.core.management import call_command
        from apps.library.models import (CashHandover, FunnelEvent, Order, ProjectPayment, ServiceBooking)
        before = (ServiceBooking.objects.count(), Order.objects.count(), ProjectPayment.objects.count())
        call_command('seed_demo_business', '--months', '3', '--reset', stdout=StringIO())
        self.assertEqual((ServiceBooking.objects.count(), Order.objects.count(), ProjectPayment.objects.count()), before)
        call_command('seed_demo_business', '--clear', stdout=StringIO())
        self.assertFalse(User.objects.filter(email__iendswith='@demo.gardenstudio.test').exists())
        self.assertEqual((ServiceBooking.objects.count(), Order.objects.count(), CashHandover.objects.count(),
                          FunnelEvent.objects.count()), (0, 0, 0, 0))
        # Real accounts are untouched
        self.assertTrue(User.objects.filter(pk__in=[self.owner.pk, self.office.pk, self.real_customer.pk]).count() == 3)


@override_settings(MEDIA_ROOT=TEMP_MEDIA)
class DesignPreviewTests(APITestCase):
    """Design card pictures (design_preview.py): owner or office only, cleaned and shrunk, never a 'change'."""

    def setUp(self):
        from apps.library.models import GardenDesign
        self.owner = User.objects.create_user(username='prevowner', password='pw')
        self.other = User.objects.create_user(username='prevother', password='pw')
        self.office = User.objects.create_user(username='prevoffice', password='pw', is_staff=True)
        self.design = GardenDesign.objects.create(user=self.owner, name='Front yard', placed_items=[],
                                                  dimensions={'width': 10, 'length': 8}, revision=3)
        self.url = f'/api/designs/{self.design.id}/preview/'

    def _as(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')

    def _png(self, size=(1600, 1000)):
        from PIL import Image
        buf = io.BytesIO()
        Image.new('RGBA', size, (40, 160, 90, 255)).save(buf, format='PNG')
        return SimpleUploadedFile('shot.png', buf.getvalue(), content_type='image/png')

    def test_owner_sets_a_small_clean_jpeg_without_touching_edited_date(self):
        from PIL import Image
        before = self.design.updated_at
        self._as(self.owner)
        r = self.client.post(self.url, {'image': _photo_with_gps('shot.jpg'), 'revision': 3}, format='multipart')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(r.data['preview_url'].endswith('.jpg'))
        self.design.refresh_from_db()
        self.assertEqual(self.design.updated_at, before)   # a picture is not an edit
        self.assertEqual((self.design.revision, self.design.preview_revision), (3, 3))
        with Image.open(self.design.preview.path) as img:
            self.assertEqual(img.format, 'JPEG')
            self.assertNotIn(0x8825, img.getexif())   # no GPS or other metadata kept

    def test_big_picture_is_shrunk_and_old_file_removed(self):
        import os
        from PIL import Image
        self._as(self.owner)
        self.client.post(self.url, {'image': self._png()}, format='multipart')
        self.design.refresh_from_db()
        first = self.design.preview.path
        with Image.open(first) as img:
            self.assertLessEqual(img.width, 800)
            self.assertLessEqual(img.height, 500)
        self.client.post(self.url, {'image': self._png((400, 250))}, format='multipart')
        self.design.refresh_from_db()
        self.assertNotEqual(self.design.preview.path, first)
        self.assertFalse(os.path.exists(first))

    def test_revision_from_the_future_or_missing_falls_back(self):
        self._as(self.owner)
        self.client.post(self.url, {'image': self._png(), 'revision': 99}, format='multipart')
        self.design.refresh_from_db()
        self.assertEqual(self.design.preview_revision, 3)

    def test_not_a_picture_is_refused(self):
        self._as(self.owner)
        r = self.client.post(self.url, {'image': SimpleUploadedFile('x.jpg', b'<html>hi</html>')}, format='multipart')
        self.assertEqual(r.status_code, 400)
        r = self.client.post(self.url, {}, format='multipart')
        self.assertEqual(r.status_code, 400)

    def test_only_owner_or_office(self):
        self._as(self.other)
        self.assertEqual(self.client.post(self.url, {'image': self._png()}, format='multipart').status_code, 404)
        self._as(self.office)
        self.assertEqual(self.client.post(self.url, {'image': self._png()}, format='multipart').status_code, 200)
        self.client.credentials()
        self.assertEqual(self.client.post(self.url, {'image': self._png()}, format='multipart').status_code, 401)

    def test_listed_with_the_design_and_deleted_with_it(self):
        import os
        self._as(self.owner)
        self.client.post(self.url, {'image': self._png()}, format='multipart')
        rows = self.client.get('/api/designs/').data
        rows = rows.get('results', rows) if isinstance(rows, dict) else rows
        row = next(d for d in rows if d['id'] == self.design.id)
        self.assertTrue(row['preview_url'])
        self.assertEqual(row['preview_revision'], 3)
        self.design.refresh_from_db()
        path = self.design.preview.path
        self.assertEqual(self.client.delete(f'/api/designs/{self.design.id}/').status_code, 204)
        self.assertFalse(os.path.exists(path))

    def test_crew_on_the_job_can_open_the_design_but_not_change_its_picture(self):
        from apps.library.models import ServiceBooking, StaffProfile
        crew = User.objects.create_user(username='prevcrew', password='pw', is_staff=True)
        StaffProfile.objects.create(user=crew, role='FIELD_CREW')
        booking = ServiceBooking.objects.create(user=self.owner, service_type='Softscape', scheduled_date='2026-12-01',
                                                design=self.design)
        booking.assigned_crew.add(crew)
        self._as(crew)
        self.assertEqual(self.client.get(f'/api/designs/{self.design.id}/').status_code, 200)
        self.assertEqual(self.client.post(self.url, {'image': self._png()}, format='multipart').status_code, 403)


class DesignPreviewOnCloudStorageTests(APITestCase):
    """Same pictures when uploads live in R2 (prod.py S3Storage): full https links, no files on this disk.
    Django's InMemoryStorage stands in for the bucket (no network)."""

    def setUp(self):
        from django.conf import settings as dj
        from apps.library.models import GardenDesign
        self._storage = override_settings(STORAGES={
            'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage',
                        'OPTIONS': {'base_url': 'https://pub-test.r2.dev/'}},
            'staticfiles': dj.STORAGES['staticfiles'],
        })
        self._storage.enable()
        self.addCleanup(self._storage.disable)
        self.owner = User.objects.create_user(username='cloudowner', password='pw')
        self.design = GardenDesign.objects.create(user=self.owner, name='Cloud yard', placed_items=[],
                                                  dimensions={'width': 10, 'length': 8})
        self.url = f'/api/designs/{self.design.id}/preview/'
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(self.owner).access_token}')

    def _png(self):
        from PIL import Image
        buf = io.BytesIO()
        Image.new('RGB', (1200, 750), (40, 160, 90)).save(buf, format='PNG')
        return SimpleUploadedFile('shot.png', buf.getvalue(), content_type='image/png')

    def test_link_is_the_buckets_own_and_replacing_removes_the_old_object(self):
        from django.core.files.storage import default_storage
        r = self.client.post(self.url, {'image': self._png()}, format='multipart')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(r.data['preview_url'].startswith('https://pub-test.r2.dev/design_previews/'), r.data)
        self.design.refresh_from_db()
        first = self.design.preview.name
        self.assertTrue(default_storage.exists(first))
        rows = self.client.get('/api/designs/').data
        rows = rows.get('results', rows) if isinstance(rows, dict) else rows
        self.assertEqual(next(d for d in rows if d['id'] == self.design.id)['preview_url'], r.data['preview_url'])
        self.client.post(self.url, {'image': self._png()}, format='multipart')
        self.design.refresh_from_db()
        self.assertNotEqual(self.design.preview.name, first)
        self.assertFalse(default_storage.exists(first))
        self.assertTrue(default_storage.exists(self.design.preview.name))
        self.assertEqual(self.client.delete(f'/api/designs/{self.design.id}/').status_code, 204)
        self.assertFalse(default_storage.exists(self.design.preview.name))


class CrewSyncStatusTests(APITestCase):
    """Crew phones report their offline outbox (counts only); the office sees who has unsent work."""

    def setUp(self):
        from apps.library.models import StaffProfile
        self.crew = User.objects.create_user(username='syncrew', password='pw', first_name='Jun', is_staff=True)
        StaffProfile.objects.create(user=self.crew, role='FIELD_CREW')
        self.crew2 = User.objects.create_user(username='syncrew2', password='pw', first_name='Ana', is_staff=True)
        StaffProfile.objects.create(user=self.crew2, role='FIELD_CREW')
        self.office = User.objects.create_superuser(username='syncboss', password='pw', email='b@example.com')
        self.customer = User.objects.create_user(username='synccust', password='pw')

    def _rows(self):
        self.client.force_authenticate(self.office)
        r = self.client.get('/api/crew-sync/')
        self.assertEqual(r.status_code, 200, r.data)
        return {row['user_id']: row for row in r.data['crew']}

    def test_phone_report_is_kept_and_unsent_work_comes_first(self):
        from django.utils import timezone
        self.client.force_authenticate(self.crew2)
        self.assertEqual(self.client.post('/api/crew-sync/', {'waiting': 0, 'failed': 0}, format='json').status_code, 200)
        self.client.force_authenticate(self.crew)
        oldest = (timezone.now() - timezone.timedelta(hours=3)).isoformat()
        self.assertEqual(self.client.post('/api/crew-sync/', {'waiting': 4, 'failed': 0, 'oldest_waiting_at': oldest},
                                          format='json').status_code, 200)
        self.client.force_authenticate(self.office)
        crew = self.client.get('/api/crew-sync/').data['crew']
        self.assertEqual([c['user_id'] for c in crew], [self.crew.id, self.crew2.id])   # unsent first
        self.assertEqual((crew[0]['state'], crew[0]['waiting']), ('unsent', 4))
        self.assertIsNotNone(crew[0]['oldest_waiting_at'])
        self.assertEqual(crew[1]['state'], 'ok')

    def test_refused_and_never_reported(self):
        self.client.force_authenticate(self.crew)
        self.client.post('/api/crew-sync/', {'waiting': 1, 'failed': 2}, format='json')
        rows = self._rows()
        self.assertEqual(rows[self.crew.id]['state'], 'refused')
        self.assertEqual(rows[self.crew2.id]['state'], 'never')

    def test_bad_values_are_cleaned(self):
        from django.utils import timezone
        self.client.force_authenticate(self.crew)
        future = (timezone.now() + timezone.timedelta(days=2)).isoformat()
        self.client.post('/api/crew-sync/', {'waiting': '-3', 'failed': 'x', 'oldest_waiting_at': future}, format='json')
        row = self._rows()[self.crew.id]
        self.assertEqual((row['waiting'], row['failed'], row['oldest_waiting_at']), (0, 0, None))
        self.client.force_authenticate(self.crew)
        self.client.post('/api/crew-sync/', {'waiting': 99999, 'oldest_waiting_at': future}, format='json')
        row = self._rows()[self.crew.id]
        self.assertEqual(row['waiting'], 500)
        self.assertLessEqual(row['oldest_waiting_at'], timezone.now())   # a fast phone clock can't set the future

    def test_quiet_while_on_a_job(self):
        from django.utils import timezone
        from apps.library.models import Attendance, StaffProfile
        Attendance.objects.create(staff=self.crew, clock_in_time=timezone.now() - timezone.timedelta(hours=3))
        StaffProfile.objects.filter(user=self.crew).update(sync_at=timezone.now() - timezone.timedelta(minutes=90))
        row = self._rows()[self.crew.id]
        self.assertEqual((row['state'], row['on_shift']), ('quiet', True))

    def test_only_crew_report_and_only_office_reads(self):
        for user in (self.customer, self.office):
            self.client.force_authenticate(user)
            self.assertEqual(self.client.post('/api/crew-sync/', {'waiting': 1}, format='json').status_code, 403)
        for user in (self.customer, self.crew):
            self.client.force_authenticate(user)
            self.assertEqual(self.client.get('/api/crew-sync/').status_code, 403)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get('/api/crew-sync/').status_code, 401)


class ProductionOriginSettingsTests(APITestCase):
    """A trailing slash pasted into Render's FRONTEND_URL must not stop the server (corsheaders.E014)."""

    def test_clean_origin(self):
        from config.settings.base import clean_origin
        self.assertEqual(clean_origin(' https://site.vercel.app/ '), 'https://site.vercel.app')
        self.assertEqual(clean_origin('https://site.vercel.app//'), 'https://site.vercel.app')
        self.assertEqual(clean_origin('http://localhost:5173'), 'http://localhost:5173')

    def test_production_settings_accept_a_trailing_slash(self):
        import os, subprocess, sys, tempfile
        from pathlib import Path
        base = Path(__file__).resolve().parents[2]
        env = {**os.environ, 'DJANGO_SETTINGS_MODULE': 'config.settings.prod',
               'DJANGO_SECRET_KEY': 'x' * 60, 'ALLOWED_HOSTS': 'api.example.com',
               'FRONTEND_URL': 'https://site.vercel.app/',
               'CORS_ALLOWED_ORIGINS': 'https://site.vercel.app/, https://www.site.app/',
               'DATABASE_PATH': str(Path(tempfile.gettempdir()) / 'origin-check.sqlite3')}
        for k in ('DATABASE_URL', 'R2_MEDIA_BUCKET', 'REQUIRE_CLOUD_STORAGE'):
            env.pop(k, None)
        code = ('import django; django.setup(); from django.conf import settings as s; '
                'from django.core.management import call_command; call_command("check"); '
                'print(s.FRONTEND_URL, s.CORS_ALLOWED_ORIGINS)')
        r = subprocess.run([sys.executable, '-c', code], cwd=base, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        self.assertIn("https://site.vercel.app ['https://site.vercel.app', 'https://www.site.app']", r.stdout)
