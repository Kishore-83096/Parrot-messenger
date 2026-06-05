import json
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from messaging.models import Room, RoomParticipant

from .models import GroupActionLog, GroupMembership, GroupMessage, GroupMessageReceipt
from .serializers import serialize_group_room
from .services import list_group_messages, mark_group_room_read


TEST_CACHE_SETTINGS = {
    'INTERNAL_SERVICE_TOKEN': 'test-internal-service-token',
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


@override_settings(**TEST_CACHE_SETTINGS)
class GroupGhostReceiptTests(TestCase):
    sender_user_id = 1
    recipient_user_id = 2

    def setUp(self):
        cache.clear()
        self.room = Room.objects.create(
            room_type=Room.TYPE_GROUP,
            title='Ghost receipts',
            created_by_user_id=self.sender_user_id,
        )
        RoomParticipant.objects.create(
            room=self.room,
            user_id=self.sender_user_id,
            account_number='7000000001',
            display_name='Sender',
            role=RoomParticipant.ROLE_ADMIN,
        )
        RoomParticipant.objects.create(
            room=self.room,
            user_id=self.recipient_user_id,
            account_number='7000000002',
            display_name='Recipient',
        )
        GroupMembership.objects.create(
            room=self.room,
            user_id=self.sender_user_id,
            role=GroupMembership.ROLE_ADMIN,
            is_active=True,
        )
        GroupMembership.objects.create(
            room=self.room,
            user_id=self.recipient_user_id,
            role=GroupMembership.ROLE_MEMBER,
            is_active=True,
        )

    @patch('group_messaging.services.resolve_parent_receipt_visibility')
    def test_hidden_group_receipts_release_after_ghosting_is_removed(
        self,
        resolve_parent_receipt_visibility,
    ):
        first_message = GroupMessage.objects.create(
            room=self.room,
            sender_user_id=self.sender_user_id,
            text='Group hidden while ghosted.',
        )
        latest_message = GroupMessage.objects.create(
            room=self.room,
            sender_user_id=self.sender_user_id,
            text='Latest group hidden while ghosted.',
        )
        first_receipt = GroupMessageReceipt.objects.create(
            message=first_message,
            room=self.room,
            user_id=self.recipient_user_id,
        )
        latest_receipt = GroupMessageReceipt.objects.create(
            message=latest_message,
            room=self.room,
            user_id=self.recipient_user_id,
        )
        resolve_parent_receipt_visibility.return_value = (
            {
                'parent': {
                    'response': {
                        'allowed': True,
                        'hidden_user_ids': [self.sender_user_id],
                        'visible_user_ids': [],
                    },
                },
            },
            200,
        )

        hidden_result, hidden_status = mark_group_room_read(
            self.recipient_user_id,
            self.room.id,
            {'last_read_message_id': latest_message.id},
        )

        self.assertEqual(hidden_status, 200)
        self.assertEqual(hidden_result['updated_messages'], 0)
        self.assertEqual(hidden_result['hidden_receipts'], 2)
        first_message.refresh_from_db()
        latest_message.refresh_from_db()
        first_receipt.refresh_from_db()
        latest_receipt.refresh_from_db()
        self.assertEqual(first_message.status, GroupMessage.STATUS_SENT)
        self.assertEqual(latest_message.status, GroupMessage.STATUS_SENT)
        self.assertTrue(first_receipt.hidden_from_sender)
        self.assertTrue(latest_receipt.hidden_from_sender)
        self.assertIsNotNone(first_receipt.read_at)
        self.assertIsNotNone(latest_receipt.read_at)

        resolve_parent_receipt_visibility.return_value = (
            {
                'parent': {
                    'response': {
                        'allowed': True,
                        'hidden_user_ids': [],
                        'visible_user_ids': [self.sender_user_id],
                    },
                },
            },
            200,
        )
        self.client.post(
            '/receipts/internal/visibility-cache/',
            data=json.dumps(
                {
                    'policies': [
                        {
                            'owner_user_id': self.recipient_user_id,
                            'candidate_user_id': self.sender_user_id,
                            'hidden': False,
                        },
                    ],
                }
            ),
            content_type='application/json',
            HTTP_X_INTERNAL_SERVICE_TOKEN='test-internal-service-token',
        )

        released_result, released_status = mark_group_room_read(
            self.recipient_user_id,
            self.room.id,
            {'last_read_message_id': latest_message.id},
        )

        self.assertEqual(released_status, 200)
        self.assertEqual(released_result['updated_messages'], 2)
        self.assertEqual(released_result['hidden_receipts'], 0)
        self.assertEqual(
            released_result['message_statuses'],
            [
                {'message_id': first_message.id, 'status': GroupMessage.STATUS_READ},
                {'message_id': latest_message.id, 'status': GroupMessage.STATUS_READ},
            ],
        )
        first_message.refresh_from_db()
        latest_message.refresh_from_db()
        first_receipt.refresh_from_db()
        latest_receipt.refresh_from_db()
        self.assertEqual(first_message.status, GroupMessage.STATUS_READ)
        self.assertEqual(latest_message.status, GroupMessage.STATUS_READ)
        self.assertFalse(first_receipt.hidden_from_sender)
        self.assertFalse(latest_receipt.hidden_from_sender)

    @patch('group_messaging.services.resolve_parent_receipt_visibility')
    def test_group_receipt_visibility_uses_cached_ghost_policy(
        self,
        resolve_parent_receipt_visibility,
    ):
        first_message = GroupMessage.objects.create(
            room=self.room,
            sender_user_id=self.sender_user_id,
            text='First cached hidden group receipt.',
        )
        first_receipt = GroupMessageReceipt.objects.create(
            message=first_message,
            room=self.room,
            user_id=self.recipient_user_id,
        )
        resolve_parent_receipt_visibility.return_value = (
            {
                'parent': {
                    'response': {
                        'allowed': True,
                        'hidden_user_ids': [self.sender_user_id],
                        'visible_user_ids': [],
                    },
                },
            },
            200,
        )

        first_result, first_status = mark_group_room_read(
            self.recipient_user_id,
            self.room.id,
            {'last_read_message_id': first_message.id},
        )

        self.assertEqual(first_status, 200)
        self.assertEqual(first_result['hidden_receipts'], 1)
        resolve_parent_receipt_visibility.assert_called_once()

        second_message = GroupMessage.objects.create(
            room=self.room,
            sender_user_id=self.sender_user_id,
            text='Second cached hidden group receipt.',
        )
        second_receipt = GroupMessageReceipt.objects.create(
            message=second_message,
            room=self.room,
            user_id=self.recipient_user_id,
        )
        resolve_parent_receipt_visibility.return_value = (
            {
                'parent': {
                    'response': {
                        'allowed': True,
                        'hidden_user_ids': [],
                        'visible_user_ids': [self.sender_user_id],
                    },
                },
            },
            200,
        )

        second_result, second_status = mark_group_room_read(
            self.recipient_user_id,
            self.room.id,
            {'last_read_message_id': second_message.id},
        )

        self.assertEqual(second_status, 200)
        self.assertEqual(second_result['updated_messages'], 0)
        self.assertEqual(second_result['hidden_receipts'], 2)
        resolve_parent_receipt_visibility.assert_called_once()
        first_receipt.refresh_from_db()
        second_receipt.refresh_from_db()
        self.assertTrue(first_receipt.hidden_from_sender)
        self.assertTrue(second_receipt.hidden_from_sender)

    @patch('group_messaging.services.resolve_parent_receipt_visibility')
    def test_internal_receipt_visibility_cache_update_hides_group_receipts(
        self,
        resolve_parent_receipt_visibility,
    ):
        message = GroupMessage.objects.create(
            room=self.room,
            sender_user_id=self.sender_user_id,
            text='Hidden from internal cache.',
        )
        receipt = GroupMessageReceipt.objects.create(
            message=message,
            room=self.room,
            user_id=self.recipient_user_id,
        )

        cache_response = self.client.post(
            '/receipts/internal/visibility-cache/',
            data=json.dumps(
                {
                    'policies': [
                        {
                            'owner_user_id': self.recipient_user_id,
                            'candidate_user_id': self.sender_user_id,
                            'hidden': True,
                        },
                    ],
                }
            ),
            content_type='application/json',
            HTTP_X_INTERNAL_SERVICE_TOKEN='test-internal-service-token',
        )
        read_result, read_status = mark_group_room_read(
            self.recipient_user_id,
            self.room.id,
            {'last_read_message_id': message.id},
        )

        self.assertEqual(cache_response.status_code, 200)
        self.assertEqual(cache_response.json()['updated'], 1)
        self.assertEqual(read_status, 200)
        self.assertEqual(read_result['updated_messages'], 0)
        self.assertEqual(read_result['hidden_receipts'], 1)
        resolve_parent_receipt_visibility.assert_not_called()
        receipt.refresh_from_db()
        self.assertTrue(receipt.hidden_from_sender)
