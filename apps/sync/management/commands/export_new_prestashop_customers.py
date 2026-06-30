from django.core.management.base import BaseCommand, CommandError

from apps.sync.tasks import export_new_customers_to_icg


class Command(BaseCommand):
    help = "Export newly created Prestashop customers into ICG ClientesWeb"

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=100,
            help="Maximum number of Prestashop customers to process in one run",
        )

    def handle(self, *args, **options):
        result = export_new_customers_to_icg(limit=options["limit"])
        if result.get("status") == "error":
            raise CommandError(result["detail"])
        self.stdout.write(self.style.SUCCESS(str(result)))
