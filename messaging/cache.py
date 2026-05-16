from django.conf import settings
from django.core.cache import cache

from .models import RoomParticipant


CACHE_NAMESPACE = 'messaging'
CACHE_VERSION_TIMEOUT = None


def get_room_list_cache_timeout():
    return getattr(settings, 'MESSAGING_ROOM_LIST_CACHE_TTL_SECONDS', 900)


def get_room_messages_cache_timeout():
    return getattr(settings, 'MESSAGING_ROOM_MESSAGES_CACHE_TTL_SECONDS', 900)


def get_cache_version(version_key):
    version = cache.get(version_key)
    if version is None:
        version = 1
        cache.set(version_key, version, timeout=CACHE_VERSION_TIMEOUT)

    return version


def bump_cache_version(version_key):
    try:
        cache.incr(version_key)
    except ValueError:
        cache.set(
            version_key,
            get_cache_version(version_key) + 1,
            timeout=CACHE_VERSION_TIMEOUT,
        )


def room_list_version_key(user_id):
    return f'{CACHE_NAMESPACE}:rooms:user:{user_id}:version'


def room_messages_version_key(room_id):
    return f'{CACHE_NAMESPACE}:room:{room_id}:messages:version'


def room_list_cache_key(user_id):
    version = get_cache_version(room_list_version_key(user_id))
    return f'{CACHE_NAMESPACE}:rooms:user:{user_id}:v:{version}'


def room_messages_cache_key(user_id, room_id, limit, before_message_id, around_message_id=None):
    version = get_cache_version(room_messages_version_key(room_id))
    before_key = before_message_id if before_message_id is not None else 'latest'
    around_key = around_message_id if around_message_id is not None else 'none'

    return (
        f'{CACHE_NAMESPACE}:room:{room_id}:messages:user:{user_id}:'
        f'limit:{limit}:before:{before_key}:around:{around_key}:v:{version}'
    )


def get_cached_user_rooms(user_id):
    return cache.get(room_list_cache_key(user_id))


def set_cached_user_rooms(user_id, result):
    cache.set(
        room_list_cache_key(user_id),
        result,
        timeout=get_room_list_cache_timeout(),
    )


def get_cached_room_messages(user_id, room_id, limit, before_message_id, around_message_id=None):
    return cache.get(room_messages_cache_key(user_id, room_id, limit, before_message_id, around_message_id))


def set_cached_room_messages(user_id, room_id, limit, before_message_id, result, around_message_id=None):
    cache.set(
        room_messages_cache_key(user_id, room_id, limit, before_message_id, around_message_id),
        result,
        timeout=get_room_messages_cache_timeout(),
    )


def get_active_room_user_ids(room_id):
    return list(
        RoomParticipant.objects.filter(room_id=room_id, is_active=True)
        .values_list('user_id', flat=True)
    )


def invalidate_user_room_list_cache(user_id):
    bump_cache_version(room_list_version_key(user_id))


def invalidate_room_messages_cache(room_id):
    bump_cache_version(room_messages_version_key(room_id))


def invalidate_room_caches(room_id, user_ids=None):
    invalidate_room_messages_cache(room_id)

    for user_id in user_ids or get_active_room_user_ids(room_id):
        invalidate_user_room_list_cache(user_id)
