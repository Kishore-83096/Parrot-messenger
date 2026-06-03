import json

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from messaging.views import get_authenticated_sender, parse_json_body

from .services import create_group, update_group, upload_group_avatar


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
