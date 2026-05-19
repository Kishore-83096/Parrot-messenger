import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('messaging', '0011_userdevicedefaultcredential'),
    ]

    operations = [
        migrations.CreateModel(
            name='MessageEncryptedUploadIntent',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('sender_user_id', models.PositiveBigIntegerField(db_index=True)),
                ('sender_account_number', models.CharField(blank=True, max_length=10)),
                ('recipient_user_id', models.PositiveBigIntegerField(db_index=True)),
                ('recipient_account_number', models.CharField(db_index=True, max_length=10)),
                ('client_message_id', models.CharField(db_index=True, max_length=120)),
                ('attachment_client_id', models.CharField(blank=True, max_length=255)),
                ('attachment_index', models.PositiveIntegerField(default=0)),
                ('original_file_name', models.CharField(blank=True, max_length=255)),
                ('original_mime_type', models.CharField(blank=True, max_length=120)),
                ('original_file_size_bytes', models.PositiveBigIntegerField(blank=True, null=True)),
                ('encrypted_file_size_bytes', models.PositiveBigIntegerField()),
                ('cloudinary_public_id', models.CharField(max_length=512, unique=True)),
                ('cloudinary_asset_id', models.CharField(blank=True, max_length=255)),
                ('cloudinary_resource_type', models.CharField(default='raw', max_length=40)),
                ('cloudinary_folder', models.CharField(max_length=255)),
                ('secure_url', models.URLField(blank=True, max_length=1000)),
                ('status', models.CharField(choices=[('issued', 'Issued'), ('completed', 'Completed'), ('consumed', 'Consumed'), ('expired', 'Expired')], default='issued', max_length=20)),
                ('signature_timestamp', models.PositiveBigIntegerField()),
                ('expires_at', models.DateTimeField(db_index=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('consumed_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'ordering': ['created_at', 'id'],
            },
        ),
        migrations.AddIndex(
            model_name='messageencrypteduploadintent',
            index=models.Index(fields=['sender_user_id', 'client_message_id', 'status'], name='messaging_m_sender__4042cf_idx'),
        ),
        migrations.AddIndex(
            model_name='messageencrypteduploadintent',
            index=models.Index(fields=['recipient_user_id', 'status'], name='messaging_m_recipie_898746_idx'),
        ),
        migrations.AddIndex(
            model_name='messageencrypteduploadintent',
            index=models.Index(fields=['status', 'expires_at'], name='messaging_m_status_c06208_idx'),
        ),
    ]
