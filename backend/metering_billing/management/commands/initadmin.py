import os

from django.core.management.base import BaseCommand
from dotenv import load_dotenv
from metering_billing.models import Organization, PricingUnit, User

load_dotenv()


class Command(BaseCommand):
    def handle(self, *args, **options):

        username = os.getenv("ADMIN_USERNAME")
        email = os.getenv("ADMIN_EMAIL")
        password = os.getenv("ADMIN_PASSWORD")

        if not User.objects.filter(username=username).exists():
            admin = User.objects.create_superuser(
                email=email, username=username, password=password
            )

            org = Organization.objects.create(company_name="Lotus Default")
            admin.organization = org
            admin.save()

        else:
            print("Admin account has already been initialized.")

        supported_currencies = [
            ("US Dollar", "USD", "$"),
            ("Euro", "EUR", "€"),
            ("Pound", "GBP", "£"),
        ]
        for name, code, symbol in supported_currencies:
            PricingUnit.objects.get_or_create(name=name, code=code, symbol=symbol)
