import os
import json
from datetime import timedelta
from unittest.mock import patch, MagicMock
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from django.contrib.auth.models import User
from rest_framework_simplejwt.tokens import RefreshToken

class GenerateLayoutsViewTests(APITestCase):
    """AI Garden Designer: hybrid Gemini + smart layout engine. Customers always get 3 designs."""

    def setUp(self):
        from django.core.cache import cache
        from apps.library.models import InventoryItem
        cache.clear()  # throttle history, the layout cache and the Gemini breaker live here
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
            self.assertEqual(E.check(plants, specs, site, plant_budget), [], d['design_name'])
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
        photo = SimpleUploadedFile('yard.jpg', b'fake', content_type='image/jpeg')
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
        base = [('1', 'Oak', 1000, 4.0), ('2', 'Fern', 500, 1.0), ('3', 'Palm', 800, 5.0), ('4', 'Rose', 200, 0.5)]
        for case in range(60):
            specs = [E.PlantSpec(i, n, p, rng.randint(1, 30), h, E.spacing_for(h)) for i, n, p, h in base]
            site = E.Site(rng.uniform(3, 25), rng.uniform(3, 25),
                          E.zone_cells(rng.sample(list(E.ZONES), rng.randint(0, 3))))
            budget = rng.choice([800, 3000, 9000, 30000])
            for style in E.STYLES:
                d = E.generate(style, specs, site, budget, seed=case)
                self.assertEqual(E.check(d['plants'], specs, site, budget), [], (case, style))
            messy = [{'plant_id': rng.choice(['1', '2', '3', '4', '999']), 'x': rng.uniform(-2, 27),
                      'z': rng.uniform(-2, 27), 'rotation': 0} for _ in range(rng.randint(0, 40))]
            fixed, _ = E.repair(messy, specs, site, budget, seed=case)
            self.assertEqual(E.check(fixed, specs, site, budget), [], (case, 'repair'))

    @patch.dict(os.environ, {'GEMINI_API_KEY': 'your_gemini_api_key_here'})
    def test_budget_too_small_for_the_site_says_what_it_takes(self):
        r = self.client.post(self.url, {**self.valid, 'budget': 20000}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn("doesn't cover the basics", r.data['error'])
        self.assertGreater(r.data['min_budget'], 20000)
        ok = self.client.post(self.url, {**self.valid, 'budget': r.data['min_budget']}, format='json')
        self.assertEqual(ok.status_code, 200)

    @patch.dict(os.environ, {'GEMINI_API_KEY': 'your_gemini_api_key_here'})
    def test_budget_range_gives_good_better_best(self):
        r = self.client.post(self.url, {**self.valid, 'budget': None, 'budget_min': 60000, 'budget_max': 120000},
                             format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['budget_range'], {'min': 60000, 'max': 120000})
        by_style = {d['design_name']: d for d in r.data['designs']}
        self.assertEqual(by_style['Minimalist']['target_budget'], 60000)
        self.assertEqual(by_style['Symmetrical']['target_budget'], 90000)
        self.assertEqual(by_style['Lush / Organic']['target_budget'], 120000)
        for d in r.data['designs']:
            self.assertLessEqual(d['cost_breakdown']['total'], d['target_budget'] + 2, d['design_name'])
        self.assertGreater(by_style['Lush / Organic']['total_cost'], by_style['Symmetrical']['total_cost'])

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
        d = E.generate('Minimalist', specs, E.Site(5, 5), 100000)
        self.assertIn('stock', d['budget_note'].lower())


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
        self._sign_off()
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
            'customer_name': 'Buyer', 'customer_email': 'b@example.com', 'customer_address': 'Lucena',
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
            'customer_name': 'Buyer', 'customer_email': 'b@example.com', 'customer_address': 'Lucena',
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
                'customer_name': 'Maria Cruz', 'customer_email': 'm@example.com', 'customer_address': 'Lucena',
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
        self.assertEqual(self._notes(self.customer), [])   # the buyer is not told about their own order

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
                                                'service_address': 'Tayabas'}, format='json')
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
            'customer_name': 'Cust', 'customer_email': 'c@example.com', 'customer_address': 'Lucena',
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
            'customer_name': 'Cust', 'customer_email': 'c@example.com', 'customer_address': 'Lucena',
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
        weak = self.client.post('/api/register/', {'username': 'newbie', 'email': 'n@example.com', 'password': '1234'})
        self.assertEqual(weak.status_code, 400)
        self.assertIn('password', weak.data)
        ok = self.client.post('/api/register/', {'username': 'newbie', 'email': 'n@example.com', 'password': 'Garden-Path-77'})
        self.assertEqual(ok.status_code, 201)
        self.assertFalse(User.objects.get(username='newbie').is_staff)

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

    def test_phone_may_send_the_headers_cross_site(self):
        r = self.client.options('/api/attendance/clock_in/', HTTP_ORIGIN='https://shimereyslandscaping.vercel.app',
                                HTTP_ACCESS_CONTROL_REQUEST_METHOD='POST',
                                HTTP_ACCESS_CONTROL_REQUEST_HEADERS='authorization,content-type,idempotency-key,x-action-time')
        allowed = r.get('Access-Control-Allow-Headers', '')
        self.assertIn('idempotency-key', allowed)
        self.assertIn('x-action-time', allowed)
