import json

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .auth import validate_messaging_token
from .e2ee.backups import get_user_key_backup, save_user_key_backup
from .e2ee.devices import (
    list_accessible_user_device_keys,
    list_user_device_keys,
    register_user_device_key,
    require_default_device_signature,
    revoke_user_device_key,
    set_default_user_device_key,
    update_default_device_password,
)
from .e2ee.files import upload_encrypted_file
from .realtime import broadcast_participant_event, broadcast_room_event, broadcast_user_event
from .signals import (
    authorize_parent_messaging,
    check_database,
    check_parent_services,
    check_redis,
)
from .services import (
    cleanup_uploaded_attachments,
    create_direct_message,
    get_existing_direct_room_authorization,
    list_room_messages,
    list_user_rooms,
    mark_room_delivered,
    mark_room_read,
    normalize_message_list_params,
    release_room_blocked_messages,
    upload_message_files,
)


def health_check(request):
    database_status = check_database()
    redis_status = check_redis()
    parent_status = check_parent_services()
    core_healthy = database_status['ok'] and redis_status['ok']

    if core_healthy and parent_status['ok']:
        status = 'ok'
        response_status = 200
    elif core_healthy and parent_status['any_connected']:
        status = 'partial'
        response_status = 200
    else:
        status = 'degraded'
        response_status = 503

    return JsonResponse(
        {
            'status': status,
            'service': 'messenger',
            'environment': settings.DJANGO_ENV,
            'debug': settings.DEBUG,
            'checks': {
                'database': database_status,
                'redis': redis_status,
                'parents': parent_status,
            },
        },
        status=response_status,
    )


def get_authorization_status(result, response_status):
    messenger_response = result.get('messenger')
    parent_response = result.get('parent', {}).get('response')

    if isinstance(messenger_response, dict) and messenger_response.get('allowed') is True:
        return 'allowed'

    if isinstance(parent_response, dict) and parent_response.get('allowed') is True:
        return 'allowed'

    if isinstance(parent_response, dict) and parent_response.get('allowed') is False:
        return 'denied'

    if response_status >= 500:
        return 'degraded'

    return 'error'


def sanitize_authorization_result(result):
    if isinstance(result, dict):
        return {
            key: sanitize_authorization_result(value)
            for key, value in result.items()
            if key not in {'delivery_blocked', 'block_context'}
        }

    if isinstance(result, list):
        return [sanitize_authorization_result(value) for value in result]

    return result


def build_public_message_result(message_result):
    return {
        key: value
        for key, value in message_result.items()
        if not key.startswith('_')
    }


def get_sender_participants(room, sender):
    sender_user_id = int(sender['user_id'])

    return [
        participant
        for participant in room['participants']
        if int(participant['user_id']) == sender_user_id
    ]


def parse_json_body(request):
    try:
        return json.loads(request.body.decode('utf-8') or '{}'), None
    except ValueError:
        return None, JsonResponse(
            {
                'status': 'error',
                'service': 'messenger',
                'message': 'Request body must be valid JSON.',
            },
            status=400,
        )


def parse_send_message_body(request):
    content_type = (request.content_type or '').split(';', 1)[0].strip().lower()

    if content_type == 'multipart/form-data':
        payload = request.POST.dict()
        uploaded_files = [
            *request.FILES.getlist('attachments'),
            *request.FILES.getlist('files'),
            *request.FILES.getlist('media'),
        ]
        return payload, uploaded_files, None

    payload, error_response = parse_json_body(request)
    return payload, [], error_response


def get_authenticated_sender(request):
    token_result, token_status = validate_messaging_token(request.headers.get('Authorization', ''))
    if token_result['ok']:
        return {
            'user_id': token_result['sender_user_id'],
            'account_number': token_result.get('account_number'),
        }, None

    return None, JsonResponse(
        {
            'status': 'error',
            'service': 'messenger',
            'message': token_result['message'],
        },
        status=token_status,
    )


def authorize_sender_for_recipient(sender, recipient_account_number):
    parent_payload = {
        'sender_user_id': sender['user_id'],
        'recipient_account_number': recipient_account_number,
    }
    authorization_result, response_status = authorize_parent_messaging(parent_payload)
    parent_response = authorization_result.get('parent', {}).get('response')

    if isinstance(parent_response, dict) and parent_response.get('allowed') is True:
        return parent_response, authorization_result, response_status

    return None, authorization_result, response_status


def should_allow_shared_room_fallback(authorization_result):
    parent_response = authorization_result.get('parent', {}).get('response')

    return (
        isinstance(parent_response, dict)
        and parent_response.get('allowed') is False
        and parent_response.get('reason') == 'contact_not_saved'
    )


def build_shared_room_authorization_result(parent_authorization_result, room_authorization):
    parent_response = parent_authorization_result.get('parent', {}).get('response')
    authorization_result = {
        'ok': True,
        'parent': parent_authorization_result.get('parent', {}),
        'messenger': {
            'allowed': True,
            'reason': 'shared_room',
            'room_id': room_authorization['room_id'],
            'room_type': room_authorization['room_type'],
            'sender_user_id': room_authorization['sender_user_id'],
            'recipient_user_id': room_authorization['recipient_user_id'],
            'recipient_account_number': room_authorization['recipient_account_number'],
            'delivery_blocked': bool(
                isinstance(parent_response, dict)
                and parent_response.get('delivery_blocked')
            ),
        },
    }

    if parent_authorization_result.get('failed_parents'):
        authorization_result['failed_parents'] = parent_authorization_result['failed_parents']

    return authorization_result


def authorize_sender_for_message(sender, recipient_account_number):
    parent_authorization, authorization_result, response_status = authorize_sender_for_recipient(
        sender,
        recipient_account_number,
    )
    if parent_authorization is not None:
        return parent_authorization, authorization_result, response_status

    if not should_allow_shared_room_fallback(authorization_result):
        return None, authorization_result, response_status

    room_authorization = get_existing_direct_room_authorization(sender, recipient_account_number)
    if not room_authorization:
        return None, authorization_result, response_status

    parent_response = authorization_result.get('parent', {}).get('response')
    if isinstance(parent_response, dict) and parent_response.get('delivery_blocked'):
        room_authorization = {
            **room_authorization,
            'delivery_blocked': True,
        }

    return room_authorization, build_shared_room_authorization_result(
        authorization_result,
        room_authorization,
    ), 200


@csrf_exempt
@require_POST
def authorize_message(request):
    payload, error_response = parse_json_body(request)
    if error_response:
        return error_response

    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    _, authorization_result, response_status = authorize_sender_for_message(
        sender,
        payload.get('recipient_account_number'),
    )

    return JsonResponse(
        {
            'status': get_authorization_status(authorization_result, response_status),
            'service': 'messenger',
            'sender': sender,
            'authorization': sanitize_authorization_result(authorization_result),
        },
        status=response_status,
    )


@csrf_exempt
@require_POST
def register_crypto_device(request):
    payload, error_response = parse_json_body(request)
    if error_response:
        return error_response

    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    result, response_status = register_user_device_key(sender['user_id'], payload)

    return JsonResponse(
        {
            'status': result.get('status', 'error'),
            'service': 'messenger',
            'sender': sender,
            'result': result,
        },
        status=response_status,
    )


@csrf_exempt
@require_POST
def revoke_crypto_device(request, device_id):
    payload, error_response = parse_json_body(request)
    if error_response:
        return error_response

    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    result, response_status = revoke_user_device_key(
        sender['user_id'],
        device_id,
        payload,
    )
    if response_status < 300 and result.get('revoked'):
        broadcast_user_event(
            sender['user_id'],
            'device.revoked',
            {
                'device_id': result['device_id'],
                'revoked_by_device_id': payload.get('acting_device_id'),
            },
        )

    return JsonResponse(
        {
            'status': result.get('status', 'error'),
            'service': 'messenger',
            'sender': sender,
            'result': result,
        },
        status=response_status,
    )


@csrf_exempt
@require_POST
def set_default_crypto_device(request, device_id):
    payload, error_response = parse_json_body(request)
    if error_response:
        return error_response

    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    result, response_status = set_default_user_device_key(
        sender['user_id'],
        device_id,
        payload,
    )
    if response_status < 300 and result.get('device'):
        broadcast_user_event(
            sender['user_id'],
            'device.default_changed',
            {
                'device': result['device'],
                'changed_by_device_id': payload.get('acting_device_id'),
            },
        )

    return JsonResponse(
        {
            'status': result.get('status', 'error'),
            'service': 'messenger',
            'sender': sender,
            'result': result,
        },
        status=response_status,
    )


@csrf_exempt
@require_POST
def update_default_crypto_device_password(request):
    payload, error_response = parse_json_body(request)
    if error_response:
        return error_response

    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    result, response_status = update_default_device_password(
        sender['user_id'],
        payload,
    )

    return JsonResponse(
        {
            'status': result.get('status', 'error'),
            'service': 'messenger',
            'sender': sender,
            'result': result,
        },
        status=response_status,
    )


@require_GET
def user_crypto_devices(request, user_id):
    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    result, response_status = list_accessible_user_device_keys(sender['user_id'], user_id)

    return JsonResponse(
        {
            'status': result.get('status', 'error'),
            'service': 'messenger',
            'sender': sender,
            'result': result,
        },
        status=response_status,
    )


@require_GET
def recipient_crypto_devices(request, recipient_account_number):
    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    parent_authorization, authorization_result, authorization_status = authorize_sender_for_message(
        sender,
        recipient_account_number,
    )
    if parent_authorization is None:
        return JsonResponse(
            {
                'status': get_authorization_status(authorization_result, authorization_status),
                'service': 'messenger',
                'sender': sender,
                'authorization': sanitize_authorization_result(authorization_result),
            },
            status=authorization_status,
        )

    result, response_status = list_user_device_keys(parent_authorization['recipient_user_id'])

    return JsonResponse(
        {
            'status': result.get('status', 'error'),
            'service': 'messenger',
            'sender': sender,
            'authorization': sanitize_authorization_result(authorization_result),
            'result': result,
        },
        status=response_status,
    )


@csrf_exempt
@require_POST
def upload_crypto_file(request):
    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    uploaded_file = request.FILES.get('file')
    result, response_status = upload_encrypted_file(uploaded_file)

    return JsonResponse(
        {
            'status': result.get('status', 'error'),
            'service': 'messenger',
            'sender': sender,
            'result': result,
        },
        status=response_status,
    )


@csrf_exempt
def crypto_key_backup(request):
    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    if request.method == 'GET':
        result, response_status = get_user_key_backup(sender['user_id'])
    elif request.method == 'POST':
        payload, error_response = parse_json_body(request)
        if error_response:
            return error_response

        signature_result, signature_status = require_default_device_signature(
            sender['user_id'],
            payload,
            'recovery.backup.save',
            'key-backup',
        )
        if signature_status >= 300:
            return JsonResponse(
                {
                    'status': signature_result.get('status', 'error'),
                    'service': 'messenger',
                    'sender': sender,
                    'result': signature_result,
                },
                status=signature_status,
            )

        result, response_status = save_user_key_backup(sender['user_id'], payload)
        if response_status < 300 and result.get('backup'):
            broadcast_user_event(
                sender['user_id'],
                'recovery.key_updated',
                {
                    'backup_updated_at': result['backup']['updated_at'],
                    'updated_by_device_id': payload.get('acting_device_id'),
                },
            )
    else:
        return JsonResponse(
            {
                'status': 'error',
                'service': 'messenger',
                'message': 'Method not allowed.',
            },
            status=405,
        )

    return JsonResponse(
        {
            'status': result.get('status', 'error'),
            'service': 'messenger',
            'sender': sender,
            'result': result,
        },
        status=response_status,
    )


@csrf_exempt
@require_POST
def send_message(request):
    payload, uploaded_files, error_response = parse_send_message_body(request)
    if error_response:
        return error_response

    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    parent_authorization, authorization_result, authorization_status = authorize_sender_for_message(
        sender,
        payload.get('recipient_account_number'),
    )
    if parent_authorization is None:
        return JsonResponse(
            {
                'status': get_authorization_status(authorization_result, authorization_status),
                'service': 'messenger',
                'sender': sender,
                'authorization': sanitize_authorization_result(authorization_result),
            },
            status=authorization_status,
        )

    uploaded_attachments, attachment_errors = upload_message_files(uploaded_files)
    if attachment_errors:
        return JsonResponse(
            {
                'status': 'error',
                'service': 'messenger',
                'sender': sender,
                'authorization': sanitize_authorization_result(authorization_result),
                'result': {
                    'status': 'error',
                    'errors': {
                        'attachments': attachment_errors,
                    },
                },
            },
            status=400,
        )

    if uploaded_attachments:
        payload = {
            **payload,
            'attachments': [
                *(
                    payload.get('attachments')
                    if isinstance(payload.get('attachments'), list)
                    else []
                ),
                *uploaded_attachments,
            ],
        }

    message_result, message_status = create_direct_message(sender, parent_authorization, payload)
    if message_status >= 300:
        cleanup_uploaded_attachments(uploaded_attachments)

    delivery_blocked = bool(message_result.get('_delivery_blocked'))
    if message_status < 300 and message_result.get('status') == 'sent':
        event_payload = {
            'room': message_result['room'],
            'message': message_result['message'],
            'sender': sender,
        }
        if not delivery_blocked:
            broadcast_room_event(
                message_result['message']['room_id'],
                'message.sent',
                event_payload,
            )
            event_participants = message_result['room']['participants']
        else:
            event_participants = get_sender_participants(message_result['room'], sender)

        broadcast_participant_event(
            event_participants,
            'message.sent',
            event_payload,
        )

    return JsonResponse(
        {
            'status': message_result['status'],
            'service': 'messenger',
            'sender': sender,
            'authorization': sanitize_authorization_result(authorization_result),
            'result': build_public_message_result(message_result),
        },
        status=message_status,
    )


@require_GET
def list_rooms(request):
    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    rooms_result, rooms_status = list_user_rooms(sender['user_id'])

    return JsonResponse(
        {
            'status': rooms_result['status'],
            'service': 'messenger',
            'user': sender,
            'result': rooms_result,
        },
        status=rooms_status,
    )


@require_GET
def room_messages(request, room_id):
    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    limit, before_message_id, around_message_id, errors = normalize_message_list_params(request.GET)
    if errors:
        return JsonResponse(
            {
                'status': 'error',
                'service': 'messenger',
                'errors': errors,
            },
            status=400,
        )

    messages_result, messages_status = list_room_messages(
        user_id=sender['user_id'],
        room_id=room_id,
        limit=limit,
        before_message_id=before_message_id,
        around_message_id=around_message_id,
    )

    return JsonResponse(
        {
            'status': messages_result['status'],
            'service': 'messenger',
            'user': sender,
            'result': messages_result,
        },
        status=messages_status,
    )


@csrf_exempt
@require_POST
def read_room(request, room_id):
    payload, error_response = parse_json_body(request)
    if error_response:
        return error_response

    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    read_result, read_status = mark_room_read(
        user_id=sender['user_id'],
        room_id=room_id,
        payload=payload,
    )
    if read_status < 300:
        event_payload = {
            'room_id': read_result['room_id'],
            'user_id': read_result['user_id'],
            'last_read_message_id': read_result['last_read_message_id'],
            'last_read_at': read_result['last_read_at'],
            'read_marker_moved': read_result['read_marker_moved'],
            'updated_messages': read_result['updated_messages'],
            'unread_count': read_result['unread_count'],
        }
        broadcast_room_event(
            room_id,
            'message.read',
            event_payload,
        )
        broadcast_participant_event(
            read_result['room']['participants'],
            'message.read',
            event_payload,
        )

    return JsonResponse(
        {
            'status': read_result['status'],
            'service': 'messenger',
            'user': sender,
            'result': read_result,
        },
        status=read_status,
    )


@csrf_exempt
@require_POST
def deliver_room(request, room_id):
    payload, error_response = parse_json_body(request)
    if error_response:
        return error_response

    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    delivered_result, delivered_status = mark_room_delivered(
        user_id=sender['user_id'],
        room_id=room_id,
        payload=payload,
    )
    if delivered_status < 300:
        event_payload = {
            'room_id': delivered_result['room_id'],
            'user_id': delivered_result['user_id'],
            'last_delivered_message_id': delivered_result['last_delivered_message_id'],
            'delivered_until': delivered_result['delivered_until'],
            'updated_messages': delivered_result['updated_messages'],
            'unread_count': delivered_result['unread_count'],
        }
        broadcast_room_event(
            room_id,
            'message.delivered',
            event_payload,
        )
        broadcast_participant_event(
            delivered_result['room']['participants'],
            'message.delivered',
            event_payload,
        )

    return JsonResponse(
        {
            'status': delivered_result['status'],
            'service': 'messenger',
            'user': sender,
            'result': delivered_result,
        },
        status=delivered_status,
    )


@csrf_exempt
@require_POST
def release_blocked_messages(request, room_id):
    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    release_result, release_status = release_room_blocked_messages(
        user_id=sender['user_id'],
        room_id=room_id,
    )
    if release_status < 300 and release_result['updated_messages'] > 0:
        event_payload = {
            'room_id': release_result['room_id'],
            'user_id': release_result['user_id'],
            'last_delivered_message_id': release_result['last_delivered_message_id'],
            'delivered_until': release_result['delivered_until'],
            'updated_messages': release_result['updated_messages'],
            'released_messages': release_result['released_messages'],
            'unread_count': release_result['unread_count'],
        }
        broadcast_room_event(
            room_id,
            'message.delivered',
            event_payload,
        )
        broadcast_participant_event(
            release_result['room']['participants'],
            'message.delivered',
            event_payload,
        )

    return JsonResponse(
        {
            'status': release_result['status'],
            'service': 'messenger',
            'user': sender,
            'result': release_result,
        },
        status=release_status,
    )
