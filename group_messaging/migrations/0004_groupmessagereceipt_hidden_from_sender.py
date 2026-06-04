from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('group_messaging', '0003_groupmessage_groupmessageencrypteduploadintent_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='groupmessagereceipt',
            name='hidden_from_sender',
            field=models.BooleanField(default=False),
        ),
        migrations.AddIndex(
            model_name='groupmessagereceipt',
            index=models.Index(
                fields=['message', 'hidden_from_sender'],
                name='group_messa_message_ghost_idx',
            ),
        ),
    ]
