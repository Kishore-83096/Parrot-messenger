import base64
import binascii
import re

from ..models import RoomParticipant, UserDeviceKey


DEVICE_ID_PATTERN = re.compile(r'^[A-Za-z0-9._:-]{1,120}$')
LIBSODIUM_PUBLIC_KEY_BYTES = 32


def validation_error(errors):
    return {
        'status': 'error',
        'errors': errors,
    }, 400


def normalize_string(value):
    if value is None:
        return ''

    return str(value).strip()


def normalize_device_key_payload(payload):
    if not isinstance(payload, dict):
        return None, {'body': ['Request body must be a JSON object.']}

    device_id = normalize_string(payload.get('device_id'))
    public_key = normalize_string(payload.get('public_key'))
    errors = {}

    if not DEVICE_ID_PATTERN.fullmatch(device_id):
        errors['device_id'] = [
            'Device id must be 1-120 characters and use only letters, numbers, dot, underscore, colon, or hyphen.',
        ]

    if not public_key:
        errors['public_key'] = ['Public key is required.']
    else:
        try:
            decoded_public_key = base64.b64decode(public_key.encode('ascii'), validate=True)
        except (binascii.Error, UnicodeEncodeError):
            decoded_public_key = b''

        if len(decoded_public_key) != LIBSODIUM_PUBLIC_KEY_BYTES:
            errors['public_key'] = [
                f'Public key must be base64 for a {LIBSODIUM_PUBLIC_KEY_BYTES}-byte libsodium public key.',
            ]

    if errors:
        return None, errors

    return {
        'device_id': device_id,
        'public_key': public_key,
    }, None


def register_user_device_key(user_id, payload):
    normalized_payload, errors = normalize_device_key_payload(payload)
    if errors:
        return validation_error(errors)

    device_key, _ = UserDeviceKey.objects.update_or_create(
        user_id=user_id,
        device_id=normalized_payload['device_id'],
        defaults={
            'public_key': normalized_payload['public_key'],
        },
    )

    return {
        'status': 'ok',
        'device': serialize_device_key(device_key),
    }, 200


def list_accessible_user_device_keys(requesting_user_id, target_user_id):
    if not can_access_user_device_keys(requesting_user_id, target_user_id):
        return {
            'status': 'error',
            'message': 'Device keys not found.',
        }, 404

    return list_user_device_keys(target_user_id)


def list_user_device_keys(target_user_id):
    device_keys = UserDeviceKey.objects.filter(user_id=target_user_id).order_by('-last_seen_at', '-id')

    return {
        'status': 'ok',
        'user_id': target_user_id,
        'devices': [
            serialize_device_key(device_key)
            for device_key in device_keys
        ],
    }, 200


def can_access_user_device_keys(requesting_user_id, target_user_id):
    if int(requesting_user_id) == int(target_user_id):
        return True

    return RoomParticipant.objects.filter(
        user_id=requesting_user_id,
        is_active=True,
        room__participants__user_id=target_user_id,
        room__participants__is_active=True,
    ).exists()


def serialize_device_key(device_key):
    return {
        'user_id': device_key.user_id,
        'device_id': device_key.device_id,
        'public_key': device_key.public_key,
        'created_at': device_key.created_at.isoformat(),
        'last_seen_at': device_key.last_seen_at.isoformat(),
    }
