from django.core.management.base import BaseCommand
from agenda.models import CoupleProfile, Memory, MemoryPhoto, Plan
from agenda.storage_client import delete_from_media_storage


class Command(BaseCommand):
    help = "Clears all Plan, Memory, and MemoryPhoto test data while keeping users and CoupleProfile intact."

    def add_arguments(self, parser):
        parser.add_argument(
            "--keep-remote",
            action="store_true",
            help="Skip deleting remote objects from Media Storage.",
        )

    def handle(self, *args, **options):
        keep_remote = bool(options.get("keep_remote"))
        remote_deleted = 0

        for mp in MemoryPhoto.objects.all():
            if mp.remote_key and not keep_remote:
                if delete_from_media_storage(mp.remote_key):
                    remote_deleted += 1
            if mp.image:
                mp.image.delete(save=False)

        for mem in Memory.objects.all():
            if mem.photo:
                mem.photo.delete(save=False)

        photo_count, _ = MemoryPhoto.objects.all().delete()
        mem_count, _ = Memory.objects.all().delete()
        plan_count, _ = Plan.objects.all().delete()

        self.stdout.write(
            self.style.SUCCESS(
                f"Cleaned Nosotros DB: plans={plan_count}, memories={mem_count}, "
                f"photos={photo_count}, remote_deleted={remote_deleted}, "
                f"profiles_preserved={CoupleProfile.objects.count()}."
            )
        )
