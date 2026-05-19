import base64
import binascii
import re
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from django.contrib.auth.hashers import check_password, make_password
from django.core.cache import cache
from django.db import transaction

from ...models import RoomParticipant, UserDeviceDefaultCredential, UserDeviceKey


DEVICE_ID_PATTERN = re.compile(r'^[A-Za-z0-9._:-]{1,120}$')
ACTION_NONCE_PATTERN = re.compile(r'^[A-Za-z0-9._:-]{8,160}$')
LIBSODIUM_PUBLIC_KEY_BYTES = 32
ED25519_PUBLIC_KEY_BYTES = 32
ED25519_SIGNATURE_BYTES = 64
ACTION_SIGNATURE_MAX_AGE_SECONDS = 5 * 60
ACTION_SIGNATURE_VERSION = 'parrot-device-action-v1'
DEFAULT_DEVICE_PASSWORD_MIN_LENGTH = 8
DEFAULT_DEVICE_PASSWORD_ATTEMPT_LIMIT = 5
DEFAULT_DEVICE_PASSWORD_ATTEMPT_WINDOW_SECONDS = 10 * 60


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


def decode_base64_field(value):
    if not value:
        return b''

    try:
        return base64.b64decode(str(value).encode('ascii'), validate=True)
    except (binascii.Error, UnicodeEncodeError):
        return b''


def normalize_device_key_payload(payload):
    if not isinstance(payload, dict):
        return None, {'body': ['Request body must be a JSON object.']}

    device_id = normalize_string(payload.get('device_id'))
    device_name = normalize_string(payload.get('device_name'))[:120]
    encryption_public_key = normalize_string(
        payload.get('encryption_public_key') or payload.get('public_key')
    )
    management_public_key = normalize_string(payload.get('management_public_key'))
    errors = {}

    _, device_errors = normalize_device_id(device_id)
    if device_errors:
        errors.update(device_errors)

    if not encryption_public_key:
        errors['encryption_public_key'] = ['Encryption public key is required.']
    elif len(decode_base64_field(encryption_public_key)) != LIBSODIUM_PUBLIC_KEY_BYTES:
        errors['encryption_public_key'] = [
            f'Encryption public key must be base64 for a {LIBSODIUM_PUBLIC_KEY_BYTES}-byte libsodium public key.',
        ]

    if not management_public_key:
        errors['management_public_key'] = ['Management public key is required.']
    elif len(decode_base64_field(management_public_key)) != ED25519_PUBLIC_KEY_BYTES:
        errors['management_public_key'] = [
            f'Management public key must be base64 for a {ED25519_PUBLIC_KEY_BYTES}-byte Ed25519 public key.',
        ]

    if errors:
        return None, errors

    return {
        'device_id': device_id,
        'device_name': device_name,
        'encryption_public_key': encryption_public_key,
        'management_public_key': management_public_key,
    }, None


def normalize_default_device_password(payload):
    password_value = payload.get('default_password')
    password = '' if password_value is None else str(password_value)

    if not password or not password.strip():
        return None, {
            'default_password': ['Default device password is required.'],
        }

    if len(password) < DEFAULT_DEVICE_PASSWORD_MIN_LENGTH:
        return None, {
            'default_password': [
                f'Default device password must be at least {DEFAULT_DEVICE_PASSWORD_MIN_LENGTH} characters.',
            ],
        }

    return password, None


def get_default_device_password_attempt_key(user_id, acting_device_id):
    return f'default-device-password-attempts:{user_id}:{acting_device_id}'


def get_default_device_password_attempts(user_id, acting_device_id):
    return int(cache.get(get_default_device_password_attempt_key(user_id, acting_device_id)) or 0)


def record_default_device_password_failure(user_id, acting_device_id):
    attempt_key = get_default_device_password_attempt_key(user_id, acting_device_id)

    if cache.add(attempt_key, 1, timeout=DEFAULT_DEVICE_PASSWORD_ATTEMPT_WINDOW_SECONDS):
        return 1

    try:
        return int(cache.incr(attempt_key))
    except ValueError:
        cache.set(attempt_key, 1, timeout=DEFAULT_DEVICE_PASSWORD_ATTEMPT_WINDOW_SECONDS)
        return 1


def clear_default_device_password_failures(user_id, acting_device_id):
    cache.delete(get_default_device_password_attempt_key(user_id, acting_device_id))


def register_user_device_key(user_id, payload):
    normalized_payload, errors = normalize_device_key_payload(payload)
    if errors:
        return validation_error(errors)

    with transaction.atomic():
        user_devices = UserDeviceKey.objects.select_for_update().filter(user_id=user_id)
        device_key = user_devices.filter(device_id=normalized_payload['device_id']).first()

        if device_key:
            device_key.public_key = normalized_payload['encryption_public_key']
            device_key.encryption_public_key = normalized_payload['encryption_public_key']
            device_key.management_public_key = normalized_payload['management_public_key']
            device_key.device_name = normalized_payload['device_name']
            device_key.status = UserDeviceKey.STATUS_ACTIVE
            device_key.save(
                update_fields=[
                    'public_key',
                    'encryption_public_key',
                    'management_public_key',
                    'device_name',
                    'status',
                    'last_seen_at',
                ],
            )
        else:
            device_key = UserDeviceKey.objects.create(
                user_id=user_id,
                device_id=normalized_payload['device_id'],
                device_name=normalized_payload['device_name'],
                public_key=normalized_payload['encryption_public_key'],
                encryption_public_key=normalized_payload['encryption_public_key'],
                management_public_key=normalized_payload['management_public_key'],
                is_default=False,
                status=UserDeviceKey.STATUS_ACTIVE,
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

    return list_user_device_keys(
        target_user_id,
        include_default_password_status=int(requesting_user_id) == int(target_user_id),
    )


def list_user_device_keys(target_user_id, include_default_password_status=False):
    device_keys = (
        UserDeviceKey.objects
        .filter(user_id=target_user_id, status=UserDeviceKey.STATUS_ACTIVE)
        .order_by('-last_seen_at', '-id')
    )

    result = {
        'status': 'ok',
        'user_id': target_user_id,
        'devices': [
            serialize_device_key(device_key)
            for device_key in device_keys
        ],
    }

    if include_default_password_status:
        result['default_password_configured'] = UserDeviceDefaultCredential.objects.filter(
            user_id=target_user_id,
        ).exists()

    return result, 200


def build_action_message(user_id, action, acting_device_id, target_device_id, timestamp, nonce):
    return '\n'.join(
        [
            ACTION_SIGNATURE_VERSION,
            str(action),
            str(user_id),
            str(acting_device_id),
            str(target_device_id or ''),
            str(timestamp),
            str(nonce),
        ],
    ).encode('utf-8')


def verify_device_action_signature(user_id, acting_device, payload, action, target_device_id):
    if not isinstance(payload, dict):
        return validation_error({'body': ['Request body must be a JSON object.']})

    if not acting_device or acting_device.status != UserDeviceKey.STATUS_ACTIVE:
        return {
            'status': 'error',
            'message': 'Acting device is not linked to this account.',
        }, 403

    if not acting_device.management_public_key:
        return {
            'status': 'error',
            'message': 'This device must be re-linked before it can manage devices.',
        }, 403

    timestamp = normalize_string(payload.get('action_timestamp'))
    nonce = normalize_string(payload.get('action_nonce'))
    signature = normalize_string(payload.get('action_signature'))
    errors = {}

    try:
        timestamp_number = int(timestamp)
    except (TypeError, ValueError):
        timestamp_number = 0

    now = int(time.time())
    if abs(now - timestamp_number) > ACTION_SIGNATURE_MAX_AGE_SECONDS:
        errors['action_timestamp'] = ['Device action signature expired.']

    if not ACTION_NONCE_PATTERN.fullmatch(nonce):
        errors['action_nonce'] = [
            'Action nonce must be 8-160 characters and use only letters, numbers, dot, underscore, colon, or hyphen.',
        ]

    signature_bytes = decode_base64_field(signature)
    if len(signature_bytes) != ED25519_SIGNATURE_BYTES:
        errors['action_signature'] = [
            f'Action signature must be base64 for a {ED25519_SIGNATURE_BYTES}-byte Ed25519 signature.',
        ]

    public_key_bytes = decode_base64_field(acting_device.management_public_key)
    if len(public_key_bytes) != ED25519_PUBLIC_KEY_BYTES:
        errors['management_public_key'] = ['Linked device management key is invalid.']

    if errors:
        return validation_error(errors)

    message = build_action_message(
        user_id,
        action,
        acting_device.device_id,
        target_device_id,
        timestamp_number,
        nonce,
    )

    try:
        Ed25519PublicKey.from_public_bytes(public_key_bytes).verify(
            signature_bytes,
            message,
        )
    except (InvalidSignature, ValueError):
        return {
            'status': 'error',
            'message': 'Device action signature is invalid.',
        }, 403

    nonce_cache_key = f'device-action-nonce:{user_id}:{acting_device.device_id}:{nonce}'
    if not cache.add(nonce_cache_key, '1', timeout=ACTION_SIGNATURE_MAX_AGE_SECONDS):
        return {
            'status': 'error',
            'message': 'Device action signature was already used.',
        }, 403

    return {
        'status': 'ok',
        'verified': True,
        'acting_device': serialize_device_key(acting_device),
    }, 200


def require_default_device_signature(user_id, payload, action, target_device_id):
    if not isinstance(payload, dict):
        return validation_error({'body': ['Request body must be a JSON object.']})

    normalized_acting_device_id, acting_errors = normalize_device_id(
        payload.get('acting_device_id'),
        'acting_device_id',
    )
    if acting_errors:
        return validation_error(acting_errors)

    user_devices = UserDeviceKey.objects.filter(
        user_id=user_id,
        status=UserDeviceKey.STATUS_ACTIVE,
    )
    acting_device = user_devices.filter(device_id=normalized_acting_device_id).first()
    if not acting_device:
        return {
            'status': 'error',
            'message': 'Acting device is not linked to this account.',
        }, 403

    signature_result, response_status = verify_device_action_signature(
        user_id,
        acting_device,
        payload,
        action,
        target_device_id,
    )
    if response_status >= 300:
        return signature_result, response_status

    if not acting_device.is_default:
        return {
            'status': 'error',
            'message': 'Only the default device can manage the recovery key.',
        }, 403

    return signature_result, 200


def set_default_user_device_key(user_id, device_id, payload):
    if not isinstance(payload, dict):
        return validation_error({'body': ['Request body must be a JSON object.']})

    normalized_device_id, device_errors = normalize_device_id(device_id)
    normalized_acting_device_id, acting_errors = normalize_device_id(
        payload.get('acting_device_id'),
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
        user_devices = UserDeviceKey.objects.select_for_update().filter(
            user_id=user_id,
            status=UserDeviceKey.STATUS_ACTIVE,
        )
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

        signature_result, response_status = verify_device_action_signature(
            user_id,
            acting_device,
            payload,
            'device.default',
            normalized_device_id,
        )
        if response_status >= 300:
            return signature_result, response_status

        default_device = user_devices.filter(is_default=True).first()
        default_password, password_errors = normalize_default_device_password(payload)
        if password_errors:
            return validation_error(password_errors)

        default_credential = (
            UserDeviceDefaultCredential.objects
            .select_for_update()
            .filter(user_id=user_id)
            .first()
        )
        if default_credential:
            if (
                get_default_device_password_attempts(user_id, normalized_acting_device_id)
                >= DEFAULT_DEVICE_PASSWORD_ATTEMPT_LIMIT
            ):
                return {
                    'status': 'error',
                    'message': 'Too many default device password attempts. Try again later.',
                }, 429

            if not check_password(default_password, default_credential.password_hash):
                failed_attempts = record_default_device_password_failure(
                    user_id,
                    normalized_acting_device_id,
                )
                if failed_attempts >= DEFAULT_DEVICE_PASSWORD_ATTEMPT_LIMIT:
                    return {
                        'status': 'error',
                        'message': 'Too many default device password attempts. Try again later.',
                    }, 429

                return {
                    'status': 'error',
                    'message': 'Default device password is incorrect.',
                }, 403

            clear_default_device_password_failures(user_id, normalized_acting_device_id)
        else:
            if default_device and default_device.device_id != normalized_acting_device_id:
                return {
                    'status': 'error',
                    'message': 'Only the current default device can create the default device password.',
                }, 403

            if not default_device and normalized_device_id != normalized_acting_device_id:
                return {
                    'status': 'error',
                    'message': 'Only this linked device can become default when no default device exists.',
                }, 403

            UserDeviceDefaultCredential.objects.create(
                user_id=user_id,
                password_hash=make_password(default_password),
            )

        if (
            default_device
            and normalized_device_id != normalized_acting_device_id
            and default_device.device_id != normalized_acting_device_id
        ):
            return {
                'status': 'error',
                'message': 'Only the current default device can make another device default.',
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
        'default_password_configured': True,
    }, 200


def revoke_user_device_key(user_id, device_id, payload):
    if not isinstance(payload, dict):
        return validation_error({'body': ['Request body must be a JSON object.']})

    normalized_device_id, device_errors = normalize_device_id(device_id)
    normalized_acting_device_id, acting_errors = normalize_device_id(
        payload.get('acting_device_id'),
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
        user_devices = UserDeviceKey.objects.select_for_update().filter(
            user_id=user_id,
            status=UserDeviceKey.STATUS_ACTIVE,
        )
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

        signature_result, response_status = verify_device_action_signature(
            user_id,
            acting_device,
            payload,
            'device.revoke',
            normalized_device_id,
        )
        if response_status >= 300:
            return signature_result, response_status

        is_self_revoke = normalized_device_id == normalized_acting_device_id
        if not is_self_revoke and not acting_device.is_default:
            return {
                'status': 'error',
                'message': 'Only the default device can revoke linked devices.',
            }, 403

        if is_self_revoke and target_device.is_default:
            return {
                'status': 'ok',
                'revoked': False,
                'deleted': False,
                'retained_default': True,
                'local_device_should_clear': False,
                'device_id': normalized_device_id,
                'device': serialize_device_key(target_device),
            }, 200

        target_device.delete()

    return {
        'status': 'ok',
        'revoked': True,
        'deleted': True,
        'retained_default': False,
        'local_device_should_clear': is_self_revoke,
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
    encryption_public_key = device_key.encryption_public_key or device_key.public_key

    return {
        'user_id': device_key.user_id,
        'device_id': device_key.device_id,
        'device_name': device_key.device_name,
        'public_key': encryption_public_key,
        'encryption_public_key': encryption_public_key,
        'management_public_key': device_key.management_public_key,
        'is_default': device_key.is_default,
        'status': device_key.status,
        'created_at': device_key.created_at.isoformat(),
        'last_seen_at': device_key.last_seen_at.isoformat(),
    }
