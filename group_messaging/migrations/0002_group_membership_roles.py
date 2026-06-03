from django.db import migrations, models
import django.db.models.deletion


def backfill_group_memberships(apps, schema_editor):
    Room = apps.get_model('messaging', 'Room')
    RoomParticipant = apps.get_model('messaging', 'RoomParticipant')
    GroupMembership = apps.get_model('group_messaging', 'GroupMembership')

    group_room_ids = Room.objects.filter(room_type='group').values_list('id', flat=True)
    participants = RoomParticipant.objects.filter(room_id__in=group_room_ids)

    memberships = []
    for participant in participants:
        memberships.append(
            GroupMembership(
                room_id=participant.room_id,
                user_id=participant.user_id,
                role='admin' if participant.role == 'admin' else 'member',
                is_active=participant.is_active,
            )
        )

    if memberships:
        GroupMembership.objects.bulk_create(memberships, ignore_conflicts=True)


class Migration(migrations.Migration):

    dependencies = [
        ('group_messaging', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='GroupMembership',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('user_id', models.PositiveBigIntegerField()),
                ('role', models.CharField(choices=[('admin', 'Admin'), ('sub_admin', 'Sub admin'), ('member', 'Member')], default='member', max_length=20)),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('room', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='group_memberships', to='messaging.room')),
            ],
            options={
                'ordering': ['created_at', 'id'],
            },
        ),
        migrations.AddConstraint(
            model_name='groupmembership',
            constraint=models.UniqueConstraint(fields=('room', 'user_id'), name='uq_group_membership_user'),
        ),
        migrations.AddIndex(
            model_name='groupmembership',
            index=models.Index(fields=['room', 'is_active'], name='group_messa_room_id_a7f98c_idx'),
        ),
        migrations.AddIndex(
            model_name='groupmembership',
            index=models.Index(fields=['user_id', 'is_active'], name='group_messa_user_id_18b3d2_idx'),
        ),
        migrations.AddIndex(
            model_name='groupmembership',
            index=models.Index(fields=['role', 'is_active'], name='group_messa_role_8f95ce_idx'),
        ),
        migrations.AlterField(
            model_name='groupactionlog',
            name='action',
            field=models.CharField(choices=[('group.created', 'Group created'), ('group.member_added', 'Member added'), ('group.member_removed', 'Member removed'), ('group.member_left', 'Member left'), ('group.updated', 'Group updated'), ('group.avatar_updated', 'Group avatar updated'), ('group.sub_admin_added', 'Sub admin added'), ('group.sub_admin_removed', 'Sub admin removed'), ('group.admin_transferred', 'Admin transferred'), ('group.deleted', 'Group deleted')], max_length=40),
        ),
        migrations.RunPython(backfill_group_memberships, migrations.RunPython.noop),
    ]
