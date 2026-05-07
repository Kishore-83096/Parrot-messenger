from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer


def get_room_group_name(room_id):
    return f'room_{int(room_id)}'


def get_user_group_name(user_id):
    return f'user_{int(user_id)}'


def broadcast_room_event(room_id, event_type, payload):
    return broadcast_group_event(get_room_group_name(room_id), event_type, payload)


def broadcast_user_event(user_id, event_type, payload):
    return broadcast_group_event(get_user_group_name(user_id), event_type, payload)


def broadcast_participant_event(participants, event_type, payload):
    broadcast_results = []
    for participant in participants:
        broadcast_results.append(
            broadcast_user_event(participant['user_id'], event_type, payload)
        )

    return any(broadcast_results)


def broadcast_group_event(group_name, event_type, payload):
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return False

    try:
        async_to_sync(channel_layer.group_send)(
            group_name,
            {
                'type': 'room.event',
                'payload': {
                    'type': event_type,
                    **payload,
                },
            },
        )
    except Exception:
        return False

    return True
