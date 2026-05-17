import base64
import binascii

from ..models import UserE2EEKeyBackup


LIBSODIUM_PUBLIC_KEY_BYTES = 32
LIBSODIUM_XCHACHA20_NONCE_BYTES = 24
MIN_BACKUP_SALT_BYTES = 16
MIN_ENCRYPTED_PRIVATE_KEY_BYTES = 48
SUPPORTED_KDF_ALGORITHMS = {'PBKDF2-SHA256'}
MIN_KDF_ITERATIONS = 100000
MAX_KDF_ITERATIONS = 2000000


def validation_error(errors):
    return {
        'status': 'error',
        'errors': errors,
    }, 400


def get_user_key_backup(user_id):
    backup = UserE2EEKeyBackup.objects.filter(user_id=user_id).first()

    return {
        'status': 'ok',
        'exists': backup is not None,
        'backup': serialize_key_backup(backup) if backup else None,
    }, 200


def save_user_key_backup(user_id, payload):
    normalized_payload, errors = normalize_key_backup_payload(payload)
    if errors:
        return validation_error(errors)

    backup, _ = UserE2EEKeyBackup.objects.update_or_create(
        user_id=user_id,
        defaults=normalized_payload,
    )

    return {
        'status': 'ok',
        'exists': True,
        'backup': serialize_key_backup(backup),
    }, 200


def normalize_key_backup_payload(payload):
    if not isinstance(payload, dict):
        return None, {'body': ['Request body must be a JSON object.']}

    public_key = normalize_string(payload.get('public_key'))
    encrypted_private_key = normalize_string(payload.get('encrypted_private_key'))
    salt = normalize_string(payload.get('salt'))
    nonce = normalize_string(payload.get('nonce'))
    kdf_algorithm = normalize_string(payload.get('kdf_algorithm')) or 'PBKDF2-SHA256'
    kdf_iterations = payload.get('kdf_iterations')
    errors = {}

    public_key_bytes = decode_base64_field(public_key)
    if len(public_key_bytes) != LIBSODIUM_PUBLIC_KEY_BYTES:
        errors['public_key'] = [
            f'Public key must be base64 for a {LIBSODIUM_PUBLIC_KEY_BYTES}-byte libsodium public key.',
        ]

    encrypted_private_key_bytes = decode_base64_field(encrypted_private_key)
    if len(encrypted_private_key_bytes) < MIN_ENCRYPTED_PRIVATE_KEY_BYTES:
        errors['encrypted_private_key'] = ['Encrypted private key backup is invalid.']

    salt_bytes = decode_base64_field(salt)
    if len(salt_bytes) < MIN_BACKUP_SALT_BYTES:
        errors['salt'] = [f'Salt must be base64 for at least {MIN_BACKUP_SALT_BYTES} bytes.']

    nonce_bytes = decode_base64_field(nonce)
    if len(nonce_bytes) != LIBSODIUM_XCHACHA20_NONCE_BYTES:
        errors['nonce'] = [
            f'Nonce must be base64 for a {LIBSODIUM_XCHACHA20_NONCE_BYTES}-byte XChaCha20 nonce.',
        ]

    if kdf_algorithm not in SUPPORTED_KDF_ALGORITHMS:
        errors['kdf_algorithm'] = ['Unsupported key backup KDF algorithm.']

    try:
        kdf_iterations = int(kdf_iterations)
    except (TypeError, ValueError):
        kdf_iterations = 0

    if not MIN_KDF_ITERATIONS <= kdf_iterations <= MAX_KDF_ITERATIONS:
        errors['kdf_iterations'] = [
            f'KDF iterations must be between {MIN_KDF_ITERATIONS} and {MAX_KDF_ITERATIONS}.',
        ]

    if errors:
        return None, errors

    return {
        'public_key': public_key,
        'encrypted_private_key': encrypted_private_key,
        'salt': salt,
        'nonce': nonce,
        'kdf_algorithm': kdf_algorithm,
        'kdf_iterations': kdf_iterations,
    }, None


def serialize_key_backup(backup):
    if not backup:
        return None

    return {
        'user_id': backup.user_id,
        'public_key': backup.public_key,
        'encrypted_private_key': backup.encrypted_private_key,
        'salt': backup.salt,
        'nonce': backup.nonce,
        'kdf_algorithm': backup.kdf_algorithm,
        'kdf_iterations': backup.kdf_iterations,
        'created_at': backup.created_at.isoformat(),
        'updated_at': backup.updated_at.isoformat(),
    }


def decode_base64_field(value):
    if not value:
        return b''

    try:
        return base64.b64decode(value.encode('ascii'), validate=True)
    except (binascii.Error, UnicodeEncodeError):
        return b''


def normalize_string(value):
    if value is None:
        return ''

    return str(value).strip()
