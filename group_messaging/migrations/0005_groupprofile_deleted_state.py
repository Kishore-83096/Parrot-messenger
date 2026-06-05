from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('group_messaging', '0004_groupmessagereceipt_hidden_from_sender'),
    ]

    operations = [
        migrations.AddField(
            model_name='groupprofile',
            name='deleted_at',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name='groupprofile',
            name='deleted_by_user_id',
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
    ]
