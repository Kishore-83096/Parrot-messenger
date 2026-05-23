from django.core.management.base import BaseCommand

from stories.services import cleanup_stories


class Command(BaseCommand):
    help = 'Mark expired stories and clean retained encrypted story media.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--retention-days',
            type=int,
            default=None,
            help=(
                'Days to retain expired story media before deleting it. '
                'Defaults to immediate cleanup.'
            ),
        )
        parser.add_argument(
            '--media-limit',
            type=int,
            default=None,
            help='Maximum number of expired media rows to clean in this run.',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show candidates without changing stories or deleting media.',
        )

    def handle(self, *args, **options):
        result = cleanup_stories(
            retention_days=options.get('retention_days'),
            media_limit=options.get('media_limit'),
            dry_run=options.get('dry_run'),
        )

        self.stdout.write(
            self.style.SUCCESS(
                'Stories cleanup complete: '
                f'expired={result["expired_stories"]}, '
                f'expired_candidates={result["expired_story_candidates"]}, '
                f'media_cleaned={result["media_cleaned"]}, '
                f'media_candidates={result["media_candidates"]}, '
                f'cloudinary_errors={len(result["cloudinary_errors"])}'
            )
        )
