import json
from hmac import compare_digest

from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .auth import validate_messaging_token
from .cache import (
    delete_cached_messaging_authorization,
    delete_cached_receipt_visibility,
    get_cached_messaging_authorization,
    set_cached_messaging_authorization,
    set_cached_receipt_visibility,
)
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
from .e2ee.files import (
    complete_encrypted_file_upload_intent,
    consume_completed_encrypted_upload_intents,
    create_encrypted_file_upload_intents,
    upload_encrypted_file,
    validate_completed_encrypted_upload_intents,
)
from .realtime import broadcast_participant_event, broadcast_room_event, broadcast_user_event
from .signals import (
    authorize_parent_messaging,
    check_database,
    check_parent_services,
    check_redis,
    resolve_parent_presence_visibility,
)
from .services import (
    cleanup_uploaded_attachments,
    create_direct_message,
    delete_direct_message_for_everyone,
    edit_direct_message,
    get_direct_message_action_client_message_id,
    get_direct_message_action_recipient_account_number,
    get_existing_direct_room_authorization,
    has_existing_sender_client_message,
    list_room_messages,
    list_user_rooms,
    mark_room_delivered,
    mark_room_read,
    normalize_message_list_params,
    react_to_message,
    release_room_blocked_messages,
    upload_message_files,
)


def is_internal_service_request(request):
    expected_token = getattr(settings, 'INTERNAL_SERVICE_TOKEN', '') or ''
    provided_token = request.headers.get('X-Internal-Service-Token', '')

    return bool(expected_token) and compare_digest(provided_token, expected_token)


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


@csrf_exempt
@require_POST
def hide_presence_from_user(request):
    return update_presence_visibility_for_user(request, force_visible=False)


@csrf_exempt
@require_POST
def update_presence_visibility_for_user(request, force_visible=None):
    if not is_internal_service_request(request):
        return JsonResponse(
            {
                'status': 'error',
                'service': 'messenger',
                'message': 'Unauthorized internal service request.',
            },
            status=401,
        )

    payload, error_response = parse_json_body(request)
    if error_response:
        return error_response

    try:
        owner_user_id = int(payload.get('owner_user_id') or 0)
        viewer_user_id = int(payload.get('viewer_user_id') or 0)
    except (TypeError, ValueError):
        owner_user_id = 0
        viewer_user_id = 0

    if owner_user_id <= 0 or viewer_user_id <= 0 or owner_user_id == viewer_user_id:
        return JsonResponse(
            {
                'status': 'error',
                'service': 'messenger',
                'message': 'Valid owner_user_id and viewer_user_id are required.',
            },
            status=400,
        )

    visible = bool(payload.get('visible'))
    if force_visible is not None:
        visible = bool(force_visible)

    result = broadcast_presence_visibility_to_viewer(
        owner_user_id=owner_user_id,
        owner_account_number=payload.get('owner_account_number') or '',
        viewer_user_id=viewer_user_id,
        visible=visible,
    )

    return JsonResponse(
        {
            'status': 'ok',
            'service': 'messenger',
            **result,
        },
        status=200,
    )


@csrf_exempt
@require_POST
def refresh_presence_visibility_for_user(request):
    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    payload, error_response = parse_json_body(request)
    if error_response:
        return error_response

    try:
        viewer_user_id = int(payload.get('viewer_user_id') or 0)
    except (TypeError, ValueError):
        viewer_user_id = 0

    if viewer_user_id <= 0 or viewer_user_id == int(sender['user_id']):
        return JsonResponse(
            {
                'status': 'error',
                'service': 'messenger',
                'message': 'Valid viewer_user_id is required.',
            },
            status=400,
        )

    policy_result, policy_status = resolve_parent_presence_visibility(
        {
            'owner_user_id': sender['user_id'],
            'candidate_user_ids': [viewer_user_id],
        }
    )
    parent_policy = policy_result.get('parent', {}).get('response')
    if policy_status >= 500:
        return JsonResponse(
            {
                'status': 'error',
                'service': 'messenger',
                'message': 'Unable to verify presence visibility with Parent service.',
                'policy': policy_result,
            },
            status=policy_status,
        )

    if not isinstance(parent_policy, dict) or parent_policy.get('allowed') is not True:
        return JsonResponse(
            {
                'status': 'denied',
                'service': 'messenger',
                'message': 'Presence visibility was not authorized.',
                'policy': policy_result,
            },
            status=policy_status if policy_status >= 400 else 403,
        )

    visible_user_ids = {
        int(user_id)
        for user_id in parent_policy.get('visible_user_ids') or []
        if user_id
    }
    visible = viewer_user_id in visible_user_ids
    result = broadcast_presence_visibility_to_viewer(
        owner_user_id=sender['user_id'],
        owner_account_number=sender.get('account_number') or '',
        viewer_user_id=viewer_user_id,
        visible=visible,
    )

    return JsonResponse(
        {
            'status': 'ok',
            'service': 'messenger',
            **result,
        },
        status=200,
    )


def broadcast_presence_visibility_to_viewer(
    owner_user_id,
    owner_account_number,
    viewer_user_id,
    visible,
):
    owner_online = is_presence_user_online(owner_user_id)
    delivered = False
    event_type = 'presence.online' if visible else 'presence.offline'
    if not visible or owner_online:
        delivered = broadcast_user_event(
            viewer_user_id,
            event_type,
            {
                'user_id': owner_user_id,
                'account_number': owner_account_number or '',
                'expires_in': (
                    getattr(settings, 'MESSAGING_PRESENCE_TTL_SECONDS', 60)
                    if visible
                    else 0
                ),
            },
        )

    return {
        'event_type': event_type if (not visible or owner_online) else None,
        'owner_online': owner_online,
        'visible': visible,
        'delivered': bool(delivered),
    }


def is_presence_user_online(user_id):
    connection_ids = cache.get(f'messaging:presence:user:{int(user_id)}:connections') or []
    active_connection_ids = [
        connection_id
        for connection_id in connection_ids
        if cache.get(
            f'messaging:presence:user:{int(user_id)}:connection:{connection_id}'
        ) is not None
    ]
    if active_connection_ids != connection_ids:
        cache.set(
            f'messaging:presence:user:{int(user_id)}:connections',
            active_connection_ids,
            timeout=getattr(settings, 'MESSAGING_PRESENCE_TTL_SECONDS', 60) * 2,
        )

    return bool(active_connection_ids)


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
            if key not in {'delivery_blocked', 'block_context', 'ghost_context'}
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


def should_cache_messaging_authorization(authorization_result, response_status):
    if response_status >= 500:
        return False

    parent_response = authorization_result.get('parent', {}).get('response')

    return isinstance(parent_response, dict) and 'allowed' in parent_response


def build_cached_parent_authorization_result(parent_response, response_status):
    return {
        'ok': response_status < 400,
        'parent': {
            'name': 'cached',
            'ok': response_status < 400,
            'status_code': response_status,
            'cached': True,
            'response': parent_response,
        },
    }


def authorize_sender_for_recipient(sender, recipient_account_number):
    cached_authorization = get_cached_messaging_authorization(
        sender['user_id'],
        recipient_account_number,
    )
    if cached_authorization:
        authorization_result, response_status = cached_authorization
    else:
        parent_payload = {
            'sender_user_id': sender['user_id'],
            'recipient_account_number': recipient_account_number,
        }
        authorization_result, response_status = authorize_parent_messaging(parent_payload)
        if should_cache_messaging_authorization(authorization_result, response_status):
            set_cached_messaging_authorization(
                sender['user_id'],
                recipient_account_number,
                authorization_result,
                response_status,
            )

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
@require_POST
def create_crypto_file_upload_intents(request):
    payload, error_response = parse_json_body(request)
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

    result, response_status = create_encrypted_file_upload_intents(
        sender,
        parent_authorization,
        payload,
    )

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
def complete_crypto_file_upload_intent(request, upload_intent_id):
    payload, error_response = parse_json_body(request)
    if error_response:
        return error_response

    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    result, response_status = complete_encrypted_file_upload_intent(
        sender,
        upload_intent_id,
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
def update_message_authorization_cache(request):
    if not is_internal_service_request(request):
        return JsonResponse(
            {
                'status': 'error',
                'service': 'messenger',
                'message': 'Unauthorized internal service request.',
            },
            status=401,
        )

    payload, error_response = parse_json_body(request)
    if error_response:
        return error_response

    authorizations = payload.get('authorizations')
    if not isinstance(authorizations, list):
        return JsonResponse(
            {
                'status': 'error',
                'service': 'messenger',
                'errors': {
                    'authorizations': ['Authorizations must be a list.'],
                },
            },
            status=400,
        )

    updated = 0
    invalidated = 0
    skipped = 0

    for authorization in authorizations:
        if not isinstance(authorization, dict):
            skipped += 1
            continue

        sender_user_id = authorization.get('sender_user_id')
        recipient_account_number = authorization.get('recipient_account_number')

        if authorization.get('invalidate') is True:
            if delete_cached_messaging_authorization(sender_user_id, recipient_account_number):
                invalidated += 1
            else:
                skipped += 1
            continue

        parent_response = authorization.get('response')
        if not isinstance(parent_response, dict):
            skipped += 1
            continue

        try:
            response_status = int(authorization.get('status_code') or 200)
        except (TypeError, ValueError):
            skipped += 1
            continue

        authorization_result = build_cached_parent_authorization_result(
            parent_response,
            response_status,
        )
        if set_cached_messaging_authorization(
            sender_user_id,
            recipient_account_number,
            authorization_result,
            response_status,
        ):
            updated += 1
        else:
            skipped += 1

    return JsonResponse(
        {
            'status': 'updated',
            'service': 'messenger',
            'updated': updated,
            'invalidated': invalidated,
            'skipped': skipped,
        }
    )


@csrf_exempt
@require_POST
def update_receipt_visibility_cache(request):
    if not is_internal_service_request(request):
        return JsonResponse(
            {
                'status': 'error',
                'service': 'messenger',
                'message': 'Unauthorized internal service request.',
            },
            status=401,
        )

    payload, error_response = parse_json_body(request)
    if error_response:
        return error_response

    policies = payload.get('policies')
    if not isinstance(policies, list):
        return JsonResponse(
            {
                'status': 'error',
                'service': 'messenger',
                'errors': {
                    'policies': ['Policies must be a list.'],
                },
            },
            status=400,
        )

    updated = 0
    invalidated = 0
    skipped = 0

    for policy in policies:
        if not isinstance(policy, dict):
            skipped += 1
            continue

        owner_user_id = policy.get('owner_user_id')
        candidate_user_id = policy.get('candidate_user_id')

        if policy.get('invalidate') is True:
            if delete_cached_receipt_visibility(owner_user_id, candidate_user_id):
                invalidated += 1
            else:
                skipped += 1
            continue

        if 'hidden' not in policy:
            skipped += 1
            continue

        if set_cached_receipt_visibility(
            owner_user_id,
            candidate_user_id,
            bool(policy.get('hidden')),
        ):
            updated += 1
        else:
            skipped += 1

    return JsonResponse(
        {
            'status': 'updated',
            'service': 'messenger',
            'updated': updated,
            'invalidated': invalidated,
            'skipped': skipped,
        }
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

    encrypted_upload_intents = []
    if not has_existing_sender_client_message(
        sender['user_id'],
        payload.get('client_message_id'),
    ):
        encrypted_upload_intents, upload_intent_errors = validate_completed_encrypted_upload_intents(
            sender,
            parent_authorization,
            payload,
        )
        if upload_intent_errors:
            cleanup_uploaded_attachments(uploaded_attachments)
            return JsonResponse(
                {
                    'status': 'error',
                    'service': 'messenger',
                    'sender': sender,
                    'authorization': sanitize_authorization_result(authorization_result),
                    'result': {
                        'status': 'error',
                        'errors': {
                            'encrypted_upload_intent_ids': upload_intent_errors,
                        },
                    },
                },
                status=400,
            )

    message_result, message_status = create_direct_message(sender, parent_authorization, payload)
    if message_status >= 300:
        cleanup_uploaded_attachments(uploaded_attachments)
    elif message_result.get('status') == 'sent':
        consume_completed_encrypted_upload_intents(encrypted_upload_intents)

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


@csrf_exempt
@require_POST
def edit_message(request, message_id):
    payload, error_response = parse_json_body(request)
    if error_response:
        return error_response

    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    recipient_account_number = get_direct_message_action_recipient_account_number(
        sender,
        message_id,
    )
    if not recipient_account_number:
        return JsonResponse(
            {
                'status': 'error',
                'service': 'messenger',
                'sender': sender,
                'result': {
                    'status': 'error',
                    'message': 'Message not found.',
                },
            },
            status=404,
        )

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

    encrypted_upload_intents = []
    if isinstance(payload, dict) and payload.get('encrypted_upload_intent_ids'):
        client_message_id = get_direct_message_action_client_message_id(sender, message_id)
        payload = {
            **payload,
            'client_message_id': client_message_id,
        }
        encrypted_upload_intents, upload_intent_errors = validate_completed_encrypted_upload_intents(
            sender,
            parent_authorization,
            payload,
        )
        if upload_intent_errors:
            return JsonResponse(
                {
                    'status': 'error',
                    'service': 'messenger',
                    'sender': sender,
                    'authorization': sanitize_authorization_result(authorization_result),
                    'result': {
                        'status': 'error',
                        'errors': {
                            'encrypted_upload_intent_ids': upload_intent_errors,
                        },
                    },
                },
                status=400,
            )

    result, response_status = edit_direct_message(
        sender,
        message_id,
        payload,
        parent_authorization,
        replacement_upload_intents=encrypted_upload_intents,
    )

    delivery_blocked = bool(result.get('_delivery_blocked'))
    if response_status < 300 and result.get('status') == 'edited':
        consume_completed_encrypted_upload_intents(encrypted_upload_intents)
        event_payload = {
            'room': result['room'],
            'message': result['message'],
            'sender': sender,
        }
        if not delivery_blocked:
            broadcast_room_event(
                result['message']['room_id'],
                'message.edited',
                event_payload,
            )
            event_participants = result['room']['participants']
        else:
            event_participants = get_sender_participants(result['room'], sender)

        broadcast_participant_event(
            event_participants,
            'message.edited',
            event_payload,
        )

    return JsonResponse(
        {
            'status': result.get('status', 'error'),
            'service': 'messenger',
            'sender': sender,
            'result': build_public_message_result(result),
        },
        status=response_status,
    )


@csrf_exempt
@require_POST
def delete_message(request, message_id):
    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    result, response_status = delete_direct_message_for_everyone(sender, message_id)
    if response_status < 300 and result.get('status') == 'deleted':
        event_payload = {
            'room': result['room'],
            'message': result['message'],
            'sender': sender,
        }
        broadcast_room_event(
            result['message']['room_id'],
            'message.deleted',
            event_payload,
        )
        broadcast_participant_event(
            result['room']['participants'],
            'message.deleted',
            event_payload,
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
def message_reaction(request, message_id):
    payload, error_response = parse_json_body(request)
    if error_response:
        return error_response

    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    reaction_result, reaction_status = react_to_message(
        user_id=sender['user_id'],
        message_id=message_id,
        payload=payload,
    )
    if reaction_status < 300:
        event_payload = {
            'room_id': reaction_result['room_id'],
            'message_id': reaction_result['message_id'],
            'user_id': reaction_result['user_id'],
            'reaction': reaction_result['reaction'],
            'previous_reaction': reaction_result['previous_reaction'],
            'action': reaction_result['action'],
            'reactions': reaction_result['reactions'],
        }
        broadcast_room_event(
            reaction_result['room_id'],
            'message.reaction_updated',
            event_payload,
        )
        broadcast_participant_event(
            reaction_result['room']['participants'],
            'message.reaction_updated',
            event_payload,
        )

    return JsonResponse(
        {
            'status': reaction_result['status'],
            'service': 'messenger',
            'user': sender,
            'result': reaction_result,
        },
        status=reaction_status,
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
    read_message_statuses = read_result.get('message_statuses', [])
    if read_status < 300 and read_message_statuses:
        last_visible_read_message_id = max(
            item['message_id']
            for item in read_message_statuses
            if item.get('message_id')
        )
        event_payload = {
            'room_id': read_result['room_id'],
            'user_id': read_result['user_id'],
            'last_read_message_id': last_visible_read_message_id,
            'last_read_at': read_result['last_read_at'],
            'read_marker_moved': read_result['read_marker_moved'],
            'updated_messages': read_result['updated_messages'],
            'unread_count': read_result['unread_count'],
            'message_statuses': read_message_statuses,
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
    delivered_message_statuses = delivered_result.get('message_statuses', [])
    if delivered_status < 300 and delivered_message_statuses:
        last_visible_delivered_message_id = max(
            item['message_id']
            for item in delivered_message_statuses
            if item.get('message_id')
        )
        event_payload = {
            'room_id': delivered_result['room_id'],
            'user_id': delivered_result['user_id'],
            'last_delivered_message_id': last_visible_delivered_message_id,
            'delivered_until': delivered_result['delivered_until'],
            'updated_messages': delivered_result['updated_messages'],
            'unread_count': delivered_result['unread_count'],
            'message_statuses': delivered_message_statuses,
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
