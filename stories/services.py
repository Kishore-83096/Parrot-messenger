import time
import uuid
from collections import defaultdict
from datetime import timedelta

from cloudinary import uploader as cloudinary_uploader
from cloudinary.exceptions import Error as CloudinaryError
from cloudinary.utils import api_sign_request, cloudinary_url
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.utils import timezone

from messaging.e2ee.files import (
    DEFAULT_UPLOAD_INTENT_TTL_SECONDS,
    MAX_ENCRYPTED_FILE_SIZE_BYTES,
    get_cloudinary_upload_settings,
    get_encrypted_upload_folder,
    normalize_non_negative_int,
    normalize_positive_int,
    normalize_string,
    validate_cloudinary_upload_response,
    validation_error,
)
from messaging.services import create_direct_message

from .models import (
    Story,
    StoryAudience,
    StoryMedia,
    StoryReaction,
    StoryReply,
    StorySettings,
    StoryUploadIntent,
    StoryView,
)
from .policy import authorize_parent_story_visibility


MAX_STORY_MEDIA_UPLOAD_INTENTS_PER_REQUEST = 1
DEFAULT_EXPIRED_STORY_MEDIA_RETENTION_DAYS = 0
DEFAULT_EXPIRED_STORY_MEDIA_CLEANUP_LIMIT = 100
STORY_MEDIA_IMAGE = StoryUploadIntent.MEDIA_IMAGE
STORY_MEDIA_VIDEO = StoryUploadIntent.MEDIA_VIDEO
STORY_MEDIA_TYPE_PREFIXES = {
    STORY_MEDIA_IMAGE: 'image/',
    STORY_MEDIA_VIDEO: 'video/',
}
STORY_ALLOWED_TYPES = {
    Story.STORY_TYPE_MEDIA,
    Story.STORY_TYPE_TEXT,
}
STORY_ALLOWED_EXPIRY_HOURS = {6, 12, 24}
STORY_TEXT_PAYLOAD_MAX_LENGTH = 12000
STORY_REACTION_MESSAGE_TEXT = {
    StoryReaction.REACTION_THUMBS_UP: '\U0001F44D',
    StoryReaction.REACTION_HEART: '\u2764\ufe0f',
    StoryReaction.REACTION_LAUGH: '\U0001F602',
    StoryReaction.REACTION_SURPRISED: '\U0001F62E',
    StoryReaction.REACTION_SAD: '\U0001F622',
}


def get_story_reaction_message_text(reaction):
    return STORY_REACTION_MESSAGE_TEXT.get(reaction, reaction)


def is_story_view_hidden_from_owner(parent_policy):
    if not isinstance(parent_policy, dict):
        return False

    ghost_context = parent_policy.get('ghost_context')
    if isinstance(ghost_context, dict):
        return bool(ghost_context.get('viewer_ghosted_owner'))

    viewer_contact = parent_policy.get('viewer_contact')
    if isinstance(viewer_contact, dict):
        return bool(viewer_contact.get('ghosted'))

    return False


def get_or_create_story_settings(sender):
    settings_row, _created = StorySettings.objects.get_or_create(
        owner_user_id=sender['user_id'],
        defaults={
            'owner_account_number': sender.get('account_number') or '',
        },
    )
    owner_account_number = sender.get('account_number') or settings_row.owner_account_number
    if owner_account_number and settings_row.owner_account_number != owner_account_number:
        settings_row.owner_account_number = owner_account_number
        settings_row.save(update_fields=['owner_account_number', 'updated_at'])

    return settings_row


def get_saved_story_settings(sender):
    settings_row = StorySettings.objects.filter(owner_user_id=sender['user_id']).first()
    if not settings_row:
        return None

    owner_account_number = sender.get('account_number') or settings_row.owner_account_number
    if owner_account_number and settings_row.owner_account_number != owner_account_number:
        settings_row.owner_account_number = owner_account_number
        settings_row.save(update_fields=['owner_account_number', 'updated_at'])

    return settings_row


def get_story_settings_default_values(sender):
    settings_row = get_saved_story_settings(sender)
    if not settings_row:
        return {
            'expiry_hours': Story.EXPIRY_24_HOURS,
            'visibility': Story.VISIBILITY_ALL_CONTACTS,
            'audience_account_numbers': [],
        }

    return {
        'expiry_hours': settings_row.expiry_hours,
        'visibility': settings_row.visibility,
        'audience_account_numbers': normalize_settings_audience_list(
            settings_row.audience_account_numbers,
        ),
    }


def get_story_settings(sender):
    settings_row = get_saved_story_settings(sender)

    return {
        'status': 'ok',
        'has_saved_settings': settings_row is not None,
        'settings': (
            serialize_story_settings(settings_row)
            if settings_row
            else get_story_settings_default_values(sender)
        ),
    }, 200


def update_story_settings(sender, parent_audience, payload):
    settings_row = get_or_create_story_settings(sender)
    normalized_payload, errors = normalize_story_settings_payload(payload, settings_row)
    if errors:
        return validation_error(errors)

    _audience_contacts, audience_errors = validate_story_parent_audience(
        normalized_payload,
        parent_audience,
    )
    if audience_errors:
        return validation_error(audience_errors)

    settings_row.expiry_hours = normalized_payload['expiry_hours']
    settings_row.visibility = normalized_payload['visibility']
    settings_row.audience_account_numbers = normalized_payload['audience_account_numbers']
    settings_row.owner_account_number = (
        sender.get('account_number')
        or parent_audience.get('owner_account_number')
        or settings_row.owner_account_number
    )
    settings_row.save(
        update_fields=[
            'expiry_hours',
            'visibility',
            'audience_account_numbers',
            'owner_account_number',
            'updated_at',
        ]
    )

    return {
        'status': 'ok',
        'settings': serialize_story_settings(settings_row),
        'audience': {
            'valid_count': parent_audience.get('valid_count', 0),
            'excluded_contacts': parent_audience.get('excluded_contacts') or [],
            'missing_account_numbers': parent_audience.get('missing_account_numbers') or [],
        },
    }, 200


def create_story_media_upload_intents(sender, parent_audience, payload):
    normalized_payload, errors = normalize_story_upload_intent_payload(payload)
    if errors:
        return validation_error(errors)

    if not isinstance(parent_audience, dict) or parent_audience.get('allowed') is not True:
        return {
            'status': 'error',
            'message': 'Story audience is not authorized.',
        }, 403

    cloudinary_settings, errors = get_cloudinary_upload_settings()
    if errors:
        return validation_error(errors)

    cleanup_expired_story_upload_intents()

    now = timezone.now()
    timestamp = int(time.time())
    ttl_seconds = int(
        getattr(
            settings,
            'STORIES_UPLOAD_INTENT_TTL_SECONDS',
            DEFAULT_UPLOAD_INTENT_TTL_SECONDS,
        )
        or DEFAULT_UPLOAD_INTENT_TTL_SECONDS
    )
    expires_at = now + timedelta(seconds=max(ttl_seconds, 60))
    folder = f'{get_encrypted_upload_folder()}/stories/user-{sender["user_id"]}'
    upload_intents = []

    for index, media in enumerate(normalized_payload['media']):
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

        intent = StoryUploadIntent.objects.create(
            owner_user_id=sender['user_id'],
            owner_account_number=(
                sender.get('account_number')
                or parent_audience.get('owner_account_number')
                or ''
            ),
            client_story_id=normalized_payload['client_story_id'],
            media_client_id=media['media_client_id'],
            media_index=media.get('sort_order', index),
            media_type=media['media_type'],
            original_file_name=media['file_name'],
            original_mime_type=media['mime_type'],
            original_file_size_bytes=media['file_size_bytes'],
            encrypted_file_size_bytes=media['encrypted_file_size_bytes'],
            cloudinary_public_id=cloudinary_public_id,
            cloudinary_resource_type='raw',
            cloudinary_folder=folder,
            signature_timestamp=timestamp,
            expires_at=expires_at,
        )
        upload_intents.append(
            {
                'id': str(intent.id),
                'media_id': intent.media_client_id,
                'media_index': intent.media_index,
                'media_type': intent.media_type,
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


def complete_story_media_upload_intent(sender, upload_intent_id, payload):
    try:
        intent_id = uuid.UUID(str(upload_intent_id))
    except (TypeError, ValueError):
        return validation_error({'upload_intent_id': ['Upload intent id is invalid.']})

    cloudinary_settings, errors = get_cloudinary_upload_settings()
    if errors:
        return validation_error(errors)

    try:
        intent = StoryUploadIntent.objects.get(
            id=intent_id,
            owner_user_id=sender['user_id'],
        )
    except StoryUploadIntent.DoesNotExist:
        return {
            'status': 'error',
            'message': 'Upload intent was not found.',
        }, 404

    if intent.status == StoryUploadIntent.STATUS_COMPLETED:
        return {
            'status': 'ok',
            'file': serialize_completed_story_upload_intent(intent),
        }, 200

    if intent.status != StoryUploadIntent.STATUS_ISSUED:
        return validation_error({'upload_intent': ['Upload intent cannot be completed.']})

    if intent.expires_at <= timezone.now():
        expire_story_upload_intent(intent, cleanup_cloudinary=False)
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
    intent.status = StoryUploadIntent.STATUS_COMPLETED
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
        'file': serialize_completed_story_upload_intent(intent),
    }, 200


def create_story_from_upload_intents(sender, parent_audience, payload):
    normalized_payload, errors = normalize_create_story_payload(payload)
    if errors:
        return validation_error(errors)

    existing_story = Story.objects.filter(
        owner_user_id=sender['user_id'],
        client_story_id=normalized_payload['client_story_id'],
    ).prefetch_related('media', 'audience').first()
    if existing_story:
        return {
            'status': 'ok',
            'story': serialize_story(existing_story, include_audience=True),
            'idempotent': True,
        }, 200

    audience_contacts, audience_errors = validate_story_parent_audience(
        normalized_payload,
        parent_audience,
    )
    if audience_errors:
        return validation_error(audience_errors)

    try:
        with transaction.atomic():
            existing_story = Story.objects.select_for_update().filter(
                owner_user_id=sender['user_id'],
                client_story_id=normalized_payload['client_story_id'],
            ).prefetch_related('media', 'audience').first()
            if existing_story:
                return {
                    'status': 'ok',
                    'story': serialize_story(existing_story, include_audience=True),
                    'idempotent': True,
                }, 200

            upload_intents = []
            if normalized_payload['story_type'] == Story.STORY_TYPE_MEDIA:
                upload_intents, intent_errors = validate_completed_story_upload_intents(
                    sender,
                    normalized_payload,
                )
                if intent_errors:
                    return validation_error({'encrypted_upload_intent_ids': intent_errors})

            now = timezone.now()
            story = Story.objects.create(
                owner_user_id=sender['user_id'],
                owner_account_number=(
                    sender.get('account_number')
                    or parent_audience.get('owner_account_number')
                    or ''
                ),
                client_story_id=normalized_payload['client_story_id'],
                story_type=normalized_payload['story_type'],
                visibility=normalized_payload['visibility'],
                expiry_hours=normalized_payload['expiry_hours'],
                encrypted_payload=normalized_payload['encrypted_payload'],
                expires_at=now + timedelta(hours=normalized_payload['expiry_hours']),
            )

            if upload_intents:
                StoryMedia.objects.bulk_create(
                    [
                        StoryMedia(
                            story=story,
                            upload_intent=intent,
                            media_type=intent.media_type,
                            encrypted_file_url=intent.secure_url,
                            file_name=intent.original_file_name,
                            mime_type=intent.original_mime_type,
                            file_size_bytes=intent.original_file_size_bytes,
                            encrypted_file_size_bytes=intent.encrypted_file_size_bytes,
                            cloudinary_public_id=intent.cloudinary_public_id,
                            cloudinary_asset_id=intent.cloudinary_asset_id,
                            cloudinary_resource_type=intent.cloudinary_resource_type,
                            cloudinary_folder=intent.cloudinary_folder,
                            sort_order=intent.media_index,
                        )
                        for intent in upload_intents
                    ]
                )

            StoryAudience.objects.bulk_create(
                [
                    StoryAudience(
                        story=story,
                        viewer_user_id=contact['user_id'],
                        viewer_account_number=contact['account_number'],
                    )
                    for contact in audience_contacts
                ],
                ignore_conflicts=True,
            )

            consumed_at = timezone.now()
            if upload_intents:
                StoryUploadIntent.objects.filter(
                    id__in=[intent.id for intent in upload_intents],
                    status=StoryUploadIntent.STATUS_COMPLETED,
                ).update(
                    status=StoryUploadIntent.STATUS_CONSUMED,
                    consumed_at=consumed_at,
                    updated_at=consumed_at,
                )
    except IntegrityError:
        existing_story = Story.objects.filter(
            owner_user_id=sender['user_id'],
            client_story_id=normalized_payload['client_story_id'],
        ).prefetch_related('media', 'audience').first()
        if existing_story:
            return {
                'status': 'ok',
                'story': serialize_story(existing_story, include_audience=True),
                'idempotent': True,
            }, 200
        raise

    story = Story.objects.prefetch_related('media', 'audience').get(id=story.id)
    return {
        'status': 'ok',
        'story': serialize_story(story, include_audience=True),
        'audience': {
            'valid_count': len(audience_contacts),
            'excluded_contacts': parent_audience.get('excluded_contacts') or [],
            'missing_account_numbers': parent_audience.get('missing_account_numbers') or [],
        },
    }, 201


def list_story_feed(sender):
    now = timezone.now()
    stories = list(
        Story.objects.filter(
            audience__viewer_user_id=sender['user_id'],
            status=Story.STATUS_ACTIVE,
            expires_at__gt=now,
        )
        .exclude(owner_user_id=sender['user_id'])
        .prefetch_related('media')
        .order_by('owner_user_id', '-created_at', '-id')
    )
    if not stories:
        return {
            'status': 'ok',
            'contacts': [],
        }, 200

    story_ids = [story.id for story in stories]
    viewed_story_ids = set(
        StoryView.objects.filter(
            story_id__in=story_ids,
            viewer_user_id=sender['user_id'],
        ).values_list('story_id', flat=True)
    )
    stories_by_owner = defaultdict(list)
    for story in stories:
        stories_by_owner[story.owner_user_id].append(story)

    contacts = []
    for owner_user_id, owner_stories in stories_by_owner.items():
        first_story = owner_stories[0]
        policy_result, policy_status = authorize_parent_story_visibility(
            {
                'owner_user_id': owner_user_id,
                'viewer_user_id': sender['user_id'],
            }
        )
        parent_policy = policy_result.get('parent', {}).get('response')

        if policy_status >= 500:
            return {
                'status': 'error',
                'message': 'Unable to verify story visibility with Parent service.',
                'policy': policy_result,
            }, policy_status

        if not isinstance(parent_policy, dict) or parent_policy.get('allowed') is not True:
            continue

        serialized_stories = [
            serialize_story(story, viewed_story_ids=viewed_story_ids)
            for story in owner_stories
        ]
        contacts.append(
            {
                'user_id': owner_user_id,
                'account_number': first_story.owner_account_number,
                'contact': serialize_story_feed_contact(first_story, parent_policy),
                'latest_story_at': serialized_stories[0]['created_at'],
                'unviewed_count': sum(
                    1 for story_result in serialized_stories if not story_result['viewed']
                ),
                'stories': serialized_stories,
            }
        )

    contacts.sort(key=lambda item: item['latest_story_at'], reverse=True)
    return {
        'status': 'ok',
        'contacts': contacts,
    }, 200


def list_my_stories(sender):
    now = timezone.now()
    stories = (
        Story.objects.filter(
            owner_user_id=sender['user_id'],
            status=Story.STATUS_ACTIVE,
            expires_at__gt=now,
        )
        .prefetch_related('media')
        .annotate(
            view_count=Count(
                'views',
                filter=Q(views__hidden_from_owner=False),
                distinct=True,
            )
        )
        .order_by('-created_at', '-id')
    )

    return {
        'status': 'ok',
        'stories': [
            serialize_my_story(story, now=now)
            for story in stories
        ],
    }, 200


def delete_story(sender, story_id):
    try:
        story_uuid = uuid.UUID(str(story_id))
    except (TypeError, ValueError):
        return validation_error({'story_id': ['Story id is invalid.']})

    story = (
        Story.objects.filter(id=story_uuid)
        .prefetch_related('audience', 'media')
        .first()
    )
    if not story or story.status == Story.STATUS_DELETED:
        return {
            'status': 'error',
            'message': 'Story was not found.',
        }, 404

    if story.owner_user_id != sender['user_id']:
        return {
            'status': 'denied',
            'reason': 'story_owner_required',
            'message': 'Only the story owner can delete this story.',
        }, 403

    now = timezone.now()
    Story.objects.filter(id=story.id).update(
        status=Story.STATUS_DELETED,
        deleted_at=now,
        updated_at=now,
    )
    story.status = Story.STATUS_DELETED
    story.deleted_at = now
    story.updated_at = now

    return {
        'status': 'ok',
        'story_id': str(story.id),
        'owner_user_id': story.owner_user_id,
        'owner_account_number': story.owner_account_number,
        'deleted': True,
        'deleted_at': now.isoformat(),
        'audience_user_ids': [
            audience.viewer_user_id
            for audience in story.audience.all()
        ],
        'story': serialize_story(story, include_audience=True),
    }, 200


def mark_story_viewed(sender, story_id):
    try:
        story_uuid = uuid.UUID(str(story_id))
    except (TypeError, ValueError):
        return validation_error({'story_id': ['Story id is invalid.']})

    now = timezone.now()
    story = (
        Story.objects.filter(
            id=story_uuid,
            status=Story.STATUS_ACTIVE,
            expires_at__gt=now,
        )
        .prefetch_related('media')
        .first()
    )
    if not story:
        return {
            'status': 'error',
            'message': 'Story was not found.',
        }, 404

    if story.owner_user_id == sender['user_id']:
        return {
            'status': 'ok',
            'story_id': str(story.id),
            'owner_user_id': story.owner_user_id,
            'owner_account_number': story.owner_account_number,
            'viewed': False,
            'created': False,
            'is_owner': True,
            'view_count': StoryView.objects.filter(
                story=story,
                hidden_from_owner=False,
            ).count(),
        }, 200

    if not StoryAudience.objects.filter(
        story=story,
        viewer_user_id=sender['user_id'],
    ).exists():
        return {
            'status': 'denied',
            'reason': 'viewer_not_in_story_audience',
            'message': 'You cannot view this story.',
        }, 403

    policy_result, policy_status = authorize_parent_story_visibility(
        {
            'owner_user_id': story.owner_user_id,
            'viewer_user_id': sender['user_id'],
        }
    )
    parent_policy = policy_result.get('parent', {}).get('response')

    if policy_status >= 500:
        return {
            'status': 'error',
            'message': 'Unable to verify story visibility with Parent service.',
            'policy': policy_result,
        }, policy_status

    if not isinstance(parent_policy, dict) or parent_policy.get('allowed') is not True:
        return {
            'status': 'denied',
            'reason': (
                parent_policy.get('reason')
                if isinstance(parent_policy, dict)
                else 'story_visibility_denied'
            ),
            'message': 'You cannot view this story.',
            'policy': policy_result,
        }, policy_status if policy_status >= 400 else 403

    hidden_from_owner = is_story_view_hidden_from_owner(parent_policy)
    view, created = StoryView.objects.get_or_create(
        story=story,
        viewer_user_id=sender['user_id'],
        defaults={
            'viewer_account_number': (
                sender.get('account_number')
                or parent_policy.get('viewer_account_number')
                or ''
            ),
            'hidden_from_owner': hidden_from_owner,
        },
    )

    return {
        'status': 'ok',
        'story_id': str(story.id),
        'owner_user_id': story.owner_user_id,
        'owner_account_number': story.owner_account_number,
        'viewer_user_id': sender['user_id'],
        'viewer_account_number': view.viewer_account_number,
        'viewed': True,
        'created': created,
        'viewed_at': view.viewed_at.isoformat(),
        'hidden_from_owner': view.hidden_from_owner,
        'view_count': StoryView.objects.filter(
            story=story,
            hidden_from_owner=False,
        ).count(),
    }, 200 if not created else 201


def list_story_viewers(sender, story_id):
    try:
        story_uuid = uuid.UUID(str(story_id))
    except (TypeError, ValueError):
        return validation_error({'story_id': ['Story id is invalid.']})

    story = Story.objects.filter(id=story_uuid).first()
    if not story or story.status == Story.STATUS_DELETED:
        return {
            'status': 'error',
            'message': 'Story was not found.',
        }, 404

    if story.owner_user_id != sender['user_id']:
        return {
            'status': 'denied',
            'reason': 'story_owner_required',
            'message': 'Only the story owner can view story viewers.',
        }, 403

    viewers = list(
        StoryView.objects.filter(story=story, hidden_from_owner=False)
        .order_by('-viewed_at', '-id')
    )

    return {
        'status': 'ok',
        'story': {
            'id': str(story.id),
            'client_story_id': story.client_story_id,
            'owner_user_id': story.owner_user_id,
            'owner_account_number': story.owner_account_number,
            'status': story.status,
            'created_at': story.created_at.isoformat(),
            'expires_at': story.expires_at.isoformat(),
        },
        'view_count': len(viewers),
        'viewers': [
            serialize_story_viewer(viewer)
            for viewer in viewers
        ],
    }, 200


def react_to_story(sender, story_id, payload):
    normalized_payload, errors = normalize_story_reaction_payload(payload)
    if errors:
        return validation_error(errors)

    story, parent_policy, error_result, error_status = get_viewable_story_for_response(
        sender,
        story_id,
    )
    if error_result:
        return error_result, error_status

    message_result, message_status = create_story_response_message(
        sender,
        story,
        parent_policy,
        {
            'client_message_id': normalized_payload['client_message_id'],
            'text': (
                normalized_payload['text']
                or get_story_reaction_message_text(normalized_payload['reaction'])
            ),
            'story_context': build_story_context(story, 'reaction'),
        },
    )
    if message_status >= 300:
        return {
            'status': 'error',
            'message_result': message_result,
        }, message_status

    record_story_view(sender, story, parent_policy)
    message_id = message_result['message']['id']
    with transaction.atomic():
        existing_reaction = (
            StoryReaction.objects.select_for_update()
            .filter(story=story, viewer_user_id=sender['user_id'])
            .first()
        )
        previous_reaction = existing_reaction.reaction if existing_reaction else None
        if existing_reaction:
            existing_reaction.reaction = normalized_payload['reaction']
            existing_reaction.viewer_account_number = (
                sender.get('account_number')
                or parent_policy.get('viewer_account_number')
                or existing_reaction.viewer_account_number
            )
            existing_reaction.message_id = message_id
            existing_reaction.save(
                update_fields=[
                    'reaction',
                    'viewer_account_number',
                    'message',
                    'updated_at',
                ]
            )
            story_reaction = existing_reaction
            action = 'updated'
        else:
            story_reaction = StoryReaction.objects.create(
                story=story,
                viewer_user_id=sender['user_id'],
                viewer_account_number=(
                    sender.get('account_number')
                    or parent_policy.get('viewer_account_number')
                    or ''
                ),
                reaction=normalized_payload['reaction'],
                message_id=message_id,
            )
            action = 'created'

    return {
        'status': 'ok',
        'action': action,
        'story_id': str(story.id),
        'reaction': serialize_story_reaction(story_reaction),
        'previous_reaction': previous_reaction,
        'message_result': message_result,
    }, 200 if action == 'updated' else 201


def reply_to_story(sender, story_id, payload):
    normalized_payload, errors = normalize_story_reply_payload(payload)
    if errors:
        return validation_error(errors)

    story, parent_policy, error_result, error_status = get_viewable_story_for_response(
        sender,
        story_id,
    )
    if error_result:
        return error_result, error_status

    message_result, message_status = create_story_response_message(
        sender,
        story,
        parent_policy,
        {
            'client_message_id': normalized_payload['client_message_id'],
            'text': normalized_payload['text'],
            'story_context': build_story_context(story, 'reply'),
        },
    )
    if message_status >= 300:
        return {
            'status': 'error',
            'message_result': message_result,
        }, message_status

    record_story_view(sender, story, parent_policy)
    story_reply, _ = StoryReply.objects.get_or_create(
        story=story,
        viewer_user_id=sender['user_id'],
        message_id=message_result['message']['id'],
        defaults={
            'viewer_account_number': (
                sender.get('account_number')
                or parent_policy.get('viewer_account_number')
                or ''
            ),
        },
    )

    return {
        'status': 'ok',
        'story_id': str(story.id),
        'reply': serialize_story_reply(story_reply),
        'message_result': message_result,
    }, 201 if message_result.get('status') == 'sent' else 200


def normalize_story_reaction_payload(payload):
    if not isinstance(payload, dict):
        return None, {'body': ['Request body must be a JSON object.']}

    errors = {}
    client_message_id = normalize_required_client_message_id(payload.get('client_message_id'), errors)
    reaction = normalize_string(payload.get('reaction'))
    text = payload.get('text', '')
    if text is None:
        text = ''
    if not isinstance(text, str):
        errors['text'] = ['Reaction message text must be a string.']
        text = ''

    if not reaction:
        errors['reaction'] = ['Reaction is required.']
    elif reaction not in StoryReaction.ALLOWED_REACTIONS:
        errors['reaction'] = ['Unsupported reaction.']

    if errors:
        return None, errors

    return {
        'client_message_id': client_message_id,
        'reaction': reaction,
        'text': text.strip(),
    }, None


def normalize_story_reply_payload(payload):
    if not isinstance(payload, dict):
        return None, {'body': ['Request body must be a JSON object.']}

    errors = {}
    client_message_id = normalize_required_client_message_id(payload.get('client_message_id'), errors)
    text = payload.get('text', '')
    if text is None:
        text = ''
    if not isinstance(text, str):
        errors['text'] = ['Reply text must be a string.']
        text = ''
    elif not text.strip():
        errors['text'] = ['Reply text is required.']

    if errors:
        return None, errors

    return {
        'client_message_id': client_message_id,
        'text': text.strip(),
    }, None


def normalize_required_client_message_id(value, errors):
    client_message_id = normalize_string(value)
    if not client_message_id:
        errors['client_message_id'] = ['Client message id is required.']
    elif len(client_message_id) > 120:
        errors['client_message_id'] = ['Client message id cannot exceed 120 characters.']

    return client_message_id


def get_viewable_story_for_response(sender, story_id):
    try:
        story_uuid = uuid.UUID(str(story_id))
    except (TypeError, ValueError):
        result, status = validation_error({'story_id': ['Story id is invalid.']})
        return None, None, result, status

    story = (
        Story.objects.filter(
            id=story_uuid,
            status=Story.STATUS_ACTIVE,
            expires_at__gt=timezone.now(),
        )
        .prefetch_related('media')
        .first()
    )
    if not story:
        return None, None, {
            'status': 'error',
            'message': 'Story was not found.',
        }, 404

    if story.owner_user_id == sender['user_id']:
        return None, None, {
            'status': 'denied',
            'reason': 'cannot_respond_to_own_story',
            'message': 'You cannot respond to your own story.',
        }, 403

    if not StoryAudience.objects.filter(
        story=story,
        viewer_user_id=sender['user_id'],
    ).exists():
        return None, None, {
            'status': 'denied',
            'reason': 'viewer_not_in_story_audience',
            'message': 'You cannot respond to this story.',
        }, 403

    policy_result, policy_status = authorize_parent_story_visibility(
        {
            'owner_user_id': story.owner_user_id,
            'viewer_user_id': sender['user_id'],
        }
    )
    parent_policy = policy_result.get('parent', {}).get('response')
    if policy_status >= 500:
        return None, None, {
            'status': 'error',
            'message': 'Unable to verify story visibility with Parent service.',
            'policy': policy_result,
        }, policy_status

    if not isinstance(parent_policy, dict) or parent_policy.get('allowed') is not True:
        return None, None, {
            'status': 'denied',
            'reason': (
                parent_policy.get('reason')
                if isinstance(parent_policy, dict)
                else 'story_visibility_denied'
            ),
            'message': 'You cannot respond to this story.',
            'policy': policy_result,
        }, policy_status if policy_status >= 400 else 403

    return story, parent_policy, None, None


def create_story_response_message(sender, story, parent_policy, payload):
    parent_authorization = {
        'sender_user_id': sender['user_id'],
        'sender_account_number': (
            sender.get('account_number')
            or parent_policy.get('viewer_account_number')
            or ''
        ),
        'recipient_user_id': story.owner_user_id,
        'recipient_account_number': story.owner_account_number,
        'delivery_blocked': False,
    }
    message_payload = {
        'recipient_account_number': story.owner_account_number,
        'client_message_id': payload['client_message_id'],
        'text': payload['text'],
        'story_context': payload['story_context'],
    }

    return create_direct_message(sender, parent_authorization, message_payload)


def build_story_context(story, context_type):
    first_media = story.media.all()[0] if story.media.all() else None

    return {
        'story_id': str(story.id),
        'type': context_type,
        'media_type': (
            first_media.media_type
            if first_media
            else story.story_type
            if story.story_type == Story.STORY_TYPE_TEXT
            else ''
        ),
        'preview_label': 'Story',
        'created_at': story.created_at.isoformat(),
        'expires_at': story.expires_at.isoformat(),
    }


def record_story_view(sender, story, parent_policy):
    return StoryView.objects.get_or_create(
        story=story,
        viewer_user_id=sender['user_id'],
        defaults={
            'viewer_account_number': (
                sender.get('account_number')
                or parent_policy.get('viewer_account_number')
                or ''
            ),
            'hidden_from_owner': is_story_view_hidden_from_owner(parent_policy),
        },
    )


def normalize_create_story_payload(payload, settings_defaults=None):
    if not isinstance(payload, dict):
        return None, {'body': ['Request body must be a JSON object.']}

    settings_defaults = settings_defaults if isinstance(settings_defaults, dict) else {}
    client_story_id = normalize_string(payload.get('client_story_id'))
    if not client_story_id:
        return None, {'client_story_id': ['Client story id is required.']}
    if len(client_story_id) > 120:
        return None, {'client_story_id': ['Client story id cannot exceed 120 characters.']}

    story_type = normalize_string(payload.get('story_type')) or Story.STORY_TYPE_MEDIA
    if story_type not in STORY_ALLOWED_TYPES:
        return None, {'story_type': ['Story type must be media or text.']}

    expiry_hours = (
        normalize_positive_int(payload.get('expiry_hours'))
        or normalize_positive_int(settings_defaults.get('expiry_hours'))
        or Story.EXPIRY_24_HOURS
    )
    if expiry_hours not in STORY_ALLOWED_EXPIRY_HOURS:
        return None, {'expiry_hours': ['Story expiry must be 6, 12, or 24 hours.']}

    visibility = (
        normalize_string(payload.get('visibility'))
        or normalize_string(settings_defaults.get('visibility'))
        or Story.VISIBILITY_ALL_CONTACTS
    )
    if visibility not in dict(Story.VISIBILITY_CHOICES):
        return None, {'visibility': ['Story visibility must be all_contacts or specific_contacts.']}

    audience_source = (
        payload.get('audience_account_numbers')
        if 'audience_account_numbers' in payload
        else settings_defaults.get('audience_account_numbers')
    )
    audience_account_numbers, audience_errors = normalize_audience_account_numbers(
        audience_source,
    )
    if audience_errors:
        return None, {'audience_account_numbers': audience_errors}
    if visibility == Story.VISIBILITY_SPECIFIC_CONTACTS and not audience_account_numbers:
        return None, {
            'audience_account_numbers': [
                'At least one audience account number is required for specific contact stories.',
            ],
        }
    if visibility == Story.VISIBILITY_ALL_CONTACTS:
        audience_account_numbers = []

    upload_intent_ids, upload_intent_errors = normalize_story_upload_intent_ids(
        payload.get('encrypted_upload_intent_ids'),
    )
    if upload_intent_errors:
        return None, {'encrypted_upload_intent_ids': upload_intent_errors}
    if story_type == Story.STORY_TYPE_MEDIA and not upload_intent_ids:
        return None, {
            'encrypted_upload_intent_ids': [
                'At least one completed encrypted story upload intent is required.',
            ],
        }
    if story_type == Story.STORY_TYPE_TEXT and upload_intent_ids:
        return None, {
            'encrypted_upload_intent_ids': [
                'Text stories cannot include encrypted media upload intents.',
            ],
        }

    encrypted_payload = normalize_string(payload.get('encrypted_payload'))
    if story_type == Story.STORY_TYPE_TEXT:
        if not encrypted_payload:
            return None, {
                'encrypted_payload': ['Text story payload is required.'],
            }
        if len(encrypted_payload) > STORY_TEXT_PAYLOAD_MAX_LENGTH:
            return None, {
                'encrypted_payload': ['Text story payload is too large.'],
            }

    return {
        'client_story_id': client_story_id,
        'story_type': story_type,
        'expiry_hours': expiry_hours,
        'visibility': visibility,
        'audience_account_numbers': audience_account_numbers,
        'encrypted_upload_intent_ids': upload_intent_ids,
        'encrypted_payload': encrypted_payload,
    }, None


def normalize_story_settings_payload(payload, current_settings=None):
    if not isinstance(payload, dict):
        return None, {'body': ['Request body must be a JSON object.']}

    if isinstance(current_settings, dict):
        current_expiry_hours = current_settings.get('expiry_hours')
        current_visibility = current_settings.get('visibility')
        current_audience = current_settings.get('audience_account_numbers')
    elif current_settings is not None:
        current_expiry_hours = current_settings.expiry_hours
        current_visibility = current_settings.visibility
        current_audience = current_settings.audience_account_numbers
    else:
        current_expiry_hours = Story.EXPIRY_24_HOURS
        current_visibility = Story.VISIBILITY_ALL_CONTACTS
        current_audience = []

    expiry_hours = (
        normalize_positive_int(payload.get('expiry_hours'))
        or normalize_positive_int(current_expiry_hours)
        or Story.EXPIRY_24_HOURS
    )
    if expiry_hours not in STORY_ALLOWED_EXPIRY_HOURS:
        return None, {'expiry_hours': ['Story expiry must be 6, 12, or 24 hours.']}

    visibility = (
        normalize_string(payload.get('visibility'))
        or normalize_string(current_visibility)
        or Story.VISIBILITY_ALL_CONTACTS
    )
    if visibility not in dict(Story.VISIBILITY_CHOICES):
        return None, {'visibility': ['Story visibility must be all_contacts or specific_contacts.']}

    audience_source = (
        payload.get('audience_account_numbers')
        if 'audience_account_numbers' in payload
        else current_audience
    )
    audience_account_numbers, audience_errors = normalize_audience_account_numbers(
        audience_source,
    )
    if audience_errors:
        return None, {'audience_account_numbers': audience_errors}
    if visibility == Story.VISIBILITY_SPECIFIC_CONTACTS and not audience_account_numbers:
        return None, {
            'audience_account_numbers': [
                'At least one audience account number is required for specific contact stories.',
            ],
        }
    if visibility == Story.VISIBILITY_ALL_CONTACTS:
        audience_account_numbers = []

    return {
        'expiry_hours': expiry_hours,
        'visibility': visibility,
        'audience_account_numbers': audience_account_numbers,
    }, None


def normalize_settings_audience_list(value):
    audience_account_numbers, audience_errors = normalize_audience_account_numbers(value)
    return [] if audience_errors else audience_account_numbers


def normalize_audience_account_numbers(value):
    if value in (None, ''):
        return [], None

    if not isinstance(value, list):
        return [], ['Audience account numbers must be a list.']

    normalized_accounts = []
    seen_accounts = set()
    errors = []

    for index, item in enumerate(value):
        account_number = normalize_string(item)
        if not account_number:
            errors.append({index: 'Audience account number is required.'})
            continue

        if account_number in seen_accounts:
            errors.append({index: 'Audience account number is duplicated.'})
            continue

        seen_accounts.add(account_number)
        normalized_accounts.append(account_number)

    return normalized_accounts, errors or None


def normalize_story_upload_intent_ids(value):
    if value in (None, ''):
        return [], None

    if not isinstance(value, list):
        return [], ['Encrypted upload intent ids must be a list.']

    if len(value) > MAX_STORY_MEDIA_UPLOAD_INTENTS_PER_REQUEST:
        return [], [
            'Stories support one encrypted media upload intent only.',
        ]

    normalized_ids = []
    seen_ids = set()
    errors = []

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


def validate_story_parent_audience(payload, parent_audience):
    if not isinstance(parent_audience, dict) or parent_audience.get('allowed') is not True:
        return None, {'audience': ['Story audience is not authorized.']}

    valid_contacts = parent_audience.get('valid_contacts')
    if not isinstance(valid_contacts, list):
        valid_contacts = []

    if payload['visibility'] == Story.VISIBILITY_SPECIFIC_CONTACTS:
        if parent_audience.get('missing_account_numbers'):
            return None, {
                'audience_account_numbers': [
                    'One or more selected contacts are not saved contacts.',
                ],
            }

        if parent_audience.get('excluded_contacts'):
            return None, {
                'audience_account_numbers': [
                    'One or more selected contacts cannot view this story because of block rules.',
                ],
            }

        if not valid_contacts:
            return None, {
                'audience_account_numbers': [
                    'At least one valid contact is required for specific contact stories.',
                ],
            }

    normalized_contacts = []
    seen_user_ids = set()
    for contact in valid_contacts:
        if not isinstance(contact, dict):
            continue

        user_id = normalize_positive_int(contact.get('user_id'))
        account_number = normalize_string(contact.get('account_number'))
        if not user_id or not account_number or user_id in seen_user_ids:
            continue

        seen_user_ids.add(user_id)
        normalized_contacts.append(
            {
                'user_id': user_id,
                'account_number': account_number,
            }
        )

    return normalized_contacts, None


def validate_completed_story_upload_intents(sender, payload):
    upload_intent_ids = payload['encrypted_upload_intent_ids']
    intents = list(
        StoryUploadIntent.objects.select_for_update().filter(id__in=upload_intent_ids)
    )
    intents_by_id = {str(intent.id): intent for intent in intents}
    now = timezone.now()
    ordered_intents = []
    errors = []

    for index, upload_intent_id in enumerate(upload_intent_ids):
        intent = intents_by_id.get(upload_intent_id)
        if not intent:
            errors.append({index: 'Upload intent was not found.'})
            continue

        item_errors = {}
        if intent.owner_user_id != sender['user_id']:
            item_errors['owner'] = 'Upload intent does not belong to this user.'
        if intent.client_story_id != payload['client_story_id']:
            item_errors['client_story_id'] = 'Upload intent does not match this story.'
        if intent.status != StoryUploadIntent.STATUS_COMPLETED:
            item_errors['status'] = 'Upload intent has not been completed.'
        if intent.expires_at <= now:
            item_errors['expires_at'] = 'Upload intent has expired.'
        if not intent.secure_url:
            item_errors['secure_url'] = 'Upload intent is missing its encrypted file URL.'

        if item_errors:
            errors.append({index: item_errors})
            continue

        ordered_intents.append(intent)

    return ordered_intents, errors or None


def normalize_story_upload_intent_payload(payload):
    if not isinstance(payload, dict):
        return None, {'body': ['Request body must be a JSON object.']}

    client_story_id = normalize_string(payload.get('client_story_id'))
    if not client_story_id:
        return None, {'client_story_id': ['Client story id is required.']}
    if len(client_story_id) > 120:
        return None, {'client_story_id': ['Client story id cannot exceed 120 characters.']}

    media = payload.get('media')
    if media is None:
        media = payload.get('attachments')

    if not isinstance(media, list) or not media:
        return None, {'media': ['At least one image or video is required.']}

    if len(media) > MAX_STORY_MEDIA_UPLOAD_INTENTS_PER_REQUEST:
        return None, {
            'media': [
                'Stories support one image or video only.',
            ],
        }

    normalized_media = []
    errors = []

    for index, media_item in enumerate(media):
        normalized_item, item_errors = normalize_story_media_upload_item(
            media_item,
            index,
        )
        if item_errors:
            errors.append({index: item_errors})
            continue

        normalized_media.append(normalized_item)

    if errors:
        return None, {'media': errors}

    return {
        'client_story_id': client_story_id,
        'media': normalized_media,
    }, None


def normalize_story_media_upload_item(media_item, index):
    if not isinstance(media_item, dict):
        return None, 'Media item must be an object.'

    max_size = int(
        getattr(
            settings,
            'STORIES_MAX_ENCRYPTED_UPLOAD_FILE_SIZE_BYTES',
            MAX_ENCRYPTED_FILE_SIZE_BYTES,
        )
        or MAX_ENCRYPTED_FILE_SIZE_BYTES
    )
    file_name = normalize_string(media_item.get('file_name')) or f'story-media-{index + 1}'
    mime_type = normalize_string(media_item.get('mime_type')).lower()
    media_type = normalize_story_media_type(media_item.get('media_type'), mime_type)
    media_client_id = normalize_string(media_item.get('id'))[:255]
    file_size_bytes = normalize_positive_int(media_item.get('file_size_bytes'))
    encrypted_file_size_bytes = normalize_positive_int(
        media_item.get('encrypted_file_size_bytes'),
    )
    sort_order = normalize_non_negative_int(media_item.get('sort_order'))
    errors = {}

    if not media_type:
        errors['media_type'] = 'Stories support image and video media only.'
    elif mime_type and not mime_type.startswith(STORY_MEDIA_TYPE_PREFIXES[media_type]):
        errors['mime_type'] = f'{media_type.title()} stories must use a {media_type} MIME type.'

    if not mime_type:
        errors['mime_type'] = 'Story media MIME type is required.'
    if not file_size_bytes:
        errors['file_size_bytes'] = 'Story media file is empty.'
    if not encrypted_file_size_bytes:
        errors['encrypted_file_size_bytes'] = 'Encrypted story media file is empty.'
    elif encrypted_file_size_bytes > max_size:
        errors['encrypted_file_size_bytes'] = (
            f'Encrypted story media cannot exceed {max_size // (1024 * 1024)} MB.'
        )

    if len(file_name) > 255:
        file_name = file_name[:255]
    if len(mime_type) > 120:
        mime_type = mime_type[:120]

    if errors:
        return None, errors

    return {
        'media_client_id': media_client_id,
        'media_type': media_type,
        'file_name': file_name,
        'mime_type': mime_type,
        'file_size_bytes': file_size_bytes,
        'encrypted_file_size_bytes': encrypted_file_size_bytes,
        'sort_order': sort_order if sort_order is not None else index,
    }, None


def normalize_story_media_type(value, mime_type):
    media_type = normalize_string(value).lower()
    if media_type in STORY_MEDIA_TYPE_PREFIXES:
        return media_type

    if mime_type.startswith('image/'):
        return STORY_MEDIA_IMAGE

    if mime_type.startswith('video/'):
        return STORY_MEDIA_VIDEO

    return ''


def cleanup_expired_story_upload_intents(limit=25):
    now = timezone.now()
    expired_intents = list(
        StoryUploadIntent.objects.filter(
            expires_at__lt=now,
            status__in=[
                StoryUploadIntent.STATUS_ISSUED,
                StoryUploadIntent.STATUS_COMPLETED,
            ],
        ).order_by('expires_at', 'created_at')[:limit]
    )

    for intent in expired_intents:
        expire_story_upload_intent(
            intent,
            cleanup_cloudinary=(
                intent.status == StoryUploadIntent.STATUS_COMPLETED
            ),
        )


def expire_story_upload_intent(intent, cleanup_cloudinary=True):
    if cleanup_cloudinary and intent.cloudinary_public_id:
        try:
            cloudinary_uploader.destroy(
                intent.cloudinary_public_id,
                resource_type=intent.cloudinary_resource_type or 'raw',
            )
        except CloudinaryError:
            pass

    intent.status = StoryUploadIntent.STATUS_EXPIRED
    intent.save(update_fields=['status', 'updated_at'])


def cleanup_stories(retention_days=None, media_limit=None, dry_run=False, now=None):
    now = now or timezone.now()
    expired_story_candidates = get_expired_story_queryset(now).count()
    expired_stories = 0

    if not dry_run:
        expired_stories = mark_expired_stories(now=now)

    media_result = cleanup_expired_story_media(
        retention_days=retention_days,
        limit=media_limit,
        dry_run=dry_run,
        now=now,
    )

    return {
        'expired_story_candidates': expired_story_candidates,
        'expired_stories': expired_stories,
        **media_result,
    }


def mark_expired_stories(now=None, limit=None):
    now = now or timezone.now()
    queryset = get_expired_story_queryset(now)
    normalized_limit = normalize_non_negative_int(limit)

    if normalized_limit is not None:
        if normalized_limit <= 0:
            return 0

        story_ids = list(
            queryset.order_by('expires_at', 'created_at')
            .values_list('id', flat=True)[:normalized_limit]
        )
        if not story_ids:
            return 0

        queryset = Story.objects.filter(id__in=story_ids)

    return queryset.update(status=Story.STATUS_EXPIRED, updated_at=now)


def get_expired_story_queryset(now):
    return Story.objects.filter(
        status=Story.STATUS_ACTIVE,
        expires_at__lte=now,
    )


def cleanup_expired_story_media(retention_days=None, limit=None, dry_run=False, now=None):
    now = now or timezone.now()
    retention_days = get_expired_story_media_retention_days(retention_days)
    cutoff = now - timedelta(days=retention_days)
    normalized_limit = get_expired_story_media_cleanup_limit(limit)

    if normalized_limit <= 0:
        return {
            'media_candidates': 0,
            'media_cleaned': 0,
            'cloudinary_errors': [],
        }

    media_queryset = (
        StoryMedia.objects.filter(story__expires_at__lte=cutoff)
        .exclude(encrypted_file_url='')
        .order_by('story__expires_at', 'created_at', 'id')
    )
    media_items = list(media_queryset[:normalized_limit])

    if dry_run:
        return {
            'media_candidates': len(media_items),
            'media_cleaned': 0,
            'cloudinary_errors': [],
        }

    cleaned_media_ids = []
    cloudinary_errors = []
    for media in media_items:
        if media.cloudinary_public_id:
            try:
                cloudinary_uploader.destroy(
                    media.cloudinary_public_id,
                    resource_type=media.cloudinary_resource_type or 'raw',
                )
            except CloudinaryError as error:
                cloudinary_errors.append(
                    {
                        'media_id': media.id,
                        'cloudinary_public_id': media.cloudinary_public_id,
                        'message': str(error),
                    }
                )
                continue

        cleaned_media_ids.append(media.id)

    if cleaned_media_ids:
        StoryMedia.objects.filter(id__in=cleaned_media_ids).update(
            encrypted_file_url='',
            thumbnail_url='',
            cloudinary_public_id='',
            cloudinary_asset_id='',
            cloudinary_resource_type='',
            cloudinary_folder='',
        )

    return {
        'media_candidates': len(media_items),
        'media_cleaned': len(cleaned_media_ids),
        'cloudinary_errors': cloudinary_errors,
    }


def get_expired_story_media_retention_days(retention_days=None):
    if retention_days is None:
        retention_days = getattr(
            settings,
            'STORIES_EXPIRED_MEDIA_RETENTION_DAYS',
            DEFAULT_EXPIRED_STORY_MEDIA_RETENTION_DAYS,
        )

    normalized_retention_days = normalize_non_negative_int(retention_days)
    if normalized_retention_days is None:
        return DEFAULT_EXPIRED_STORY_MEDIA_RETENTION_DAYS

    return normalized_retention_days


def get_expired_story_media_cleanup_limit(limit=None):
    if limit is None:
        limit = getattr(
            settings,
            'STORIES_EXPIRED_MEDIA_CLEANUP_LIMIT',
            DEFAULT_EXPIRED_STORY_MEDIA_CLEANUP_LIMIT,
        )

    normalized_limit = normalize_non_negative_int(limit)
    if normalized_limit is None:
        return DEFAULT_EXPIRED_STORY_MEDIA_CLEANUP_LIMIT

    return normalized_limit


def serialize_completed_story_upload_intent(intent):
    return {
        'upload_intent_id': str(intent.id),
        'client_story_id': intent.client_story_id,
        'media_id': intent.media_client_id,
        'media_index': intent.media_index,
        'media_type': intent.media_type,
        'file_name': intent.original_file_name,
        'mime_type': intent.original_mime_type,
        'file_size_bytes': intent.original_file_size_bytes,
        'encrypted_file_url': intent.secure_url,
        'encrypted_file_size_bytes': intent.encrypted_file_size_bytes,
        'cloudinary_public_id': intent.cloudinary_public_id,
        'cloudinary_asset_id': intent.cloudinary_asset_id,
        'cloudinary_resource_type': intent.cloudinary_resource_type,
        'cloudinary_folder': intent.cloudinary_folder,
    }


def serialize_story(story, viewed_story_ids=None, include_audience=False):
    viewed_story_ids = viewed_story_ids or set()
    result = {
        'id': str(story.id),
        'client_story_id': story.client_story_id,
        'owner_user_id': story.owner_user_id,
        'owner_account_number': story.owner_account_number,
        'story_type': story.story_type,
        'visibility': story.visibility,
        'expiry_hours': story.expiry_hours,
        'encrypted_payload': story.encrypted_payload,
        'status': story.status,
        'expires_at': story.expires_at.isoformat(),
        'deleted_at': story.deleted_at.isoformat() if story.deleted_at else None,
        'created_at': story.created_at.isoformat(),
        'updated_at': story.updated_at.isoformat(),
        'viewed': story.id in viewed_story_ids,
        'media': [
            serialize_story_media(media)
            for media in story.media.all()
        ],
    }

    if include_audience:
        result['audience_count'] = story.audience.count()

    return result


def serialize_my_story(story, now=None):
    now = now or timezone.now()
    result = serialize_story(story)
    result['view_count'] = int(getattr(story, 'view_count', 0) or 0)
    result['expires_in_seconds'] = max(0, int((story.expires_at - now).total_seconds()))
    result['media_preview'] = [
        serialize_story_media_preview(media)
        for media in story.media.all()
    ]

    return result


def serialize_story_media(media):
    return {
        'id': media.id,
        'media_type': media.media_type,
        'encrypted_file_url': media.encrypted_file_url,
        'thumbnail_url': media.thumbnail_url,
        'file_name': media.file_name,
        'mime_type': media.mime_type,
        'file_size_bytes': media.file_size_bytes,
        'encrypted_file_size_bytes': media.encrypted_file_size_bytes,
        'width': media.width,
        'height': media.height,
        'duration_seconds': media.duration_seconds,
        'cloudinary_public_id': media.cloudinary_public_id,
        'cloudinary_asset_id': media.cloudinary_asset_id,
        'cloudinary_resource_type': media.cloudinary_resource_type,
        'cloudinary_folder': media.cloudinary_folder,
        'sort_order': media.sort_order,
    }


def serialize_story_media_preview(media):
    return {
        'id': media.id,
        'media_type': media.media_type,
        'encrypted_file_url': media.encrypted_file_url,
        'thumbnail_url': media.thumbnail_url,
        'file_name': media.file_name,
        'mime_type': media.mime_type,
        'file_size_bytes': media.file_size_bytes,
        'encrypted_file_size_bytes': media.encrypted_file_size_bytes,
        'width': media.width,
        'height': media.height,
        'duration_seconds': media.duration_seconds,
        'sort_order': media.sort_order,
    }


def serialize_story_settings(settings_row):
    return {
        'owner_user_id': settings_row.owner_user_id,
        'owner_account_number': settings_row.owner_account_number,
        'expiry_hours': settings_row.expiry_hours,
        'visibility': settings_row.visibility,
        'audience_account_numbers': normalize_settings_audience_list(
            settings_row.audience_account_numbers,
        ),
        'created_at': settings_row.created_at.isoformat(),
        'updated_at': settings_row.updated_at.isoformat(),
    }


def serialize_story_viewer(viewer):
    return {
        'user_id': viewer.viewer_user_id,
        'account_number': viewer.viewer_account_number,
        'viewed_at': viewer.viewed_at.isoformat(),
    }


def serialize_story_reaction(story_reaction):
    return {
        'id': story_reaction.id,
        'story_id': str(story_reaction.story_id),
        'viewer_user_id': story_reaction.viewer_user_id,
        'viewer_account_number': story_reaction.viewer_account_number,
        'reaction': story_reaction.reaction,
        'message_id': story_reaction.message_id,
        'created_at': story_reaction.created_at.isoformat(),
        'updated_at': story_reaction.updated_at.isoformat(),
    }


def serialize_story_reply(story_reply):
    return {
        'id': story_reply.id,
        'story_id': str(story_reply.story_id),
        'viewer_user_id': story_reply.viewer_user_id,
        'viewer_account_number': story_reply.viewer_account_number,
        'message_id': story_reply.message_id,
        'created_at': story_reply.created_at.isoformat(),
    }


def serialize_story_feed_contact(story, parent_policy):
    viewer_contact = parent_policy.get('viewer_contact')
    if not isinstance(viewer_contact, dict):
        viewer_contact = {}

    return {
        'user_id': story.owner_user_id,
        'account_number': story.owner_account_number,
        'alias_name': viewer_contact.get('alias_name'),
        'profile_picture': viewer_contact.get('profile_picture'),
    }
