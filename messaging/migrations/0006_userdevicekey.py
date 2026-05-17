from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('messaging', '0005_messageattachment_cloudinary_metadata'),
    ]

    operations = [
        migrations.CreateModel(
            name='UserDeviceKey',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('user_id', models.PositiveBigIntegerField(db_index=True)),
                ('device_id', models.CharField(max_length=120)),
                ('public_key', models.TextField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('last_seen_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'ordering': ['-last_seen_at', '-id'],
                'indexes': [
                    models.Index(fields=['user_id', 'last_seen_at'], name='messaging_u_user_id_6ed253_idx'),
                ],
                'constraints': [
                    models.UniqueConstraint(fields=('user_id', 'device_id'), name='uq_user_device_key'),
                ],
            },
        ),
    ]
