import json

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from messaging.views import get_authenticated_sender, parse_json_body

from .services import (
    add_group_members,
    create_group,
    delete_group,
    get_group_room,
    leave_group,
    remove_group_member,
    set_group_sub_admin,
    transfer_group_admin,
    update_group,
    upload_group_avatar,
)


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
