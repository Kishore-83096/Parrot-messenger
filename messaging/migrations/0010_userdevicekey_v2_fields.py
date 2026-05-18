from django.db import migrations, models


def copy_public_key_to_encryption_key(apps, schema_editor):
    UserDeviceKey = apps.get_model('messaging', 'UserDeviceKey')
    for device in UserDeviceKey.objects.all().only('id', 'public_key', 'encryption_public_key'):
        if not device.encryption_public_key:
            device.encryption_public_key = device.public_key
            device.save(update_fields=['encryption_public_key'])


class Migration(migrations.Migration):

    dependencies = [
        ('messaging', '0009_userdevicekey_device_name'),
    ]

    operations = [
        migrations.AddField(
            model_name='userdevicekey',
            name='encryption_public_key',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='userdevicekey',
            name='management_public_key',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='userdevicekey',
            name='status',
            field=models.CharField(
                choices=[('active', 'Active'), ('revoked', 'Revoked')],
                default='active',
                max_length=20,
            ),
        ),
        migrations.RunPython(copy_public_key_to_encryption_key, migrations.RunPython.noop),
        migrations.AddIndex(
            model_name='userdevicekey',
            index=models.Index(fields=['user_id', 'status'], name='messaging_u_user_id_51ce05_idx'),
        ),
    ]
