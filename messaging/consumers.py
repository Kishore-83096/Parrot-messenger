from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.conf import settings
from django.core.cache import cache

from .auth import validate_messaging_token
from .models import RoomParticipant
from .realtime import get_room_group_name, get_user_group_name


def get_typing_cache_key(room_id, user_id):
    return f'messaging:typing:room:{int(room_id)}:user:{int(user_id)}'


class RoomConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        self.room_id = self.scope['url_route']['kwargs']['room_id']
        token_result, token_status = self.authenticate()

        if not token_result['ok']:
            await self.close(code=self.get_close_code(token_status))
            return

        self.user_id = token_result['sender_user_id']
        self.account_number = token_result.get('account_number')

        if not await self.is_room_participant():
            await self.close(code=4403)
            return

        self.room_group_name = get_room_group_name(self.room_id)
        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        await self.accept()
        active_typing_user_ids = await self.get_active_typing_user_ids()
        await self.send_json(
            {
                'type': 'connection.accepted',
                'room_id': self.room_id,
                'user_id': self.user_id,
            }
        )
        await self.send_json(
            {
                'type': 'typing.snapshot',
                'room_id': self.room_id,
                'typing_user_ids': active_typing_user_ids,
            }
        )

    async def disconnect(self, close_code):
        if hasattr(self, 'room_group_name') and hasattr(self, 'user_id'):
            was_typing = await self.clear_typing_state()
            if was_typing:
                await self.broadcast_typing_event('typing.stopped')

        if hasattr(self, 'room_group_name'):
            await self.channel_layer.group_discard(self.room_group_name, self.channel_name)

    async def receive_json(self, content, **kwargs):
        event_type = content.get('type')

        if event_type == 'ping':
            await self.send_json(
                {
                    'type': 'pong',
                    'room_id': self.room_id,
                    'user_id': self.user_id,
                }
            )
            return

        if event_type == 'typing.started':
            await self.set_typing_state()
            await self.broadcast_typing_event(event_type)
            return

        if event_type == 'typing.stopped':
            await self.clear_typing_state()
            await self.broadcast_typing_event(event_type)
            return

        await self.send_json(
            {
                'type': 'error',
                'message': 'Unsupported WebSocket event type.',
            }
        )

    async def room_event(self, event):
        await self.send_json(event['payload'])

    async def broadcast_typing_event(self, event_type):
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'room.event',
                'payload': {
                    'type': event_type,
                    'room_id': self.room_id,
                    'user_id': self.user_id,
                    'account_number': self.account_number,
                    'expires_in': getattr(settings, 'MESSAGING_TYPING_TTL_SECONDS', 7),
                },
            },
        )

    def authenticate(self):
        authorization_header = self.get_authorization_header()
        return validate_messaging_token(authorization_header)

    def get_authorization_header(self):
        query_params = parse_qs(self.scope.get('query_string', b'').decode('utf-8'))
        token = query_params.get('token', [''])[0]
        if token:
            return f'Bearer {token}'

        for header_name, header_value in self.scope.get('headers', []):
            if header_name == b'authorization':
                return header_value.decode('utf-8')

        return ''

    @database_sync_to_async
    def is_room_participant(self):
        return RoomParticipant.objects.filter(
            room_id=self.room_id,
            user_id=self.user_id,
            is_active=True,
        ).exists()

    @database_sync_to_async
    def get_active_typing_user_ids(self):
        participant_user_ids = RoomParticipant.objects.filter(
            room_id=self.room_id,
            is_active=True,
        ).exclude(user_id=self.user_id).values_list('user_id', flat=True)

        return [
            user_id
            for user_id in participant_user_ids
            if cache.get(get_typing_cache_key(self.room_id, user_id)) is not None
        ]

    @database_sync_to_async
    def set_typing_state(self):
        cache.set(
            get_typing_cache_key(self.room_id, self.user_id),
            True,
            timeout=getattr(settings, 'MESSAGING_TYPING_TTL_SECONDS', 7),
        )

    @database_sync_to_async
    def clear_typing_state(self):
        cache_key = get_typing_cache_key(self.room_id, self.user_id)
        was_typing = cache.get(cache_key) is not None
        cache.delete(cache_key)
        return was_typing

    def get_close_code(self, status):
        if status == 401:
            return 4401

        return 4500


class InboxConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        token_result, token_status = self.authenticate()

        if not token_result['ok']:
            await self.close(code=self.get_close_code(token_status))
            return

        self.user_id = token_result['sender_user_id']
        self.account_number = token_result.get('account_number')
        self.user_group_name = get_user_group_name(self.user_id)

        await self.channel_layer.group_add(self.user_group_name, self.channel_name)
        await self.accept()
        await self.send_json(
            {
                'type': 'connection.accepted',
                'scope': 'inbox',
                'user_id': self.user_id,
            }
        )

    async def disconnect(self, close_code):
        if hasattr(self, 'user_group_name'):
            await self.channel_layer.group_discard(self.user_group_name, self.channel_name)

    async def receive_json(self, content, **kwargs):
        if content.get('type') == 'ping':
            await self.send_json(
                {
                    'type': 'pong',
                    'scope': 'inbox',
                    'user_id': self.user_id,
                }
            )
            return

        await self.send_json(
            {
                'type': 'error',
                'message': 'Unsupported WebSocket event type.',
            }
        )

    async def room_event(self, event):
        await self.send_json(event['payload'])

    def authenticate(self):
        authorization_header = self.get_authorization_header()
        return validate_messaging_token(authorization_header)

    def get_authorization_header(self):
        query_params = parse_qs(self.scope.get('query_string', b'').decode('utf-8'))
        token = query_params.get('token', [''])[0]
        if token:
            return f'Bearer {token}'

        for header_name, header_value in self.scope.get('headers', []):
            if header_name == b'authorization':
                return header_value.decode('utf-8')

        return ''

    def get_close_code(self, status):
        if status == 401:
            return 4401

        return 4500
