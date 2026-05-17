from django.db import migrations, models


def select_default_devices(apps, schema_editor):
    UserDeviceKey = apps.get_model('messaging', 'UserDeviceKey')

    for user_id in UserDeviceKey.objects.values_list('user_id', flat=True).distinct():
        default_device = (
            UserDeviceKey.objects.filter(user_id=user_id)
            .order_by('-last_seen_at', '-id')
            .first()
        )
        if default_device:
            UserDeviceKey.objects.filter(pk=default_device.pk).update(is_default=True)


def clear_default_devices(apps, schema_editor):
    UserDeviceKey = apps.get_model('messaging', 'UserDeviceKey')
    UserDeviceKey.objects.update(is_default=False)


class Migration(migrations.Migration):

    dependencies = [
        ('messaging', '0007_usere2eekeybackup'),
    ]

    operations = [
        migrations.AddField(
            model_name='userdevicekey',
            name='is_default',
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(select_default_devices, clear_default_devices),
        migrations.AddConstraint(
            model_name='userdevicekey',
            constraint=models.UniqueConstraint(
                condition=models.Q(('is_default', True)),
                fields=('user_id',),
                name='uq_user_default_device_key',
            ),
        ),
        migrations.AddIndex(
            model_name='userdevicekey',
            index=models.Index(fields=['user_id', 'is_default'], name='messaging_u_user_id_5f0901_idx'),
        ),
    ]
