from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('stories', '0003_story_story_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='storyview',
            name='hidden_from_owner',
            field=models.BooleanField(default=False),
        ),
        migrations.AddIndex(
            model_name='storyview',
            index=models.Index(
                fields=['story', 'hidden_from_owner'],
                name='stories_sto_story_i_ghost_idx',
            ),
        ),
    ]
