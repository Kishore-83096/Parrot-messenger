from messaging.models import RoomParticipant

from .models import (
    GroupActionLog,
    GroupMembership,
    GroupMessage,
    GroupMessageReaction,
    GroupProfile,
)


def get_group_role_map(room_id):
    return {
        membership.user_id: membership.role
        for membership in GroupMembership.objects.filter(room_id=room_id, is_active=True)
    }


def serialize_group_participant(participant, role=None):
    group_role = role or participant.role or GroupMembership.ROLE_MEMBER

    return {
        'user_id': participant.user_id,
        'account_number': participant.account_number,
        'display_name': participant.display_name,
        'role': group_role,
        'group_role': group_role,
        'is_active': participant.is_active,
        'joined_at': participant.joined_at.isoformat(),
        'last_read_at': participant.last_read_at.isoformat() if participant.last_read_at else None,
    }


def serialize_group_log(log):
    metadata = log.metadata or {}
    return {
        'id': log.id,
        'room_id': log.room_id,
        'action': log.action,
        'actor_user_id': log.actor_user_id,
        'actor_account_number': metadata.get('actor_account_number', ''),
        'actor_display_name': metadata.get('actor_display_name', ''),
        'target_user_id': log.target_user_id,
        'target_account_number': metadata.get('target_account_number', ''),
        'target_display_name': metadata.get('target_display_name', ''),
        'title': metadata.get('title', ''),
        'previous_title': metadata.get('previous_title', ''),
        'avatar_url': metadata.get('avatar_url', ''),
        'text': build_group_log_text(log),
        'created_at': log.created_at.isoformat(),
    }


def get_group_participant_identity_map(room_id):
    return {
        participant.user_id: {
            'user_id': participant.user_id,
            'account_number': participant.account_number,
            'display_name': participant.display_name or participant.account_number,
        }
        for participant in RoomParticipant.objects.filter(room_id=room_id)
    }


def attach_group_participant_identities(messages, room_id):
    identity_map = get_group_participant_identity_map(room_id)

    for message in messages:
        message._group_participant_identity_by_user_id = identity_map
        if getattr(message, 'reply_to', None):
            message.reply_to._group_participant_identity_by_user_id = identity_map

    return messages


def get_group_message_participant_identity_map(message):
    identity_map = getattr(message, '_group_participant_identity_by_user_id', None)
    if identity_map is not None:
        return identity_map

    return get_group_participant_identity_map(message.room_id)


def get_group_participant_identity(identity_map, user_id):
    numeric_user_id = int(user_id or 0)
    identity = identity_map.get(numeric_user_id) or {}

    return {
        'user_id': numeric_user_id,
        'account_number': identity.get('account_number', ''),
        'display_name': identity.get('display_name') or f'User {numeric_user_id}',
    }


def build_group_log_text(log):
    metadata = log.metadata or {}
    actor = metadata.get('actor_display_name') or metadata.get('actor_account_number') or 'Someone'
    target = metadata.get('target_display_name') or metadata.get('target_account_number') or 'a member'

    if log.action == GroupActionLog.ACTION_GROUP_CREATED:
        return f'{actor} created the group'

    if log.action == GroupActionLog.ACTION_MEMBER_ADDED:
        return f'{actor} added {target}'

    if log.action == GroupActionLog.ACTION_MEMBER_REMOVED:
        return f'{actor} removed {target}'

    if log.action == GroupActionLog.ACTION_MEMBER_LEFT:
        return f'{actor} left the group'

    if log.action == GroupActionLog.ACTION_GROUP_UPDATED:
        title = metadata.get('title')
        return f'{actor} changed the group name to {title}' if title else f'{actor} updated the group'

    if log.action == GroupActionLog.ACTION_AVATAR_UPDATED:
        return f'{actor} changed the group picture'

    if log.action == GroupActionLog.ACTION_SUB_ADMIN_ADDED:
        return f'{actor} made {target} a sub admin'

    if log.action == GroupActionLog.ACTION_SUB_ADMIN_REMOVED:
        return f'{actor} removed sub admin from {target}'

    if log.action == GroupActionLog.ACTION_ADMIN_TRANSFERRED:
        return f'{actor} made {target} the admin'

    if log.action == GroupActionLog.ACTION_GROUP_DELETED:
        return f'{actor} deleted the group'

    return 'Group updated'


def get_group_room_extension(room, current_user_id=None, latest_log_limit=8):
    if not room.is_group:
        return {}

    profile = GroupProfile.objects.filter(room_id=room.id).first()
    active_participants = list(
        room.participants.filter(is_active=True).order_by('joined_at', 'id')
    )
    role_map = get_group_role_map(room.id)
    participants = [
        serialize_group_participant(
            participant,
            role_map.get(participant.user_id, participant.role),
        )
        for participant in active_participants
    ]
    current_participant = next(
        (
            participant
            for participant in participants
            if current_user_id and int(participant['user_id']) == int(current_user_id)
        ),
        None,
    )
    logs = list(
        GroupActionLog.objects.filter(room_id=room.id)
        .order_by('-created_at', '-id')[:latest_log_limit]
    )
    latest_message = (
        GroupMessage.objects.filter(room_id=room.id, deleted_at__isnull=True)
        .select_related('reply_to')
        .prefetch_related('receipts', 'reactions')
        .order_by('-created_at', '-id')
        .first()
    )
    unread_count = (
        get_group_room_unread_count(room.id, current_user_id)
        if current_user_id
        else 0
    )

    return {
        'title': profile.title if profile else room.title,
        'avatar_url': profile.avatar_url if profile else '',
        'created_by_user_id': profile.created_by_user_id if profile else room.created_by_user_id,
        'participants': participants,
        'member_count': len(active_participants),
        'my_role': current_participant['role'] if current_participant else '',
        'last_message': serialize_group_message(latest_message, current_user_id) if latest_message else None,
        'unread_count': unread_count,
        'has_unread': unread_count > 0,
        'my_last_read_at': current_participant['last_read_at'] if current_participant else None,
        'latest_logs': [
            serialize_group_log(log)
            for log in reversed(logs)
        ],
    }


def serialize_group_room(room, current_user_id=None):
    extension = get_group_room_extension(room, current_user_id=current_user_id)
    participants = extension.get('participants') or []
    current_participant = next(
        (
            participant
            for participant in participants
            if current_user_id and int(participant['user_id']) == int(current_user_id)
        ),
        None,
    )

    return {
        'id': room.id,
        'room_type': room.room_type,
        'is_group': room.is_group,
        'title': extension.get('title') or room.title,
        'avatar_url': extension.get('avatar_url', ''),
        'created_by_user_id': extension.get('created_by_user_id') or room.created_by_user_id,
        'created_at': room.created_at.isoformat(),
        'updated_at': room.updated_at.isoformat(),
        'participants': participants,
        'other_participants': [
            participant
            for participant in participants
            if not current_user_id or int(participant['user_id']) != int(current_user_id)
        ],
        'member_count': extension.get('member_count', len(participants)),
        'my_role': current_participant['role'] if current_participant else extension.get('my_role', ''),
        'last_message': extension.get('last_message'),
        'unread_count': extension.get('unread_count', 0),
        'has_unread': extension.get('has_unread', False),
        'my_last_read_at': extension.get('my_last_read_at'),
        'latest_logs': extension.get('latest_logs', []),
    }


def get_group_room_unread_count(room_id, current_user_id):
    if not current_user_id:
        return 0

    return (
        GroupMessage.objects.filter(
            room_id=room_id,
            deleted_at__isnull=True,
            receipts__user_id=current_user_id,
            receipts__read_at__isnull=True,
        )
        .exclude(sender_user_id=current_user_id)
        .distinct()
        .count()
    )


def serialize_group_message(message, current_user_id=None):
    if not message:
        return None

    participant_identity_by_user_id = get_group_message_participant_identity_map(message)
    sender_identity = get_group_participant_identity(
        participant_identity_by_user_id,
        message.sender_user_id,
    )
    reaction_data = serialize_group_message_reactions(
        message,
        current_user_id,
        participant_identity_by_user_id=participant_identity_by_user_id,
    )
    return {
        'id': message.id,
        'room_id': message.room_id,
        'room_type': 'group',
        'is_group_message': True,
        'sender_user_id': message.sender_user_id,
        'sender_account_number': sender_identity['account_number'],
        'sender_display_name': sender_identity['display_name'],
        'recipient_user_id': None,
        'reply_to_message_id': message.reply_to_id,
        'reply_to': serialize_group_reply_preview(
            message.reply_to,
            current_user_id,
            participant_identity_by_user_id=participant_identity_by_user_id,
        ) if message.reply_to_id else None,
        'text': message.text,
        'client_message_id': message.client_message_id,
        'status': message.status,
        'created_at': message.created_at.isoformat(),
        'updated_at': message.updated_at.isoformat(),
        'attachments': [],
        'reactions': reaction_data['reactions'],
        'my_reaction': reaction_data['my_reaction'],
        'receipts': serialize_group_message_receipts(message, current_user_id),
    }


def serialize_group_reply_preview(message, current_user_id=None, participant_identity_by_user_id=None):
    if not message or message.deleted_at:
        return None

    identity_map = participant_identity_by_user_id or get_group_message_participant_identity_map(message)
    sender_identity = get_group_participant_identity(identity_map, message.sender_user_id)

    return {
        'id': message.id,
        'room_id': message.room_id,
        'room_type': 'group',
        'is_group_message': True,
        'sender_user_id': message.sender_user_id,
        'sender_account_number': sender_identity['account_number'],
        'sender_display_name': sender_identity['display_name'],
        'recipient_user_id': None,
        'text': message.text,
        'attachment_count': 0,
        'created_at': message.created_at.isoformat(),
    }


def serialize_group_message_reactions(
    message,
    current_user_id=None,
    participant_identity_by_user_id=None,
):
    reaction_counts = {}
    reaction_users = {}
    my_reaction = None
    identity_map = (
        participant_identity_by_user_id or
        get_group_message_participant_identity_map(message)
    )

    for reaction in message.reactions.all():
        reaction_key = reaction.reaction
        identity = get_group_participant_identity(identity_map, reaction.user_id)
        reaction_counts[reaction_key] = reaction_counts.get(reaction_key, 0) + 1
        reaction_users.setdefault(reaction_key, []).append(identity)

        if current_user_id and int(reaction.user_id) == int(current_user_id):
            my_reaction = reaction_key

    reactions = []
    for reaction_key in GroupMessageReaction.ALLOWED_REACTIONS:
        count = reaction_counts.get(reaction_key, 0)
        if not count:
            continue

        reaction_data = {
            'reaction': reaction_key,
            'count': count,
            'users': reaction_users.get(reaction_key, []),
        }
        if current_user_id:
            reaction_data['reacted_by_me'] = reaction_key == my_reaction
        reactions.append(reaction_data)

    return {
        'reactions': reactions,
        'my_reaction': my_reaction,
    }


def serialize_group_message_reaction_summary(message):
    return serialize_group_message_reactions(message, current_user_id=None)['reactions']


def serialize_group_message_receipts(message, current_user_id=None):
    if current_user_id and int(message.sender_user_id) != int(current_user_id):
        return []

    return [
        {
            'user_id': receipt.user_id,
            'delivered_at': receipt.delivered_at.isoformat() if receipt.delivered_at else None,
            'read_at': receipt.read_at.isoformat() if receipt.read_at else None,
        }
        for receipt in message.receipts.all()
    ]
