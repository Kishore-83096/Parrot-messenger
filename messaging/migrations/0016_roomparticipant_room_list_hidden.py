from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('messaging', '0015_message_receipt_hidden_from_sender'),
    ]

    operations = [
        migrations.AddField(
            model_name='roomparticipant',
            name='room_list_hidden',
            field=models.BooleanField(default=False),
        ),
        migrations.AddIndex(
            model_name='roomparticipant',
            index=models.Index(
                fields=['user_id', 'is_active', 'room_list_hidden'],
                name='messaging_r_user_id_789655_idx',
            ),
        ),
    ]
