import json
from datetime import timedelta
from unittest.mock import patch

import jwt
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import Message, Room, RoomParticipant
from .cache import invalidate_room_messages_cache
from .services import (
    create_direct_message,
    get_room_unread_count,
    list_room_messages,
    list_user_rooms,
    mark_room_delivered,
    mark_room_read,
    release_room_blocked_messages,
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

    def parent_allowed(self, delivery_blocked=False, sender_blocked_recipient=False):
        return {
            'ok': True,
            'parent': {
                'response': {
                    'allowed': True,
                    'sender_user_id': self.sender_user_id,
                    'sender_account_number': self.sender_account_number,
                    'recipient_user_id': self.recipient_user_id,
                    'recipient_account_number': self.recipient_account_number,
                    'delivery_blocked': delivery_blocked,
                    'block_context': {
                        'sender_blocked_recipient': sender_blocked_recipient,
                        'recipient_blocked_sender': delivery_blocked,
                    },
                    'contact': {
                        'alias_name': 'Recipient',
                        'blocked': sender_blocked_recipient,
                    },
                },
                'status_code': 200,
            },
        }, 200

    @patch('messaging.views.broadcast_participant_event')
    @patch('messaging.views.broadcast_room_event')
    @patch('messaging.views.authorize_parent_messaging')
    def test_send_accepts_reply_target_and_broadcasts_reply_preview(
        self,
        authorize_parent_messaging,
        broadcast_room_event,
        broadcast_participant_event,
    ):
        room = self.create_direct_room()
        original_message = Message.objects.create(
            room=room,
            sender_user_id=self.recipient_user_id,
            recipient_user_id=self.sender_user_id,
            text='Earlier message.',
        )
        authorize_parent_messaging.return_value = self.parent_allowed()

        response = self.post_send_message(
            {
                'recipient_account_number': self.recipient_account_number,
                'text': 'Replying to that.',
                'reply_to_message_id': original_message.id,
                'client_message_id': 'reply-message-1',
            }
        )

        self.assertEqual(response.status_code, 201)
        body = response.json()
        reply_message = Message.objects.order_by('-id').first()
        self.assertEqual(reply_message.reply_to_id, original_message.id)
        self.assertEqual(body['result']['message']['reply_to_message_id'], original_message.id)
        self.assertEqual(body['result']['message']['reply_to']['text'], 'Earlier message.')

        broadcast_room_event.assert_called_once()
        event_payload = broadcast_room_event.call_args.args[2]
        self.assertEqual(event_payload['message']['reply_to']['id'], original_message.id)
        broadcast_participant_event.assert_called_once()

    @patch('messaging.views.authorize_parent_messaging')
    def test_send_rejects_reply_target_outside_room(
        self,
        authorize_parent_messaging,
    ):
        self.create_direct_room()
        other_room = Room.objects.create(
            room_type=Room.TYPE_DIRECT,
            created_by_user_id=99,
        )
        Message.objects.create(
            room=other_room,
            sender_user_id=99,
            recipient_user_id=self.sender_user_id,
            text='Different room message.',
        )
        authorize_parent_messaging.return_value = self.parent_allowed()

        response = self.post_send_message(
            {
                'recipient_account_number': self.recipient_account_number,
                'text': 'This reply target is invalid.',
                'reply_to_message_id': Message.objects.get(room=other_room).id,
            }
        )

        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body['status'], 'error')
        self.assertIn('reply_to_message_id', body['result']['errors'])

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

    @patch('messaging.views.broadcast_participant_event')
    @patch('messaging.views.broadcast_room_event')
    @patch('messaging.views.authorize_parent_messaging')
    def test_send_creates_sent_only_message_when_recipient_blocked_sender(
        self,
        authorize_parent_messaging,
        broadcast_room_event,
        broadcast_participant_event,
    ):
        authorize_parent_messaging.return_value = self.parent_allowed(delivery_blocked=True)

        response = self.post_send_message(
            {
                'recipient_account_number': self.recipient_account_number,
                'text': 'This should stay sent only.',
            }
        )

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body['status'], 'sent')
        self.assertNotIn('delivery_blocked', body['authorization']['parent']['response'])

        message = Message.objects.get()
        self.assertEqual(message.status, Message.STATUS_SENT)
        self.assertTrue(message.delivery_blocked)

        broadcast_room_event.assert_not_called()
        broadcast_participant_event.assert_called_once()
        participants = broadcast_participant_event.call_args.args[0]
        self.assertEqual([participant['user_id'] for participant in participants], [self.sender_user_id])

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

    def parent_authorization(self, delivery_blocked=False):
        return {
            'sender_user_id': self.sender_user_id,
            'sender_account_number': self.sender_account_number,
            'recipient_user_id': self.recipient_user_id,
            'recipient_account_number': self.recipient_account_number,
            'delivery_blocked': delivery_blocked,
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

    def test_delivery_blocked_message_is_visible_only_to_sender_and_kept_sent(self):
        room = self.create_direct_room()

        send_result, send_status = create_direct_message(
            self.sender(),
            self.parent_authorization(delivery_blocked=True),
            {
                'recipient_account_number': self.recipient_account_number,
                'text': 'Blocked inbound message.',
            },
        )
        self.assertEqual(send_status, 201)
        self.assertEqual(send_result['status'], 'sent')

        message = Message.objects.get()
        self.assertEqual(message.status, Message.STATUS_SENT)
        self.assertTrue(message.delivery_blocked)
        self.assertTrue(message.sent_while_blocked)
        self.assertFalse(send_result['message']['sent_while_blocked'])

        sender_messages_result, sender_messages_status = list_room_messages(
            self.sender_user_id,
            room.id,
        )
        recipient_messages_result, recipient_messages_status = list_room_messages(
            self.recipient_user_id,
            room.id,
        )
        self.assertEqual(sender_messages_status, 200)
        self.assertEqual(recipient_messages_status, 200)
        self.assertEqual(
            [message['text'] for message in sender_messages_result['messages']],
            ['Blocked inbound message.'],
        )
        self.assertEqual(recipient_messages_result['messages'], [])

        recipient_rooms_result, recipient_rooms_status = list_user_rooms(self.recipient_user_id)
        self.assertEqual(recipient_rooms_status, 200)
        self.assertEqual(recipient_rooms_result['rooms'][0]['last_message'], None)
        self.assertEqual(recipient_rooms_result['rooms'][0]['unread_count'], 0)

        delivered_result, delivered_status = mark_room_delivered(
            self.recipient_user_id,
            room.id,
            {},
        )
        self.assertEqual(delivered_status, 200)
        self.assertEqual(delivered_result['updated_messages'], 0)

        read_result, read_status = mark_room_read(
            self.recipient_user_id,
            room.id,
            {},
        )
        self.assertEqual(read_status, 200)
        self.assertEqual(read_result['updated_messages'], 0)

        message.refresh_from_db()
        self.assertEqual(message.status, Message.STATUS_SENT)

    def test_released_blocked_message_is_delivered_and_marked_for_recipient(self):
        room = self.create_direct_room()

        create_direct_message(
            self.sender(),
            self.parent_authorization(delivery_blocked=True),
            {
                'recipient_account_number': self.recipient_account_number,
                'text': 'Blocked then released message.',
            },
        )
        message = Message.objects.get()

        release_result, release_status = release_room_blocked_messages(
            self.recipient_user_id,
            room.id,
        )
        self.assertEqual(release_status, 200)
        self.assertEqual(release_result['released_messages'], 1)
        self.assertEqual(release_result['updated_messages'], 1)
        self.assertEqual(release_result['last_delivered_message_id'], message.id)

        message.refresh_from_db()
        self.assertFalse(message.delivery_blocked)
        self.assertTrue(message.sent_while_blocked)
        self.assertEqual(message.status, Message.STATUS_DELIVERED)

        sender_messages_result, sender_messages_status = list_room_messages(
            self.sender_user_id,
            room.id,
        )
        recipient_messages_result, recipient_messages_status = list_room_messages(
            self.recipient_user_id,
            room.id,
        )
        self.assertEqual(sender_messages_status, 200)
        self.assertEqual(recipient_messages_status, 200)
        self.assertFalse(sender_messages_result['messages'][0]['sent_while_blocked'])
        self.assertTrue(recipient_messages_result['messages'][0]['sent_while_blocked'])
        self.assertEqual(recipient_messages_result['messages'][0]['status'], Message.STATUS_DELIVERED)

    def test_sender_blocked_recipient_message_can_still_be_delivered_and_read(self):
        room = self.create_direct_room()

        create_direct_message(
            self.sender(),
            self.parent_authorization(delivery_blocked=False),
            {
                'recipient_account_number': self.recipient_account_number,
                'text': 'Sender blocked recipient but sends anyway.',
            },
        )
        message = Message.objects.get()

        delivered_result, delivered_status = mark_room_delivered(
            self.recipient_user_id,
            room.id,
            {'last_delivered_message_id': message.id},
        )
        self.assertEqual(delivered_status, 200)
        self.assertEqual(delivered_result['updated_messages'], 1)

        message.refresh_from_db()
        self.assertEqual(message.status, Message.STATUS_DELIVERED)

        read_result, read_status = mark_room_read(
            self.recipient_user_id,
            room.id,
            {'last_read_message_id': message.id},
        )
        self.assertEqual(read_status, 200)
        self.assertEqual(read_result['updated_messages'], 1)

        message.refresh_from_db()
        self.assertEqual(message.status, Message.STATUS_READ)

    def test_delivery_blocked_is_rejected_for_group_room_messages(self):
        room = Room.objects.create(
            room_type=Room.TYPE_GROUP,
            created_by_user_id=self.sender_user_id,
        )

        with self.assertRaises(ValidationError) as error_context:
            Message.objects.create(
                room=room,
                sender_user_id=self.sender_user_id,
                recipient_user_id=self.recipient_user_id,
                text='Group messages cannot use direct-room block delivery.',
                delivery_blocked=True,
            )

        self.assertIn('delivery_blocked', error_context.exception.message_dict)

        with self.assertRaises(ValidationError) as blocked_marker_context:
            Message.objects.create(
                room=room,
                sender_user_id=self.sender_user_id,
                recipient_user_id=self.recipient_user_id,
                text='Group messages cannot use direct-room blocked markers.',
                sent_while_blocked=True,
            )

        self.assertIn('sent_while_blocked', blocked_marker_context.exception.message_dict)

    def test_group_room_messages_are_not_hidden_by_delivery_blocked_flag(self):
        room = Room.objects.create(
            room_type=Room.TYPE_GROUP,
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
        message = Message.objects.create(
            room=room,
            sender_user_id=self.sender_user_id,
            recipient_user_id=self.recipient_user_id,
            text='Future group message.',
        )
        Message.objects.filter(id=message.id).update(delivery_blocked=True)

        messages_result, messages_status = list_room_messages(self.recipient_user_id, room.id)
        self.assertEqual(messages_status, 200)
        self.assertEqual(
            [message['text'] for message in messages_result['messages']],
            ['Future group message.'],
        )
        self.assertEqual(get_room_unread_count(room.id, self.recipient_user_id), 1)

        delivered_result, delivered_status = mark_room_delivered(
            self.recipient_user_id,
            room.id,
            {'last_delivered_message_id': message.id},
        )
        self.assertEqual(delivered_status, 200)
        self.assertEqual(delivered_result['updated_messages'], 1)

        message.refresh_from_db()
        self.assertEqual(message.status, Message.STATUS_DELIVERED)

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

    def test_list_room_messages_defaults_to_twenty_messages_per_page(self):
        room = self.create_direct_room()
        for index in range(25):
            Message.objects.create(
                room=room,
                sender_user_id=self.sender_user_id,
                recipient_user_id=self.recipient_user_id,
                text=f'Message {index + 1}',
            )

        first_page_result, first_page_status = list_room_messages(
            self.sender_user_id,
            room.id,
        )
        self.assertEqual(first_page_status, 200)
        self.assertEqual(len(first_page_result['messages']), 20)
        self.assertTrue(first_page_result['pagination']['has_more'])
        self.assertIsNotNone(first_page_result['pagination']['next_before_message_id'])

        second_page_result, second_page_status = list_room_messages(
            self.sender_user_id,
            room.id,
            before_message_id=first_page_result['pagination']['next_before_message_id'],
        )
        self.assertEqual(second_page_status, 200)
        self.assertEqual(len(second_page_result['messages']), 5)
        self.assertFalse(second_page_result['pagination']['has_more'])

    def test_list_room_messages_includes_reply_preview_for_unloaded_target(self):
        room = self.create_direct_room()
        original_message = Message.objects.create(
            room=room,
            sender_user_id=self.recipient_user_id,
            recipient_user_id=self.sender_user_id,
            text='Original reply target.',
        )
        for index in range(10):
            Message.objects.create(
                room=room,
                sender_user_id=self.sender_user_id,
                recipient_user_id=self.recipient_user_id,
                text=f'Filler message {index + 1}',
            )
        Message.objects.create(
            room=room,
            reply_to=original_message,
            sender_user_id=self.sender_user_id,
            recipient_user_id=self.recipient_user_id,
            text='Reply on latest page.',
        )

        messages_result, messages_status = list_room_messages(
            self.sender_user_id,
            room.id,
            limit=5,
        )

        self.assertEqual(messages_status, 200)
        self.assertNotIn(
            original_message.id,
            [message['id'] for message in messages_result['messages']],
        )
        reply_message = messages_result['messages'][-1]
        self.assertEqual(reply_message['text'], 'Reply on latest page.')
        self.assertEqual(reply_message['reply_to_message_id'], original_message.id)
        self.assertEqual(reply_message['reply_to']['text'], 'Original reply target.')

    def test_list_room_messages_around_message_returns_target_page(self):
        room = self.create_direct_room()
        target_message = None
        for index in range(15):
            message = Message.objects.create(
                room=room,
                sender_user_id=self.sender_user_id,
                recipient_user_id=self.recipient_user_id,
                text=f'Message {index + 1}',
            )
            if index == 3:
                target_message = message

        messages_result, messages_status = list_room_messages(
            self.sender_user_id,
            room.id,
            limit=5,
            around_message_id=target_message.id,
        )

        self.assertEqual(messages_status, 200)
        self.assertEqual(messages_result['pagination']['target_message_id'], target_message.id)
        self.assertIn(
            target_message.id,
            [message['id'] for message in messages_result['messages']],
        )
        self.assertTrue(messages_result['pagination']['has_newer'])
