import mimetypes
import re
import uuid
from pathlib import Path

from cloudinary import config as cloudinary_config
from cloudinary import uploader as cloudinary_uploader
from cloudinary.exceptions import Error as CloudinaryError
from django.conf import settings
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
from .e2ee.payloads import (
    MAX_ENCRYPTED_MESSAGE_TEXT_LENGTH,
    is_encrypted_message_text,
)
from .models import Message, MessageAttachment, MessageReaction, Room, RoomParticipant
from .signals import resolve_parent_receipt_visibility


ACCOUNT_NUMBER_PATTERN = re.compile(r'^7\d{9}$')
MAX_MESSAGE_TEXT_LENGTH = 5000
MAX_ATTACHMENTS_PER_MESSAGE = 10
MAIN_CLOUDINARY_FOLDER = 'MAIN'
IMAGE_EXTENSIONS = {'.avif', '.gif', '.heic', '.jpeg', '.jpg', '.png', '.webp'}
PDF_EXTENSIONS = {'.pdf'}
VIDEO_EXTENSIONS = {'.avi', '.m4v', '.mov', '.mp4', '.mpeg', '.mpg', '.webm'}
AUDIO_EXTENSIONS = {'.aac', '.flac', '.m4a', '.mp3', '.ogg', '.wav', '.webm'}
DOCUMENT_EXTENSIONS = {
    '.csv',
    '.doc',
    '.docx',
    '.json',
    '.md',
    '.ppt',
    '.pptx',
    '.rtf',
    '.txt',
    '.xls',
    '.xlsx',
}
BLOCKED_UPLOAD_EXTENSIONS = {
    '.bat',
    '.cmd',
    '.com',
    '.dll',
    '.exe',
    '.js',
    '.msi',
    '.ps1',
    '.scr',
    '.sh',
    '.vbs',
}
STORY_CONTEXT_MEDIA_TYPES = {'image', 'video', 'text'}


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
    else:
        max_text_length = (
            MAX_ENCRYPTED_MESSAGE_TEXT_LENGTH
            if is_encrypted_message_text(text)
            else MAX_MESSAGE_TEXT_LENGTH
        )
        if len(text) > max_text_length:
            errors['text'] = [f'Message text cannot exceed {max_text_length} characters.']

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

    story_context, story_context_errors = normalize_story_context(payload.get('story_context'))
    if story_context_errors:
        errors['story_context'] = story_context_errors

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
        'story_context': story_context,
    }, None


def normalize_story_context(value):
    if value in (None, ''):
        return {}, None

    if not isinstance(value, dict):
        return {}, ['Story context must be an object.']

    errors = {}
    story_id = normalize_string(value.get('story_id'))
    context_type = normalize_string(value.get('type'))
    media_type = normalize_string(value.get('media_type'))
    preview_label = normalize_string(value.get('preview_label')) or 'Story'
    created_at = normalize_string(value.get('created_at'))
    expires_at = normalize_string(value.get('expires_at'))

    if not story_id:
        errors['story_id'] = 'Story id is required.'
    else:
        try:
            story_id = str(uuid.UUID(story_id))
        except (TypeError, ValueError):
            errors['story_id'] = 'Story id is invalid.'

    if not context_type:
        errors['type'] = 'Story context type is required.'
    elif context_type not in {'reply', 'reaction'}:
        errors['type'] = 'Story context type must be reply or reaction.'

    if media_type and media_type not in STORY_CONTEXT_MEDIA_TYPES:
        errors['media_type'] = 'Story media type must be image, video, or text.'

    if len(preview_label) > 120:
        preview_label = preview_label[:120]

    if errors:
        return {}, errors

    return {
        'story_id': story_id,
        'type': context_type,
        'media_type': media_type,
        'preview_label': preview_label,
        'created_at': created_at,
        'expires_at': expires_at,
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
                'cloudinary_public_id': normalize_string(attachment.get('cloudinary_public_id')),
                'cloudinary_asset_id': normalize_string(attachment.get('cloudinary_asset_id')),
                'cloudinary_resource_type': normalize_string(attachment.get('cloudinary_resource_type')),
                'cloudinary_folder': normalize_string(attachment.get('cloudinary_folder')),
                'sort_order': normalize_optional_positive_int(attachment.get('sort_order')) or index,
            }
        )

    return attachments, errors or None


def upload_message_files(uploaded_files):
    uploaded_files = list(uploaded_files or [])
    if not uploaded_files:
        return [], None

    if len(uploaded_files) > MAX_ATTACHMENTS_PER_MESSAGE:
        return [], [f'Cannot attach more than {MAX_ATTACHMENTS_PER_MESSAGE} files to one message.']

    if not getattr(settings, 'CLOUDINARY_URL', ''):
        return [], ['Cloudinary is not configured for media uploads.']

    cloudinary_config(cloudinary_url=settings.CLOUDINARY_URL, secure=True)

    attachments = []
    errors = []

    for index, uploaded_file in enumerate(uploaded_files):
        normalized_file, file_errors = normalize_uploaded_message_file(uploaded_file, index)
        if file_errors:
            errors.append({index: file_errors})
            continue

        try:
            if hasattr(uploaded_file, 'seek'):
                uploaded_file.seek(0)
            upload_result = cloudinary_uploader.upload(
                uploaded_file,
                folder=normalized_file['cloudinary_folder'],
                resource_type=normalized_file['cloudinary_resource_type'],
                use_filename=True,
                unique_filename=True,
                overwrite=False,
            )
        except CloudinaryError as error:
            errors.append({index: str(error) or 'Unable to upload attachment.'})
            continue

        attachments.append(
            build_uploaded_attachment(upload_result, normalized_file, index)
        )

    if errors:
        cleanup_uploaded_attachments(attachments)
        return [], errors

    return attachments, None


def normalize_uploaded_message_file(uploaded_file, index):
    file_name = Path(uploaded_file.name or f'attachment-{index + 1}').name
    extension = Path(file_name).suffix.lower()
    mime_type = (
        getattr(uploaded_file, 'content_type', '') or
        mimetypes.guess_type(file_name)[0] or
        'application/octet-stream'
    )
    max_size = getattr(settings, 'MESSAGING_MAX_UPLOAD_FILE_SIZE_BYTES', 25 * 1024 * 1024)
    file_size = int(getattr(uploaded_file, 'size', 0) or 0)
    errors = {}

    if extension in BLOCKED_UPLOAD_EXTENSIONS:
        errors['file_name'] = 'This file type is not allowed.'

    if file_size <= 0:
        errors['file_size_bytes'] = 'Attachment file is empty.'
    elif file_size > max_size:
        errors['file_size_bytes'] = f'Attachment cannot exceed {max_size // (1024 * 1024)} MB.'

    file_type, resource_type, folder_suffix = get_upload_file_routing(mime_type, extension)
    if not file_type:
        errors['file_type'] = 'Unsupported attachment file type.'

    if errors:
        return None, errors

    root_folder = getattr(settings, 'CLOUDINARY_MAIN_FOLDER', MAIN_CLOUDINARY_FOLDER).strip('/') or MAIN_CLOUDINARY_FOLDER

    return {
        'file_name': file_name,
        'mime_type': mime_type,
        'file_size_bytes': file_size,
        'file_type': file_type,
        'cloudinary_resource_type': resource_type,
        'cloudinary_folder': f'{root_folder}/{folder_suffix}',
    }, None


def get_upload_file_routing(mime_type, extension):
    if mime_type.startswith('image/') or extension in IMAGE_EXTENSIONS:
        return MessageAttachment.TYPE_IMAGE, 'image', 'pics'

    if mime_type == 'application/pdf' or extension in PDF_EXTENSIONS:
        return MessageAttachment.TYPE_DOCUMENT, 'raw', 'pdfs'

    if mime_type.startswith('video/') or extension in VIDEO_EXTENSIONS:
        return MessageAttachment.TYPE_VIDEO, 'video', 'videos'

    if mime_type.startswith('audio/') or extension in AUDIO_EXTENSIONS:
        return MessageAttachment.TYPE_AUDIO, 'video', 'audio'

    if extension in DOCUMENT_EXTENSIONS or mime_type in {
        'application/json',
        'application/msword',
        'application/rtf',
        'application/vnd.ms-excel',
        'application/vnd.ms-powerpoint',
        'application/vnd.openxmlformats-officedocument.presentationml.presentation',
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'text/csv',
        'text/markdown',
        'text/plain',
    }:
        return MessageAttachment.TYPE_DOCUMENT, 'raw', 'docs'

    return None, None, None


def build_uploaded_attachment(upload_result, normalized_file, index):
    resource_type = upload_result.get('resource_type') or normalized_file['cloudinary_resource_type']
    duration = upload_result.get('duration')

    return {
        'file_type': normalized_file['file_type'],
        'file_url': upload_result.get('secure_url') or upload_result.get('url') or '',
        'thumbnail_url': upload_result.get('secure_url', '') if normalized_file['file_type'] == MessageAttachment.TYPE_IMAGE else '',
        'file_name': normalized_file['file_name'],
        'mime_type': normalized_file['mime_type'],
        'file_size_bytes': upload_result.get('bytes') or normalized_file['file_size_bytes'],
        'width': normalize_optional_positive_int(upload_result.get('width')),
        'height': normalize_optional_positive_int(upload_result.get('height')),
        'duration_seconds': normalize_optional_positive_int(round(duration)) if duration else None,
        'cloudinary_public_id': upload_result.get('public_id', ''),
        'cloudinary_asset_id': upload_result.get('asset_id', ''),
        'cloudinary_resource_type': resource_type,
        'cloudinary_folder': normalized_file['cloudinary_folder'],
        'sort_order': index,
    }


def cleanup_uploaded_attachments(attachments):
    for attachment in attachments or []:
        public_id = attachment.get('cloudinary_public_id')
        resource_type = attachment.get('cloudinary_resource_type') or 'raw'
        if not public_id:
            continue

        try:
            cloudinary_uploader.destroy(public_id, resource_type=resource_type)
        except CloudinaryError:
            pass


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


def has_existing_sender_client_message(sender_user_id, client_message_id):
    client_message_id = normalize_string(client_message_id)
    if not client_message_id:
        return False

    return Message.objects.filter(
        sender_user_id=sender_user_id,
        client_message_id=client_message_id,
    ).exists()


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
                story_context=normalized_payload['story_context'],
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
    participant = (
        RoomParticipant.objects.filter(
            room_id=room_id,
            user_id=user_id,
            is_active=True,
        )
        .select_related('room')
        .first()
    )
    if not participant:
        return 0

    messages = Message.objects.filter(
        room_id=room_id,
        recipient_user_id=user_id,
        deleted_at__isnull=True,
    ).exclude(sender_user_id=user_id)

    if participant.room.is_direct:
        messages = messages.filter(delivery_blocked=False)
        if participant.last_read_at:
            messages = messages.filter(created_at__gt=participant.last_read_at)
        return messages.count()

    return messages.exclude(status=Message.STATUS_READ).count()


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

    messages = (
        get_visible_messages_queryset(user_id, room_id=room_id)
        .select_related('reply_to')
        .prefetch_related('attachments', 'reactions')
    )
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


def react_to_message(user_id, message_id, payload):
    normalized_payload, errors = normalize_reaction_payload(payload)
    if errors:
        return validation_error(errors)

    message = (
        get_visible_messages_queryset(user_id)
        .select_related('room')
        .prefetch_related('reactions')
        .filter(id=message_id)
        .first()
    )
    if not message:
        return {
            'status': 'error',
            'message': 'Message not found.',
        }, 404

    participant = RoomParticipant.objects.filter(
        room_id=message.room_id,
        user_id=user_id,
        is_active=True,
    ).first()
    if not participant:
        return {
            'status': 'error',
            'message': 'Message not found.',
        }, 404

    requested_reaction = normalized_payload['reaction']
    previous_reaction = None
    action = 'set'

    with transaction.atomic():
        reaction = (
            MessageReaction.objects.select_for_update()
            .filter(message_id=message.id, user_id=user_id)
            .first()
        )

        if reaction:
            previous_reaction = reaction.reaction

        if reaction and reaction.reaction == requested_reaction:
            reaction.delete()
            action = 'removed'
            current_reaction = None
        elif reaction:
            reaction.reaction = requested_reaction
            reaction.save(update_fields=['reaction', 'updated_at'])
            action = 'updated'
            current_reaction = requested_reaction
        else:
            MessageReaction.objects.create(
                message_id=message.id,
                user_id=user_id,
                reaction=requested_reaction,
            )
            current_reaction = requested_reaction

    message = (
        Message.objects
        .select_related('room')
        .prefetch_related('reactions')
        .get(id=message.id)
    )
    invalidate_room_caches(message.room_id)

    return {
        'status': 'ok',
        'action': action,
        'room_id': message.room_id,
        'message_id': message.id,
        'user_id': user_id,
        'reaction': current_reaction,
        'previous_reaction': previous_reaction,
        'reactions': serialize_message_reaction_summary(message),
        'my_reaction': current_reaction,
        'room': serialize_room(message.room),
    }, 200


def normalize_reaction_payload(payload):
    errors = {}
    reaction = normalize_string(payload.get('reaction') if isinstance(payload, dict) else None)

    if not reaction:
        errors['reaction'] = ['Reaction is required.']
    elif reaction not in MessageReaction.ALLOWED_REACTIONS:
        errors['reaction'] = ['Unsupported reaction.']

    return {
        'reaction': reaction,
    }, errors


def resolve_hidden_receipt_sender_ids(owner_user_id, sender_user_ids):
    candidate_user_ids = sorted(
        {
            int(user_id)
            for user_id in sender_user_ids
            if user_id and int(user_id) != int(owner_user_id)
        }
    )
    if not candidate_user_ids:
        return set(), None, None

    policy_result, policy_status = resolve_parent_receipt_visibility(
        {
            'owner_user_id': int(owner_user_id),
            'candidate_user_ids': candidate_user_ids,
        }
    )
    parent_policy = policy_result.get('parent', {}).get('response')

    if policy_status >= 500:
        return None, {
            'status': 'error',
            'message': 'Unable to verify receipt visibility with Parent service.',
            'policy': policy_result,
        }, policy_status

    if not isinstance(parent_policy, dict) or parent_policy.get('allowed') is not True:
        return None, {
            'status': 'error',
            'message': 'Receipt visibility was not authorized.',
            'policy': policy_result,
        }, policy_status if policy_status >= 400 else 403

    return {
        int(user_id)
        for user_id in parent_policy.get('hidden_user_ids') or []
    }, None, None


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

    status_candidates = received_messages.filter(
        created_at__lte=delivered_until,
        status=Message.STATUS_SENT,
        receipt_hidden_from_sender=False,
    )
    hidden_sender_ids, policy_error, policy_status = resolve_hidden_receipt_sender_ids(
        user_id,
        status_candidates.values_list('sender_user_id', flat=True).distinct(),
    )
    if policy_error:
        return policy_error, policy_status

    hidden_message_ids = list(
        status_candidates.filter(sender_user_id__in=hidden_sender_ids)
        .values_list('id', flat=True)
    )
    visible_status_candidates = status_candidates.exclude(
        sender_user_id__in=hidden_sender_ids,
    )
    visible_message_ids = list(visible_status_candidates.values_list('id', flat=True))

    now = timezone.now()
    if hidden_message_ids:
        Message.objects.filter(id__in=hidden_message_ids).update(
            receipt_hidden_from_sender=True,
            updated_at=now,
        )
    delivered_count = Message.objects.filter(id__in=visible_message_ids).update(
        status=Message.STATUS_DELIVERED,
        updated_at=now,
    )

    invalidate_room_caches(participant.room_id)

    return {
        'status': 'delivered',
        'room_id': participant.room_id,
        'user_id': user_id,
        'last_delivered_message_id': delivered_message.id if delivered_message else None,
        'delivered_until': delivered_until.isoformat(),
        'updated_messages': delivered_count,
        'hidden_receipts': len(hidden_message_ids),
        'message_statuses': [
            {
                'message_id': message_id,
                'status': Message.STATUS_DELIVERED,
            }
            for message_id in visible_message_ids
        ],
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
    hidden_message_ids = []
    visible_message_ids = []
    if participant.room.is_direct:
        status_candidates = Message.objects.filter(
            room_id=room_id,
            recipient_user_id=user_id,
            delivery_blocked=False,
            deleted_at__isnull=True,
            created_at__lte=final_read_at,
            receipt_hidden_from_sender=False,
        ).exclude(
            sender_user_id=user_id,
        ).exclude(
            status=Message.STATUS_READ,
        )
        hidden_sender_ids, policy_error, policy_status = resolve_hidden_receipt_sender_ids(
            user_id,
            status_candidates.values_list('sender_user_id', flat=True).distinct(),
        )
        if policy_error:
            return policy_error, policy_status

        hidden_message_ids = list(
            status_candidates.filter(sender_user_id__in=hidden_sender_ids)
            .values_list('id', flat=True)
        )
        visible_status_candidates = status_candidates.exclude(
            sender_user_id__in=hidden_sender_ids,
        )
        visible_message_ids = list(visible_status_candidates.values_list('id', flat=True))

    with transaction.atomic():
        if read_marker_moved:
            participant.last_read_at = final_read_at
            participant.save(update_fields=['last_read_at'])

        if participant.room.is_direct:
            now = timezone.now()
            if hidden_message_ids:
                Message.objects.filter(id__in=hidden_message_ids).update(
                    receipt_hidden_from_sender=True,
                    updated_at=now,
                )
            updated_messages = Message.objects.filter(id__in=visible_message_ids).update(
                status=Message.STATUS_READ,
                updated_at=now,
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
        'hidden_receipts': len(hidden_message_ids) if participant.room.is_direct else 0,
        'message_statuses': [
            {
                'message_id': message_id,
                'status': Message.STATUS_READ,
            }
            for message_id in (visible_message_ids if participant.room.is_direct else [])
        ],
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
    if room.is_group:
        try:
            from group_messaging.serializers import serialize_group_room

            return serialize_group_room(room, current_user_id=current_user_id)
        except Exception:
            pass

    latest_message = (
        get_visible_messages_queryset(current_user_id, room_id=room.id)
        .prefetch_related('attachments', 'reactions')
        .order_by('-created_at', '-id')
        .first()
    )
    room_data = serialize_room(room, current_user_id=current_user_id)
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


def serialize_room(room, current_user_id=None):
    room_data = {
        'id': room.id,
        'room_type': room.room_type,
        'is_group': room.is_group,
        'title': room.title,
        'created_by_user_id': room.created_by_user_id,
        'created_at': room.created_at.isoformat(),
        'updated_at': room.updated_at.isoformat(),
        'participants': [
            serialize_participant(participant)
            for participant in room.participants.filter(is_active=True).order_by('joined_at', 'id')
        ],
    }
    if room.is_group:
        try:
            from group_messaging.serializers import get_group_room_extension

            room_data.update(
                get_group_room_extension(room, current_user_id=current_user_id)
            )
        except Exception:
            pass

    return room_data


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
    reaction_data = serialize_message_reactions(message, current_user_id)
    data = {
        'id': message.id,
        'room_id': message.room_id,
        'sender_user_id': message.sender_user_id,
        'recipient_user_id': message.recipient_user_id,
        'reply_to_message_id': message.reply_to_id,
        'reply_to': serialize_reply_preview(message.reply_to, current_user_id) if message.reply_to_id else None,
        'text': message.text,
        'client_message_id': message.client_message_id,
        'story_context': message.story_context or {},
        'status': get_message_status_for_viewer(message, current_user_id),
        'sent_while_blocked': is_sent_while_blocked_visible(message, current_user_id),
        'created_at': message.created_at.isoformat(),
        'updated_at': message.updated_at.isoformat(),
        'attachments': [
            serialize_attachment(attachment)
            for attachment in message.attachments.all().order_by('sort_order', 'id')
        ],
        'reactions': reaction_data['reactions'],
        'my_reaction': reaction_data['my_reaction'],
    }

    return data


def get_message_status_for_viewer(message, current_user_id=None):
    if (
        current_user_id
        and int(message.sender_user_id or 0) == int(current_user_id)
        and message.receipt_hidden_from_sender
    ):
        return Message.STATUS_SENT

    return message.status


def serialize_message_reaction_summary(message):
    return serialize_message_reactions(message, current_user_id=None)['reactions']


def serialize_message_reactions(message, current_user_id=None):
    reaction_counts = {}
    my_reaction = None

    for reaction in message.reactions.all():
        reaction_key = reaction.reaction
        reaction_counts[reaction_key] = reaction_counts.get(reaction_key, 0) + 1

        if current_user_id and int(reaction.user_id) == int(current_user_id):
            my_reaction = reaction_key

    reactions = []
    for reaction_key in MessageReaction.ALLOWED_REACTIONS:
        count = reaction_counts.get(reaction_key, 0)
        if not count:
            continue

        reaction_data = {
            'reaction': reaction_key,
            'count': count,
        }
        if current_user_id:
            reaction_data['reacted_by_me'] = reaction_key == my_reaction
        reactions.append(reaction_data)

    return {
        'reactions': reactions,
        'my_reaction': my_reaction,
    }


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
        'story_context': message.story_context or {},
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
