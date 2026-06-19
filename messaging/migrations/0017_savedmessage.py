from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('group_messaging', '0005_groupprofile_deleted_state'),
        ('messaging', '0016_roomparticipant_room_list_hidden'),
    ]

    operations = [
        migrations.CreateModel(
            name='SavedMessage',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('user_id', models.PositiveBigIntegerField(db_index=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('direct_message', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='saved_by', to='messaging.message')),
                ('group_message', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='saved_by', to='group_messaging.groupmessage')),
            ],
            options={
                'ordering': ['-created_at', '-id'],
            },
        ),
        migrations.AddConstraint(
            model_name='savedmessage',
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(('direct_message__isnull', False), ('group_message__isnull', True))
                    | models.Q(('direct_message__isnull', True), ('group_message__isnull', False))
                ),
                name='ck_saved_message_one_target',
            ),
        ),
        migrations.AddConstraint(
            model_name='savedmessage',
            constraint=models.UniqueConstraint(
                condition=models.Q(('direct_message__isnull', False)),
                fields=('user_id', 'direct_message'),
                name='uq_saved_direct_message_user',
            ),
        ),
        migrations.AddConstraint(
            model_name='savedmessage',
            constraint=models.UniqueConstraint(
                condition=models.Q(('group_message__isnull', False)),
                fields=('user_id', 'group_message'),
                name='uq_saved_group_message_user',
            ),
        ),
        migrations.AddIndex(
            model_name='savedmessage',
            index=models.Index(fields=['user_id', '-created_at'], name='saved_msg_user_created_idx'),
        ),
        migrations.AddIndex(
            model_name='savedmessage',
            index=models.Index(fields=['direct_message'], name='saved_msg_direct_idx'),
        ),
        migrations.AddIndex(
            model_name='savedmessage',
            index=models.Index(fields=['group_message'], name='saved_msg_group_idx'),
        ),
    ]
