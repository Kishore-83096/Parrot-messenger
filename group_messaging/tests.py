from datetime import timedelta

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from messaging.models import Room, RoomParticipant

from .models import GroupActionLog, GroupMembership, GroupMessage, GroupMessageReceipt
from .serializers import serialize_group_room
from .services import list_group_messages


TEST_CACHE_SETTINGS = {
    'CACHES': {
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'parrot-group-messaging-tests',
        },
    },
}


@override_settings(**TEST_CACHE_SETTINGS)
class GroupMembershipVisibilityTests(TestCase):
    def setUp(self):
        cache.clear()
        self.room = Room.objects.create(
            room_type=Room.TYPE_GROUP,
            title='Timeline boundary',
            created_by_user_id=1,
        )
        self.admin = RoomParticipant.objects.create(
            room=self.room,
            user_id=1,
            account_number='7000000001',
            display_name='Admin',
            role=RoomParticipant.ROLE_ADMIN,
        )
        self.new_member = RoomParticipant.objects.create(
            room=self.room,
            user_id=2,
            account_number='7000000002',
            display_name='New member',
        )
        GroupMembership.objects.create(
            room=self.room,
            user_id=1,
            role=GroupMembership.ROLE_ADMIN,
            is_active=True,
        )
        GroupMembership.objects.create(
            room=self.room,
            user_id=2,
            role=GroupMembership.ROLE_MEMBER,
            is_active=True,
        )

        self.joined_at = timezone.now()
        self.old_time = self.joined_at - timedelta(days=5)
        self.new_time = self.joined_at + timedelta(minutes=1)

        RoomParticipant.objects.filter(pk=self.admin.pk).update(joined_at=self.old_time)
        RoomParticipant.objects.filter(pk=self.new_member.pk).update(joined_at=self.joined_at)
        self.admin.joined_at = self.old_time
        self.new_member.joined_at = self.joined_at

        self.old_message = GroupMessage.objects.create(
            room=self.room,
            sender_user_id=1,
            text='Old group message',
        )
        self.new_message = GroupMessage.objects.create(
            room=self.room,
            sender_user_id=1,
            text='Visible group message',
        )
        GroupMessage.objects.filter(pk=self.old_message.pk).update(created_at=self.old_time)
        GroupMessage.objects.filter(pk=self.new_message.pk).update(created_at=self.new_time)
        self.old_message.created_at = self.old_time
        self.new_message.created_at = self.new_time

        GroupMessageReceipt.objects.create(
            message=self.old_message,
            room=self.room,
            user_id=2,
        )
        GroupMessageReceipt.objects.create(
            message=self.new_message,
            room=self.room,
            user_id=2,
        )

        self.old_log = GroupActionLog.objects.create(
            room=self.room,
            actor_user_id=1,
            action=GroupActionLog.ACTION_GROUP_UPDATED,
            metadata={'actor_display_name': 'Admin', 'title': 'Old name'},
        )
        self.new_log = GroupActionLog.objects.create(
            room=self.room,
            actor_user_id=1,
            target_user_id=2,
            action=GroupActionLog.ACTION_MEMBER_ADDED,
            metadata={
                'actor_display_name': 'Admin',
                'target_display_name': 'New member',
            },
        )
        GroupActionLog.objects.filter(pk=self.old_log.pk).update(created_at=self.old_time)
        GroupActionLog.objects.filter(pk=self.new_log.pk).update(created_at=self.new_time)

    def test_new_member_group_timeline_starts_at_join_time(self):
        result, status = list_group_messages(user_id=2, room_id=self.room.id)

        self.assertEqual(status, 200)
        self.assertEqual(
            [message['text'] for message in result['messages']],
            ['Visible group message'],
        )
        self.assertEqual(
            [log['action'] for log in result['logs']],
            [GroupActionLog.ACTION_MEMBER_ADDED],
        )

    def test_new_member_room_summary_ignores_pre_join_message_and_unread(self):
        room = serialize_group_room(self.room, current_user_id=2)

        self.assertEqual(room['last_message']['text'], 'Visible group message')
        self.assertEqual(room['unread_count'], 1)
        self.assertEqual(
            [log['action'] for log in room['latest_logs']],
            [GroupActionLog.ACTION_MEMBER_ADDED],
        )
