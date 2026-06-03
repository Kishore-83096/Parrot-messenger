from .models import GroupActionLog, GroupMembership, GroupProfile


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

    return {
        'title': profile.title if profile else room.title,
        'avatar_url': profile.avatar_url if profile else '',
        'created_by_user_id': profile.created_by_user_id if profile else room.created_by_user_id,
        'participants': participants,
        'member_count': len(active_participants),
        'my_role': current_participant['role'] if current_participant else '',
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
        'latest_logs': extension.get('latest_logs', []),
    }
