import base64
import binascii
import re

from django.db import transaction

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


def normalize_device_id(value, field_name='device_id'):
    normalized_device_id = normalize_string(value)
    if DEVICE_ID_PATTERN.fullmatch(normalized_device_id):
        return normalized_device_id, None

    return None, {
        field_name: [
            'Device id must be 1-120 characters and use only letters, numbers, dot, underscore, colon, or hyphen.',
        ],
    }


def normalize_device_key_payload(payload):
    if not isinstance(payload, dict):
        return None, {'body': ['Request body must be a JSON object.']}

    device_id = normalize_string(payload.get('device_id'))
    device_name = normalize_string(payload.get('device_name'))[:120]
    public_key = normalize_string(payload.get('public_key'))
    errors = {}

    _, device_errors = normalize_device_id(device_id)
    if device_errors:
        errors.update(device_errors)

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
        'device_name': device_name,
        'public_key': public_key,
    }, None


def register_user_device_key(user_id, payload):
    normalized_payload, errors = normalize_device_key_payload(payload)
    if errors:
        return validation_error(errors)

    with transaction.atomic():
        user_devices = UserDeviceKey.objects.select_for_update().filter(user_id=user_id)
        device_key = user_devices.filter(device_id=normalized_payload['device_id']).first()

        if device_key:
            device_key.public_key = normalized_payload['public_key']
            device_key.device_name = normalized_payload['device_name']
            device_key.save(update_fields=['public_key', 'device_name', 'is_default', 'last_seen_at'])
        else:
            device_key = UserDeviceKey.objects.create(
                user_id=user_id,
                device_id=normalized_payload['device_id'],
                device_name=normalized_payload['device_name'],
                public_key=normalized_payload['public_key'],
                is_default=False,
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


def set_default_user_device_key(user_id, device_id, acting_device_id):
    normalized_device_id, device_errors = normalize_device_id(device_id)
    normalized_acting_device_id, acting_errors = normalize_device_id(
        acting_device_id,
        'acting_device_id',
    )
    errors = {}
    if device_errors:
        errors.update(device_errors)
    if acting_errors:
        errors.update(acting_errors)
    if errors:
        return validation_error(errors)

    with transaction.atomic():
        user_devices = UserDeviceKey.objects.select_for_update().filter(user_id=user_id)
        acting_device = user_devices.filter(device_id=normalized_acting_device_id).first()
        target_device = user_devices.filter(device_id=normalized_device_id).first()

        if not target_device:
            return {
                'status': 'error',
                'message': 'Device key not found.',
            }, 404

        if not acting_device:
            return {
                'status': 'error',
                'message': 'Acting device is not linked to this account.',
            }, 403

        default_device = user_devices.filter(is_default=True).first()
        if default_device and default_device.device_id != normalized_acting_device_id:
            return {
                'status': 'error',
                'message': 'Only the default device can change the default linked device.',
            }, 403

        if not default_device and normalized_device_id != normalized_acting_device_id:
            return {
                'status': 'error',
                'message': 'Only this linked device can become default when no default device exists.',
            }, 403

        user_devices.update(is_default=False)
        UserDeviceKey.objects.filter(pk=target_device.pk).update(is_default=True)
        target_device.refresh_from_db()

    return {
        'status': 'ok',
        'device': serialize_device_key(target_device),
    }, 200


def revoke_user_device_key(user_id, device_id, acting_device_id):
    normalized_device_id, device_errors = normalize_device_id(device_id)
    normalized_acting_device_id, acting_errors = normalize_device_id(
        acting_device_id,
        'acting_device_id',
    )
    errors = {}
    if device_errors:
        errors.update(device_errors)
    if acting_errors:
        errors.update(acting_errors)
    if errors:
        return validation_error(errors)

    with transaction.atomic():
        user_devices = UserDeviceKey.objects.select_for_update().filter(user_id=user_id)
        acting_device = user_devices.filter(device_id=normalized_acting_device_id).first()
        target_device = user_devices.filter(device_id=normalized_device_id).first()

        if not acting_device:
            return {
                'status': 'error',
                'message': 'Acting device is not linked to this account.',
            }, 403

        if not target_device:
            return {
                'status': 'error',
                'message': 'Device key not found.',
            }, 404

        is_self_revoke = normalized_device_id == normalized_acting_device_id
        if not is_self_revoke and not acting_device.is_default:
            return {
                'status': 'error',
                'message': 'Only the default device can revoke linked devices.',
            }, 403

        target_device.delete()

    return {
        'status': 'ok',
        'revoked': True,
        'device_id': normalized_device_id,
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
        'device_name': device_key.device_name,
        'public_key': device_key.public_key,
        'is_default': device_key.is_default,
        'created_at': device_key.created_at.isoformat(),
        'last_seen_at': device_key.last_seen_at.isoformat(),
    }
