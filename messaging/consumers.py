from uuid import uuid4
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


def get_presence_connections_key(user_id):
    return f'messaging:presence:user:{int(user_id)}:connections'


def get_presence_connection_key(user_id, connection_id):
    return f'messaging:presence:user:{int(user_id)}:connection:{connection_id}'


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
                'expires_in': self.get_typing_timeout(),
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
                    'expires_in': self.get_typing_timeout(),
                },
            },
        )

    def get_typing_timeout(self):
        return getattr(settings, 'MESSAGING_TYPING_TTL_SECONDS', 7)

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
            timeout=self.get_typing_timeout(),
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
        self.connection_id = uuid4().hex
        self.user_group_name = get_user_group_name(self.user_id)

        await self.channel_layer.group_add(self.user_group_name, self.channel_name)
        await self.accept()
        became_online = await self.add_presence_connection()
        online_user_ids = await self.get_visible_online_user_ids()
        await self.send_json(
            {
                'type': 'connection.accepted',
                'scope': 'inbox',
                'user_id': self.user_id,
            }
        )
        await self.send_json(
            {
                'type': 'presence.snapshot',
                'online_user_ids': online_user_ids,
                'expires_in': self.get_presence_timeout(),
            }
        )

        if became_online:
            await self.broadcast_presence_event('presence.online')

    async def disconnect(self, close_code):
        if hasattr(self, 'connection_id') and hasattr(self, 'user_id'):
            became_offline = await self.remove_presence_connection()
            if became_offline:
                await self.broadcast_presence_event('presence.offline')

        if hasattr(self, 'user_group_name'):
            await self.channel_layer.group_discard(self.user_group_name, self.channel_name)

    async def receive_json(self, content, **kwargs):
        if content.get('type') == 'ping':
            await self.refresh_presence_connection()
            await self.broadcast_presence_event('presence.online')
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

    async def broadcast_presence_event(self, event_type):
        recipient_user_ids = await self.get_presence_recipient_user_ids()

        for recipient_user_id in recipient_user_ids:
            await self.channel_layer.group_send(
                get_user_group_name(recipient_user_id),
                {
                    'type': 'room.event',
                    'payload': {
                        'type': event_type,
                        'user_id': self.user_id,
                        'account_number': self.account_number,
                        'expires_in': self.get_presence_timeout(),
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

    def get_presence_timeout(self):
        return getattr(settings, 'MESSAGING_PRESENCE_TTL_SECONDS', 60)

    def get_presence_list_timeout(self):
        return self.get_presence_timeout() * 2

    def prune_presence_connections(self, user_id, connection_ids=None):
        connections_key = get_presence_connections_key(user_id)
        current_connection_ids = connection_ids

        if current_connection_ids is None:
            current_connection_ids = cache.get(connections_key) or []

        active_connection_ids = [
            connection_id
            for connection_id in current_connection_ids
            if cache.get(get_presence_connection_key(user_id, connection_id)) is not None
        ]
        cache.set(
            connections_key,
            active_connection_ids,
            timeout=self.get_presence_list_timeout(),
        )

        return active_connection_ids

    def is_user_online(self, user_id):
        return bool(self.prune_presence_connections(user_id))

    @database_sync_to_async
    def add_presence_connection(self):
        was_online = self.is_user_online(self.user_id)
        connections_key = get_presence_connections_key(self.user_id)
        current_connection_ids = cache.get(connections_key) or []

        if self.connection_id not in current_connection_ids:
            current_connection_ids.append(self.connection_id)

        cache.set(
            get_presence_connection_key(self.user_id, self.connection_id),
            True,
            timeout=self.get_presence_timeout(),
        )
        cache.set(
            connections_key,
            current_connection_ids,
            timeout=self.get_presence_list_timeout(),
        )

        return not was_online

    @database_sync_to_async
    def refresh_presence_connection(self):
        if not getattr(self, 'connection_id', None):
            return

        cache.set(
            get_presence_connection_key(self.user_id, self.connection_id),
            True,
            timeout=self.get_presence_timeout(),
        )
        current_connection_ids = cache.get(get_presence_connections_key(self.user_id)) or []
        if self.connection_id not in current_connection_ids:
            current_connection_ids.append(self.connection_id)

        self.prune_presence_connections(self.user_id, current_connection_ids)

    @database_sync_to_async
    def remove_presence_connection(self):
        connections_key = get_presence_connections_key(self.user_id)
        cache.delete(get_presence_connection_key(self.user_id, self.connection_id))
        current_connection_ids = cache.get(connections_key) or []
        remaining_connection_ids = [
            connection_id
            for connection_id in current_connection_ids
            if connection_id != self.connection_id
        ]
        active_connection_ids = self.prune_presence_connections(
            self.user_id,
            remaining_connection_ids,
        )

        return not active_connection_ids

    def get_presence_recipient_user_ids_sync(self):
        room_ids = RoomParticipant.objects.filter(
            user_id=self.user_id,
            is_active=True,
        ).values_list('room_id', flat=True)

        return list(
            RoomParticipant.objects.filter(
                room_id__in=room_ids,
                is_active=True,
            )
            .exclude(user_id=self.user_id)
            .values_list('user_id', flat=True)
            .distinct()
        )

    @database_sync_to_async
    def get_presence_recipient_user_ids(self):
        return self.get_presence_recipient_user_ids_sync()

    @database_sync_to_async
    def get_visible_online_user_ids(self):
        visible_user_ids = self.get_presence_recipient_user_ids_sync()

        return [
            user_id
            for user_id in visible_user_ids
            if self.is_user_online(user_id)
        ]

    def get_close_code(self, status):
        if status == 401:
            return 4401

        return 4500
