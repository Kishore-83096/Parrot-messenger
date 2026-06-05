import json
import re
import time
import uuid
from datetime import timedelta
from urllib.parse import unquote, urlparse
from uuid import uuid4

from cloudinary import config as cloudinary_config
from cloudinary import uploader as cloudinary_uploader
from cloudinary.exceptions import Error as CloudinaryError
from cloudinary.utils import (
    api_sign_request,
    cloudinary_url,
    verify_api_response_signature,
)
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
import httpx

from messaging.cache import (
    get_cached_receipt_visibility,
    get_cached_room_messages,
    invalidate_room_caches,
    set_cached_receipt_visibility,
    set_cached_room_messages,
)
from messaging.models import Room, RoomParticipant, UserDeviceKey
from messaging.e2ee.devices.service import serialize_device_key
from messaging.realtime import broadcast_room_event, broadcast_user_event
from messaging.signals import (
    build_parent_headers,
    decode_parent_response,
    label_parent_url,
    resolve_parent_receipt_visibility,
)

from .models import (
    GroupActionLog,
    GroupMembership,
    GroupMessage,
    GroupMessageEncryptedUploadIntent,
    GroupMessageReaction,
    GroupMessageReceipt,
    GroupProfile,
)
from .serializers import (
    attach_group_participant_identities,
    get_group_room_unread_count,
    serialize_group_log,
    serialize_group_message,
    serialize_group_message_reaction_summary,
    serialize_group_room,
)


ACCOUNT_NUMBER_PATTERN = re.compile(r'^7\d{9}$')
MAX_GROUP_MEMBERS = 100
MAX_GROUP_TITLE_LENGTH = 120
MAX_GROUP_AVATAR_BYTES = 5 * 1024 * 1024
ALLOWED_AVATAR_CONTENT_TYPES = {'image/avif', 'image/gif', 'image/jpeg', 'image/png', 'image/webp'}
GROUP_MANAGER_ROLES = {GroupMembership.ROLE_ADMIN, GroupMembership.ROLE_SUB_ADMIN}
GROUP_E2EE_MESSAGE_TYPE = 'e2ee.group_message'
GROUP_E2EE_MESSAGE_VERSION = 1
MAX_GROUP_MESSAGE_TEXT_LENGTH = 5000
MAX_GROUP_ENCRYPTED_MESSAGE_TEXT_LENGTH = 500000
MAX_GROUP_ATTACHMENTS_PER_MESSAGE = 10
MAX_GROUP_ENCRYPTED_FILE_SIZE_BYTES = 26 * 1024 * 1024
MAX_GROUP_ENCRYPTED_UPLOAD_INTENTS_PER_REQUEST = 10
DEFAULT_GROUP_UPLOAD_INTENT_TTL_SECONDS = 600
MAIN_CLOUDINARY_FOLDER = 'MAIN'
GROUP_TIMELINE_LOG_LIMIT = 100


def validation_error(errors):
    return {
        'status': 'error',
        'errors': errors,
    }, 400


def normalize_string(value):
    return value.strip() if isinstance(value, str) else ''


def normalize_account_numbers(value):
    if not isinstance(value, list):
        return [], {'member_account_numbers': ['Member account numbers must be a list.']}

    errors = []
    account_numbers = []
    seen = set()
    duplicates = []
    for index, account_number in enumerate(value):
        normalized = normalize_string(account_number)
        if not ACCOUNT_NUMBER_PATTERN.match(normalized):
            errors.append({index: 'Account number must start with 7 and contain exactly 10 digits.'})
            continue

        if normalized in seen:
            duplicates.append(normalized)
            continue

        seen.add(normalized)
        account_numbers.append(normalized)

    if duplicates:
        errors.append({'duplicates': duplicates})

    if len(account_numbers) < 1:
        errors.append('Select at least one member.')

    if len(account_numbers) > MAX_GROUP_MEMBERS:
        errors.append(f'Cannot add more than {MAX_GROUP_MEMBERS} members.')

    return account_numbers, {'member_account_numbers': errors} if errors else None


def normalize_group_create_payload(payload):
    if not isinstance(payload, dict):
        return None, {'body': ['Request body must be a JSON object.']}

    title = normalize_string(payload.get('title'))
    errors = {}
    if not title:
        errors['title'] = ['Group name is required.']
    elif len(title) > MAX_GROUP_TITLE_LENGTH:
        errors['title'] = [f'Group name cannot exceed {MAX_GROUP_TITLE_LENGTH} characters.']

    account_numbers, account_errors = normalize_account_numbers(payload.get('member_account_numbers'))
    if account_errors:
        errors.update(account_errors)

    if errors:
        return None, errors

    return {
        'title': title,
        'member_account_numbers': account_numbers,
    }, None


def normalize_group_update_payload(payload):
    if not isinstance(payload, dict):
        return None, {'body': ['Request body must be a JSON object.']}

    title = normalize_string(payload.get('title'))
    errors = {}
    if 'title' in payload:
        if not title:
            errors['title'] = ['Group name is required.']
        elif len(title) > MAX_GROUP_TITLE_LENGTH:
            errors['title'] = [f'Group name cannot exceed {MAX_GROUP_TITLE_LENGTH} characters.']

    if errors:
        return None, errors

    return {
        'title': title,
    }, None


def resolve_group_members_from_parent(sender_user_id, account_numbers):
    parent_base_urls = getattr(settings, 'PARENT_SERVICE_URLS', None) or [settings.PARENT_SERVICE_URL]
    parent_base_urls = [url for url in parent_base_urls if url]
    if not parent_base_urls:
        return None, {
            'status': 'error',
            'message': 'PARENT_SERVICE_URL is not configured.',
        }, 503

    failed_parent_checks = []
    last_result = None
    last_status = 503
    payload = {
        'owner_user_id': sender_user_id,
        'member_account_numbers': account_numbers,
    }

    for parent_base_url in parent_base_urls:
        resolve_url = f'{parent_base_url.rstrip("/")}/parent/internal/groups/members/resolve'

        try:
            response = httpx.post(
                resolve_url,
                json=payload,
                headers=build_parent_headers(),
                timeout=settings.PARENT_SERVICE_TIMEOUT_SECONDS,
            )
        except httpx.RequestError as error:
            failed_parent_checks.append(
                {
                    'name': label_parent_url(parent_base_url),
                    'base_url': parent_base_url,
                    'url': resolve_url,
                    'ok': False,
                    'error': error.__class__.__name__,
                }
            )
            continue

        result = decode_parent_response(response)
        last_result = result
        last_status = response.status_code

        if response.is_success:
            return result, None, response.status_code

        if response.status_code < 500:
            return None, result, response.status_code

        failed_parent_checks.append(
            {
                'name': label_parent_url(parent_base_url),
                'base_url': parent_base_url,
                'url': resolve_url,
                'ok': False,
                'status_code': response.status_code,
                'response': result,
            }
        )

    return None, {
        'status': 'error',
        'parent': last_result or {},
        'failed_parents': failed_parent_checks,
    }, last_status


def room_participant_role_for_group_role(group_role):
    return RoomParticipant.ROLE_ADMIN if group_role == GroupMembership.ROLE_ADMIN else RoomParticipant.ROLE_MEMBER


def sync_room_participant_role(participant, group_role):
    next_role = room_participant_role_for_group_role(group_role)
    if participant.role != next_role:
        participant.role = next_role
        participant.save(update_fields=['role'])


def get_default_group_role(participant):
    return (
        GroupMembership.ROLE_ADMIN
        if participant.role == RoomParticipant.ROLE_ADMIN
        else GroupMembership.ROLE_MEMBER
    )


def ensure_group_membership(participant):
    membership, created = GroupMembership.objects.get_or_create(
        room_id=participant.room_id,
        user_id=participant.user_id,
        defaults={
            'role': get_default_group_role(participant),
            'is_active': participant.is_active,
        },
    )

    update_fields = []
    if membership.is_active != participant.is_active:
        membership.is_active = participant.is_active
        update_fields.append('is_active')

    if created and membership.role != get_default_group_role(participant):
        membership.role = get_default_group_role(participant)
        update_fields.append('role')

    if update_fields:
        update_fields.append('updated_at')
        membership.save(update_fields=update_fields)

    return membership


def actor_metadata(sender, participant=None):
    account_number = sender.get('account_number', '')
    return {
        'actor_account_number': account_number,
        'actor_display_name': (participant.display_name if participant else '') or account_number,
    }


def target_metadata(participant):
    return {
        'target_account_number': participant.account_number,
        'target_display_name': participant.display_name or participant.account_number,
    }


def get_group_context(room_id, user_id):
    participant = (
        RoomParticipant.objects.filter(
            room_id=room_id,
            user_id=user_id,
            is_active=True,
        )
        .select_related('room', 'room__group_profile')
        .first()
    )
    if not participant or not participant.room.is_group:
        return None

    membership = ensure_group_membership(participant)
    if not membership.is_active:
        return None

    return {
        'room': participant.room,
        'participant': participant,
        'membership': membership,
    }


def get_group_profile(room):
    try:
        return room.group_profile
    except GroupProfile.DoesNotExist:
        return None


def is_group_deleted(room):
    profile = get_group_profile(room)
    return bool(profile and profile.deleted_at)


def deleted_group_error(room, current_user_id=None):
    return {
        'status': 'error',
        'message': 'This group has been deleted. Messaging is closed.',
        'room': serialize_group_room(room, current_user_id=current_user_id),
    }, 403


def require_group_open(context, current_user_id=None):
    if is_group_deleted(context['room']):
        return deleted_group_error(context['room'], current_user_id=current_user_id)

    return None, None


def require_group_member(user_id, room_id):
    context = get_group_context(room_id, user_id)
    if not context:
        return None, {
            'status': 'error',
            'message': 'Group not found.',
        }, 404

    return context, None, 200


def require_group_manager(user_id, room_id):
    context, error, status = require_group_member(user_id, room_id)
    if error:
        return None, error, status

    if context['membership'].role not in GROUP_MANAGER_ROLES:
        return None, {
            'status': 'error',
            'message': 'Only group admins and sub admins can change this group.',
        }, 403

    return context, None, 200


def require_group_admin(user_id, room_id):
    context, error, status = require_group_member(user_id, room_id)
    if error:
        return None, error, status

    if context['membership'].role != GroupMembership.ROLE_ADMIN:
        return None, {
            'status': 'error',
            'message': 'Only the group admin can do this.',
        }, 403

    return context, None, 200


def get_active_target_context(room, target_user_id):
    participant = (
        RoomParticipant.objects.filter(
            room_id=room.id,
            user_id=target_user_id,
            is_active=True,
        )
        .select_related('room')
        .first()
    )
    if not participant:
        return None

    membership = ensure_group_membership(participant)
    if not membership.is_active:
        return None

    return {
        'participant': participant,
        'membership': membership,
    }


def create_group(sender, payload):
    normalized_payload, errors = normalize_group_create_payload(payload)
    if errors:
        return validation_error(errors)

    if sender.get('account_number') in normalized_payload['member_account_numbers']:
        return validation_error({'member_account_numbers': ['The group creator is added automatically.']})

    parent_result, parent_error, parent_status = resolve_group_members_from_parent(
        sender['user_id'],
        normalized_payload['member_account_numbers'],
    )
    if parent_error:
        return {
            'status': 'error',
            'message': parent_error.get('message', 'Unable to validate group members.'),
            'parent': parent_error,
        }, parent_status

    not_saved = parent_result.get('not_saved_account_numbers') or []
    duplicates = parent_result.get('duplicate_account_numbers') or []
    valid_contacts = parent_result.get('valid_contacts') or []
    if not_saved or duplicates or len(valid_contacts) != len(normalized_payload['member_account_numbers']):
        return validation_error(
            {
                'member_account_numbers': {
                    'not_saved': not_saved,
                    'duplicates': duplicates,
                    'message': 'All group members must be saved contacts.',
                }
            }
        )

    now = timezone.now()
    owner_display_name = normalize_string(parent_result.get('owner_display_name')) or sender.get('account_number') or 'Creator'
    logs = []
    with transaction.atomic():
        room = Room.objects.create(
            room_type=Room.TYPE_GROUP,
            title=normalized_payload['title'],
            created_by_user_id=sender['user_id'],
        )
        GroupProfile.objects.create(
            room=room,
            title=normalized_payload['title'],
            created_by_user_id=sender['user_id'],
        )
        creator_participant = RoomParticipant.objects.create(
            room=room,
            user_id=sender['user_id'],
            account_number=sender.get('account_number') or parent_result.get('owner_account_number', ''),
            display_name=owner_display_name,
            role=RoomParticipant.ROLE_ADMIN,
        )
        GroupMembership.objects.create(
            room=room,
            user_id=sender['user_id'],
            role=GroupMembership.ROLE_ADMIN,
            is_active=True,
        )

        created_log = GroupActionLog.objects.create(
            room=room,
            actor_user_id=sender['user_id'],
            action=GroupActionLog.ACTION_GROUP_CREATED,
            metadata={
                **actor_metadata(sender, creator_participant),
                'title': normalized_payload['title'],
            },
        )
        logs.append(created_log)

        for contact in valid_contacts:
            participant = RoomParticipant.objects.create(
                room=room,
                user_id=contact['user_id'],
                account_number=contact['account_number'],
                display_name=contact.get('display_name') or contact['account_number'],
                role=RoomParticipant.ROLE_MEMBER,
            )
            GroupMembership.objects.create(
                room=room,
                user_id=contact['user_id'],
                role=GroupMembership.ROLE_MEMBER,
                is_active=True,
            )
            logs.append(
                GroupActionLog.objects.create(
                    room=room,
                    actor_user_id=sender['user_id'],
                    target_user_id=contact['user_id'],
                    action=GroupActionLog.ACTION_MEMBER_ADDED,
                    metadata={
                        **actor_metadata(sender, creator_participant),
                        **target_metadata(participant),
                    },
                )
            )

        room.updated_at = now
        room.save(update_fields=['updated_at'])

    serialized_room = serialize_group_room(room, current_user_id=sender['user_id'])
    serialized_logs = [serialize_group_log(log) for log in logs]
    invalidate_room_caches(room.id)
    broadcast_group_created(room, serialized_room, serialized_logs)

    return {
        'status': 'created',
        'room': serialized_room,
        'logs': serialized_logs,
    }, 201


def get_group_room(sender, room_id):
    context, error, status = require_group_member(sender['user_id'], room_id)
    if error:
        return error, status

    return {
        'status': 'ok',
        'room': serialize_group_room(context['room'], current_user_id=sender['user_id']),
    }, 200


def update_group(sender, room_id, payload):
    normalized_payload, errors = normalize_group_update_payload(payload)
    if errors:
        return validation_error(errors)

    context, error, status = require_group_manager(sender['user_id'], room_id)
    if error:
        return error, status
    error, status = require_group_open(context, current_user_id=sender['user_id'])
    if error:
        return error, status

    title = normalized_payload.get('title')
    if not title:
        return validation_error({'group': ['No group changes were provided.']})

    room = context['room']
    profile = GroupProfile.objects.filter(room_id=room.id).first()
    previous_title = profile.title if profile else room.title
    if previous_title == title:
        return {
            'status': 'ok',
            'room': serialize_group_room(room, current_user_id=sender['user_id']),
            'log': None,
        }, 200

    with transaction.atomic():
        if not profile:
            profile = GroupProfile.objects.create(
                room=room,
                title=title,
                created_by_user_id=room.created_by_user_id or sender['user_id'],
            )
        else:
            profile.title = title
            profile.save(update_fields=['title', 'updated_at'])

        room.title = title
        room.updated_at = timezone.now()
        room.save(update_fields=['title', 'updated_at'])
        log = GroupActionLog.objects.create(
            room=room,
            actor_user_id=sender['user_id'],
            action=GroupActionLog.ACTION_GROUP_UPDATED,
            metadata={
                **actor_metadata(sender, context['participant']),
                'previous_title': previous_title,
                'title': title,
            },
        )

    return build_group_update_result(sender, room, log)


def upload_group_avatar(sender, room_id, uploaded_file):
    context, error, status = require_group_manager(sender['user_id'], room_id)
    if error:
        return error, status
    error, status = require_group_open(context, current_user_id=sender['user_id'])
    if error:
        return error, status

    if not uploaded_file:
        return validation_error({'avatar': ['Group picture is required.']})

    content_type = (getattr(uploaded_file, 'content_type', '') or '').lower()
    if content_type not in ALLOWED_AVATAR_CONTENT_TYPES:
        return validation_error({'avatar': ['Choose an image file.']})

    if getattr(uploaded_file, 'size', 0) > MAX_GROUP_AVATAR_BYTES:
        return validation_error({'avatar': ['Group picture cannot exceed 5 MB.']})

    if not getattr(settings, 'CLOUDINARY_URL', ''):
        return validation_error({'cloudinary': ['Cloudinary is not configured for group pictures.']})

    room = context['room']
    profile = GroupProfile.objects.filter(room_id=room.id).first()
    cloudinary_config(cloudinary_url=settings.CLOUDINARY_URL, secure=True)
    folder = f'{settings.CLOUDINARY_MAIN_FOLDER}/groups/{room.id}'
    public_id = f'avatar-{uuid4().hex}'

    try:
        upload_result = cloudinary_uploader.upload(
            uploaded_file,
            folder=folder,
            public_id=public_id,
            resource_type='image',
            overwrite=True,
        )
    except CloudinaryError as error:
        return {
            'status': 'error',
            'message': str(error) or 'Unable to upload group picture.',
        }, 502

    avatar_url = upload_result.get('secure_url', '')
    if not avatar_url:
        return {
            'status': 'error',
            'message': 'Cloudinary did not return a group picture URL.',
        }, 502

    with transaction.atomic():
        if not profile:
            profile = GroupProfile.objects.create(
                room=room,
                title=room.title,
                created_by_user_id=room.created_by_user_id or sender['user_id'],
            )

        profile.avatar_url = avatar_url
        profile.avatar_cloudinary_public_id = upload_result.get('public_id', '')
        profile.avatar_cloudinary_asset_id = upload_result.get('asset_id', '')
        profile.save(
            update_fields=[
                'avatar_url',
                'avatar_cloudinary_public_id',
                'avatar_cloudinary_asset_id',
                'updated_at',
            ]
        )
        room.updated_at = timezone.now()
        room.save(update_fields=['updated_at'])
        log = GroupActionLog.objects.create(
            room=room,
            actor_user_id=sender['user_id'],
            action=GroupActionLog.ACTION_AVATAR_UPDATED,
            metadata={
                **actor_metadata(sender, context['participant']),
                'avatar_url': avatar_url,
            },
        )

    return build_group_update_result(sender, room, log)


def add_group_members(sender, room_id, payload):
    account_numbers, errors = normalize_account_numbers((payload or {}).get('member_account_numbers'))
    if errors:
        return validation_error(errors)

    context, error, status = require_group_manager(sender['user_id'], room_id)
    if error:
        return error, status
    error, status = require_group_open(context, current_user_id=sender['user_id'])
    if error:
        return error, status

    room = context['room']
    parent_result, parent_error, parent_status = resolve_group_members_from_parent(
        sender['user_id'],
        account_numbers,
    )
    if parent_error:
        return {
            'status': 'error',
            'message': parent_error.get('message', 'Unable to validate group members.'),
            'parent': parent_error,
        }, parent_status

    valid_contacts = parent_result.get('valid_contacts') or []
    not_saved = parent_result.get('not_saved_account_numbers') or []
    duplicates = parent_result.get('duplicate_account_numbers') or []
    active_accounts = set(
        room.participants.filter(is_active=True).values_list('account_number', flat=True)
    )
    already_members = [
        contact['account_number']
        for contact in valid_contacts
        if contact['account_number'] in active_accounts
    ]

    if not_saved or duplicates or already_members or len(valid_contacts) != len(account_numbers):
        return validation_error(
            {
                'member_account_numbers': {
                    'not_saved': not_saved,
                    'duplicates': duplicates,
                    'already_members': already_members,
                    'message': 'All added members must be saved contacts and not already in the group.',
                }
            }
        )

    logs = []
    with transaction.atomic():
        for contact in valid_contacts:
            joined_at = timezone.now()
            participant, created = RoomParticipant.objects.get_or_create(
                room=room,
                user_id=contact['user_id'],
                defaults={
                    'account_number': contact['account_number'],
                    'display_name': contact.get('display_name') or contact['account_number'],
                    'role': RoomParticipant.ROLE_MEMBER,
                    'is_active': True,
                },
            )
            if created:
                RoomParticipant.objects.filter(pk=participant.pk).update(joined_at=joined_at)
                participant.joined_at = joined_at
            else:
                participant.account_number = contact['account_number']
                participant.display_name = contact.get('display_name') or contact['account_number']
                participant.role = RoomParticipant.ROLE_MEMBER
                participant.is_active = True
                participant.last_read_at = None
                participant.joined_at = joined_at
                participant.save(
                    update_fields=[
                        'account_number',
                        'display_name',
                        'role',
                        'is_active',
                        'last_read_at',
                        'joined_at',
                    ]
                )

            membership, _ = GroupMembership.objects.get_or_create(
                room=room,
                user_id=contact['user_id'],
                defaults={
                    'role': GroupMembership.ROLE_MEMBER,
                    'is_active': True,
                },
            )
            membership.role = GroupMembership.ROLE_MEMBER
            membership.is_active = True
            membership.save(update_fields=['role', 'is_active', 'updated_at'])

            logs.append(
                GroupActionLog.objects.create(
                    room=room,
                    actor_user_id=sender['user_id'],
                    target_user_id=contact['user_id'],
                    action=GroupActionLog.ACTION_MEMBER_ADDED,
                    metadata={
                        **actor_metadata(sender, context['participant']),
                        **target_metadata(participant),
                    },
                )
            )

        room.updated_at = timezone.now()
        room.save(update_fields=['updated_at'])

    return build_group_update_result(
        sender,
        room,
        logs[-1],
        logs=logs,
        extra_user_ids=[contact['user_id'] for contact in valid_contacts],
    )


def remove_group_member(sender, room_id, target_user_id):
    context, error, status = require_group_manager(sender['user_id'], room_id)
    if error:
        return error, status
    error, status = require_group_open(context, current_user_id=sender['user_id'])
    if error:
        return error, status

    room = context['room']
    actor_role = context['membership'].role
    if int(target_user_id) == int(sender['user_id']):
        return validation_error({'member': ['Use leave group to remove yourself.']})

    target_context = get_active_target_context(room, target_user_id)
    if not target_context:
        return validation_error({'member': ['Member not found in this group.']})

    target_role = target_context['membership'].role
    if target_role == GroupMembership.ROLE_ADMIN:
        return validation_error({'member': ['The group admin cannot be removed.']})

    if actor_role == GroupMembership.ROLE_SUB_ADMIN and target_role == GroupMembership.ROLE_ADMIN:
        return validation_error({'member': ['Sub admins cannot remove the group admin.']})

    with transaction.atomic():
        target_context['participant'].is_active = False
        target_context['participant'].save(update_fields=['is_active'])
        target_context['membership'].is_active = False
        target_context['membership'].save(update_fields=['is_active', 'updated_at'])
        room.updated_at = timezone.now()
        room.save(update_fields=['updated_at'])
        log = GroupActionLog.objects.create(
            room=room,
            actor_user_id=sender['user_id'],
            target_user_id=target_context['participant'].user_id,
            action=GroupActionLog.ACTION_MEMBER_REMOVED,
            metadata={
                **actor_metadata(sender, context['participant']),
                **target_metadata(target_context['participant']),
            },
        )

    return build_group_update_result(sender, room, log, extra_user_ids=[target_context['participant'].user_id])


def set_group_sub_admin(sender, room_id, target_user_id, enabled):
    context, error, status = require_group_manager(sender['user_id'], room_id)
    if error:
        return error, status
    error, status = require_group_open(context, current_user_id=sender['user_id'])
    if error:
        return error, status

    room = context['room']
    if int(target_user_id) == int(sender['user_id']):
        return validation_error({'member': ['Change another member role.']})

    target_context = get_active_target_context(room, target_user_id)
    if not target_context:
        return validation_error({'member': ['Member not found in this group.']})

    target_membership = target_context['membership']
    if target_membership.role == GroupMembership.ROLE_ADMIN:
        return validation_error({'member': ['The group admin role cannot be changed here.']})

    next_role = GroupMembership.ROLE_SUB_ADMIN if enabled else GroupMembership.ROLE_MEMBER
    if target_membership.role == next_role:
        return {
            'status': 'ok',
            'room': serialize_group_room(room, current_user_id=sender['user_id']),
            'log': None,
        }, 200

    with transaction.atomic():
        target_membership.role = next_role
        target_membership.is_active = True
        target_membership.save(update_fields=['role', 'is_active', 'updated_at'])
        sync_room_participant_role(target_context['participant'], next_role)
        room.updated_at = timezone.now()
        room.save(update_fields=['updated_at'])
        log = GroupActionLog.objects.create(
            room=room,
            actor_user_id=sender['user_id'],
            target_user_id=target_context['participant'].user_id,
            action=(
                GroupActionLog.ACTION_SUB_ADMIN_ADDED
                if enabled
                else GroupActionLog.ACTION_SUB_ADMIN_REMOVED
            ),
            metadata={
                **actor_metadata(sender, context['participant']),
                **target_metadata(target_context['participant']),
            },
        )

    return build_group_update_result(sender, room, log)


def transfer_group_admin(sender, room_id, target_user_id):
    context, error, status = require_group_admin(sender['user_id'], room_id)
    if error:
        return error, status
    error, status = require_group_open(context, current_user_id=sender['user_id'])
    if error:
        return error, status

    room = context['room']
    if int(target_user_id) == int(sender['user_id']):
        return validation_error({'member': ['Choose another member as admin.']})

    target_context = get_active_target_context(room, target_user_id)
    if not target_context:
        return validation_error({'member': ['Member not found in this group.']})

    with transaction.atomic():
        context['membership'].role = GroupMembership.ROLE_MEMBER
        context['membership'].save(update_fields=['role', 'updated_at'])
        sync_room_participant_role(context['participant'], GroupMembership.ROLE_MEMBER)

        target_context['membership'].role = GroupMembership.ROLE_ADMIN
        target_context['membership'].is_active = True
        target_context['membership'].save(update_fields=['role', 'is_active', 'updated_at'])
        sync_room_participant_role(target_context['participant'], GroupMembership.ROLE_ADMIN)

        room.updated_at = timezone.now()
        room.save(update_fields=['updated_at'])
        log = GroupActionLog.objects.create(
            room=room,
            actor_user_id=sender['user_id'],
            target_user_id=target_context['participant'].user_id,
            action=GroupActionLog.ACTION_ADMIN_TRANSFERRED,
            metadata={
                **actor_metadata(sender, context['participant']),
                **target_metadata(target_context['participant']),
            },
        )

    return build_group_update_result(sender, room, log)


def leave_group(sender, room_id):
    context, error, status = require_group_member(sender['user_id'], room_id)
    if error:
        return error, status
    error, status = require_group_open(context, current_user_id=sender['user_id'])
    if error:
        return error, status

    if context['membership'].role == GroupMembership.ROLE_ADMIN:
        return validation_error({'group': ['Transfer admin before leaving the group.']})

    room = context['room']
    with transaction.atomic():
        context['participant'].is_active = False
        context['participant'].save(update_fields=['is_active'])
        context['membership'].is_active = False
        context['membership'].save(update_fields=['is_active', 'updated_at'])
        room.updated_at = timezone.now()
        room.save(update_fields=['updated_at'])
        log = GroupActionLog.objects.create(
            room=room,
            actor_user_id=sender['user_id'],
            target_user_id=sender['user_id'],
            action=GroupActionLog.ACTION_MEMBER_LEFT,
            metadata={
                **actor_metadata(sender, context['participant']),
                **target_metadata(context['participant']),
            },
        )

    serialized_log = serialize_group_log(log)
    invalidate_room_caches(room.id)
    broadcast_group_update(room, serialize_group_room(room, current_user_id=sender['user_id']), serialized_log, extra_user_ids=[sender['user_id']])

    return {
        'status': 'left',
        'room_id': room.id,
        'removed_room_id': room.id,
        'log': serialized_log,
    }, 200


def delete_group(sender, room_id):
    context, error, status = require_group_admin(sender['user_id'], room_id)
    if error:
        return error, status

    room = context['room']
    if is_group_deleted(room):
        return {
            'status': 'deleted',
            'room_id': room.id,
            'room': serialize_group_room(room, current_user_id=sender['user_id']),
            'deleted': True,
            'log': None,
        }, 200

    with transaction.atomic():
        profile = get_group_profile(room)
        if not profile:
            profile = GroupProfile.objects.create(
                room=room,
                title=room.title,
                created_by_user_id=room.created_by_user_id or sender['user_id'],
            )

        deleted_at = timezone.now()
        profile.deleted_at = deleted_at
        profile.deleted_by_user_id = sender['user_id']
        profile.save(update_fields=['deleted_at', 'deleted_by_user_id', 'updated_at'])
        room.updated_at = deleted_at
        room.save(update_fields=['updated_at'])
        log = GroupActionLog.objects.create(
            room=room,
            actor_user_id=sender['user_id'],
            action=GroupActionLog.ACTION_GROUP_DELETED,
            metadata=actor_metadata(sender, context['participant']),
        )

    serialized_room = serialize_group_room(room, current_user_id=sender['user_id'])
    serialized_log = serialize_group_log(log)
    invalidate_room_caches(room.id)
    broadcast_group_update(room, serialized_room, serialized_log)

    return {
        'status': 'deleted',
        'room_id': room.id,
        'room': serialized_room,
        'deleted': True,
        'deleted_at': serialized_room.get('deleted_at'),
        'log': serialized_log,
    }, 200


def build_group_update_result(sender, room, log, logs=None, extra_user_ids=None):
    serialized_room = serialize_group_room(room, current_user_id=sender['user_id'])
    serialized_logs = [serialize_group_log(item) for item in logs] if logs else None
    serialized_log = serialize_group_log(log) if log else None
    invalidate_room_caches(room.id)
    if serialized_logs:
        for item in serialized_logs:
            broadcast_group_update(room, serialized_room, item, extra_user_ids=extra_user_ids)
    elif serialized_log:
        broadcast_group_update(room, serialized_room, serialized_log, extra_user_ids=extra_user_ids)

    return {
        'status': 'ok',
        'room': serialized_room,
        'log': serialized_log,
        'logs': serialized_logs or ([] if serialized_log is None else [serialized_log]),
    }, 200


def get_room_participants_for_broadcast(room):
    return [
        {
            'user_id': participant.user_id,
            'account_number': participant.account_number,
            'display_name': participant.display_name,
            'role': participant.role,
            'is_active': participant.is_active,
        }
        for participant in room.participants.filter(is_active=True).order_by('joined_at', 'id')
    ]


def broadcast_group_created(room, serialized_room, serialized_logs):
    participants = get_room_participants_for_broadcast(room)

    for participant in participants:
        participant_room = serialize_group_room(room, current_user_id=participant['user_id'])
        payload = {
            'room_id': room.id,
            'room': participant_room or serialized_room,
            'logs': serialized_logs,
        }
        broadcast_user_event(participant['user_id'], GroupActionLog.ACTION_GROUP_CREATED, payload)

        for log in serialized_logs:
            event_payload = {
                'room_id': room.id,
                'room': participant_room or serialized_room,
                'log': log,
            }
            broadcast_user_event(participant['user_id'], log['action'], event_payload)


def broadcast_group_update(room, serialized_room, serialized_log, extra_user_ids=None):
    participants = get_room_participants_for_broadcast(room)
    room_payload = {
        'room_id': room.id,
        'log': serialized_log,
    }
    broadcast_room_event(room.id, serialized_log['action'], room_payload)

    sent_user_ids = set()
    for participant in participants:
        participant_payload = {
            'room_id': room.id,
            'room': serialize_group_room(room, current_user_id=participant['user_id']),
            'log': serialized_log,
        }
        broadcast_user_event(participant['user_id'], serialized_log['action'], participant_payload)
        sent_user_ids.add(int(participant['user_id']))

    for user_id in extra_user_ids or []:
        numeric_user_id = int(user_id)
        if numeric_user_id in sent_user_ids:
            continue

        extra_payload = {
            'room_id': room.id,
            'room': serialize_group_room(room, current_user_id=numeric_user_id),
            'log': serialized_log,
            'removed_room_id': room.id,
        }
        broadcast_user_event(numeric_user_id, serialized_log['action'], extra_payload)


def is_group_encrypted_message_text(value):
    if not isinstance(value, str):
        return False

    normalized_value = value.strip()
    if not normalized_value or not normalized_value.startswith('{'):
        return False

    try:
        payload = json.loads(normalized_value)
    except ValueError:
        return False

    return (
        payload.get('type') == GROUP_E2EE_MESSAGE_TYPE
        and payload.get('v') == GROUP_E2EE_MESSAGE_VERSION
        and isinstance(payload.get('nonce'), str)
        and isinstance(payload.get('ciphertext'), str)
        and isinstance(payload.get('keys'), list)
    )


def normalize_group_message_payload(payload):
    if not isinstance(payload, dict):
        return None, {'body': ['Request body must be a JSON object.']}

    errors = {}
    text = payload.get('text', '')
    if text is None:
        text = ''
    if not isinstance(text, str):
        errors['text'] = ['Message text must be a string.']
        text = ''
    else:
        max_text_length = (
            MAX_GROUP_ENCRYPTED_MESSAGE_TEXT_LENGTH
            if is_group_encrypted_message_text(text)
            else MAX_GROUP_MESSAGE_TEXT_LENGTH
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

    if not text.strip() and not payload.get('encrypted_upload_intent_ids'):
        errors['message'] = ['Message must include text or at least one attachment.']

    if errors:
        return None, errors

    return {
        'text': text.strip(),
        'client_message_id': client_message_id.strip(),
        'reply_to_message_id': reply_to_message_id,
    }, None


def normalize_group_message_list_params(params):
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


def normalize_group_message_marker_payload(payload, field_name):
    if not isinstance(payload, dict):
        return None, {'body': ['Request body must be a JSON object.']}

    errors = {}
    message_id = payload.get(field_name)
    if message_id in ('', None):
        message_id = None
    else:
        try:
            message_id = int(message_id)
        except (TypeError, ValueError):
            errors[field_name] = ['Message id must be a number.']

    if errors:
        return None, errors

    return {field_name: message_id}, None


def normalize_group_reaction_payload(payload):
    errors = {}
    reaction = normalize_string(payload.get('reaction') if isinstance(payload, dict) else None)

    if not reaction:
        errors['reaction'] = ['Reaction is required.']
    elif reaction not in GroupMessageReaction.ALLOWED_REACTIONS:
        errors['reaction'] = ['Unsupported reaction.']

    return {
        'reaction': reaction,
    }, errors


def list_group_crypto_devices(sender, room_id):
    context, error, status = require_group_member(sender['user_id'], room_id)
    if error:
        return error, status

    user_ids = list(
        RoomParticipant.objects.filter(room_id=context['room'].id, is_active=True)
        .values_list('user_id', flat=True)
    )
    devices = (
        UserDeviceKey.objects
        .filter(user_id__in=user_ids, status=UserDeviceKey.STATUS_ACTIVE)
        .order_by('user_id', '-last_seen_at', '-id')
    )

    return {
        'status': 'ok',
        'room_id': context['room'].id,
        'member_user_ids': user_ids,
        'devices': [serialize_device_key(device) for device in devices],
    }, 200


def has_existing_group_client_message(sender_user_id, client_message_id):
    client_message_id = normalize_string(client_message_id)
    if not client_message_id:
        return False

    return GroupMessage.objects.filter(
        sender_user_id=sender_user_id,
        client_message_id=client_message_id,
    ).exists()


def get_group_reply_target(room, reply_to_message_id, visible_since=None):
    if not reply_to_message_id:
        return None

    messages = GroupMessage.objects.filter(
        room_id=room.id,
        deleted_at__isnull=True,
    )
    if visible_since:
        messages = messages.filter(created_at__gte=visible_since)

    return messages.get(pk=reply_to_message_id)


def create_group_message_receipts(room, message, sender_user_id):
    recipients = RoomParticipant.objects.filter(
        room_id=room.id,
        is_active=True,
    ).exclude(user_id=sender_user_id)

    GroupMessageReceipt.objects.bulk_create(
        [
            GroupMessageReceipt(
                message=message,
                room=room,
                user_id=recipient.user_id,
            )
            for recipient in recipients
        ],
        ignore_conflicts=True,
    )


def resolve_hidden_group_receipt_sender_ids(owner_user_id, sender_user_ids):
    candidate_user_ids = sorted(
        {
            int(user_id)
            for user_id in sender_user_ids
            if user_id and int(user_id) != int(owner_user_id)
        }
    )
    if not candidate_user_ids:
        return set(), None, None

    hidden_user_ids = set()
    missing_candidate_user_ids = []
    for candidate_user_id in candidate_user_ids:
        cached_visibility = get_cached_receipt_visibility(
            owner_user_id,
            candidate_user_id,
        )
        if cached_visibility is None:
            missing_candidate_user_ids.append(candidate_user_id)
        elif cached_visibility:
            hidden_user_ids.add(candidate_user_id)

    if not missing_candidate_user_ids:
        return hidden_user_ids, None, None

    policy_result, policy_status = resolve_parent_receipt_visibility(
        {
            'owner_user_id': int(owner_user_id),
            'candidate_user_ids': missing_candidate_user_ids,
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

    hidden_parent_user_ids = {
        int(user_id)
        for user_id in parent_policy.get('hidden_user_ids') or []
    }
    for candidate_user_id in missing_candidate_user_ids:
        is_hidden = candidate_user_id in hidden_parent_user_ids
        set_cached_receipt_visibility(
            owner_user_id,
            candidate_user_id,
            is_hidden,
        )
        if is_hidden:
            hidden_user_ids.add(candidate_user_id)

    return hidden_user_ids, None, None


def prewarm_group_receipt_visibility(user_id, room_id):
    context, error, status = require_group_member(user_id, room_id)
    if error:
        return error, status

    candidate_user_ids = list(
        RoomParticipant.objects.filter(
            room_id=context['room'].id,
            is_active=True,
        )
        .exclude(user_id=user_id)
        .values_list('user_id', flat=True)
    )
    hidden_sender_ids, policy_error, policy_status = resolve_hidden_group_receipt_sender_ids(
        user_id,
        candidate_user_ids,
    )
    if policy_error:
        return policy_error, policy_status

    return {
        'status': 'prewarmed',
        'room_id': context['room'].id,
        'user_id': user_id,
        'candidate_users': len(candidate_user_ids),
        'hidden_users': len(hidden_sender_ids),
    }, 200


def sync_group_message_status(message):
    receipts = list(
        GroupMessageReceipt.objects.filter(
            message_id=message.id,
            hidden_from_sender=False,
        )
    )
    if not receipts:
        next_status = GroupMessage.STATUS_SENT
    elif all(receipt.read_at for receipt in receipts):
        next_status = GroupMessage.STATUS_READ
    elif all(receipt.delivered_at or receipt.read_at for receipt in receipts):
        next_status = GroupMessage.STATUS_DELIVERED
    else:
        next_status = GroupMessage.STATUS_SENT

    if message.status != next_status:
        message.status = next_status
        message.save(update_fields=['status', 'updated_at'])

    return next_status


def refresh_group_message_statuses(messages, emit_message_ids=None):
    emit_message_id_set = (
        {int(message_id) for message_id in emit_message_ids}
        if emit_message_ids is not None
        else None
    )
    changed_statuses = []
    for message in messages:
        previous_status = message.status
        next_status = sync_group_message_status(message)
        if (
            previous_status != next_status
            and (
                emit_message_id_set is None
                or int(message.id) in emit_message_id_set
            )
        ):
            changed_statuses.append(
                {
                    'message_id': message.id,
                    'status': next_status,
                }
            )

    return changed_statuses


def send_group_message(sender, room_id, payload):
    normalized_payload, errors = normalize_group_message_payload(payload)
    if errors:
        return validation_error(errors)

    context, error, status = require_group_member(sender['user_id'], room_id)
    if error:
        return error, status
    error, status = require_group_open(context, current_user_id=sender['user_id'])
    if error:
        return error, status

    existing_message = None
    if normalized_payload['client_message_id']:
        existing_message = GroupMessage.objects.filter(
            sender_user_id=sender['user_id'],
            client_message_id=normalized_payload['client_message_id'],
        ).first()
    if existing_message:
        return {
            'status': 'duplicate',
            'room': serialize_group_room(existing_message.room, current_user_id=sender['user_id']),
            'message': serialize_group_message(existing_message, sender['user_id']),
        }, 200

    try:
        with transaction.atomic():
            room = context['room']
            reply_to = get_group_reply_target(
                room,
                normalized_payload['reply_to_message_id'],
                visible_since=context['participant'].joined_at,
            )
            message = GroupMessage.objects.create(
                room=room,
                reply_to=reply_to,
                sender_user_id=sender['user_id'],
                text=normalized_payload['text'],
                client_message_id=normalized_payload['client_message_id'],
                status=GroupMessage.STATUS_SENT,
            )
            create_group_message_receipts(room, message, sender['user_id'])
            room.updated_at = timezone.now()
            room.save(update_fields=['updated_at'])
    except GroupMessage.DoesNotExist:
        return validation_error({'reply_to_message_id': ['Reply target message was not found in this group.']})
    except ValidationError as error:
        return validation_error(error.message_dict if hasattr(error, 'message_dict') else error.messages)

    message = (
        GroupMessage.objects
        .select_related('room', 'reply_to')
        .prefetch_related('receipts', 'reactions')
        .get(id=message.id)
    )
    invalidate_room_caches(room.id)

    return {
        'status': 'sent',
        'room': serialize_group_room(room, current_user_id=sender['user_id']),
        'message': serialize_group_message(message, sender['user_id']),
    }, 201


def list_group_messages(user_id, room_id, limit=20, before_message_id=None, around_message_id=None):
    context, error, status = require_group_member(user_id, room_id)
    if error:
        return error, status

    cached_result = get_cached_room_messages(
        user_id,
        room_id,
        limit,
        before_message_id,
        around_message_id,
    )
    if cached_result is not None:
        return cached_result, 200

    messages = (
        GroupMessage.objects.filter(room_id=room_id, deleted_at__isnull=True)
        .select_related('reply_to')
        .prefetch_related('receipts', 'reactions')
    )
    visible_since = context['participant'].joined_at
    if visible_since:
        messages = messages.filter(created_at__gte=visible_since)
    if around_message_id:
        result, status = list_group_messages_around_target(
            user_id=user_id,
            room=context['room'],
            messages=messages,
            limit=limit,
            around_message_id=around_message_id,
            visible_since=visible_since,
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
    ordered_page_messages = list(reversed(page_messages))
    attach_group_participant_identities(ordered_page_messages, context['room'].id)
    serialized_messages = [
        serialize_group_message(message, user_id)
        for message in ordered_page_messages
    ]
    next_before_message_id = page_messages[-1].id if has_more and page_messages else None

    result = {
        'status': 'ok',
        'room': serialize_group_room(context['room'], current_user_id=user_id),
        'messages': serialized_messages,
        'logs': list_group_timeline_logs(context['room'].id, visible_since=visible_since),
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


def list_group_timeline_logs(room_id, limit=GROUP_TIMELINE_LOG_LIMIT, visible_since=None):
    logs = GroupActionLog.objects.filter(room_id=room_id)
    if visible_since:
        logs = logs.filter(created_at__gte=visible_since)

    logs = list(logs.order_by('-created_at', '-id')[:limit])

    return [
        serialize_group_log(log)
        for log in reversed(logs)
    ]


def list_group_messages_around_target(user_id, room, messages, limit, around_message_id, visible_since=None):
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
    attach_group_participant_identities(page_messages, room.id)
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
        'room': serialize_group_room(room, current_user_id=user_id),
        'messages': [
            serialize_group_message(message, user_id)
            for message in page_messages
        ],
        'logs': list_group_timeline_logs(room.id, visible_since=visible_since),
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


def mark_group_room_delivered(user_id, room_id, payload):
    normalized_payload, errors = normalize_group_message_marker_payload(
        payload,
        'last_delivered_message_id',
    )
    if errors:
        return validation_error(errors)

    context, error, status = require_group_member(user_id, room_id)
    if error:
        return error, status

    visible_since = context['participant'].joined_at
    received_messages = GroupMessage.objects.filter(
        room_id=room_id,
        deleted_at__isnull=True,
        receipts__user_id=user_id,
    ).exclude(sender_user_id=user_id)
    if visible_since:
        received_messages = received_messages.filter(created_at__gte=visible_since)

    delivered_message = None
    if normalized_payload['last_delivered_message_id']:
        delivered_message = received_messages.filter(
            id=normalized_payload['last_delivered_message_id'],
        ).first()
        if not delivered_message:
            return validation_error(
                {
                    'last_delivered_message_id': [
                        'Last delivered message must be a received message in this group.',
                    ],
                }
            )
        delivered_until = delivered_message.created_at
    else:
        delivered_message = received_messages.order_by('-created_at', '-id').first()
        delivered_until = delivered_message.created_at if delivered_message else timezone.now()

    receipt_rows = list(
        GroupMessageReceipt.objects.filter(
            room_id=room_id,
            user_id=user_id,
            message__created_at__gte=visible_since,
            message__created_at__lte=delivered_until,
            message__deleted_at__isnull=True,
        )
        .filter(Q(delivered_at__isnull=True) | Q(hidden_from_sender=True))
        .exclude(message__sender_user_id=user_id)
        .values('id', 'message_id', 'message__sender_user_id')
    )
    hidden_sender_ids, policy_error, policy_status = resolve_hidden_group_receipt_sender_ids(
        user_id,
        [row['message__sender_user_id'] for row in receipt_rows],
    )
    if policy_error:
        return policy_error, policy_status

    hidden_receipt_ids = [
        row['id']
        for row in receipt_rows
        if int(row['message__sender_user_id']) in hidden_sender_ids
    ]
    visible_receipt_ids = [
        row['id']
        for row in receipt_rows
        if int(row['message__sender_user_id']) not in hidden_sender_ids
    ]
    visible_message_ids = {
        row['message_id']
        for row in receipt_rows
        if int(row['message__sender_user_id']) not in hidden_sender_ids
    }
    now = timezone.now()
    updated_receipts = 0
    changed_statuses = []
    if hidden_receipt_ids:
        GroupMessageReceipt.objects.filter(id__in=hidden_receipt_ids).update(
            delivered_at=now,
            hidden_from_sender=True,
            updated_at=now,
        )
    if visible_receipt_ids:
        GroupMessageReceipt.objects.filter(id__in=visible_receipt_ids).update(
            delivered_at=now,
            hidden_from_sender=False,
            updated_at=now,
        )
        updated_receipts = len(visible_receipt_ids)
    if receipt_rows:
        affected_messages = list(
            GroupMessage.objects.filter(
                receipts__id__in=[
                    *hidden_receipt_ids,
                    *visible_receipt_ids,
                ]
            )
            .distinct()
            .prefetch_related('receipts')
        )
        changed_statuses = refresh_group_message_statuses(
            affected_messages,
            emit_message_ids=visible_message_ids,
        )

    invalidate_room_caches(context['room'].id)

    return {
        'status': 'delivered',
        'room_id': context['room'].id,
        'user_id': user_id,
        'last_delivered_message_id': delivered_message.id if delivered_message else None,
        'delivered_until': delivered_until.isoformat(),
        'updated_messages': updated_receipts,
        'hidden_receipts': len(hidden_receipt_ids),
        'hidden_sender_user_ids': sorted(hidden_sender_ids),
        'message_statuses': changed_statuses,
        'unread_count': get_group_room_unread_count(
            context['room'].id,
            user_id,
            visible_since=visible_since,
        ),
        'room': serialize_group_room(context['room'], current_user_id=user_id),
    }, 200


def mark_group_room_read(user_id, room_id, payload):
    normalized_payload, errors = normalize_group_message_marker_payload(
        payload,
        'last_read_message_id',
    )
    if errors:
        return validation_error(errors)

    context, error, status = require_group_member(user_id, room_id)
    if error:
        return error, status

    read_message = None
    visible_since = context['participant'].joined_at
    messages = GroupMessage.objects.filter(room_id=room_id, deleted_at__isnull=True)
    if visible_since:
        messages = messages.filter(created_at__gte=visible_since)
    if normalized_payload['last_read_message_id']:
        read_message = messages.filter(id=normalized_payload['last_read_message_id']).first()
        if not read_message:
            return validation_error(
                {
                    'last_read_message_id': [
                        'Last read message was not found in this group.',
                    ],
                }
            )
        requested_read_at = read_message.created_at
    else:
        read_message = messages.order_by('-created_at', '-id').first()
        requested_read_at = read_message.created_at if read_message else timezone.now()

    participant = context['participant']
    current_read_at = participant.last_read_at
    final_read_at = requested_read_at
    read_marker_moved = True
    if current_read_at and current_read_at >= requested_read_at:
        final_read_at = current_read_at
        read_marker_moved = False

    receipt_rows = list(
        GroupMessageReceipt.objects.filter(
            room_id=room_id,
            user_id=user_id,
            message__created_at__gte=visible_since,
            message__created_at__lte=final_read_at,
            message__deleted_at__isnull=True,
        )
        .filter(Q(read_at__isnull=True) | Q(hidden_from_sender=True))
        .exclude(message__sender_user_id=user_id)
        .values('id', 'message_id', 'message__sender_user_id')
    )
    hidden_sender_ids, policy_error, policy_status = resolve_hidden_group_receipt_sender_ids(
        user_id,
        [row['message__sender_user_id'] for row in receipt_rows],
    )
    if policy_error:
        return policy_error, policy_status

    hidden_receipt_ids = [
        row['id']
        for row in receipt_rows
        if int(row['message__sender_user_id']) in hidden_sender_ids
    ]
    visible_receipt_ids = [
        row['id']
        for row in receipt_rows
        if int(row['message__sender_user_id']) not in hidden_sender_ids
    ]
    visible_message_ids = {
        row['message_id']
        for row in receipt_rows
        if int(row['message__sender_user_id']) not in hidden_sender_ids
    }
    now = timezone.now()
    changed_statuses = []
    with transaction.atomic():
        if read_marker_moved:
            participant.last_read_at = final_read_at
            participant.save(update_fields=['last_read_at'])

        if hidden_receipt_ids:
            GroupMessageReceipt.objects.filter(id__in=hidden_receipt_ids).update(
                delivered_at=now,
                read_at=now,
                hidden_from_sender=True,
                updated_at=now,
            )

        if visible_receipt_ids:
            GroupMessageReceipt.objects.filter(id__in=visible_receipt_ids).update(
                delivered_at=now,
                read_at=now,
                hidden_from_sender=False,
                updated_at=now,
            )

    if receipt_rows:
        affected_messages = list(
            GroupMessage.objects.filter(
                receipts__id__in=[
                    *hidden_receipt_ids,
                    *visible_receipt_ids,
                ]
            )
            .distinct()
            .prefetch_related('receipts')
        )
        changed_statuses = refresh_group_message_statuses(
            affected_messages,
            emit_message_ids=visible_message_ids,
        )

    invalidate_room_caches(context['room'].id)

    return {
        'status': 'read',
        'room_id': context['room'].id,
        'user_id': user_id,
        'last_read_message_id': read_message.id if read_message else None,
        'last_read_at': final_read_at.isoformat(),
        'read_marker_moved': read_marker_moved,
        'updated_messages': len(visible_receipt_ids),
        'hidden_receipts': len(hidden_receipt_ids),
        'hidden_sender_user_ids': sorted(hidden_sender_ids),
        'message_statuses': changed_statuses,
        'unread_count': get_group_room_unread_count(
            context['room'].id,
            user_id,
            visible_since=visible_since,
        ),
        'room': serialize_group_room(context['room'], current_user_id=user_id),
    }, 200


def react_to_group_message(user_id, room_id, message_id, payload):
    normalized_payload, errors = normalize_group_reaction_payload(payload)
    if errors:
        return validation_error(errors)

    context, error, status = require_group_member(user_id, room_id)
    if error:
        return error, status
    error, status = require_group_open(context, current_user_id=user_id)
    if error:
        return error, status

    visible_since = context['participant'].joined_at
    message = (
        GroupMessage.objects.filter(
            id=message_id,
            room_id=context['room'].id,
            deleted_at__isnull=True,
            created_at__gte=visible_since,
        )
        .prefetch_related('reactions')
        .first()
    )
    if not message:
        return {
            'status': 'error',
            'message': 'Message not found.',
        }, 404

    requested_reaction = normalized_payload['reaction']
    previous_reaction = None
    action = 'set'

    with transaction.atomic():
        reaction = (
            GroupMessageReaction.objects.select_for_update()
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
            GroupMessageReaction.objects.create(
                message_id=message.id,
                user_id=user_id,
                reaction=requested_reaction,
            )
            current_reaction = requested_reaction

    message = (
        GroupMessage.objects
        .prefetch_related('reactions')
        .get(id=message.id)
    )
    invalidate_room_caches(context['room'].id)

    return {
        'status': 'ok',
        'action': action,
        'room_id': context['room'].id,
        'message_id': message.id,
        'user_id': user_id,
        'reaction': current_reaction,
        'previous_reaction': previous_reaction,
        'reactions': serialize_group_message_reaction_summary(message),
        'my_reaction': current_reaction,
        'room': serialize_group_room(context['room'], current_user_id=user_id),
    }, 200


def create_group_encrypted_file_upload_intents(sender, room_id, payload):
    context, error, status = require_group_member(sender['user_id'], room_id)
    if error:
        return error, status
    error, status = require_group_open(context, current_user_id=sender['user_id'])
    if error:
        return error, status

    normalized_payload, errors = normalize_group_encrypted_upload_intent_payload(payload)
    if errors:
        return validation_error(errors)

    cloudinary_settings, errors = get_group_cloudinary_upload_settings()
    if errors:
        return validation_error(errors)

    cleanup_expired_group_encrypted_upload_intents()

    now = timezone.now()
    timestamp = int(time.time())
    ttl_seconds = int(
        getattr(
            settings,
            'GROUP_MESSAGING_ENCRYPTED_UPLOAD_INTENT_TTL_SECONDS',
            DEFAULT_GROUP_UPLOAD_INTENT_TTL_SECONDS,
        )
        or DEFAULT_GROUP_UPLOAD_INTENT_TTL_SECONDS
    )
    expires_at = now + timedelta(seconds=max(ttl_seconds, 60))
    folder = f'{get_group_encrypted_upload_folder()}/room-{context["room"].id}/user-{sender["user_id"]}'
    upload_intents = []

    for index, attachment in enumerate(normalized_payload['attachments']):
        public_id_param = f'{uuid.uuid4().hex}.txt'
        cloudinary_public_id = f'{folder}/{public_id_param}'
        signature_params = {
            'folder': folder,
            'overwrite': 'false',
            'public_id': public_id_param,
            'timestamp': timestamp,
            'unique_filename': 'false',
            'use_filename': 'false',
        }
        signature = api_sign_request(
            signature_params,
            cloudinary_settings['api_secret'],
        )

        intent = GroupMessageEncryptedUploadIntent.objects.create(
            room=context['room'],
            sender_user_id=sender['user_id'],
            sender_account_number=sender.get('account_number') or context['participant'].account_number or '',
            client_message_id=normalized_payload['client_message_id'],
            attachment_client_id=attachment['attachment_client_id'],
            attachment_index=attachment.get('sort_order', index),
            original_file_name=attachment['file_name'],
            original_mime_type=attachment['mime_type'],
            original_file_size_bytes=attachment['file_size_bytes'],
            encrypted_file_size_bytes=attachment['encrypted_file_size_bytes'],
            cloudinary_public_id=cloudinary_public_id,
            cloudinary_resource_type='raw',
            cloudinary_folder=folder,
            signature_timestamp=timestamp,
            expires_at=expires_at,
        )
        upload_intents.append(
            {
                'id': str(intent.id),
                'attachment_id': intent.attachment_client_id,
                'attachment_index': intent.attachment_index,
                'upload_url': (
                    f'https://api.cloudinary.com/v1_1/'
                    f'{cloudinary_settings["cloud_name"]}/raw/upload'
                ),
                'cloud_name': cloudinary_settings['cloud_name'],
                'api_key': cloudinary_settings['api_key'],
                'resource_type': 'raw',
                'expires_at': intent.expires_at.isoformat(),
                'encrypted_file_size_bytes': intent.encrypted_file_size_bytes,
                'parameters': {
                    **signature_params,
                    'api_key': cloudinary_settings['api_key'],
                    'signature': signature,
                },
            }
        )

    return {
        'status': 'ok',
        'room_id': context['room'].id,
        'upload_intents': upload_intents,
    }, 201


def complete_group_encrypted_file_upload_intent(sender, room_id, upload_intent_id, payload):
    context, error, status = require_group_member(sender['user_id'], room_id)
    if error:
        return error, status
    error, status = require_group_open(context, current_user_id=sender['user_id'])
    if error:
        return error, status

    try:
        intent_id = uuid.UUID(str(upload_intent_id))
    except (TypeError, ValueError):
        return validation_error({'upload_intent_id': ['Upload intent id is invalid.']})

    cloudinary_settings, errors = get_group_cloudinary_upload_settings()
    if errors:
        return validation_error(errors)

    try:
        intent = GroupMessageEncryptedUploadIntent.objects.get(
            id=intent_id,
            room_id=context['room'].id,
            sender_user_id=sender['user_id'],
        )
    except GroupMessageEncryptedUploadIntent.DoesNotExist:
        return {
            'status': 'error',
            'message': 'Upload intent was not found.',
        }, 404

    if intent.status == GroupMessageEncryptedUploadIntent.STATUS_COMPLETED:
        return {
            'status': 'ok',
            'file': serialize_completed_group_upload_intent(intent),
        }, 200

    if intent.status != GroupMessageEncryptedUploadIntent.STATUS_ISSUED:
        return validation_error({'upload_intent': ['Upload intent cannot be completed.']})

    if intent.expires_at <= timezone.now():
        expire_group_upload_intent(intent, cleanup_cloudinary=False)
        return validation_error({'upload_intent': ['Upload intent has expired.']})

    errors = validate_group_cloudinary_upload_response(intent, payload)
    if errors:
        return validation_error(errors)

    version = str(payload.get('version')).strip()
    secure_url = cloudinary_url(
        intent.cloudinary_public_id,
        resource_type='raw',
        secure=True,
        version=version,
        cloud_name=cloudinary_settings['cloud_name'],
    )[0]

    intent.cloudinary_asset_id = normalize_string(payload.get('asset_id'))
    intent.secure_url = secure_url
    intent.status = GroupMessageEncryptedUploadIntent.STATUS_COMPLETED
    intent.completed_at = timezone.now()
    intent.save(
        update_fields=[
            'cloudinary_asset_id',
            'secure_url',
            'status',
            'completed_at',
            'updated_at',
        ]
    )

    return {
        'status': 'ok',
        'file': serialize_completed_group_upload_intent(intent),
    }, 200


def validate_completed_group_encrypted_upload_intents(sender, room_id, payload):
    upload_intent_ids, errors = normalize_group_upload_intent_ids(
        payload.get('encrypted_upload_intent_ids'),
    )
    if errors:
        return None, errors

    if not upload_intent_ids:
        return [], None

    client_message_id = normalize_string(payload.get('client_message_id'))
    if not client_message_id:
        return None, {
            'encrypted_upload_intent_ids': [
                'Client message id is required when encrypted upload intents are attached.',
            ],
        }

    intents = list(
        GroupMessageEncryptedUploadIntent.objects.filter(id__in=upload_intent_ids)
    )
    intents_by_id = {str(intent.id): intent for intent in intents}
    now = timezone.now()
    errors = []

    for index, upload_intent_id in enumerate(upload_intent_ids):
        intent = intents_by_id.get(str(upload_intent_id))
        if not intent:
            errors.append({index: 'Upload intent was not found.'})
            continue

        item_errors = {}
        if intent.sender_user_id != sender['user_id']:
            item_errors['sender'] = 'Upload intent does not belong to this user.'
        if int(intent.room_id) != int(room_id):
            item_errors['room'] = 'Upload intent room does not match this message.'
        if intent.client_message_id != client_message_id:
            item_errors['client_message_id'] = 'Upload intent does not match this message.'
        if intent.status != GroupMessageEncryptedUploadIntent.STATUS_COMPLETED:
            item_errors['status'] = 'Upload intent has not been completed.'
        if intent.expires_at <= now:
            item_errors['expires_at'] = 'Upload intent has expired.'

        if item_errors:
            errors.append({index: item_errors})

    return intents, errors or None


def consume_completed_group_encrypted_upload_intents(intents):
    intent_ids = [intent.id for intent in intents or []]
    if not intent_ids:
        return

    now = timezone.now()
    with transaction.atomic():
        GroupMessageEncryptedUploadIntent.objects.filter(
            id__in=intent_ids,
            status=GroupMessageEncryptedUploadIntent.STATUS_COMPLETED,
        ).update(
            status=GroupMessageEncryptedUploadIntent.STATUS_CONSUMED,
            consumed_at=now,
            updated_at=now,
        )


def normalize_group_encrypted_upload_intent_payload(payload):
    if not isinstance(payload, dict):
        return None, {'body': ['Request body must be a JSON object.']}

    client_message_id = normalize_string(payload.get('client_message_id'))
    if not client_message_id:
        return None, {'client_message_id': ['Client message id is required.']}
    if len(client_message_id) > 120:
        return None, {'client_message_id': ['Client message id cannot exceed 120 characters.']}

    attachments = payload.get('attachments')
    if not isinstance(attachments, list) or not attachments:
        return None, {'attachments': ['At least one attachment is required.']}
    if len(attachments) > MAX_GROUP_ENCRYPTED_UPLOAD_INTENTS_PER_REQUEST:
        return None, {
            'attachments': [
                f'Cannot attach more than {MAX_GROUP_ENCRYPTED_UPLOAD_INTENTS_PER_REQUEST} files to one message.',
            ],
        }

    normalized_attachments = []
    errors = []
    for index, attachment in enumerate(attachments):
        normalized_attachment, item_errors = normalize_group_encrypted_upload_attachment(
            attachment,
            index,
        )
        if item_errors:
            errors.append({index: item_errors})
            continue

        normalized_attachments.append(normalized_attachment)

    if errors:
        return None, {'attachments': errors}

    return {
        'client_message_id': client_message_id,
        'attachments': normalized_attachments,
    }, None


def normalize_group_encrypted_upload_attachment(attachment, index):
    if not isinstance(attachment, dict):
        return None, 'Attachment must be an object.'

    max_size = getattr(
        settings,
        'GROUP_MESSAGING_MAX_ENCRYPTED_UPLOAD_FILE_SIZE_BYTES',
        MAX_GROUP_ENCRYPTED_FILE_SIZE_BYTES,
    )
    file_name = normalize_string(attachment.get('file_name')) or f'attachment-{index + 1}'
    mime_type = normalize_string(attachment.get('mime_type')) or 'application/octet-stream'
    attachment_client_id = normalize_string(attachment.get('id'))[:255]
    file_size_bytes = normalize_positive_int(attachment.get('file_size_bytes'))
    encrypted_file_size_bytes = normalize_positive_int(attachment.get('encrypted_file_size_bytes'))
    sort_order = normalize_non_negative_int(attachment.get('sort_order'))
    errors = {}

    if not file_size_bytes:
        errors['file_size_bytes'] = 'Attachment file is empty.'
    if not encrypted_file_size_bytes:
        errors['encrypted_file_size_bytes'] = 'Encrypted file is empty.'
    elif encrypted_file_size_bytes > max_size:
        errors['encrypted_file_size_bytes'] = (
            f'Encrypted attachment cannot exceed {max_size // (1024 * 1024)} MB.'
        )

    if len(file_name) > 255:
        file_name = file_name[:255]
    if len(mime_type) > 120:
        mime_type = mime_type[:120]

    if errors:
        return None, errors

    return {
        'attachment_client_id': attachment_client_id,
        'file_name': file_name,
        'mime_type': mime_type,
        'file_size_bytes': file_size_bytes,
        'encrypted_file_size_bytes': encrypted_file_size_bytes,
        'sort_order': sort_order if sort_order is not None else index,
    }, None


def normalize_group_upload_intent_ids(value):
    if value in (None, ''):
        return [], None

    if not isinstance(value, list):
        return [], ['Encrypted upload intent ids must be a list.']

    if len(value) > MAX_GROUP_ENCRYPTED_UPLOAD_INTENTS_PER_REQUEST:
        return [], [
            f'Cannot attach more than {MAX_GROUP_ENCRYPTED_UPLOAD_INTENTS_PER_REQUEST} encrypted files to one message.',
        ]

    normalized_ids = []
    errors = []
    seen_ids = set()

    for index, item in enumerate(value):
        try:
            upload_intent_id = str(uuid.UUID(str(item)))
        except (TypeError, ValueError):
            errors.append({index: 'Upload intent id is invalid.'})
            continue

        if upload_intent_id in seen_ids:
            errors.append({index: 'Upload intent id is duplicated.'})
            continue

        seen_ids.add(upload_intent_id)
        normalized_ids.append(upload_intent_id)

    return normalized_ids, errors or None


def validate_group_cloudinary_upload_response(intent, payload):
    if not isinstance(payload, dict):
        return {'body': ['Request body must be a JSON object.']}

    public_id = normalize_string(payload.get('public_id'))
    resource_type = normalize_string(payload.get('resource_type')) or 'raw'
    version = str(payload.get('version') or '').strip()
    signature = normalize_string(payload.get('signature'))
    uploaded_bytes = normalize_positive_int(payload.get('bytes'))
    errors = {}

    if public_id != intent.cloudinary_public_id:
        errors['public_id'] = 'Cloudinary upload does not match this intent.'
    if resource_type != 'raw':
        errors['resource_type'] = 'Encrypted uploads must use raw Cloudinary resource type.'
    if uploaded_bytes != intent.encrypted_file_size_bytes:
        errors['bytes'] = 'Uploaded encrypted file size does not match this intent.'
    if not version:
        errors['version'] = 'Cloudinary upload version is required.'
    if not signature:
        errors['signature'] = 'Cloudinary upload signature is required.'

    if not errors and not verify_api_response_signature(public_id, version, signature):
        errors['signature'] = 'Cloudinary upload signature is invalid.'

    return errors or None


def serialize_completed_group_upload_intent(intent):
    return {
        'upload_intent_id': str(intent.id),
        'encrypted_file_url': intent.secure_url,
        'encrypted_file_size_bytes': intent.encrypted_file_size_bytes,
        'cloudinary_public_id': intent.cloudinary_public_id,
        'cloudinary_asset_id': intent.cloudinary_asset_id,
        'cloudinary_resource_type': intent.cloudinary_resource_type,
        'cloudinary_folder': intent.cloudinary_folder,
    }


def get_group_encrypted_upload_folder():
    root_folder = (
        getattr(settings, 'CLOUDINARY_MAIN_FOLDER', MAIN_CLOUDINARY_FOLDER).strip('/')
        or MAIN_CLOUDINARY_FOLDER
    )
    return f'{root_folder}/e2ee/groups'


def get_group_cloudinary_upload_settings():
    cloudinary_url_value = getattr(settings, 'CLOUDINARY_URL', '')
    if not cloudinary_url_value:
        return None, {'cloudinary': ['Cloudinary is not configured for encrypted uploads.']}

    parsed_url = urlparse(cloudinary_url_value)
    if parsed_url.scheme != 'cloudinary':
        return None, {'cloudinary': ['Cloudinary upload credentials are invalid.']}

    cloud_name = parsed_url.hostname or ''
    api_key = unquote(parsed_url.username or '')
    api_secret = unquote(parsed_url.password or '')

    if not cloud_name or not api_key or not api_secret:
        return None, {'cloudinary': ['Cloudinary upload credentials are incomplete.']}

    cloudinary_config(
        cloud_name=cloud_name,
        api_key=api_key,
        api_secret=api_secret,
        secure=True,
    )

    return {
        'cloud_name': cloud_name,
        'api_key': api_key,
        'api_secret': api_secret,
    }, None


def cleanup_expired_group_encrypted_upload_intents(limit=25):
    now = timezone.now()
    expired_intents = list(
        GroupMessageEncryptedUploadIntent.objects.filter(
            expires_at__lt=now,
            status__in=[
                GroupMessageEncryptedUploadIntent.STATUS_ISSUED,
                GroupMessageEncryptedUploadIntent.STATUS_COMPLETED,
            ],
        ).order_by('expires_at', 'created_at')[:limit]
    )

    for intent in expired_intents:
        expire_group_upload_intent(
            intent,
            cleanup_cloudinary=(
                intent.status == GroupMessageEncryptedUploadIntent.STATUS_COMPLETED
            ),
        )


def expire_group_upload_intent(intent, cleanup_cloudinary=True):
    if cleanup_cloudinary and intent.cloudinary_public_id:
        try:
            cloudinary_uploader.destroy(
                intent.cloudinary_public_id,
                resource_type=intent.cloudinary_resource_type or 'raw',
            )
        except CloudinaryError:
            pass

    intent.status = GroupMessageEncryptedUploadIntent.STATUS_EXPIRED
    intent.save(update_fields=['status', 'updated_at'])


def normalize_positive_int(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None

    return value if value > 0 else None


def normalize_non_negative_int(value):
    if value in (None, ''):
        return None

    try:
        value = int(value)
    except (TypeError, ValueError):
        return None

    return value if value >= 0 else None
