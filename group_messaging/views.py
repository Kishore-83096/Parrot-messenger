import json

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from messaging.models import Room
from messaging.realtime import broadcast_room_event, broadcast_user_event
from messaging.views import get_authenticated_sender, parse_json_body

from .serializers import serialize_group_room
from .services import (
    add_group_members,
    complete_group_encrypted_file_upload_intent,
    create_group,
    create_group_encrypted_file_upload_intents,
    consume_completed_group_encrypted_upload_intents,
    delete_group,
    delete_group_message_for_everyone,
    edit_group_message,
    get_group_message_action_client_message_id,
    get_group_room,
    has_existing_group_client_message,
    leave_group,
    list_group_crypto_devices,
    list_group_messages,
    mark_group_room_delivered,
    mark_group_room_read,
    normalize_group_message_list_params,
    prewarm_group_receipt_visibility,
    react_to_group_message,
    remove_group_member,
    set_group_message_saved,
    set_group_sub_admin,
    send_group_message,
    transfer_group_admin,
    update_group,
    upload_group_avatar,
    validate_completed_group_encrypted_upload_intents,
)


def broadcast_group_participant_event(room_payload, event_type, payload, exclude_user_ids=None):
    participants = room_payload.get('participants') if isinstance(room_payload, dict) else []
    sent_user_ids = set()
    excluded_user_ids = {
        int(user_id)
        for user_id in (exclude_user_ids or [])
        if user_id
    }
    room = None
    payload_room = payload.get('room') if isinstance(payload, dict) else None
    if isinstance(payload_room, dict):
        room_id = payload_room.get('id') or (
            room_payload.get('id') if isinstance(room_payload, dict) else None
        )
        if room_id:
            room = Room.objects.filter(pk=room_id, room_type=Room.TYPE_GROUP).first()

    for participant in participants or []:
        user_id = participant.get('user_id')
        if (
            not user_id
            or int(user_id) in sent_user_ids
            or int(user_id) in excluded_user_ids
        ):
            continue

        participant_payload = payload
        if room is not None:
            participant_payload = {
                **payload,
                'room': serialize_group_room(room, current_user_id=user_id),
            }

        broadcast_user_event(user_id, event_type, participant_payload)
        sent_user_ids.add(int(user_id))


@csrf_exempt
@require_POST
def create_group_room(request):
    payload, error_response = parse_json_body(request)
    if error_response:
        return error_response

    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    result, response_status = create_group(sender, payload)

    return JsonResponse(
        {
            'status': result.get('status', 'error'),
            'service': 'group_messaging',
            'sender': sender,
            'result': result,
        },
        status=response_status,
    )


@csrf_exempt
def group_room_detail(request, room_id):
    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    if request.method == 'GET':
        result, response_status = get_group_room(sender, room_id)
        return JsonResponse(
            {
                'status': result.get('status', 'error'),
                'service': 'group_messaging',
                'sender': sender,
                'result': result,
            },
            status=response_status,
        )

    if request.method == 'DELETE':
        result, response_status = delete_group(sender, room_id)
        return JsonResponse(
            {
                'status': result.get('status', 'error'),
                'service': 'group_messaging',
                'sender': sender,
                'result': result,
            },
            status=response_status,
        )

    if request.method != 'PATCH':
        return JsonResponse(
            {
                'status': 'error',
                'service': 'group_messaging',
                'message': 'Method not allowed.',
            },
            status=405,
        )

    try:
        payload = json.loads(request.body.decode('utf-8') or '{}')
    except ValueError:
        return JsonResponse(
            {
                'status': 'error',
                'service': 'group_messaging',
                'message': 'Request body must be valid JSON.',
            },
            status=400,
        )

    result, response_status = update_group(sender, room_id, payload)

    return JsonResponse(
        {
            'status': result.get('status', 'error'),
            'service': 'group_messaging',
            'sender': sender,
            'result': result,
        },
        status=response_status,
    )


@csrf_exempt
@require_POST
def group_members(request, room_id):
    payload, error_response = parse_json_body(request)
    if error_response:
        return error_response

    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    result, response_status = add_group_members(sender, room_id, payload)

    return JsonResponse(
        {
            'status': result.get('status', 'error'),
            'service': 'group_messaging',
            'sender': sender,
            'result': result,
        },
        status=response_status,
    )


@csrf_exempt
def group_member_detail(request, room_id, user_id):
    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    if request.method != 'DELETE':
        return JsonResponse(
            {
                'status': 'error',
                'service': 'group_messaging',
                'message': 'Method not allowed.',
            },
            status=405,
        )

    result, response_status = remove_group_member(sender, room_id, user_id)

    return JsonResponse(
        {
            'status': result.get('status', 'error'),
            'service': 'group_messaging',
            'sender': sender,
            'result': result,
        },
        status=response_status,
    )


@csrf_exempt
def group_member_sub_admin(request, room_id, user_id):
    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    if request.method not in {'POST', 'DELETE'}:
        return JsonResponse(
            {
                'status': 'error',
                'service': 'group_messaging',
                'message': 'Method not allowed.',
            },
            status=405,
        )

    result, response_status = set_group_sub_admin(
        sender,
        room_id,
        user_id,
        request.method == 'POST',
    )

    return JsonResponse(
        {
            'status': result.get('status', 'error'),
            'service': 'group_messaging',
            'sender': sender,
            'result': result,
        },
        status=response_status,
    )


@csrf_exempt
@require_POST
def group_admin_transfer(request, room_id):
    payload, error_response = parse_json_body(request)
    if error_response:
        return error_response

    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    result, response_status = transfer_group_admin(
        sender,
        room_id,
        payload.get('user_id') or payload.get('target_user_id'),
    )

    return JsonResponse(
        {
            'status': result.get('status', 'error'),
            'service': 'group_messaging',
            'sender': sender,
            'result': result,
        },
        status=response_status,
    )


@csrf_exempt
@require_POST
def group_leave(request, room_id):
    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    result, response_status = leave_group(sender, room_id)

    return JsonResponse(
        {
            'status': result.get('status', 'error'),
            'service': 'group_messaging',
            'sender': sender,
            'result': result,
        },
        status=response_status,
    )


@csrf_exempt
@require_POST
def group_avatar(request, room_id):
    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    result, response_status = upload_group_avatar(
        sender,
        room_id,
        request.FILES.get('avatar') or request.FILES.get('group_picture'),
    )

    return JsonResponse(
        {
            'status': result.get('status', 'error'),
            'service': 'group_messaging',
            'sender': sender,
            'result': result,
        },
        status=response_status,
    )


@require_GET
def group_crypto_devices(request, room_id):
    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    result, response_status = list_group_crypto_devices(sender, room_id)

    return JsonResponse(
        {
            'status': result.get('status', 'error'),
            'service': 'group_messaging',
            'sender': sender,
            'result': result,
        },
        status=response_status,
    )


@csrf_exempt
@require_POST
def group_create_crypto_file_upload_intents(request, room_id):
    payload, error_response = parse_json_body(request)
    if error_response:
        return error_response

    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    result, response_status = create_group_encrypted_file_upload_intents(
        sender,
        room_id,
        payload,
    )

    return JsonResponse(
        {
            'status': result.get('status', 'error'),
            'service': 'group_messaging',
            'sender': sender,
            'result': result,
        },
        status=response_status,
    )


@csrf_exempt
@require_POST
def group_complete_crypto_file_upload_intent(request, room_id, upload_intent_id):
    payload, error_response = parse_json_body(request)
    if error_response:
        return error_response

    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    result, response_status = complete_group_encrypted_file_upload_intent(
        sender,
        room_id,
        upload_intent_id,
        payload,
    )

    return JsonResponse(
        {
            'status': result.get('status', 'error'),
            'service': 'group_messaging',
            'sender': sender,
            'result': result,
        },
        status=response_status,
    )


@csrf_exempt
@require_POST
def group_send_message(request, room_id):
    payload, error_response = parse_json_body(request)
    if error_response:
        return error_response

    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    encrypted_upload_intents = []
    if not payload or not isinstance(payload, dict):
        payload = {}

    membership_result, membership_status = get_group_room(sender, room_id)
    if membership_status >= 300:
        return JsonResponse(
            {
                'status': membership_result.get('status', 'error'),
                'service': 'group_messaging',
                'sender': sender,
                'result': membership_result,
            },
            status=membership_status,
        )

    if not has_existing_group_client_message(
        sender['user_id'],
        payload.get('client_message_id'),
    ):
        encrypted_upload_intents, upload_intent_errors = validate_completed_group_encrypted_upload_intents(
            sender,
            room_id,
            payload,
        )
        if upload_intent_errors:
            return JsonResponse(
                {
                    'status': 'error',
                    'service': 'group_messaging',
                    'sender': sender,
                    'result': {
                        'status': 'error',
                        'errors': {
                            'encrypted_upload_intent_ids': upload_intent_errors,
                        },
                    },
                },
                status=400,
            )

    result, response_status = send_group_message(sender, room_id, payload)
    if response_status < 300 and result.get('status') == 'sent':
        consume_completed_group_encrypted_upload_intents(encrypted_upload_intents)
        event_payload = {
            'room': result['room'],
            'message': result['message'],
            'sender': sender,
        }
        broadcast_room_event(
            result['message']['room_id'],
            'group.message.sent',
            {
                'message': result['message'],
                'sender': sender,
            },
        )
        broadcast_group_participant_event(result['room'], 'group.message.sent', event_payload)

    return JsonResponse(
        {
            'status': result.get('status', 'error'),
            'service': 'group_messaging',
            'sender': sender,
            'result': result,
        },
        status=response_status,
    )


@csrf_exempt
@require_POST
def group_edit_message(request, room_id, message_id):
    payload, error_response = parse_json_body(request)
    if error_response:
        return error_response

    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    encrypted_upload_intents = []
    if isinstance(payload, dict) and payload.get('encrypted_upload_intent_ids'):
        client_message_id = get_group_message_action_client_message_id(
            sender,
            room_id,
            message_id,
        )
        payload = {
            **payload,
            'client_message_id': client_message_id,
        }
        encrypted_upload_intents, upload_intent_errors = validate_completed_group_encrypted_upload_intents(
            sender,
            room_id,
            payload,
        )
        if upload_intent_errors:
            return JsonResponse(
                {
                    'status': 'error',
                    'service': 'group_messaging',
                    'sender': sender,
                    'result': {
                        'status': 'error',
                        'errors': {
                            'encrypted_upload_intent_ids': upload_intent_errors,
                        },
                    },
                },
                status=400,
            )

    result, response_status = edit_group_message(
        sender,
        room_id,
        message_id,
        payload,
        replacement_upload_intents=encrypted_upload_intents,
    )
    if response_status < 300 and result.get('status') == 'edited':
        consume_completed_group_encrypted_upload_intents(encrypted_upload_intents)
        event_payload = {
            'room': result['room'],
            'message': result['message'],
            'sender': sender,
        }
        broadcast_room_event(
            result['message']['room_id'],
            'group.message.edited',
            {
                'message': result['message'],
                'sender': sender,
            },
        )
        broadcast_group_participant_event(
            result['room'],
            'group.message.edited',
            event_payload,
        )

    return JsonResponse(
        {
            'status': result.get('status', 'error'),
            'service': 'group_messaging',
            'sender': sender,
            'result': result,
        },
        status=response_status,
    )


@csrf_exempt
@require_POST
def group_delete_message(request, room_id, message_id):
    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    result, response_status = delete_group_message_for_everyone(
        sender,
        room_id,
        message_id,
    )
    if response_status < 300 and result.get('status') == 'deleted':
        event_payload = {
            'room': result['room'],
            'message': result['message'],
            'sender': sender,
        }
        broadcast_room_event(
            result['message']['room_id'],
            'group.message.deleted',
            {
                'message': result['message'],
                'sender': sender,
            },
        )
        broadcast_group_participant_event(
            result['room'],
            'group.message.deleted',
            event_payload,
        )

    return JsonResponse(
        {
            'status': result.get('status', 'error'),
            'service': 'group_messaging',
            'sender': sender,
            'result': result,
        },
        status=response_status,
    )


@require_GET
def group_room_messages(request, room_id):
    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    limit, before_message_id, around_message_id, errors = normalize_group_message_list_params(request.GET)
    if errors:
        return JsonResponse(
            {
                'status': 'error',
                'service': 'group_messaging',
                'errors': errors,
            },
            status=400,
        )

    result, response_status = list_group_messages(
        user_id=sender['user_id'],
        room_id=room_id,
        limit=limit,
        before_message_id=before_message_id,
        around_message_id=around_message_id,
    )

    return JsonResponse(
        {
            'status': result.get('status', 'error'),
            'service': 'group_messaging',
            'sender': sender,
            'result': result,
        },
        status=response_status,
    )


@csrf_exempt
@require_POST
def group_receipt_visibility_prewarm(request, room_id):
    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    result, response_status = prewarm_group_receipt_visibility(
        sender['user_id'],
        room_id,
    )

    return JsonResponse(
        {
            'status': result.get('status', 'error'),
            'service': 'group_messaging',
            'sender': sender,
            'result': result,
        },
        status=response_status,
    )


@csrf_exempt
@require_POST
def group_deliver_room(request, room_id):
    payload, error_response = parse_json_body(request)
    if error_response:
        return error_response

    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    result, response_status = mark_group_room_delivered(
        sender['user_id'],
        room_id,
        payload,
    )
    if response_status < 300 and result.get('updated_messages', 0) > 0:
        event_payload = {
            'room_id': result['room_id'],
            'user_id': result['user_id'],
            'last_delivered_message_id': result['last_delivered_message_id'],
            'delivered_until': result['delivered_until'],
            'updated_messages': result['updated_messages'],
            'unread_count': result['unread_count'],
            'message_statuses': result.get('message_statuses', []),
        }
        broadcast_group_participant_event(
            result['room'],
            'group.message.delivered',
            event_payload,
            exclude_user_ids=result.get('hidden_sender_user_ids', []),
        )

    return JsonResponse(
        {
            'status': result.get('status', 'error'),
            'service': 'group_messaging',
            'sender': sender,
            'result': result,
        },
        status=response_status,
    )


@csrf_exempt
@require_POST
def group_read_room(request, room_id):
    payload, error_response = parse_json_body(request)
    if error_response:
        return error_response

    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    result, response_status = mark_group_room_read(
        sender['user_id'],
        room_id,
        payload,
    )
    message_statuses = result.get('message_statuses', [])
    should_emit_read_event = (
        response_status < 300
        and (
            result.get('updated_messages', 0) > 0
            or result.get('hidden_receipts', 0) > 0
        )
    )
    if should_emit_read_event:
        event_payload = {
            'room_id': result['room_id'],
            'user_id': result['user_id'],
            'last_read_message_id': result['last_read_message_id'],
            'last_read_at': result['last_read_at'],
            'read_marker_moved': result['read_marker_moved'],
            'updated_messages': result['updated_messages'],
            'unread_count': result['unread_count'],
            'message_statuses': message_statuses,
        }
        if message_statuses and not result.get('hidden_sender_user_ids'):
            broadcast_room_event(
                room_id,
                'group.message.read',
                event_payload,
            )
        broadcast_group_participant_event(
            result['room'],
            'group.message.read',
            event_payload,
            exclude_user_ids=result.get('hidden_sender_user_ids', []),
        )

    return JsonResponse(
        {
            'status': result.get('status', 'error'),
            'service': 'group_messaging',
            'sender': sender,
            'result': result,
        },
        status=response_status,
    )


@csrf_exempt
@require_POST
def group_message_reaction(request, room_id, message_id):
    payload, error_response = parse_json_body(request)
    if error_response:
        return error_response

    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    result, response_status = react_to_group_message(
        user_id=sender['user_id'],
        room_id=room_id,
        message_id=message_id,
        payload=payload,
    )
    if response_status < 300:
        event_payload = {
            'room_id': result['room_id'],
            'message_id': result['message_id'],
            'user_id': result['user_id'],
            'reaction': result['reaction'],
            'previous_reaction': result['previous_reaction'],
            'action': result['action'],
            'reactions': result['reactions'],
        }
        broadcast_room_event(room_id, 'group.message.reaction_updated', event_payload)
        broadcast_group_participant_event(result['room'], 'group.message.reaction_updated', event_payload)

    return JsonResponse(
        {
            'status': result.get('status', 'error'),
            'service': 'group_messaging',
            'sender': sender,
            'result': result,
        },
        status=response_status,
    )


@csrf_exempt
@require_POST
def group_save_message(request, room_id, message_id):
    payload, error_response = parse_json_body(request)
    if error_response:
        return error_response

    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    result, response_status = set_group_message_saved(
        user_id=sender['user_id'],
        room_id=room_id,
        message_id=message_id,
        payload=payload,
    )

    return JsonResponse(
        {
            'status': result.get('status', 'error'),
            'service': 'group_messaging',
            'sender': sender,
            'result': result,
        },
        status=response_status,
    )
