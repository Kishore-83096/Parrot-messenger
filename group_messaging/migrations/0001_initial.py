from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('messaging', '0014_message_story_context'),
    ]

    operations = [
        migrations.CreateModel(
            name='GroupProfile',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title', models.CharField(max_length=120)),
                ('avatar_url', models.URLField(blank=True, max_length=1000)),
                ('avatar_cloudinary_public_id', models.CharField(blank=True, max_length=512)),
                ('avatar_cloudinary_asset_id', models.CharField(blank=True, max_length=255)),
                ('created_by_user_id', models.PositiveBigIntegerField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('room', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='group_profile', to='messaging.room')),
            ],
            options={
                'ordering': ['-updated_at', '-id'],
            },
        ),
        migrations.CreateModel(
            name='GroupActionLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('actor_user_id', models.PositiveBigIntegerField()),
                ('target_user_id', models.PositiveBigIntegerField(blank=True, null=True)),
                ('action', models.CharField(choices=[('group.created', 'Group created'), ('group.member_added', 'Member added'), ('group.updated', 'Group updated'), ('group.avatar_updated', 'Group avatar updated')], max_length=40)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('room', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='group_action_logs', to='messaging.room')),
            ],
            options={
                'ordering': ['created_at', 'id'],
            },
        ),
        migrations.AddIndex(
            model_name='groupprofile',
            index=models.Index(fields=['created_by_user_id', 'updated_at'], name='group_messa_created_0051e8_idx'),
        ),
        migrations.AddIndex(
            model_name='groupactionlog',
            index=models.Index(fields=['room', 'created_at'], name='group_messa_room_id_2a99f9_idx'),
        ),
        migrations.AddIndex(
            model_name='groupactionlog',
            index=models.Index(fields=['action', 'created_at'], name='group_messa_action_44da4b_idx'),
        ),
        migrations.AddIndex(
            model_name='groupactionlog',
            index=models.Index(fields=['target_user_id', 'created_at'], name='group_messa_target__728a37_idx'),
        ),
    ]
