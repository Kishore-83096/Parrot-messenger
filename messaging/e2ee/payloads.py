import json


E2EE_MESSAGE_TYPE = 'e2ee.message'
E2EE_MESSAGE_VERSION = 1
MAX_ENCRYPTED_MESSAGE_TEXT_LENGTH = 100000


def is_encrypted_message_text(value):
    if not isinstance(value, str):
        return False

    normalized_value = value.strip()
    if not normalized_value or not normalized_value.startswith('{'):
        return False

    try:
        payload = json.loads(normalized_value)
    except ValueError:
        return False

    return (
        payload.get('type') == E2EE_MESSAGE_TYPE
        and payload.get('v') == E2EE_MESSAGE_VERSION
        and isinstance(payload.get('nonce'), str)
        and isinstance(payload.get('ciphertext'), str)
        and isinstance(payload.get('keys'), list)
    )
