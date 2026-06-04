from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('messaging', '0014_message_story_context'),
    ]

    operations = [
        migrations.AddField(
            model_name='message',
            name='receipt_hidden_from_sender',
            field=models.BooleanField(default=False),
        ),
    ]
