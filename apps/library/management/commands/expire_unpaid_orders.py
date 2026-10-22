from django.core.management.base import BaseCommand

from apps.library.paymongo import expire_unpaid_online_orders


class Command(BaseCommand):
    help = 'Cancel online orders left unpaid past PAYMONGO_ORDER_TTL_MINUTES and return their stock.'

    def handle(self, *args, **options):
        count = expire_unpaid_online_orders()
        self.stdout.write(self.style.SUCCESS(f'Expired {count} unpaid online order(s).'))
