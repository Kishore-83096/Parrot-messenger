from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('messaging', '0006_userdevicekey'),
    ]

    operations = [
        migrations.CreateModel(
            name='UserE2EEKeyBackup',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('user_id', models.PositiveBigIntegerField(db_index=True, unique=True)),
                ('public_key', models.TextField()),
                ('encrypted_private_key', models.TextField()),
                ('salt', models.TextField()),
                ('nonce', models.TextField()),
                ('kdf_algorithm', models.CharField(default='PBKDF2-SHA256', max_length=40)),
                ('kdf_iterations', models.PositiveIntegerField(default=600000)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'ordering': ['-updated_at', '-id'],
                'indexes': [models.Index(fields=['user_id', 'updated_at'], name='messaging_u_user_id_74248d_idx')],
            },
        ),
    ]
