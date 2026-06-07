import time
import uuid
from datetime import timedelta
from urllib.parse import unquote, urlparse

import cloudinary
from cloudinary import config as cloudinary_config
from cloudinary import uploader as cloudinary_uploader
from cloudinary.exceptions import Error as CloudinaryError
from cloudinary.utils import (
    api_sign_request,
    cloudinary_url,
    verify_api_response_signature,
)
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ..cloudinary_paths import (
    build_direct_message_cloudinary_folder,
    build_sender_cloudinary_folder,
)
from ..models import MessageEncryptedUploadIntent


MAX_ENCRYPTED_FILE_SIZE_BYTES = 26 * 1024 * 1024
MAX_ENCRYPTED_UPLOAD_INTENTS_PER_REQUEST = 10
DEFAULT_UPLOAD_INTENT_TTL_SECONDS = 600


def validation_error(errors):
    return {
        'status': 'error',
        'errors': errors,
    }, 400


def upload_encrypted_file(uploaded_file, sender=None):
    errors = validate_encrypted_file(uploaded_file)
    if errors:
        return validation_error(errors)

    _, cloudinary_errors = get_cloudinary_upload_settings()
    if cloudinary_errors:
        return validation_error({
            'file': ['Cloudinary is not configured for encrypted uploads.'],
        })

    try:
        if hasattr(uploaded_file, 'seek'):
            uploaded_file.seek(0)
        upload_result = cloudinary_uploader.upload(
            uploaded_file,
            folder=build_sender_cloudinary_folder(sender or {}),
            resource_type='raw',
            use_filename=False,
            unique_filename=True,
            overwrite=False,
        )
    except CloudinaryError as error:
        return validation_error({'file': [str(error) or 'Unable to upload encrypted attachment.']})

    encrypted_file_url = upload_result.get('secure_url') or upload_result.get('url') or ''
    if not encrypted_file_url:
        cleanup_encrypted_upload(upload_result)
        return validation_error({'file': ['Encrypted upload did not return a file URL.']})

    return {
        'status': 'ok',
        'file': {
            'encrypted_file_url': encrypted_file_url,
            'encrypted_file_size_bytes': (
                upload_result.get('bytes')
                or int(getattr(uploaded_file, 'size', 0) or 0)
            ),
        },
    }, 200


def create_encrypted_file_upload_intents(sender, parent_authorization, payload):
    normalized_payload, errors = normalize_encrypted_upload_intent_payload(payload)
    if errors:
        return validation_error(errors)

    cloudinary_settings, errors = get_cloudinary_upload_settings()
    if errors:
        return validation_error(errors)

    cleanup_expired_encrypted_upload_intents()

    now = timezone.now()
    timestamp = int(time.time())
    ttl_seconds = int(
        getattr(
            settings,
            'MESSAGING_ENCRYPTED_UPLOAD_INTENT_TTL_SECONDS',
            DEFAULT_UPLOAD_INTENT_TTL_SECONDS,
        )
        or DEFAULT_UPLOAD_INTENT_TTL_SECONDS
    )
    expires_at = now + timedelta(seconds=max(ttl_seconds, 60))
    folder = build_direct_message_cloudinary_folder(sender, parent_authorization)
    upload_intents = []

    for index, attachment in enumerate(normalized_payload['attachments']):
        public_id_param = f'{uuid.uuid4().hex}.txt'
        cloudinary_public_id = f'{folder}/{public_id_param}'
        signature_params = {
            'folder': folder,
            'overwrite': 'false',
            'public_id': public_id_param,
            'timestamp': timestamp,
            'unique_filename': 'false',
            'use_filename': 'false',
        }
        signature = api_sign_request(
            signature_params,
            cloudinary_settings['api_secret'],
        )

        intent = MessageEncryptedUploadIntent.objects.create(
            sender_user_id=sender['user_id'],
            sender_account_number=sender.get('account_number') or parent_authorization.get('sender_account_number') or '',
            recipient_user_id=parent_authorization['recipient_user_id'],
            recipient_account_number=parent_authorization['recipient_account_number'],
            client_message_id=normalized_payload['client_message_id'],
            attachment_client_id=attachment['attachment_client_id'],
            attachment_index=attachment.get('sort_order', index),
            original_file_name=attachment['file_name'],
            original_mime_type=attachment['mime_type'],
            original_file_size_bytes=attachment['file_size_bytes'],
            encrypted_file_size_bytes=attachment['encrypted_file_size_bytes'],
            cloudinary_public_id=cloudinary_public_id,
            cloudinary_resource_type='raw',
            cloudinary_folder=folder,
            signature_timestamp=timestamp,
            expires_at=expires_at,
        )
        upload_intents.append(
            {
                'id': str(intent.id),
                'attachment_id': intent.attachment_client_id,
                'attachment_index': intent.attachment_index,
                'upload_url': (
                    f'https://api.cloudinary.com/v1_1/'
                    f'{cloudinary_settings["cloud_name"]}/raw/upload'
                ),
                'cloud_name': cloudinary_settings['cloud_name'],
                'api_key': cloudinary_settings['api_key'],
                'resource_type': 'raw',
                'expires_at': intent.expires_at.isoformat(),
                'encrypted_file_size_bytes': intent.encrypted_file_size_bytes,
                'parameters': {
                    **signature_params,
                    'api_key': cloudinary_settings['api_key'],
                    'signature': signature,
                },
            }
        )

    return {
        'status': 'ok',
        'upload_intents': upload_intents,
    }, 201


def complete_encrypted_file_upload_intent(sender, upload_intent_id, payload):
    try:
        intent_id = uuid.UUID(str(upload_intent_id))
    except (TypeError, ValueError):
        return validation_error({'upload_intent_id': ['Upload intent id is invalid.']})

    cloudinary_settings, errors = get_cloudinary_upload_settings()
    if errors:
        return validation_error(errors)

    try:
        intent = MessageEncryptedUploadIntent.objects.get(
            id=intent_id,
            sender_user_id=sender['user_id'],
        )
    except MessageEncryptedUploadIntent.DoesNotExist:
        return {
            'status': 'error',
            'message': 'Upload intent was not found.',
        }, 404

    if intent.status == MessageEncryptedUploadIntent.STATUS_COMPLETED:
        return {
            'status': 'ok',
            'file': serialize_completed_upload_intent(intent),
        }, 200

    if intent.status != MessageEncryptedUploadIntent.STATUS_ISSUED:
        return validation_error({'upload_intent': ['Upload intent cannot be completed.']})

    if intent.expires_at <= timezone.now():
        expire_upload_intent(intent, cleanup_cloudinary=False)
        return validation_error({'upload_intent': ['Upload intent has expired.']})

    errors = validate_cloudinary_upload_response(intent, payload)
    if errors:
        return validation_error(errors)

    version = str(payload.get('version')).strip()
    secure_url = cloudinary_url(
        intent.cloudinary_public_id,
        resource_type='raw',
        secure=True,
        version=version,
        cloud_name=cloudinary_settings['cloud_name'],
    )[0]

    intent.cloudinary_asset_id = normalize_string(payload.get('asset_id'))
    intent.secure_url = secure_url
    intent.status = MessageEncryptedUploadIntent.STATUS_COMPLETED
    intent.completed_at = timezone.now()
    intent.save(
        update_fields=[
            'cloudinary_asset_id',
            'secure_url',
            'status',
            'completed_at',
            'updated_at',
        ]
    )

    return {
        'status': 'ok',
        'file': serialize_completed_upload_intent(intent),
    }, 200


def validate_completed_encrypted_upload_intents(sender, parent_authorization, payload):
    upload_intent_ids, errors = normalize_upload_intent_ids(
        payload.get('encrypted_upload_intent_ids'),
    )
    if errors:
        return None, errors

    if not upload_intent_ids:
        return [], None

    client_message_id = normalize_string(payload.get('client_message_id'))
    if not client_message_id:
        return None, {
            'encrypted_upload_intent_ids': [
                'Client message id is required when encrypted upload intents are attached.',
            ],
        }

    intents = list(
        MessageEncryptedUploadIntent.objects.filter(id__in=upload_intent_ids)
    )
    intents_by_id = {str(intent.id): intent for intent in intents}
    now = timezone.now()
    errors = []

    for index, upload_intent_id in enumerate(upload_intent_ids):
        intent = intents_by_id.get(str(upload_intent_id))
        if not intent:
            errors.append({index: 'Upload intent was not found.'})
            continue

        item_errors = {}
        if intent.sender_user_id != sender['user_id']:
            item_errors['sender'] = 'Upload intent does not belong to this user.'
        if intent.recipient_user_id != parent_authorization['recipient_user_id']:
            item_errors['recipient'] = 'Upload intent recipient does not match this message.'
        if intent.recipient_account_number != parent_authorization['recipient_account_number']:
            item_errors['recipient_account_number'] = 'Upload intent account does not match this message.'
        if intent.client_message_id != client_message_id:
            item_errors['client_message_id'] = 'Upload intent does not match this message.'
        if intent.status != MessageEncryptedUploadIntent.STATUS_COMPLETED:
            item_errors['status'] = 'Upload intent has not been completed.'
        if intent.expires_at <= now:
            item_errors['expires_at'] = 'Upload intent has expired.'

        if item_errors:
            errors.append({index: item_errors})

    return intents, errors or None


def consume_completed_encrypted_upload_intents(intents):
    intent_ids = [intent.id for intent in intents or []]
    if not intent_ids:
        return

    now = timezone.now()
    with transaction.atomic():
        MessageEncryptedUploadIntent.objects.filter(
            id__in=intent_ids,
            status=MessageEncryptedUploadIntent.STATUS_COMPLETED,
        ).update(
            status=MessageEncryptedUploadIntent.STATUS_CONSUMED,
            consumed_at=now,
            updated_at=now,
        )


def normalize_encrypted_upload_intent_payload(payload):
    if not isinstance(payload, dict):
        return None, {'body': ['Request body must be a JSON object.']}

    client_message_id = normalize_string(payload.get('client_message_id'))
    if not client_message_id:
        return None, {'client_message_id': ['Client message id is required.']}
    if len(client_message_id) > 120:
        return None, {'client_message_id': ['Client message id cannot exceed 120 characters.']}

    attachments = payload.get('attachments')
    if not isinstance(attachments, list) or not attachments:
        return None, {'attachments': ['At least one attachment is required.']}
    if len(attachments) > MAX_ENCRYPTED_UPLOAD_INTENTS_PER_REQUEST:
        return None, {
            'attachments': [
                f'Cannot attach more than {MAX_ENCRYPTED_UPLOAD_INTENTS_PER_REQUEST} files to one message.',
            ],
        }

    normalized_attachments = []
    errors = []

    for index, attachment in enumerate(attachments):
        normalized_attachment, item_errors = normalize_encrypted_upload_attachment(
            attachment,
            index,
        )
        if item_errors:
            errors.append({index: item_errors})
            continue

        normalized_attachments.append(normalized_attachment)

    if errors:
        return None, {'attachments': errors}

    return {
        'client_message_id': client_message_id,
        'attachments': normalized_attachments,
    }, None


def normalize_encrypted_upload_attachment(attachment, index):
    if not isinstance(attachment, dict):
        return None, 'Attachment must be an object.'

    max_size = getattr(
        settings,
        'MESSAGING_MAX_ENCRYPTED_UPLOAD_FILE_SIZE_BYTES',
        MAX_ENCRYPTED_FILE_SIZE_BYTES,
    )
    file_name = normalize_string(attachment.get('file_name')) or f'attachment-{index + 1}'
    mime_type = normalize_string(attachment.get('mime_type')) or 'application/octet-stream'
    attachment_client_id = normalize_string(attachment.get('id'))[:255]
    file_size_bytes = normalize_positive_int(attachment.get('file_size_bytes'))
    encrypted_file_size_bytes = normalize_positive_int(
        attachment.get('encrypted_file_size_bytes'),
    )
    sort_order = normalize_non_negative_int(attachment.get('sort_order'))
    errors = {}

    if not file_size_bytes:
        errors['file_size_bytes'] = 'Attachment file is empty.'
    if not encrypted_file_size_bytes:
        errors['encrypted_file_size_bytes'] = 'Encrypted file is empty.'
    elif encrypted_file_size_bytes > max_size:
        errors['encrypted_file_size_bytes'] = (
            f'Encrypted attachment cannot exceed {max_size // (1024 * 1024)} MB.'
        )

    if len(file_name) > 255:
        file_name = file_name[:255]
    if len(mime_type) > 120:
        mime_type = mime_type[:120]

    if errors:
        return None, errors

    return {
        'attachment_client_id': attachment_client_id,
        'file_name': file_name,
        'mime_type': mime_type,
        'file_size_bytes': file_size_bytes,
        'encrypted_file_size_bytes': encrypted_file_size_bytes,
        'sort_order': sort_order if sort_order is not None else index,
    }, None


def normalize_upload_intent_ids(value):
    if value in (None, ''):
        return [], None

    if not isinstance(value, list):
        return [], ['Encrypted upload intent ids must be a list.']

    if len(value) > MAX_ENCRYPTED_UPLOAD_INTENTS_PER_REQUEST:
        return [], [
            f'Cannot attach more than {MAX_ENCRYPTED_UPLOAD_INTENTS_PER_REQUEST} encrypted files to one message.',
        ]

    normalized_ids = []
    errors = []
    seen_ids = set()

    for index, item in enumerate(value):
        try:
            upload_intent_id = str(uuid.UUID(str(item)))
        except (TypeError, ValueError):
            errors.append({index: 'Upload intent id is invalid.'})
            continue

        if upload_intent_id in seen_ids:
            errors.append({index: 'Upload intent id is duplicated.'})
            continue

        seen_ids.add(upload_intent_id)
        normalized_ids.append(upload_intent_id)

    return normalized_ids, errors or None


def validate_cloudinary_upload_response(intent, payload):
    if not isinstance(payload, dict):
        return {'body': ['Request body must be a JSON object.']}

    public_id = normalize_string(payload.get('public_id'))
    resource_type = normalize_string(payload.get('resource_type')) or 'raw'
    version = normalize_string(payload.get('version'))
    signature = normalize_string(payload.get('signature'))
    uploaded_bytes = normalize_positive_int(payload.get('bytes'))
    errors = {}

    if public_id != intent.cloudinary_public_id:
        errors['public_id'] = 'Cloudinary upload does not match this intent.'
    if resource_type != 'raw':
        errors['resource_type'] = 'Encrypted uploads must use raw Cloudinary resource type.'
    if uploaded_bytes != intent.encrypted_file_size_bytes:
        errors['bytes'] = 'Uploaded encrypted file size does not match this intent.'
    if not version:
        errors['version'] = 'Cloudinary upload version is required.'
    if not signature:
        errors['signature'] = 'Cloudinary upload signature is required.'

    if not errors and not verify_api_response_signature(public_id, version, signature):
        errors['signature'] = 'Cloudinary upload signature is invalid.'

    return errors or None


def serialize_completed_upload_intent(intent):
    return {
        'upload_intent_id': str(intent.id),
        'encrypted_file_url': intent.secure_url,
        'encrypted_file_size_bytes': intent.encrypted_file_size_bytes,
        'cloudinary_public_id': intent.cloudinary_public_id,
        'cloudinary_asset_id': intent.cloudinary_asset_id,
        'cloudinary_resource_type': intent.cloudinary_resource_type,
        'cloudinary_folder': intent.cloudinary_folder,
    }


def validate_encrypted_file(uploaded_file):
    if not uploaded_file:
        return {'file': ['Encrypted file is required.']}

    file_size = int(getattr(uploaded_file, 'size', 0) or 0)
    max_size = getattr(
        settings,
        'MESSAGING_MAX_ENCRYPTED_UPLOAD_FILE_SIZE_BYTES',
        MAX_ENCRYPTED_FILE_SIZE_BYTES,
    )

    if file_size <= 0:
        return {'file': ['Encrypted file is empty.']}

    if file_size > max_size:
        return {
            'file': [
                f'Encrypted attachment cannot exceed {max_size // (1024 * 1024)} MB.',
            ],
        }

    return None


def get_encrypted_upload_folder():
    return build_sender_cloudinary_folder({})


def get_cloudinary_upload_settings():
    cloudinary_url_value = getattr(settings, 'CLOUDINARY_URL', '')
    if not cloudinary_url_value:
        return None, {'cloudinary': ['Cloudinary is not configured for encrypted uploads.']}

    parsed_url = urlparse(cloudinary_url_value)
    if parsed_url.scheme != 'cloudinary':
        return None, {'cloudinary': ['Cloudinary upload credentials are invalid.']}

    cloud_name = parsed_url.hostname or ''
    api_key = unquote(parsed_url.username or '')
    api_secret = unquote(parsed_url.password or '')

    if not cloud_name or not api_key or not api_secret:
        return None, {'cloudinary': ['Cloudinary upload credentials are incomplete.']}

    cloudinary_config(
        cloud_name=cloud_name,
        api_key=api_key,
        api_secret=api_secret,
        secure=True,
    )

    return {
        'cloud_name': cloud_name,
        'api_key': api_key,
        'api_secret': api_secret,
    }, None


def cleanup_expired_encrypted_upload_intents(limit=25):
    now = timezone.now()
    expired_intents = list(
        MessageEncryptedUploadIntent.objects.filter(
            expires_at__lt=now,
            status__in=[
                MessageEncryptedUploadIntent.STATUS_ISSUED,
                MessageEncryptedUploadIntent.STATUS_COMPLETED,
            ],
        ).order_by('expires_at', 'created_at')[:limit]
    )

    for intent in expired_intents:
        expire_upload_intent(
            intent,
            cleanup_cloudinary=(
                intent.status == MessageEncryptedUploadIntent.STATUS_COMPLETED
            ),
        )


def expire_upload_intent(intent, cleanup_cloudinary=True):
    if cleanup_cloudinary and intent.cloudinary_public_id:
        try:
            cloudinary_uploader.destroy(
                intent.cloudinary_public_id,
                resource_type=intent.cloudinary_resource_type or 'raw',
            )
        except CloudinaryError:
            pass

    intent.status = MessageEncryptedUploadIntent.STATUS_EXPIRED
    intent.save(update_fields=['status', 'updated_at'])


def cleanup_encrypted_upload(upload_result):
    public_id = upload_result.get('public_id')
    if not public_id:
        return

    try:
        cloudinary_uploader.destroy(
            public_id,
            resource_type=upload_result.get('resource_type') or 'raw',
        )
    except CloudinaryError:
        pass


def normalize_string(value):
    if value is None:
        return ''

    return str(value).strip()


def normalize_positive_int(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None

    return value if value > 0 else None


def normalize_non_negative_int(value):
    if value in (None, ''):
        return None

    try:
        value = int(value)
    except (TypeError, ValueError):
        return None

    return value if value >= 0 else None
