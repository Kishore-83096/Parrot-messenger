from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('messaging', '0010_userdevicekey_v2_fields'),
    ]

    operations = [
        migrations.CreateModel(
            name='UserDeviceDefaultCredential',
            fields=[
                (
                    'id',
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                ('user_id', models.PositiveBigIntegerField(db_index=True, unique=True)),
                ('password_hash', models.CharField(max_length=256)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'ordering': ['-updated_at', '-id'],
            },
        ),
        migrations.AddIndex(
            model_name='userdevicedefaultcredential',
            index=models.Index(
                fields=['user_id', 'updated_at'],
                name='messaging_u_user_id_f317a2_idx',
            ),
        ),
    ]
