import re

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .cache import (
    get_cached_room_messages,
    get_cached_user_rooms,
    invalidate_room_caches,
    set_cached_room_messages,
    set_cached_user_rooms,
)
from .models import Message, MessageAttachment, Room, RoomParticipant


ACCOUNT_NUMBER_PATTERN = re.compile(r'^7\d{9}$')
MAX_MESSAGE_TEXT_LENGTH = 5000
MAX_ATTACHMENTS_PER_MESSAGE = 10


def validation_error(errors):
    return {
        'status': 'error',
        'errors': errors,
    }, 400


def normalize_send_payload(payload):
    if not isinstance(payload, dict):
        return None, {'body': ['Request body must be a JSON object.']}

    errors = {}
    recipient_account_number = payload.get('recipient_account_number')
    if not isinstance(recipient_account_number, str) or not ACCOUNT_NUMBER_PATTERN.match(recipient_account_number):
        errors['recipient_account_number'] = [
            'Recipient account number must start with 7 and contain exactly 10 digits.',
        ]

    text = payload.get('text', '')
    if text is None:
        text = ''
    if not isinstance(text, str):
        errors['text'] = ['Message text must be a string.']
        text = ''
    elif len(text) > MAX_MESSAGE_TEXT_LENGTH:
        errors['text'] = [f'Message text cannot exceed {MAX_MESSAGE_TEXT_LENGTH} characters.']

    client_message_id = payload.get('client_message_id', '')
    if client_message_id is None:
        client_message_id = ''
    if not isinstance(client_message_id, str):
        errors['client_message_id'] = ['Client message id must be a string.']
        client_message_id = ''
    elif len(client_message_id) > 120:
        errors['client_message_id'] = ['Client message id cannot exceed 120 characters.']

    reply_to_message_id = payload.get('reply_to_message_id')
    if reply_to_message_id in ('', None):
        reply_to_message_id = None
    else:
        try:
            reply_to_message_id = int(reply_to_message_id)
        except (TypeError, ValueError):
            errors['reply_to_message_id'] = ['Reply target must be a message id.']

    attachments, attachment_errors = normalize_attachments(payload.get('attachments', []))
    if attachment_errors:
        errors['attachments'] = attachment_errors

    if not text.strip() and not attachments:
        errors['message'] = ['Message must include text or at least one attachment.']

    if errors:
        return None, errors

    return {
        'recipient_account_number': recipient_account_number,
        'text': text.strip(),
        'client_message_id': client_message_id.strip(),
        'reply_to_message_id': reply_to_message_id,
        'attachments': attachments,
    }, None


def normalize_attachments(value):
    if value in (None, ''):
        return [], None

    if not isinstance(value, list):
        return [], ['Attachments must be a list.']

    if len(value) > MAX_ATTACHMENTS_PER_MESSAGE:
        return [], [f'Cannot attach more than {MAX_ATTACHMENTS_PER_MESSAGE} files to one message.']

    allowed_file_types = {choice[0] for choice in MessageAttachment.FILE_TYPE_CHOICES}
    attachments = []
    errors = []

    for index, attachment in enumerate(value):
        if not isinstance(attachment, dict):
            errors.append({index: 'Attachment must be an object.'})
            continue

        file_type = attachment.get('file_type') or MessageAttachment.TYPE_OTHER
        file_url = attachment.get('file_url')
        item_errors = {}

        if file_type not in allowed_file_types:
            item_errors['file_type'] = f'Unsupported file type: {file_type}.'

        if not isinstance(file_url, str) or not file_url.strip():
            item_errors['file_url'] = 'Attachment file URL is required.'

        if item_errors:
            errors.append({index: item_errors})
            continue

        attachments.append(
            {
                'file_type': file_type,
                'file_url': file_url.strip(),
                'thumbnail_url': normalize_string(attachment.get('thumbnail_url')),
                'file_name': normalize_string(attachment.get('file_name')),
                'mime_type': normalize_string(attachment.get('mime_type')),
                'file_size_bytes': normalize_optional_positive_int(attachment.get('file_size_bytes')),
                'width': normalize_optional_positive_int(attachment.get('width')),
                'height': normalize_optional_positive_int(attachment.get('height')),
                'duration_seconds': normalize_optional_positive_int(attachment.get('duration_seconds')),
                'sort_order': normalize_optional_positive_int(attachment.get('sort_order')) or index,
            }
        )

    return attachments, errors or None


def normalize_string(value):
    if value is None:
        return ''

    return str(value).strip()


def normalize_optional_positive_int(value):
    if value in (None, ''):
        return None

    try:
        value = int(value)
    except (TypeError, ValueError):
        return None

    return value if value >= 0 else None


def get_or_create_direct_room(sender_user_id, recipient_user_id, sender_account_number, recipient_account_number):
    room = find_direct_room(sender_user_id, recipient_user_id)
    created = False

    if room is None:
        room = Room.objects.create(
            room_type=Room.TYPE_DIRECT,
            created_by_user_id=sender_user_id,
        )
        created = True

    ensure_room_participant(room, sender_user_id, sender_account_number)
    ensure_room_participant(room, recipient_user_id, recipient_account_number)

    return room, created


def find_direct_room(sender_user_id, recipient_user_id):
    return (
        Room.objects.filter(
            room_type=Room.TYPE_DIRECT,
            participants__user_id=sender_user_id,
            participants__is_active=True,
        )
        .filter(
            participants__user_id=recipient_user_id,
            participants__is_active=True,
        )
        .distinct()
        .first()
    )


def ensure_room_participant(room, user_id, account_number):
    participant, created = RoomParticipant.objects.get_or_create(
        room=room,
        user_id=user_id,
        defaults={
            'account_number': account_number or '',
            'role': RoomParticipant.ROLE_MEMBER,
            'is_active': True,
        },
    )

    changed = False
    if not participant.is_active:
        participant.is_active = True
        changed = True

    if account_number and participant.account_number != account_number:
        participant.account_number = account_number
        changed = True

    if changed:
        participant.save()

    return participant, created


def get_existing_direct_room_authorization(sender, recipient_account_number):
    if not isinstance(recipient_account_number, str) or not ACCOUNT_NUMBER_PATTERN.match(recipient_account_number):
        return None

    recipient_participant = (
        RoomParticipant.objects.filter(
            room__room_type=Room.TYPE_DIRECT,
            room__participants__user_id=sender['user_id'],
            room__participants__is_active=True,
            account_number=recipient_account_number,
            is_active=True,
        )
        .exclude(user_id=sender['user_id'])
        .select_related('room')
        .order_by('-room__updated_at', '-room__id', 'id')
        .first()
    )
    if not recipient_participant:
        return None

    sender_participant = RoomParticipant.objects.filter(
        room_id=recipient_participant.room_id,
        user_id=sender['user_id'],
        is_active=True,
    ).first()
    if not sender_participant:
        return None

    return {
        'sender_user_id': sender['user_id'],
        'sender_account_number': sender.get('account_number') or sender_participant.account_number,
        'recipient_user_id': recipient_participant.user_id,
        'recipient_account_number': recipient_participant.account_number,
        'room_id': recipient_participant.room_id,
        'room_type': recipient_participant.room.room_type,
        'authorization_source': 'shared_room',
    }


def create_direct_message(sender, parent_authorization, payload):
    normalized_payload, errors = normalize_send_payload(payload)
    if errors:
        return validation_error(errors)

    if normalized_payload['client_message_id']:
        existing_message = Message.objects.filter(
            sender_user_id=sender['user_id'],
            client_message_id=normalized_payload['client_message_id'],
        ).first()
        if existing_message:
            return {
                'status': 'duplicate',
                'room': serialize_room(existing_message.room),
                'message': serialize_message(existing_message, sender['user_id']),
                '_delivery_blocked': existing_message.delivery_blocked,
            }, 200

    recipient_user_id = parent_authorization['recipient_user_id']
    recipient_account_number = parent_authorization['recipient_account_number']
    delivery_blocked = bool(parent_authorization.get('delivery_blocked'))

    try:
        with transaction.atomic():
            room, room_created = get_or_create_direct_room(
                sender_user_id=sender['user_id'],
                recipient_user_id=recipient_user_id,
                sender_account_number=sender.get('account_number') or parent_authorization.get('sender_account_number'),
                recipient_account_number=recipient_account_number,
            )
            reply_to = get_reply_target(room, normalized_payload['reply_to_message_id'], sender['user_id'])

            message = Message.objects.create(
                room=room,
                reply_to=reply_to,
                sender_user_id=sender['user_id'],
                recipient_user_id=recipient_user_id,
                text=normalized_payload['text'],
                client_message_id=normalized_payload['client_message_id'],
                status=Message.STATUS_SENT,
                delivery_blocked=delivery_blocked,
                sent_while_blocked=delivery_blocked,
            )
            create_message_attachments(message, normalized_payload['attachments'])
            room.updated_at = timezone.now()
            room.save(update_fields=['updated_at'])
    except Message.DoesNotExist:
        return validation_error({'reply_to_message_id': ['Reply target message was not found in this room.']})
    except ValidationError as error:
        return validation_error(error.message_dict if hasattr(error, 'message_dict') else error.messages)

    invalidate_room_caches(room.id)

    return {
        'status': 'sent',
        'room_created': room_created,
        'room': serialize_room(room),
        'message': serialize_message(message, sender['user_id']),
        '_delivery_blocked': message.delivery_blocked,
    }, 201


def get_reply_target(room, reply_to_message_id, user_id):
    if not reply_to_message_id:
        return None

    return get_visible_messages_queryset(user_id, room_id=room.id).get(pk=reply_to_message_id)


def create_message_attachments(message, attachments):
    for attachment in attachments:
        MessageAttachment.objects.create(message=message, **attachment)


def release_room_blocked_messages(user_id, room_id):
    participant = RoomParticipant.objects.filter(
        room_id=room_id,
        user_id=user_id,
        is_active=True,
    ).select_related('room').first()
    if not participant:
        return {
            'status': 'error',
            'message': 'Room not found.',
        }, 404

    if not participant.room.is_direct:
        return validation_error({'room': ['Blocked message release is only supported for direct rooms.']})

    blocked_messages = Message.objects.filter(
        room_id=room_id,
        recipient_user_id=user_id,
        delivery_blocked=True,
        deleted_at__isnull=True,
    ).exclude(
        sender_user_id=user_id,
    ).order_by('created_at', 'id')
    blocked_message_ids = list(blocked_messages.values_list('id', flat=True))
    if not blocked_message_ids:
        return {
            'status': 'released',
            'room_id': participant.room_id,
            'user_id': user_id,
            'released_messages': 0,
            'updated_messages': 0,
            'last_delivered_message_id': None,
            'delivered_until': timezone.now().isoformat(),
            'unread_count': get_room_unread_count(participant.room_id, user_id),
            'room': serialize_room(participant.room),
        }, 200

    now = timezone.now()
    last_released_message = Message.objects.get(pk=blocked_message_ids[-1])

    Message.objects.filter(id__in=blocked_message_ids).update(
        delivery_blocked=False,
        updated_at=now,
    )
    delivered_count = Message.objects.filter(
        id__in=blocked_message_ids,
        status=Message.STATUS_SENT,
    ).update(
        status=Message.STATUS_DELIVERED,
        updated_at=now,
    )

    invalidate_room_caches(participant.room_id)

    return {
        'status': 'released',
        'room_id': participant.room_id,
        'user_id': user_id,
        'released_messages': len(blocked_message_ids),
        'updated_messages': delivered_count,
        'last_delivered_message_id': last_released_message.id,
        'delivered_until': last_released_message.created_at.isoformat(),
        'unread_count': get_room_unread_count(participant.room_id, user_id),
        'room': serialize_room(participant.room),
    }, 200


def list_user_rooms(user_id):
    cached_result = get_cached_user_rooms(user_id)
    if cached_result is not None:
        return cached_result, 200

    participants = (
        RoomParticipant.objects.filter(user_id=user_id, is_active=True)
        .select_related('room')
        .prefetch_related('room__participants', 'room__messages')
        .order_by('-room__updated_at', '-room__id')
    )

    rooms = []
    for participant in participants:
        room = participant.room
        rooms.append(serialize_room_summary(room, user_id))

    result = {
        'status': 'ok',
        'total_unread_count': sum(room['unread_count'] for room in rooms),
        'unread_rooms_count': sum(1 for room in rooms if room['has_unread']),
        'rooms': rooms,
    }
    set_cached_user_rooms(user_id, result)

    return result, 200


def get_room_unread_count(room_id, user_id):
    return (
        Message.objects.filter(
            room_id=room_id,
            recipient_user_id=user_id,
            deleted_at__isnull=True,
        )
        .exclude(sender_user_id=user_id)
        .exclude(room__room_type=Room.TYPE_DIRECT, delivery_blocked=True)
        .exclude(status=Message.STATUS_READ)
        .count()
    )


def list_room_messages(user_id, room_id, limit=20, before_message_id=None, around_message_id=None):
    cached_result = get_cached_room_messages(
        user_id,
        room_id,
        limit,
        before_message_id,
        around_message_id,
    )
    if cached_result is not None:
        return cached_result, 200

    participant = RoomParticipant.objects.filter(
        room_id=room_id,
        user_id=user_id,
        is_active=True,
    ).first()
    if not participant:
        return {
            'status': 'error',
            'message': 'Room not found.',
        }, 404

    messages = get_visible_messages_queryset(user_id, room_id=room_id).select_related('reply_to')
    if around_message_id:
        result, status = list_room_messages_around_target(
            user_id=user_id,
            room=participant.room,
            messages=messages,
            limit=limit,
            around_message_id=around_message_id,
        )
        if status < 300:
            set_cached_room_messages(
                user_id,
                room_id,
                limit,
                before_message_id,
                result,
                around_message_id,
            )

        return result, status

    if before_message_id:
        messages = messages.filter(id__lt=before_message_id)

    fetched_messages = list(messages.order_by('-created_at', '-id')[: limit + 1])
    has_more = len(fetched_messages) > limit
    page_messages = fetched_messages[:limit]
    serialized_messages = [
        serialize_message(message, user_id)
        for message in reversed(page_messages)
    ]
    next_before_message_id = page_messages[-1].id if has_more and page_messages else None

    result = {
        'status': 'ok',
        'room': serialize_room(participant.room),
        'messages': serialized_messages,
        'pagination': {
            'limit': limit,
            'before_message_id': before_message_id,
            'count': len(serialized_messages),
            'has_more': has_more,
            'next_before_message_id': next_before_message_id,
        },
    }
    set_cached_room_messages(user_id, room_id, limit, before_message_id, result)

    return result, 200


def list_room_messages_around_target(user_id, room, messages, limit, around_message_id):
    target_message = messages.filter(id=around_message_id).first()
    if not target_message:
        return {
            'status': 'error',
            'message': 'Message not found.',
        }, 404

    older_limit = max((limit - 1) // 2, 0)
    newer_limit = max(limit - older_limit - 1, 0)
    older_messages = list(
        messages.filter(id__lt=target_message.id).order_by('-created_at', '-id')[:older_limit]
    )
    newer_messages = list(
        messages.filter(id__gt=target_message.id).order_by('created_at', 'id')[:newer_limit]
    )
    page_messages = sorted(
        [*older_messages, target_message, *newer_messages],
        key=lambda message: (message.created_at, message.id),
    )
    oldest_message = page_messages[0] if page_messages else None
    newest_message = page_messages[-1] if page_messages else None
    has_more_older = (
        messages.filter(id__lt=oldest_message.id).exists()
        if oldest_message
        else False
    )
    has_more_newer = (
        messages.filter(id__gt=newest_message.id).exists()
        if newest_message
        else False
    )

    return {
        'status': 'ok',
        'room': serialize_room(room),
        'messages': [
            serialize_message(message, user_id)
            for message in page_messages
        ],
        'pagination': {
            'limit': limit,
            'before_message_id': None,
            'around_message_id': around_message_id,
            'target_message_id': target_message.id,
            'count': len(page_messages),
            'has_more': has_more_older,
            'next_before_message_id': oldest_message.id if has_more_older and oldest_message else None,
            'has_newer': has_more_newer,
            'next_after_message_id': newest_message.id if has_more_newer and newest_message else None,
        },
    }, 200


def mark_room_delivered(user_id, room_id, payload):
    normalized_payload, errors = normalize_delivered_payload(payload)
    if errors:
        return validation_error(errors)

    participant = RoomParticipant.objects.filter(
        room_id=room_id,
        user_id=user_id,
        is_active=True,
    ).select_related('room').first()
    if not participant:
        return {
            'status': 'error',
            'message': 'Room not found.',
        }, 404

    received_messages = Message.objects.filter(
        room_id=room_id,
        recipient_user_id=user_id,
        deleted_at__isnull=True,
    ).exclude(
        sender_user_id=user_id,
    )
    if participant.room.is_direct:
        received_messages = received_messages.filter(delivery_blocked=False)

    delivered_message = None
    if normalized_payload['last_delivered_message_id']:
        delivered_message = received_messages.filter(
            id=normalized_payload['last_delivered_message_id'],
        ).first()
        if not delivered_message:
            return validation_error(
                {
                    'last_delivered_message_id': [
                        'Last delivered message must be a received message in this room.',
                    ],
                }
            )
        delivered_until = delivered_message.created_at
    else:
        delivered_message = (
            received_messages.order_by('-created_at', '-id').first()
        )
        delivered_until = delivered_message.created_at if delivered_message else timezone.now()

    delivered_count = received_messages.filter(
        created_at__lte=delivered_until,
        status=Message.STATUS_SENT,
    ).update(
        status=Message.STATUS_DELIVERED,
        updated_at=timezone.now(),
    )

    invalidate_room_caches(participant.room_id)

    return {
        'status': 'delivered',
        'room_id': participant.room_id,
        'user_id': user_id,
        'last_delivered_message_id': delivered_message.id if delivered_message else None,
        'delivered_until': delivered_until.isoformat(),
        'updated_messages': delivered_count,
        'unread_count': get_room_unread_count(participant.room_id, user_id),
        'room': serialize_room(participant.room),
    }, 200


def mark_room_read(user_id, room_id, payload):
    normalized_payload, errors = normalize_read_payload(payload)
    if errors:
        return validation_error(errors)

    participant = RoomParticipant.objects.filter(
        room_id=room_id,
        user_id=user_id,
        is_active=True,
    ).select_related('room').first()
    if not participant:
        return {
            'status': 'error',
            'message': 'Room not found.',
        }, 404

    read_message = None
    if normalized_payload['last_read_message_id']:
        read_message = get_visible_messages_queryset(
            user_id,
            room_id=room_id,
        ).filter(id=normalized_payload['last_read_message_id']).first()
        if not read_message:
            return validation_error(
                {
                    'last_read_message_id': [
                        'Last read message was not found in this room.',
                    ],
                }
            )
        requested_read_at = read_message.created_at
    else:
        read_message = (
            get_visible_messages_queryset(user_id, room_id=room_id)
            .order_by('-created_at', '-id')
            .first()
        )
        requested_read_at = read_message.created_at if read_message else timezone.now()

    current_read_at = participant.last_read_at
    final_read_at = requested_read_at
    read_marker_moved = True
    if current_read_at and current_read_at >= requested_read_at:
        final_read_at = current_read_at
        read_marker_moved = False

    updated_messages = 0
    with transaction.atomic():
        if read_marker_moved:
            participant.last_read_at = final_read_at
            participant.save(update_fields=['last_read_at'])

        if participant.room.is_direct:
            updated_messages = Message.objects.filter(
                room_id=room_id,
                recipient_user_id=user_id,
                delivery_blocked=False,
                deleted_at__isnull=True,
                created_at__lte=final_read_at,
            ).exclude(
                sender_user_id=user_id,
            ).exclude(
                status=Message.STATUS_READ,
            ).update(
                status=Message.STATUS_READ,
                updated_at=timezone.now(),
            )

    invalidate_room_caches(participant.room_id)

    return {
        'status': 'read',
        'room_id': participant.room_id,
        'user_id': user_id,
        'last_read_message_id': read_message.id if read_message else None,
        'last_read_at': final_read_at.isoformat(),
        'read_marker_moved': read_marker_moved,
        'updated_messages': updated_messages,
        'unread_count': get_room_unread_count(participant.room_id, user_id),
        'message_status_updated': participant.room.is_direct,
        'room': serialize_room(participant.room),
    }, 200


def normalize_message_list_params(params):
    errors = {}
    limit = params.get('limit', 20)
    before_message_id = params.get('before_message_id')
    around_message_id = params.get('around_message_id')

    try:
        limit = int(limit)
    except (TypeError, ValueError):
        errors['limit'] = ['Limit must be a number.']
        limit = 20

    if limit < 1 or limit > 100:
        errors['limit'] = ['Limit must be between 1 and 100.']

    if before_message_id in ('', None):
        before_message_id = None
    else:
        try:
            before_message_id = int(before_message_id)
        except (TypeError, ValueError):
            errors['before_message_id'] = ['before_message_id must be a message id.']

    if around_message_id in ('', None):
        around_message_id = None
    else:
        try:
            around_message_id = int(around_message_id)
        except (TypeError, ValueError):
            errors['around_message_id'] = ['around_message_id must be a message id.']

    if before_message_id and around_message_id:
        errors['message'] = ['Use either before_message_id or around_message_id, not both.']

    if errors:
        return None, None, None, errors

    return limit, before_message_id, around_message_id, None


def normalize_read_payload(payload):
    if not isinstance(payload, dict):
        return None, {'body': ['Request body must be a JSON object.']}

    errors = {}
    last_read_message_id = payload.get('last_read_message_id')
    if last_read_message_id in ('', None):
        last_read_message_id = None
    else:
        try:
            last_read_message_id = int(last_read_message_id)
        except (TypeError, ValueError):
            errors['last_read_message_id'] = ['Last read message id must be a number.']

    if errors:
        return None, errors

    return {
        'last_read_message_id': last_read_message_id,
    }, None


def normalize_delivered_payload(payload):
    if not isinstance(payload, dict):
        return None, {'body': ['Request body must be a JSON object.']}

    errors = {}
    last_delivered_message_id = payload.get('last_delivered_message_id')
    if last_delivered_message_id in ('', None):
        last_delivered_message_id = None
    else:
        try:
            last_delivered_message_id = int(last_delivered_message_id)
        except (TypeError, ValueError):
            errors['last_delivered_message_id'] = ['Last delivered message id must be a number.']

    if errors:
        return None, errors

    return {
        'last_delivered_message_id': last_delivered_message_id,
    }, None


def get_visible_messages_queryset(user_id, room_id=None):
    messages = Message.objects.filter(deleted_at__isnull=True)
    if room_id is not None:
        messages = messages.filter(room_id=room_id)

    return messages.exclude(
        room__room_type=Room.TYPE_DIRECT,
        recipient_user_id=user_id,
        delivery_blocked=True,
    )


def serialize_room_summary(room, current_user_id):
    latest_message = (
        get_visible_messages_queryset(current_user_id, room_id=room.id)
        .order_by('-created_at', '-id')
        .first()
    )
    room_data = serialize_room(room)
    current_participant = next(
        (
            participant
            for participant in room_data['participants']
            if participant['user_id'] == current_user_id
        ),
        None,
    )
    room_data['other_participants'] = [
        participant
        for participant in room_data['participants']
        if participant['user_id'] != current_user_id
    ]
    room_data['last_message'] = serialize_message(latest_message, current_user_id) if latest_message else None
    room_data['unread_count'] = get_room_unread_count(room.id, current_user_id)
    room_data['has_unread'] = room_data['unread_count'] > 0
    room_data['my_last_read_at'] = current_participant['last_read_at'] if current_participant else None

    return room_data


def serialize_room(room):
    return {
        'id': room.id,
        'room_type': room.room_type,
        'is_group': room.is_group,
        'created_by_user_id': room.created_by_user_id,
        'created_at': room.created_at.isoformat(),
        'updated_at': room.updated_at.isoformat(),
        'participants': [
            serialize_participant(participant)
            for participant in room.participants.filter(is_active=True).order_by('joined_at', 'id')
        ],
    }


def serialize_participant(participant):
    return {
        'user_id': participant.user_id,
        'account_number': participant.account_number,
        'display_name': participant.display_name,
        'role': participant.role,
        'is_active': participant.is_active,
        'joined_at': participant.joined_at.isoformat(),
        'last_read_at': participant.last_read_at.isoformat() if participant.last_read_at else None,
    }


def serialize_message(message, current_user_id=None):
    data = {
        'id': message.id,
        'room_id': message.room_id,
        'sender_user_id': message.sender_user_id,
        'recipient_user_id': message.recipient_user_id,
        'reply_to_message_id': message.reply_to_id,
        'reply_to': serialize_reply_preview(message.reply_to, current_user_id) if message.reply_to_id else None,
        'text': message.text,
        'client_message_id': message.client_message_id,
        'status': message.status,
        'sent_while_blocked': is_sent_while_blocked_visible(message, current_user_id),
        'created_at': message.created_at.isoformat(),
        'updated_at': message.updated_at.isoformat(),
        'attachments': [
            serialize_attachment(attachment)
            for attachment in message.attachments.all().order_by('sort_order', 'id')
        ],
    }

    return data


def serialize_reply_preview(message, current_user_id=None):
    if not message or message.deleted_at:
        return None

    if (
        current_user_id
        and message.delivery_blocked
        and int(message.recipient_user_id or 0) == int(current_user_id)
    ):
        return None

    return {
        'id': message.id,
        'room_id': message.room_id,
        'sender_user_id': message.sender_user_id,
        'recipient_user_id': message.recipient_user_id,
        'text': message.text,
        'attachment_count': message.attachments.count(),
        'created_at': message.created_at.isoformat(),
    }


def is_sent_while_blocked_visible(message, current_user_id):
    if not current_user_id or not message.sent_while_blocked:
        return False

    return int(message.recipient_user_id or 0) == int(current_user_id)


def serialize_attachment(attachment):
    return {
        'id': attachment.id,
        'file_type': attachment.file_type,
        'file_url': attachment.file_url,
        'thumbnail_url': attachment.thumbnail_url,
        'file_name': attachment.file_name,
        'mime_type': attachment.mime_type,
        'file_size_bytes': attachment.file_size_bytes,
        'width': attachment.width,
        'height': attachment.height,
        'duration_seconds': attachment.duration_seconds,
        'sort_order': attachment.sort_order,
        'created_at': attachment.created_at.isoformat(),
    }
