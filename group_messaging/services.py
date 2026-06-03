import re
from uuid import uuid4

from cloudinary import config as cloudinary_config
from cloudinary import uploader as cloudinary_uploader
from cloudinary.exceptions import Error as CloudinaryError
from django.conf import settings
from django.db import transaction
from django.utils import timezone
import httpx

from messaging.cache import invalidate_room_caches
from messaging.models import Room, RoomParticipant
from messaging.realtime import broadcast_room_event, broadcast_user_event
from messaging.signals import (
    build_parent_headers,
    decode_parent_response,
    label_parent_url,
)

from .models import GroupActionLog, GroupMembership, GroupProfile
from .serializers import serialize_group_log, serialize_group_room


ACCOUNT_NUMBER_PATTERN = re.compile(r'^7\d{9}$')
MAX_GROUP_MEMBERS = 100
MAX_GROUP_TITLE_LENGTH = 120
MAX_GROUP_AVATAR_BYTES = 5 * 1024 * 1024
ALLOWED_AVATAR_CONTENT_TYPES = {'image/avif', 'image/gif', 'image/jpeg', 'image/png', 'image/webp'}
GROUP_MANAGER_ROLES = {GroupMembership.ROLE_ADMIN, GroupMembership.ROLE_SUB_ADMIN}


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
        .select_related('room')
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
            if not created:
                participant.account_number = contact['account_number']
                participant.display_name = contact.get('display_name') or contact['account_number']
                participant.role = RoomParticipant.ROLE_MEMBER
                participant.is_active = True
                participant.save(update_fields=['account_number', 'display_name', 'role', 'is_active'])

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
    participants = get_room_participants_for_broadcast(room)
    with transaction.atomic():
        log = GroupActionLog.objects.create(
            room=room,
            actor_user_id=sender['user_id'],
            action=GroupActionLog.ACTION_GROUP_DELETED,
            metadata=actor_metadata(sender, context['participant']),
        )
        serialized_log = serialize_group_log(log)
        room_id_value = room.id
        room.delete()

    invalidate_room_caches(room_id_value)
    payload = {
        'room_id': room_id_value,
        'room': None,
        'log': serialized_log,
        'deleted': True,
        'removed_room_id': room_id_value,
    }
    broadcast_room_event(room_id_value, GroupActionLog.ACTION_GROUP_DELETED, payload)
    for participant in participants:
        broadcast_user_event(participant['user_id'], GroupActionLog.ACTION_GROUP_DELETED, payload)

    return {
        'status': 'deleted',
        'room_id': room_id_value,
        'removed_room_id': room_id_value,
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
