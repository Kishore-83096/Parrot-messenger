import json

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from messaging.auth import validate_messaging_token
from messaging.realtime import (
    broadcast_participant_event,
    broadcast_room_event,
    broadcast_user_event,
)

from .models import StoryAudience
from .policy import resolve_parent_story_audience
from .services import (
    complete_story_media_upload_intent,
    create_story_from_upload_intents,
    create_story_media_upload_intents,
    delete_story,
    list_my_stories,
    list_story_feed,
    list_story_viewers,
    mark_story_viewed,
    normalize_create_story_payload,
    react_to_story,
    reply_to_story,
)


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


def get_policy_status(result, response_status):
    parent_response = result.get('parent', {}).get('response')

    if isinstance(parent_response, dict) and parent_response.get('allowed') is True:
        return 'allowed'

    if isinstance(parent_response, dict) and parent_response.get('allowed') is False:
        return 'denied'

    if response_status >= 500:
        return 'degraded'

    return 'error'


def sanitize_policy_result(result):
    if isinstance(result, dict):
        return {
            key: sanitize_policy_result(value)
            for key, value in result.items()
            if key != 'block_context'
        }

    if isinstance(result, list):
        return [sanitize_policy_result(value) for value in result]

    return result


@csrf_exempt
@require_POST
def create_story(request):
    payload, error_response = parse_json_body(request)
    if error_response:
        return error_response

    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    normalized_payload, errors = normalize_create_story_payload(payload)
    if errors:
        return JsonResponse(
            {
                'status': 'error',
                'service': 'messenger',
                'sender': sender,
                'result': {
                    'status': 'error',
                    'errors': errors,
                },
            },
            status=400,
        )

    policy_payload = {
        'owner_user_id': sender['user_id'],
        'audience_account_numbers': normalized_payload['audience_account_numbers'] or None,
    }
    policy_result, policy_status = resolve_parent_story_audience(policy_payload)
    parent_policy = policy_result.get('parent', {}).get('response')
    if not isinstance(parent_policy, dict) or parent_policy.get('allowed') is not True:
        return JsonResponse(
            {
                'status': get_policy_status(policy_result, policy_status),
                'service': 'messenger',
                'sender': sender,
                'policy': sanitize_policy_result(policy_result),
            },
            status=policy_status,
        )

    result, response_status = create_story_from_upload_intents(
        sender,
        parent_policy,
        normalized_payload,
    )
    if response_status == 201:
        broadcast_story_created(result, sender)

    return JsonResponse(
        {
            'status': result.get('status', 'error'),
            'service': 'messenger',
            'sender': sender,
            'policy': sanitize_policy_result(parent_policy),
            'result': result,
        },
        status=response_status,
    )


@require_GET
def story_feed(request):
    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    result, response_status = list_story_feed(sender)

    return JsonResponse(
        {
            'status': result.get('status', 'error'),
            'service': 'messenger',
            'user': sender,
            'result': result,
        },
        status=response_status,
    )


@require_GET
def my_stories(request):
    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    result, response_status = list_my_stories(sender)

    return JsonResponse(
        {
            'status': result.get('status', 'error'),
            'service': 'messenger',
            'user': sender,
            'result': result,
        },
        status=response_status,
    )


@csrf_exempt
@require_http_methods(['DELETE'])
def story_delete(request, story_id):
    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    result, response_status = delete_story(sender, story_id)
    if response_status == 200 and result.get('deleted'):
        broadcast_story_deleted(result, sender)

    return JsonResponse(
        {
            'status': result.get('status', 'error'),
            'service': 'messenger',
            'user': sender,
            'result': result,
        },
        status=response_status,
    )


@csrf_exempt
@require_POST
def story_view(request, story_id):
    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    result, response_status = mark_story_viewed(sender, story_id)
    if response_status == 201:
        broadcast_story_viewed(result, sender)

    return JsonResponse(
        {
            'status': result.get('status', 'error'),
            'service': 'messenger',
            'user': sender,
            'result': sanitize_policy_result(result),
        },
        status=response_status,
    )


@require_GET
def story_viewers(request, story_id):
    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    result, response_status = list_story_viewers(sender, story_id)

    return JsonResponse(
        {
            'status': result.get('status', 'error'),
            'service': 'messenger',
            'user': sender,
            'result': result,
        },
        status=response_status,
    )


@csrf_exempt
@require_POST
def story_reaction(request, story_id):
    payload, error_response = parse_json_body(request)
    if error_response:
        return error_response

    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    result, response_status = react_to_story(sender, story_id, payload)
    broadcast_story_chat_message(result, sender)

    return JsonResponse(
        {
            'status': result.get('status', 'error'),
            'service': 'messenger',
            'user': sender,
            'result': sanitize_policy_result(result),
        },
        status=response_status,
    )


@csrf_exempt
@require_POST
def story_reply(request, story_id):
    payload, error_response = parse_json_body(request)
    if error_response:
        return error_response

    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    result, response_status = reply_to_story(sender, story_id, payload)
    broadcast_story_chat_message(result, sender)

    return JsonResponse(
        {
            'status': result.get('status', 'error'),
            'service': 'messenger',
            'user': sender,
            'result': sanitize_policy_result(result),
        },
        status=response_status,
    )


def broadcast_story_created(result, sender):
    if not isinstance(result, dict) or result.get('status') != 'ok':
        return

    story = result.get('story')
    if not isinstance(story, dict) or not story.get('id'):
        return

    audience_user_ids = StoryAudience.objects.filter(
        story_id=story['id'],
    ).values_list('viewer_user_id', flat=True).distinct()
    event_payload = {
        'story': story,
        'owner': sender,
    }

    for user_id in audience_user_ids:
        if int(user_id) == int(sender['user_id']):
            continue

        broadcast_user_event(user_id, 'story.created', event_payload)


def broadcast_story_deleted(result, sender):
    if not isinstance(result, dict) or result.get('status') != 'ok':
        return

    story_id = result.get('story_id')
    if not story_id:
        return

    event_payload = {
        'story_id': story_id,
        'story': result.get('story'),
        'owner': sender,
        'deleted_at': result.get('deleted_at'),
    }

    for user_id in result.get('audience_user_ids') or []:
        if int(user_id) == int(sender['user_id']):
            continue

        broadcast_user_event(user_id, 'story.deleted', event_payload)


def broadcast_story_viewed(result, sender):
    if not isinstance(result, dict) or result.get('status') != 'ok':
        return

    owner_user_id = result.get('owner_user_id')
    if not owner_user_id or int(owner_user_id) == int(sender['user_id']):
        return

    broadcast_user_event(
        owner_user_id,
        'story.viewed',
        {
            'story_id': result.get('story_id'),
            'view_count': result.get('view_count'),
            'viewer': {
                'user_id': sender['user_id'],
                'account_number': (
                    result.get('viewer_account_number')
                    or sender.get('account_number')
                    or ''
                ),
                'viewed_at': result.get('viewed_at'),
            },
        },
    )


def broadcast_story_chat_message(result, sender):
    message_result = result.get('message_result') if isinstance(result, dict) else None
    if not isinstance(message_result, dict) or message_result.get('status') != 'sent':
        return

    event_payload = {
        'room': message_result['room'],
        'message': message_result['message'],
        'sender': sender,
    }
    broadcast_room_event(
        message_result['message']['room_id'],
        'message.sent',
        event_payload,
    )
    broadcast_participant_event(
        message_result['room']['participants'],
        'message.sent',
        event_payload,
    )


@csrf_exempt
@require_POST
def create_story_upload_intents(request):
    payload, error_response = parse_json_body(request)
    if error_response:
        return error_response

    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    policy_payload = {
        'owner_user_id': sender['user_id'],
        'audience_account_numbers': payload.get('audience_account_numbers'),
    }
    policy_result, policy_status = resolve_parent_story_audience(policy_payload)
    parent_policy = policy_result.get('parent', {}).get('response')
    if not isinstance(parent_policy, dict) or parent_policy.get('allowed') is not True:
        return JsonResponse(
            {
                'status': get_policy_status(policy_result, policy_status),
                'service': 'messenger',
                'sender': sender,
                'policy': sanitize_policy_result(policy_result),
            },
            status=policy_status,
        )

    result, response_status = create_story_media_upload_intents(
        sender,
        parent_policy,
        payload,
    )

    return JsonResponse(
        {
            'status': result.get('status', 'error'),
            'service': 'messenger',
            'sender': sender,
            'policy': sanitize_policy_result(parent_policy),
            'result': result,
        },
        status=response_status,
    )


@csrf_exempt
@require_POST
def complete_story_upload_intent(request, upload_intent_id):
    payload, error_response = parse_json_body(request)
    if error_response:
        return error_response

    sender, error_response = get_authenticated_sender(request)
    if error_response:
        return error_response

    result, response_status = complete_story_media_upload_intent(
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
