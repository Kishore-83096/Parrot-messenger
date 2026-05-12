import json
from datetime import timedelta
from unittest.mock import patch

import jwt
from django.conf import settings
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import Message, Room, RoomParticipant
from .services import (
    create_direct_message,
    invalidate_room_messages_cache,
    list_room_messages,
    list_user_rooms,
    mark_room_read,
)


TEST_JWT_SETTINGS = {
    'MESSAGING_JWT_SECRET': 'test-messenger-secret-at-least-32-bytes',
    'MESSAGING_JWT_ISSUER': 'parrot-parent',
    'MESSAGING_JWT_AUDIENCE': 'parrot-messenger',
}


@override_settings(**TEST_JWT_SETTINGS)
class MessageSendAuthorizationTests(TestCase):
    sender_user_id = 1
    recipient_user_id = 2
    sender_account_number = '7000000001'
    recipient_account_number = '7000000002'

    def auth_header(self, user_id=None, account_number=None):
        now = timezone.now()
        token = jwt.encode(
            {
                'sub': str(user_id or self.sender_user_id),
                'user_id': user_id or self.sender_user_id,
                'account_number': account_number or self.sender_account_number,
                'iss': settings.MESSAGING_JWT_ISSUER,
                'aud': settings.MESSAGING_JWT_AUDIENCE,
                'iat': now,
                'exp': now + timedelta(minutes=5),
            },
            settings.MESSAGING_JWT_SECRET,
            algorithm='HS256',
        )

        return f'Bearer {token}'

    def create_direct_room(self, sender_active=True, recipient_active=True):
        room = Room.objects.create(
            room_type=Room.TYPE_DIRECT,
            created_by_user_id=self.sender_user_id,
        )
        RoomParticipant.objects.create(
            room=room,
            user_id=self.sender_user_id,
            account_number=self.sender_account_number,
            is_active=sender_active,
        )
        RoomParticipant.objects.create(
            room=room,
            user_id=self.recipient_user_id,
            account_number=self.recipient_account_number,
            is_active=recipient_active,
        )

        return room

    def post_send_message(self, payload):
        return self.client.post(
            '/messages/send/',
            data=json.dumps(payload),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )

    def post_authorize_message(self, payload):
        return self.client.post(
            '/messages/authorize/',
            data=json.dumps(payload),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )

    def parent_denial(self, reason='contact_not_saved', status=403):
        return {
            'ok': False,
            'parent': {
                'response': {
                    'allowed': False,
                    'reason': reason,
                    'message': reason,
                },
                'status_code': status,
            },
        }, status

    @patch('messaging.views.broadcast_participant_event')
    @patch('messaging.views.broadcast_room_event')
    @patch('messaging.views.authorize_parent_messaging')
    def test_send_allows_unsaved_contact_when_existing_direct_room_is_shared(
        self,
        authorize_parent_messaging,
        broadcast_room_event,
        broadcast_participant_event,
    ):
        room = self.create_direct_room()
        authorize_parent_messaging.return_value = self.parent_denial()

        response = self.post_send_message(
            {
                'recipient_account_number': self.recipient_account_number,
                'text': 'Hello from an existing room.',
                'client_message_id': 'shared-room-message-1',
            }
        )

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body['status'], 'sent')
        self.assertEqual(body['authorization']['messenger']['reason'], 'shared_room')
        self.assertEqual(body['authorization']['messenger']['room_id'], room.id)
        self.assertEqual(Room.objects.count(), 1)

        message = Message.objects.get()
        self.assertEqual(message.room_id, room.id)
        self.assertEqual(message.sender_user_id, self.sender_user_id)
        self.assertEqual(message.recipient_user_id, self.recipient_user_id)
        broadcast_room_event.assert_called_once()
        broadcast_participant_event.assert_called_once()

    @patch('messaging.views.authorize_parent_messaging')
    def test_authorize_allows_unsaved_contact_when_existing_direct_room_is_shared(
        self,
        authorize_parent_messaging,
    ):
        room = self.create_direct_room()
        authorize_parent_messaging.return_value = self.parent_denial()

        response = self.post_authorize_message(
            {
                'recipient_account_number': self.recipient_account_number,
            }
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['status'], 'allowed')
        self.assertEqual(body['authorization']['messenger']['room_id'], room.id)
        self.assertEqual(Message.objects.count(), 0)

    @patch('messaging.views.authorize_parent_messaging')
    def test_send_keeps_contact_not_saved_denial_without_shared_direct_room(
        self,
        authorize_parent_messaging,
    ):
        authorize_parent_messaging.return_value = self.parent_denial()

        response = self.post_send_message(
            {
                'recipient_account_number': self.recipient_account_number,
                'text': 'This should not send.',
            }
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['status'], 'denied')
        self.assertEqual(Message.objects.count(), 0)
        self.assertEqual(Room.objects.count(), 0)

    @patch('messaging.views.authorize_parent_messaging')
    def test_send_keeps_blocked_denial_even_when_direct_room_is_shared(
        self,
        authorize_parent_messaging,
    ):
        self.create_direct_room()
        authorize_parent_messaging.return_value = self.parent_denial('recipient_blocked_sender')

        response = self.post_send_message(
            {
                'recipient_account_number': self.recipient_account_number,
                'text': 'This should stay blocked.',
            }
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['status'], 'denied')
        self.assertEqual(Message.objects.count(), 0)

    @patch('messaging.views.authorize_parent_messaging')
    def test_send_requires_active_room_participants_for_shared_room_fallback(
        self,
        authorize_parent_messaging,
    ):
        self.create_direct_room(recipient_active=False)
        authorize_parent_messaging.return_value = self.parent_denial()

        response = self.post_send_message(
            {
                'recipient_account_number': self.recipient_account_number,
                'text': 'Inactive participants should not authorize.',
            }
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['status'], 'denied')
        self.assertEqual(Message.objects.count(), 0)


class MessagingCacheTests(TestCase):
    sender_user_id = 1
    recipient_user_id = 2
    sender_account_number = '7000000001'
    recipient_account_number = '7000000002'

    def setUp(self):
        cache.clear()

    def create_direct_room(self):
        room = Room.objects.create(
            room_type=Room.TYPE_DIRECT,
            created_by_user_id=self.sender_user_id,
        )
        RoomParticipant.objects.create(
            room=room,
            user_id=self.sender_user_id,
            account_number=self.sender_account_number,
        )
        RoomParticipant.objects.create(
            room=room,
            user_id=self.recipient_user_id,
            account_number=self.recipient_account_number,
        )

        return room

    def parent_authorization(self):
        return {
            'sender_user_id': self.sender_user_id,
            'sender_account_number': self.sender_account_number,
            'recipient_user_id': self.recipient_user_id,
            'recipient_account_number': self.recipient_account_number,
        }

    def sender(self):
        return {
            'user_id': self.sender_user_id,
            'account_number': self.sender_account_number,
        }

    def test_list_room_messages_uses_cache_until_invalidated(self):
        room = self.create_direct_room()
        message = Message.objects.create(
            room=room,
            sender_user_id=self.sender_user_id,
            recipient_user_id=self.recipient_user_id,
            text='Original cached message.',
        )

        messages_result, messages_status = list_room_messages(
            self.sender_user_id,
            room.id,
        )
        self.assertEqual(messages_status, 200)
        self.assertEqual(messages_result['messages'][0]['text'], 'Original cached message.')

        Message.objects.filter(id=message.id).update(text='Updated in database.')

        cached_messages_result, cached_messages_status = list_room_messages(
            self.sender_user_id,
            room.id,
        )
        self.assertEqual(cached_messages_status, 200)
        self.assertEqual(cached_messages_result['messages'][0]['text'], 'Original cached message.')

        invalidate_room_messages_cache(room.id)

        fresh_messages_result, fresh_messages_status = list_room_messages(
            self.sender_user_id,
            room.id,
        )
        self.assertEqual(fresh_messages_status, 200)
        self.assertEqual(fresh_messages_result['messages'][0]['text'], 'Updated in database.')

    def test_create_direct_message_invalidates_room_and_message_caches(self):
        room = self.create_direct_room()
        Message.objects.create(
            room=room,
            sender_user_id=self.sender_user_id,
            recipient_user_id=self.recipient_user_id,
            text='Old message.',
        )

        rooms_result, rooms_status = list_user_rooms(self.sender_user_id)
        messages_result, messages_status = list_room_messages(self.sender_user_id, room.id)
        self.assertEqual(rooms_status, 200)
        self.assertEqual(messages_status, 200)
        self.assertEqual(rooms_result['rooms'][0]['last_message']['text'], 'Old message.')
        self.assertEqual(len(messages_result['messages']), 1)

        send_result, send_status = create_direct_message(
            self.sender(),
            self.parent_authorization(),
            {
                'recipient_account_number': self.recipient_account_number,
                'text': 'New message.',
                'client_message_id': 'cache-invalidation-send-1',
            },
        )
        self.assertEqual(send_status, 201)
        self.assertEqual(send_result['status'], 'sent')

        fresh_rooms_result, fresh_rooms_status = list_user_rooms(self.sender_user_id)
        fresh_messages_result, fresh_messages_status = list_room_messages(
            self.sender_user_id,
            room.id,
        )
        self.assertEqual(fresh_rooms_status, 200)
        self.assertEqual(fresh_messages_status, 200)
        self.assertEqual(fresh_rooms_result['rooms'][0]['last_message']['text'], 'New message.')
        self.assertEqual(
            [message['text'] for message in fresh_messages_result['messages']],
            ['Old message.', 'New message.'],
        )

    def test_mark_room_read_invalidates_room_and_message_caches(self):
        room = self.create_direct_room()
        message = Message.objects.create(
            room=room,
            sender_user_id=self.recipient_user_id,
            recipient_user_id=self.sender_user_id,
            text='Unread message.',
            status=Message.STATUS_DELIVERED,
        )

        rooms_result, rooms_status = list_user_rooms(self.sender_user_id)
        messages_result, messages_status = list_room_messages(self.sender_user_id, room.id)
        self.assertEqual(rooms_status, 200)
        self.assertEqual(messages_status, 200)
        self.assertEqual(rooms_result['rooms'][0]['unread_count'], 1)
        self.assertEqual(messages_result['messages'][0]['status'], Message.STATUS_DELIVERED)

        read_result, read_status = mark_room_read(
            self.sender_user_id,
            room.id,
            {'last_read_message_id': message.id},
        )
        self.assertEqual(read_status, 200)
        self.assertEqual(read_result['status'], 'read')

        fresh_rooms_result, fresh_rooms_status = list_user_rooms(self.sender_user_id)
        fresh_messages_result, fresh_messages_status = list_room_messages(
            self.sender_user_id,
            room.id,
        )
        self.assertEqual(fresh_rooms_status, 200)
        self.assertEqual(fresh_messages_status, 200)
        self.assertEqual(fresh_rooms_result['rooms'][0]['unread_count'], 0)
        self.assertEqual(fresh_messages_result['messages'][0]['status'], Message.STATUS_READ)
