from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('messaging', '0008_userdevicekey_default'),
    ]

    operations = [
        migrations.AddField(
            model_name='userdevicekey',
            name='device_name',
            field=models.CharField(blank=True, max_length=120),
        ),
    ]
