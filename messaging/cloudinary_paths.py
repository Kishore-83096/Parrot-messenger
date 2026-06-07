import re

from django.conf import settings


DEFAULT_CLOUDINARY_ROOT_FOLDER = 'Parrot'
DIRECT_MESSAGES_FOLDER = 'direct messages'
GROUP_MESSAGES_FOLDER = 'groupmessages'
GROUP_AVATARS_FOLDER = 'groups-dp'
STORIES_FOLDER = 'stories'


def get_cloudinary_root_folder():
    root_folder = (
        getattr(settings, 'CLOUDINARY_MAIN_FOLDER', DEFAULT_CLOUDINARY_ROOT_FOLDER).strip('/')
        or DEFAULT_CLOUDINARY_ROOT_FOLDER
    )
    return normalize_cloudinary_path_segment(root_folder, DEFAULT_CLOUDINARY_ROOT_FOLDER)


def build_user_cloudinary_segment(username='', account_number='', user_id=None):
    name_segment = normalize_cloudinary_path_segment(username, '')
    account_segment = normalize_cloudinary_path_segment(account_number, '')

    if name_segment and account_segment:
        return f'{name_segment}-{account_segment}'
    if account_segment:
        return account_segment
    if user_id:
        return f'user-{user_id}'

    return 'unknown-user'


def build_named_account_cloudinary_segment(name='', account_number='', fallback_name='contact'):
    name_segment = normalize_cloudinary_path_segment(name, fallback_name)
    account_segment = normalize_cloudinary_path_segment(account_number, '')

    if account_segment:
        return f'{name_segment}-{account_segment}'

    return name_segment


def build_sender_cloudinary_folder(sender, account_number=''):
    return '/'.join(
        [
            get_cloudinary_root_folder(),
            build_user_cloudinary_segment(
                username=sender.get('username'),
                account_number=sender.get('account_number') or account_number,
                user_id=sender.get('user_id'),
            ),
        ]
    )


def build_direct_message_cloudinary_folder(sender, parent_authorization):
    contact = parent_authorization.get('contact')
    if not isinstance(contact, dict):
        contact = {}

    recipient_name = (
        contact.get('display_name')
        or contact.get('alias_name')
        or parent_authorization.get('recipient_username')
        or parent_authorization.get('recipient_account_number')
    )
    recipient_segment = build_named_account_cloudinary_segment(
        recipient_name,
        parent_authorization.get('recipient_account_number'),
        fallback_name='receiver',
    )

    return '/'.join(
        [
            build_sender_cloudinary_folder(
                sender,
                account_number=parent_authorization.get('sender_account_number'),
            ),
            DIRECT_MESSAGES_FOLDER,
            recipient_segment,
        ]
    )


def build_group_message_cloudinary_folder(sender, room, participant=None):
    group_segment = normalize_cloudinary_path_segment(
        getattr(room, 'title', ''),
        f'group-{getattr(room, "id", "") or "room"}',
    )
    account_number = getattr(participant, 'account_number', '') if participant else ''

    return '/'.join(
        [
            build_sender_cloudinary_folder(sender, account_number=account_number),
            GROUP_MESSAGES_FOLDER,
            group_segment,
        ]
    )


def build_group_avatar_cloudinary_folder(sender, room):
    group_segment = normalize_cloudinary_path_segment(
        getattr(room, 'title', ''),
        f'group-{getattr(room, "id", "") or "room"}',
    )
    return '/'.join(
        [
            build_sender_cloudinary_folder(sender),
            GROUP_AVATARS_FOLDER,
            group_segment,
        ]
    )


def build_story_cloudinary_folder(sender, owner_account_number=''):
    return '/'.join(
        [
            build_sender_cloudinary_folder(sender, account_number=owner_account_number),
            STORIES_FOLDER,
        ]
    )


def normalize_cloudinary_path_segment(value, fallback):
    normalized = str(value or '').strip().replace('/', '-').replace('\\', '-')
    normalized = re.sub(r'\s+', ' ', normalized)
    normalized = re.sub(r'[^A-Za-z0-9._ -]+', '-', normalized)
    normalized = re.sub(r'-{2,}', '-', normalized).strip(' .-')
    return (normalized or fallback)[:120]
