import base64
import json
import time
import uuid
from datetime import timedelta
from unittest.mock import patch

import jwt
from asgiref.sync import sync_to_async
from channels.testing import WebsocketCommunicator
from cloudinary import config as cloudinary_config
from cloudinary.utils import api_sign_request
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from django.conf import settings
from django.contrib.auth.hashers import check_password
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from messenger_service.asgi import application

from .models import (
    Message,
    MessageAttachment,
    MessageEncryptedUploadIntent,
    MessageReaction,
    Room,
    RoomParticipant,
    UserDeviceDefaultCredential,
    UserDeviceKey,
    UserE2EEKeyBackup,
)
from .cache import get_cached_messaging_authorization, invalidate_room_messages_cache
from .e2ee.devices.service import build_action_message
from .realtime import broadcast_room_event, broadcast_user_event
from .services import (
    create_direct_message,
    edit_direct_message,
    get_room_unread_count,
    list_room_messages,
    list_user_rooms,
    mark_room_delivered,
    mark_room_read,
    release_room_blocked_messages,
    set_direct_room_list_hidden,
)


TEST_JWT_SETTINGS = {
    'MESSAGING_JWT_SECRET': 'test-messenger-secret-at-least-32-bytes',
    'MESSAGING_JWT_ISSUER': 'parrot-parent',
    'MESSAGING_JWT_AUDIENCE': 'parrot-messenger',
    'INTERNAL_SERVICE_TOKEN': 'test-internal-service-token',
    'CLOUDINARY_URL': 'cloudinary://test-key:test-secret@test-cloud',
    'CLOUDINARY_MAIN_FOLDER': 'MAIN',
    'CACHES': {
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'parrot-messenger-tests',
        },
    },
}


def test_public_key(byte_value=b'a'):
    return base64.b64encode(byte_value * 32).decode('ascii')


def test_base64_bytes(byte_value=b'c', length=32):
    return base64.b64encode(byte_value * length).decode('ascii')


@override_settings(**TEST_JWT_SETTINGS)
class CryptoDeviceKeyTests(TestCase):
    sender_user_id = 1
    recipient_user_id = 2
    sender_username = 'sender'
    sender_account_number = '7000000001'
    recipient_account_number = '7000000002'

    def setUp(self):
        cache.clear()

    def auth_header(self, user_id=None, account_number=None):
        now = timezone.now()
        token = jwt.encode(
            {
                'sub': str(user_id or self.sender_user_id),
                'user_id': user_id or self.sender_user_id,
                'username': self.sender_username,
                'account_number': account_number or self.sender_account_number,
                'iss': settings.MESSAGING_JWT_ISSUER,
                'aud': settings.MESSAGING_JWT_AUDIENCE,
                'iat': now,
                'exp': now + timedelta(minutes=5),
            },
            settings.MESSAGING_JWT_SECRET,
            algorithm='HS256',
        )

        return f'Bearer {token}'

    def create_direct_room(self):
        room = Room.objects.create(
            room_type=Room.TYPE_DIRECT,
            created_by_user_id=self.sender_user_id,
        )
        RoomParticipant.objects.create(
            room=room,
            user_id=self.sender_user_id,
            account_number=self.sender_account_number,
            is_active=True,
        )
        RoomParticipant.objects.create(
            room=room,
            user_id=self.recipient_user_id,
            account_number=self.recipient_account_number,
            is_active=True,
        )

        return room

    def parent_allowed(self):
        return {
            'ok': True,
            'parent': {
                'response': {
                    'allowed': True,
                    'sender_user_id': self.sender_user_id,
                    'sender_account_number': self.sender_account_number,
                    'recipient_user_id': self.recipient_user_id,
                    'recipient_account_number': self.recipient_account_number,
                    'delivery_blocked': False,
                },
                'status_code': 200,
            },
        }, 200

    def parent_denial(self):
        return {
            'ok': False,
            'parent': {
                'response': {
                    'allowed': False,
                    'reason': 'contact_not_saved',
                    'message': 'contact_not_saved',
                },
                'status_code': 403,
            },
        }, 403

    def post_device_key(self, payload, user_id=None):
        return self.client.post(
            '/crypto/devices/',
            data=json.dumps(payload),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(user_id=user_id),
        )

    def create_management_private_key(self):
        return Ed25519PrivateKey.generate()

    def management_public_key(self, private_key):
        return base64.b64encode(
            private_key.public_key().public_bytes(
                encoding=Encoding.Raw,
                format=PublicFormat.Raw,
            )
        ).decode('ascii')

    def signed_payload(self, action, acting_device_id, target_device_id, private_key):
        timestamp = int(time.time())
        nonce = f'test-{uuid.uuid4()}'
        message = build_action_message(
            self.sender_user_id,
            action,
            acting_device_id,
            target_device_id,
            timestamp,
            nonce,
        )
        signature = private_key.sign(message)

        return {
            'acting_device_id': acting_device_id,
            'action_timestamp': timestamp,
            'action_nonce': nonce,
            'action_signature': base64.b64encode(signature).decode('ascii'),
        }

    def signed_default_payload(
        self,
        acting_device_id,
        target_device_id,
        private_key,
        default_password='default-device-password',
    ):
        return {
            **self.signed_payload(
                'device.default',
                acting_device_id,
                target_device_id,
                private_key,
            ),
            'default_password': default_password,
        }

    def signed_update_default_password_payload(
        self,
        acting_device_id,
        private_key,
        current_password='default-device-password',
        new_password='updated-default-password',
    ):
        return {
            **self.signed_payload(
                'device.default_password.update',
                acting_device_id,
                'default-password',
                private_key,
            ),
            'current_default_password': current_password,
            'new_default_password': new_password,
        }

    def registration_payload(self, device_id, public_key=None, device_name=''):
        private_key = self.create_management_private_key()

        return {
            'device_id': device_id,
            'device_name': device_name,
            'public_key': public_key or test_public_key(),
            'management_public_key': self.management_public_key(private_key),
        }

    def create_device(self, device_id, public_key=None, is_default=False, user_id=None):
        private_key = self.create_management_private_key()
        encryption_public_key = public_key or test_public_key()
        UserDeviceKey.objects.create(
            user_id=user_id or self.sender_user_id,
            device_id=device_id,
            public_key=encryption_public_key,
            encryption_public_key=encryption_public_key,
            management_public_key=self.management_public_key(private_key),
            is_default=is_default,
        )

        return private_key

    def key_backup_payload(self):
        return {
            'public_key': test_public_key(),
            'encrypted_private_key': test_base64_bytes(b'p', 48),
            'salt': test_base64_bytes(b's', 16),
            'nonce': test_base64_bytes(b'n', 24),
            'kdf_algorithm': 'PBKDF2-SHA256',
            'kdf_iterations': 600000,
        }

    def signed_key_backup_payload(self, private_key, device_id, payload=None):
        return {
            **(payload or self.key_backup_payload()),
            **self.signed_payload(
                'recovery.backup.save',
                device_id,
                'key-backup',
                private_key,
            ),
        }

    def test_register_crypto_device_key(self):
        response = self.post_device_key(
            self.registration_payload(
                'browser-device-1',
                public_key=test_public_key(),
                device_name='Chrome on Windows',
            )
        )
        body = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(body['status'], 'ok')
        self.assertEqual(body['result']['device']['device_id'], 'browser-device-1')
        self.assertEqual(body['result']['device']['device_name'], 'Chrome on Windows')
        self.assertFalse(body['result']['device']['is_default'])
        self.assertEqual(UserDeviceKey.objects.count(), 1)
        self.assertEqual(UserDeviceKey.objects.get().user_id, self.sender_user_id)
        self.assertEqual(UserDeviceKey.objects.get().device_name, 'Chrome on Windows')
        self.assertFalse(UserDeviceKey.objects.get().is_default)

    def test_register_second_crypto_device_key_is_not_default(self):
        self.post_device_key(
            self.registration_payload('browser-device-1', public_key=test_public_key(b'a'))
        )
        response = self.post_device_key(
            self.registration_payload('browser-device-2', public_key=test_public_key(b'b'))
        )
        body = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertFalse(body['result']['device']['is_default'])
        self.assertFalse(
            UserDeviceKey.objects.get(device_id='browser-device-1').is_default
        )

    def test_register_crypto_device_key_does_not_choose_default_when_devices_have_no_default(self):
        self.create_device('browser-device-1', public_key=test_public_key(b'a'))

        response = self.post_device_key(
            self.registration_payload(
                'browser-device-1',
                public_key=test_public_key(b'b'),
                device_name='Firefox on Linux',
            )
        )
        body = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(body['result']['device']['device_name'], 'Firefox on Linux')
        self.assertFalse(body['result']['device']['is_default'])
        self.assertFalse(UserDeviceKey.objects.get(device_id='browser-device-1').is_default)

    def test_register_crypto_device_key_replaces_same_device(self):
        self.post_device_key(
            self.registration_payload('browser-device-1', public_key=test_public_key(b'a'))
        )
        response = self.post_device_key(
            self.registration_payload('browser-device-1', public_key=test_public_key(b'b'))
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(UserDeviceKey.objects.count(), 1)
        self.assertEqual(UserDeviceKey.objects.get().public_key, test_public_key(b'b'))

    def test_register_crypto_device_key_rejects_invalid_public_key(self):
        response = self.post_device_key(
            {
                'device_id': 'browser-device-1',
                'public_key': base64.b64encode(b'too-short').decode('ascii'),
                'management_public_key': test_base64_bytes(b'm', 32),
            }
        )
        body = response.json()

        self.assertEqual(response.status_code, 400)
        self.assertIn('encryption_public_key', body['result']['errors'])

    def test_default_device_can_delete_other_crypto_device_key(self):
        default_private_key = self.create_device(
            'browser-device-default',
            public_key=test_public_key(b'd'),
            is_default=True,
        )
        self.create_device('browser-device-1', public_key=test_public_key())

        response = self.client.post(
            '/crypto/devices/browser-device-1/revoke/',
            data=json.dumps(
                self.signed_payload(
                    'device.revoke',
                    'browser-device-default',
                    'browser-device-1',
                    default_private_key,
                )
            ),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )
        body = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(body['status'], 'ok')
        self.assertTrue(body['result']['revoked'])
        self.assertTrue(body['result']['deleted'])
        self.assertFalse(
            UserDeviceKey.objects.filter(
                user_id=self.sender_user_id,
                device_id='browser-device-1',
            ).exists()
        )

    def test_non_default_device_cannot_revoke_crypto_device_key(self):
        self.create_device(
            'browser-device-default',
            public_key=test_public_key(b'd'),
            is_default=True,
        )
        non_default_private_key = self.create_device(
            'browser-device-1',
            public_key=test_public_key(),
        )

        response = self.client.post(
            '/crypto/devices/browser-device-default/revoke/',
            data=json.dumps(
                self.signed_payload(
                    'device.revoke',
                    'browser-device-1',
                    'browser-device-default',
                    non_default_private_key,
                )
            ),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )

        self.assertEqual(response.status_code, 403)
        self.assertTrue(
            UserDeviceKey.objects.filter(
                user_id=self.sender_user_id,
                device_id='browser-device-default',
            ).exists()
        )

    def test_current_default_device_logout_retains_device_key(self):
        default_private_key = self.create_device(
            'browser-device-default',
            public_key=test_public_key(b'd'),
            is_default=True,
        )

        response = self.client.post(
            '/crypto/devices/browser-device-default/revoke/',
            data=json.dumps(
                self.signed_payload(
                    'device.revoke',
                    'browser-device-default',
                    'browser-device-default',
                    default_private_key,
                )
            ),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )
        body = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertFalse(body['result']['revoked'])
        self.assertTrue(body['result']['retained_default'])
        retained_device = UserDeviceKey.objects.get(
            user_id=self.sender_user_id,
            device_id='browser-device-default',
        )
        self.assertEqual(retained_device.status, UserDeviceKey.STATUS_ACTIVE)
        self.assertTrue(retained_device.is_default)

    def test_current_non_default_device_logout_deletes_device_key(self):
        self.create_device(
            'browser-device-default',
            public_key=test_public_key(b'd'),
            is_default=True,
        )
        non_default_private_key = self.create_device(
            'browser-device-1',
            public_key=test_public_key(),
        )

        response = self.client.post(
            '/crypto/devices/browser-device-1/revoke/',
            data=json.dumps(
                self.signed_payload(
                    'device.revoke',
                    'browser-device-1',
                    'browser-device-1',
                    non_default_private_key,
                )
            ),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )
        body = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(body['result']['revoked'])
        self.assertTrue(body['result']['deleted'])
        self.assertTrue(body['result']['local_device_should_clear'])
        self.assertFalse(
            UserDeviceKey.objects.filter(
                user_id=self.sender_user_id,
                device_id='browser-device-1',
            ).exists()
        )

    def test_revoke_crypto_device_key_requires_owner(self):
        default_private_key = self.create_device(
            'browser-device-default',
            public_key=test_public_key(b'd'),
            is_default=True,
        )
        self.create_device('browser-device-1', public_key=test_public_key(), user_id=99)

        response = self.client.post(
            '/crypto/devices/browser-device-1/revoke/',
            data=json.dumps(
                self.signed_payload(
                    'device.revoke',
                    'browser-device-default',
                    'browser-device-1',
                    default_private_key,
                )
            ),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )

        self.assertEqual(response.status_code, 404)
        self.assertTrue(
            UserDeviceKey.objects.filter(
                user_id=99,
                device_id='browser-device-1',
            ).exists()
        )

    def test_default_device_can_select_new_default_device(self):
        default_private_key = self.create_device(
            'browser-device-default',
            public_key=test_public_key(b'd'),
            is_default=True,
        )
        self.create_device('browser-device-2', public_key=test_public_key(b'b'))

        response = self.client.post(
            '/crypto/devices/browser-device-2/default/',
            data=json.dumps(
                self.signed_default_payload(
                    'browser-device-default',
                    'browser-device-2',
                    default_private_key,
                )
            ),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )
        body = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(body['status'], 'ok')
        self.assertTrue(body['result']['device']['is_default'])
        self.assertFalse(
            UserDeviceKey.objects.get(device_id='browser-device-default').is_default
        )
        self.assertTrue(
            UserDeviceKey.objects.get(device_id='browser-device-2').is_default
        )
        self.assertTrue(
            UserDeviceDefaultCredential.objects.filter(
                user_id=self.sender_user_id,
            ).exists()
        )

    def test_non_default_device_cannot_select_new_default_device(self):
        default_private_key = self.create_device(
            'browser-device-default',
            public_key=test_public_key(b'd'),
            is_default=True,
        )
        self.client.post(
            '/crypto/devices/browser-device-default/default/',
            data=json.dumps(
                self.signed_default_payload(
                    'browser-device-default',
                    'browser-device-default',
                    default_private_key,
                )
            ),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )
        non_default_private_key = self.create_device('browser-device-2', public_key=test_public_key(b'b'))

        response = self.client.post(
            '/crypto/devices/browser-device-default/default/',
            data=json.dumps(
                self.signed_default_payload(
                    'browser-device-2',
                    'browser-device-default',
                    non_default_private_key,
                )
            ),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )

        self.assertEqual(response.status_code, 403)
        self.assertTrue(
            UserDeviceKey.objects.get(device_id='browser-device-default').is_default
        )
        self.assertFalse(
            UserDeviceKey.objects.get(device_id='browser-device-2').is_default
        )

    def test_non_default_device_can_select_itself_as_default_with_password(self):
        default_private_key = self.create_device(
            'browser-device-default',
            public_key=test_public_key(b'd'),
            is_default=True,
        )
        self.client.post(
            '/crypto/devices/browser-device-default/default/',
            data=json.dumps(
                self.signed_default_payload(
                    'browser-device-default',
                    'browser-device-default',
                    default_private_key,
                )
            ),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )
        non_default_private_key = self.create_device('browser-device-2', public_key=test_public_key(b'b'))

        response = self.client.post(
            '/crypto/devices/browser-device-2/default/',
            data=json.dumps(
                self.signed_default_payload(
                    'browser-device-2',
                    'browser-device-2',
                    non_default_private_key,
                )
            ),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )
        body = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(body['status'], 'ok')
        self.assertFalse(
            UserDeviceKey.objects.get(device_id='browser-device-default').is_default
        )
        self.assertTrue(
            UserDeviceKey.objects.get(device_id='browser-device-2').is_default
        )

    def test_set_default_rejects_wrong_default_password(self):
        default_private_key = self.create_device(
            'browser-device-default',
            public_key=test_public_key(b'd'),
            is_default=True,
        )
        self.client.post(
            '/crypto/devices/browser-device-default/default/',
            data=json.dumps(
                self.signed_default_payload(
                    'browser-device-default',
                    'browser-device-default',
                    default_private_key,
                )
            ),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )
        non_default_private_key = self.create_device('browser-device-2', public_key=test_public_key(b'b'))

        response = self.client.post(
            '/crypto/devices/browser-device-2/default/',
            data=json.dumps(
                self.signed_default_payload(
                    'browser-device-2',
                    'browser-device-2',
                    non_default_private_key,
                    default_password='wrong-device-password',
                )
            ),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )

        self.assertEqual(response.status_code, 403)
        self.assertTrue(
            UserDeviceKey.objects.get(device_id='browser-device-default').is_default
        )
        self.assertFalse(
            UserDeviceKey.objects.get(device_id='browser-device-2').is_default
        )

    def test_set_default_rate_limits_wrong_default_password(self):
        default_private_key = self.create_device(
            'browser-device-default',
            public_key=test_public_key(b'd'),
            is_default=True,
        )
        self.client.post(
            '/crypto/devices/browser-device-default/default/',
            data=json.dumps(
                self.signed_default_payload(
                    'browser-device-default',
                    'browser-device-default',
                    default_private_key,
                )
            ),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )
        non_default_private_key = self.create_device('browser-device-2', public_key=test_public_key(b'b'))

        for attempt_number in range(5):
            response = self.client.post(
                '/crypto/devices/browser-device-2/default/',
                data=json.dumps(
                    self.signed_default_payload(
                        'browser-device-2',
                        'browser-device-2',
                        non_default_private_key,
                        default_password=f'wrong-device-password-{attempt_number}',
                    )
                ),
                content_type='application/json',
                HTTP_AUTHORIZATION=self.auth_header(),
            )

        self.assertEqual(response.status_code, 429)

        response = self.client.post(
            '/crypto/devices/browser-device-2/default/',
            data=json.dumps(
                self.signed_default_payload(
                    'browser-device-2',
                    'browser-device-2',
                    non_default_private_key,
                )
            ),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )

        self.assertEqual(response.status_code, 429)
        self.assertTrue(
            UserDeviceKey.objects.get(device_id='browser-device-default').is_default
        )
        self.assertFalse(
            UserDeviceKey.objects.get(device_id='browser-device-2').is_default
        )

    def test_default_device_can_update_default_password(self):
        default_private_key = self.create_device(
            'browser-device-default',
            public_key=test_public_key(b'd'),
            is_default=True,
        )
        self.client.post(
            '/crypto/devices/browser-device-default/default/',
            data=json.dumps(
                self.signed_default_payload(
                    'browser-device-default',
                    'browser-device-default',
                    default_private_key,
                )
            ),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )

        response = self.client.post(
            '/crypto/devices/default-password/',
            data=json.dumps(
                self.signed_update_default_password_payload(
                    'browser-device-default',
                    default_private_key,
                )
            ),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )
        credential = UserDeviceDefaultCredential.objects.get(
            user_id=self.sender_user_id,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(check_password('updated-default-password', credential.password_hash))

    def test_non_default_device_cannot_update_default_password(self):
        default_private_key = self.create_device(
            'browser-device-default',
            public_key=test_public_key(b'd'),
            is_default=True,
        )
        self.client.post(
            '/crypto/devices/browser-device-default/default/',
            data=json.dumps(
                self.signed_default_payload(
                    'browser-device-default',
                    'browser-device-default',
                    default_private_key,
                )
            ),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )
        non_default_private_key = self.create_device(
            'browser-device-2',
            public_key=test_public_key(b'b'),
        )

        response = self.client.post(
            '/crypto/devices/default-password/',
            data=json.dumps(
                self.signed_update_default_password_payload(
                    'browser-device-2',
                    non_default_private_key,
                )
            ),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )
        credential = UserDeviceDefaultCredential.objects.get(
            user_id=self.sender_user_id,
        )

        self.assertEqual(response.status_code, 403)
        self.assertTrue(check_password('default-device-password', credential.password_hash))

    def test_update_default_password_rejects_wrong_current_password(self):
        default_private_key = self.create_device(
            'browser-device-default',
            public_key=test_public_key(b'd'),
            is_default=True,
        )
        self.client.post(
            '/crypto/devices/browser-device-default/default/',
            data=json.dumps(
                self.signed_default_payload(
                    'browser-device-default',
                    'browser-device-default',
                    default_private_key,
                )
            ),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )

        response = self.client.post(
            '/crypto/devices/default-password/',
            data=json.dumps(
                self.signed_update_default_password_payload(
                    'browser-device-default',
                    default_private_key,
                    current_password='wrong-default-password',
                )
            ),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )
        credential = UserDeviceDefaultCredential.objects.get(
            user_id=self.sender_user_id,
        )

        self.assertEqual(response.status_code, 403)
        self.assertTrue(check_password('default-device-password', credential.password_hash))

    def test_set_default_requires_default_password(self):
        device_private_key = self.create_device('browser-device-1', public_key=test_public_key())

        response = self.client.post(
            '/crypto/devices/browser-device-1/default/',
            data=json.dumps(
                self.signed_payload(
                    'device.default',
                    'browser-device-1',
                    'browser-device-1',
                    device_private_key,
                )
            ),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )
        body = response.json()

        self.assertEqual(response.status_code, 400)
        self.assertIn('default_password', body['result']['errors'])
        self.assertFalse(
            UserDeviceKey.objects.get(device_id='browser-device-1').is_default
        )

    def test_existing_default_without_password_must_create_password_first(self):
        self.create_device(
            'browser-device-default',
            public_key=test_public_key(b'd'),
            is_default=True,
        )
        non_default_private_key = self.create_device('browser-device-2', public_key=test_public_key(b'b'))

        response = self.client.post(
            '/crypto/devices/browser-device-default/default/',
            data=json.dumps(
                self.signed_default_payload(
                    'browser-device-2',
                    'browser-device-default',
                    non_default_private_key,
                )
            ),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )

        self.assertEqual(response.status_code, 403)
        self.assertTrue(
            UserDeviceKey.objects.get(device_id='browser-device-default').is_default
        )
        self.assertFalse(
            UserDeviceKey.objects.get(device_id='browser-device-2').is_default
        )

    def test_recovered_non_default_key_can_select_itself_as_default_with_password(self):
        recovered_public_key = test_public_key(b'd')
        default_private_key = self.create_device(
            'browser-device-default',
            public_key=recovered_public_key,
            is_default=True,
        )
        self.client.post(
            '/crypto/devices/browser-device-default/default/',
            data=json.dumps(
                self.signed_default_payload(
                    'browser-device-default',
                    'browser-device-default',
                    default_private_key,
                )
            ),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )
        recovered_private_key = self.create_device(
            'browser-device-recovered',
            public_key=recovered_public_key,
        )

        response = self.client.post(
            '/crypto/devices/browser-device-recovered/default/',
            data=json.dumps(
                self.signed_default_payload(
                    'browser-device-recovered',
                    'browser-device-recovered',
                    recovered_private_key,
                )
            ),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            UserDeviceKey.objects.get(device_id='browser-device-default').is_default
        )
        self.assertTrue(
            UserDeviceKey.objects.get(device_id='browser-device-recovered').is_default
        )

    def test_recovered_default_key_cannot_select_another_device_as_default(self):
        recovered_public_key = test_public_key(b'd')
        default_private_key = self.create_device(
            'browser-device-default',
            public_key=recovered_public_key,
            is_default=True,
        )
        self.client.post(
            '/crypto/devices/browser-device-default/default/',
            data=json.dumps(
                self.signed_default_payload(
                    'browser-device-default',
                    'browser-device-default',
                    default_private_key,
                )
            ),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )
        recovered_private_key = self.create_device(
            'browser-device-recovered',
            public_key=recovered_public_key,
        )
        self.create_device('browser-device-2', public_key=test_public_key(b'b'))

        response = self.client.post(
            '/crypto/devices/browser-device-2/default/',
            data=json.dumps(
                self.signed_default_payload(
                    'browser-device-recovered',
                    'browser-device-2',
                    recovered_private_key,
                )
            ),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )

        self.assertEqual(response.status_code, 403)
        self.assertTrue(
            UserDeviceKey.objects.get(device_id='browser-device-default').is_default
        )
        self.assertFalse(
            UserDeviceKey.objects.get(device_id='browser-device-recovered').is_default
        )
        self.assertFalse(
            UserDeviceKey.objects.get(device_id='browser-device-2').is_default
        )

    def test_device_can_select_default_when_no_default_exists(self):
        device_private_key = self.create_device('browser-device-1', public_key=test_public_key())

        response = self.client.post(
            '/crypto/devices/browser-device-1/default/',
            data=json.dumps(
                self.signed_default_payload(
                    'browser-device-1',
                    'browser-device-1',
                    device_private_key,
                )
            ),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['result']['default_password_configured'])
        self.assertTrue(
            UserDeviceKey.objects.get(device_id='browser-device-1').is_default
        )
        self.assertTrue(
            UserDeviceDefaultCredential.objects.filter(
                user_id=self.sender_user_id,
            ).exists()
        )

    def test_device_cannot_select_other_default_when_no_default_exists(self):
        device_private_key = self.create_device('browser-device-1', public_key=test_public_key())
        self.create_device('browser-device-2', public_key=test_public_key(b'b'))

        response = self.client.post(
            '/crypto/devices/browser-device-2/default/',
            data=json.dumps(
                self.signed_default_payload(
                    'browser-device-1',
                    'browser-device-2',
                    device_private_key,
                )
            ),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            UserDeviceKey.objects.get(device_id='browser-device-1').is_default
        )
        self.assertFalse(
            UserDeviceKey.objects.get(device_id='browser-device-2').is_default
        )

    def test_list_own_crypto_device_keys(self):
        UserDeviceKey.objects.create(
            user_id=self.sender_user_id,
            device_id='browser-device-1',
            public_key=test_public_key(),
            is_default=True,
        )
        UserDeviceDefaultCredential.objects.create(
            user_id=self.sender_user_id,
            password_hash='test-hash',
        )
        response = self.client.get(
            f'/crypto/users/{self.sender_user_id}/devices/',
            HTTP_AUTHORIZATION=self.auth_header(),
        )
        body = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(body['result']['user_id'], self.sender_user_id)
        self.assertTrue(body['result']['default_password_configured'])
        self.assertEqual(body['result']['devices'][0]['device_id'], 'browser-device-1')
        self.assertTrue(body['result']['devices'][0]['is_default'])

    def test_list_shared_room_crypto_device_keys(self):
        self.create_direct_room()
        UserDeviceKey.objects.create(
            user_id=self.recipient_user_id,
            device_id='recipient-device-1',
            public_key=test_public_key(),
        )

        response = self.client.get(
            f'/crypto/users/{self.recipient_user_id}/devices/',
            HTTP_AUTHORIZATION=self.auth_header(),
        )
        body = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertNotIn('default_password_configured', body['result'])
        self.assertEqual(body['result']['devices'][0]['device_id'], 'recipient-device-1')

    def test_list_unrelated_crypto_device_keys_returns_not_found(self):
        UserDeviceKey.objects.create(
            user_id=99,
            device_id='other-device-1',
            public_key=test_public_key(),
        )

        response = self.client.get(
            '/crypto/users/99/devices/',
            HTTP_AUTHORIZATION=self.auth_header(),
        )

        self.assertEqual(response.status_code, 404)

    @patch('messaging.views.authorize_parent_messaging')
    def test_list_authorized_recipient_crypto_device_keys(self, authorize_parent_messaging):
        authorize_parent_messaging.return_value = self.parent_allowed()
        UserDeviceKey.objects.create(
            user_id=self.recipient_user_id,
            device_id='recipient-device-1',
            public_key=test_public_key(),
        )

        response = self.client.get(
            f'/crypto/recipients/{self.recipient_account_number}/devices/',
            HTTP_AUTHORIZATION=self.auth_header(),
        )
        body = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(body['result']['user_id'], self.recipient_user_id)
        self.assertEqual(body['result']['devices'][0]['device_id'], 'recipient-device-1')

    @patch('messaging.views.authorize_parent_messaging')
    def test_list_recipient_crypto_device_keys_requires_authorization(self, authorize_parent_messaging):
        authorize_parent_messaging.return_value = self.parent_denial()

        response = self.client.get(
            f'/crypto/recipients/{self.recipient_account_number}/devices/',
            HTTP_AUTHORIZATION=self.auth_header(),
        )

        self.assertEqual(response.status_code, 403)

    @patch('messaging.e2ee.files.cloudinary_uploader.upload')
    def test_upload_encrypted_attachment_blob(self, cloudinary_upload):
        cloudinary_upload.return_value = {
            'secure_url': 'https://res.cloudinary.com/demo/raw/upload/encrypted.txt',
            'bytes': 27,
            'public_id': 'MAIN/e2ee/encrypted',
            'resource_type': 'raw',
        }

        response = self.client.post(
            '/crypto/files/',
            data={
                'file': SimpleUploadedFile(
                    'encrypted.txt',
                    b'encrypted-ciphertext',
                    content_type='application/octet-stream',
                ),
            },
            HTTP_AUTHORIZATION=self.auth_header(),
        )
        body = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(body['status'], 'ok')
        self.assertEqual(
            body['result']['file']['encrypted_file_url'],
            'https://res.cloudinary.com/demo/raw/upload/encrypted.txt',
        )
        self.assertNotIn('file_name', body['result']['file'])
        self.assertEqual(MessageAttachment.objects.count(), 0)
        self.assertEqual(cloudinary_upload.call_args.kwargs['folder'], 'MAIN/sender-7000000001')
        self.assertEqual(cloudinary_upload.call_args.kwargs['resource_type'], 'raw')
        self.assertIs(cloudinary_upload.call_args.kwargs['use_filename'], False)

    def test_upload_encrypted_attachment_requires_file(self):
        response = self.client.post(
            '/crypto/files/',
            data={},
            HTTP_AUTHORIZATION=self.auth_header(),
        )

        self.assertEqual(response.status_code, 400)

    def test_save_and_get_crypto_key_backup(self):
        default_private_key = self.create_device(
            'browser-device-default',
            public_key=test_public_key(b'd'),
            is_default=True,
        )
        response = self.client.post(
            '/crypto/key-backup/',
            data=json.dumps(
                self.signed_key_backup_payload(
                    default_private_key,
                    'browser-device-default',
                )
            ),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )
        body = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(body['status'], 'ok')
        self.assertTrue(body['result']['exists'])
        self.assertEqual(UserE2EEKeyBackup.objects.count(), 1)
        self.assertEqual(UserE2EEKeyBackup.objects.get().user_id, self.sender_user_id)

        get_response = self.client.get(
            '/crypto/key-backup/',
            HTTP_AUTHORIZATION=self.auth_header(),
        )
        get_body = get_response.json()

        self.assertEqual(get_response.status_code, 200)
        self.assertTrue(get_body['result']['exists'])
        self.assertEqual(
            get_body['result']['backup']['encrypted_private_key'],
            self.key_backup_payload()['encrypted_private_key'],
        )

    @patch('messaging.views.broadcast_user_event')
    def test_save_crypto_key_backup_broadcasts_recovery_key_update(self, broadcast_user_event):
        default_private_key = self.create_device(
            'browser-device-default',
            public_key=test_public_key(b'd'),
            is_default=True,
        )

        response = self.client.post(
            '/crypto/key-backup/',
            data=json.dumps(
                self.signed_key_backup_payload(
                    default_private_key,
                    'browser-device-default',
                )
            ),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )
        body = response.json()

        self.assertEqual(response.status_code, 200)
        broadcast_user_event.assert_called_once_with(
            self.sender_user_id,
            'recovery.key_updated',
            {
                'backup_updated_at': body['result']['backup']['updated_at'],
                'updated_by_device_id': 'browser-device-default',
            },
        )

    def test_get_crypto_key_backup_without_backup(self):
        response = self.client.get(
            '/crypto/key-backup/',
            HTTP_AUTHORIZATION=self.auth_header(),
        )
        body = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertFalse(body['result']['exists'])
        self.assertIsNone(body['result']['backup'])

    def test_save_crypto_key_backup_rejects_invalid_payload(self):
        default_private_key = self.create_device(
            'browser-device-default',
            public_key=test_public_key(b'd'),
            is_default=True,
        )
        payload = self.key_backup_payload()
        payload['nonce'] = test_base64_bytes(b'n', 8)

        response = self.client.post(
            '/crypto/key-backup/',
            data=json.dumps(
                self.signed_key_backup_payload(
                    default_private_key,
                    'browser-device-default',
                    payload,
                )
            ),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )
        body = response.json()

        self.assertEqual(response.status_code, 400)
        self.assertIn('nonce', body['result']['errors'])

    def test_non_default_device_cannot_save_crypto_key_backup(self):
        self.create_device(
            'browser-device-default',
            public_key=test_public_key(b'd'),
            is_default=True,
        )
        non_default_private_key = self.create_device(
            'browser-device-2',
            public_key=test_public_key(b'b'),
        )

        response = self.client.post(
            '/crypto/key-backup/',
            data=json.dumps(
                self.signed_key_backup_payload(
                    non_default_private_key,
                    'browser-device-2',
                )
            ),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(UserE2EEKeyBackup.objects.count(), 0)


@override_settings(**TEST_JWT_SETTINGS)
class MessageSendAuthorizationTests(TestCase):
    sender_user_id = 1
    recipient_user_id = 2
    sender_username = 'sender'
    sender_account_number = '7000000001'
    recipient_account_number = '7000000002'

    def setUp(self):
        cache.clear()

    def auth_header(self, user_id=None, account_number=None):
        now = timezone.now()
        token = jwt.encode(
            {
                'sub': str(user_id or self.sender_user_id),
                'user_id': user_id or self.sender_user_id,
                'username': self.sender_username,
                'account_number': account_number or self.sender_account_number,
                'iss': settings.MESSAGING_JWT_ISSUER,
                'aud': settings.MESSAGING_JWT_AUDIENCE,
                'iat': now,
                'exp': now + timedelta(minutes=5),
            },
            settings.MESSAGING_JWT_SECRET,
            algorithm='HS256',
        )

        return f'Bearer {token}'

    def create_direct_room(self, sender_active=True, recipient_active=True):
        room = Room.objects.create(
            room_type=Room.TYPE_DIRECT,
            created_by_user_id=self.sender_user_id,
        )
        RoomParticipant.objects.create(
            room=room,
            user_id=self.sender_user_id,
            account_number=self.sender_account_number,
            is_active=sender_active,
        )
        RoomParticipant.objects.create(
            room=room,
            user_id=self.recipient_user_id,
            account_number=self.recipient_account_number,
            is_active=recipient_active,
        )

        return room

    def post_send_message(self, payload):
        return self.client.post(
            '/messages/send/',
            data=json.dumps(payload),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )

    def post_send_media_message(self, payload):
        return self.client.post(
            '/messages/send/',
            data=payload,
            HTTP_AUTHORIZATION=self.auth_header(),
        )

    def post_authorize_message(self, payload):
        return self.client.post(
            '/messages/authorize/',
            data=json.dumps(payload),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )

    def post_authorization_cache(self, payload, token=None):
        headers = {}
        if token is not None:
            headers['HTTP_X_INTERNAL_SERVICE_TOKEN'] = token

        return self.client.post(
            '/messages/internal/authorization-cache/',
            data=json.dumps(payload),
            content_type='application/json',
            **headers,
        )

    def post_receipt_visibility_cache(self, payload, token=None):
        headers = {}
        if token is not None:
            headers['HTTP_X_INTERNAL_SERVICE_TOKEN'] = token

        return self.client.post(
            '/receipts/internal/visibility-cache/',
            data=json.dumps(payload),
            content_type='application/json',
            **headers,
        )

    def post_direct_room_visibility(self, payload, token=None):
        headers = {}
        if token is not None:
            headers['HTTP_X_INTERNAL_SERVICE_TOKEN'] = token

        return self.client.post(
            '/rooms/internal/direct-visibility/',
            data=json.dumps(payload),
            content_type='application/json',
            **headers,
        )

    def post_upload_intents(self, payload):
        return self.client.post(
            '/crypto/files/upload-intents/',
            data=json.dumps(payload),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )

    def post_complete_upload_intent(self, upload_intent_id, payload):
        return self.client.post(
            f'/crypto/files/upload-intents/{upload_intent_id}/complete/',
            data=json.dumps(payload),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )

    def post_message_reaction(self, message_id, reaction, user_id=None, account_number=None):
        return self.client.post(
            f'/messages/{message_id}/reaction/',
            data=json.dumps({'reaction': reaction}),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(user_id=user_id, account_number=account_number),
        )

    def cloudinary_response_signature(self, public_id, version):
        cloudinary_config(cloudinary_url=settings.CLOUDINARY_URL, secure=True)
        return api_sign_request(
            {
                'public_id': public_id,
                'version': str(version),
            },
            'test-secret',
            signature_version=1,
        )

    def create_completed_upload_intent(self, client_message_id='direct-upload-message-1'):
        return MessageEncryptedUploadIntent.objects.create(
            sender_user_id=self.sender_user_id,
            sender_account_number=self.sender_account_number,
            recipient_user_id=self.recipient_user_id,
            recipient_account_number=self.recipient_account_number,
            client_message_id=client_message_id,
            attachment_client_id='attachment-1',
            original_file_name='photo.jpg',
            original_mime_type='image/jpeg',
            original_file_size_bytes=20,
            encrypted_file_size_bytes=36,
            cloudinary_public_id=f'MAIN/e2ee/user-{self.sender_user_id}/encrypted-photo',
            cloudinary_resource_type='raw',
            cloudinary_folder=f'MAIN/e2ee/user-{self.sender_user_id}',
            secure_url='https://res.cloudinary.com/test-cloud/raw/upload/v123/MAIN/e2ee/user-1/encrypted-photo',
            status=MessageEncryptedUploadIntent.STATUS_COMPLETED,
            signature_timestamp=int(time.time()),
            expires_at=timezone.now() + timedelta(minutes=5),
            completed_at=timezone.now(),
        )

    def create_completed_direct_upload_intent(
        self,
        client_message_id,
        attachment_client_id,
        cloudinary_public_id,
        mime_type='audio/webm',
    ):
        return MessageEncryptedUploadIntent.objects.create(
            sender_user_id=self.sender_user_id,
            sender_account_number=self.sender_account_number,
            recipient_user_id=self.recipient_user_id,
            recipient_account_number=self.recipient_account_number,
            client_message_id=client_message_id,
            attachment_client_id=attachment_client_id,
            original_file_name='voice-note.webm',
            original_mime_type=mime_type,
            original_file_size_bytes=20,
            encrypted_file_size_bytes=36,
            cloudinary_public_id=cloudinary_public_id,
            cloudinary_resource_type='raw',
            cloudinary_folder=f'MAIN/e2ee/user-{self.sender_user_id}',
            secure_url=f'https://res.cloudinary.com/test-cloud/raw/upload/v123/{cloudinary_public_id}',
            status=MessageEncryptedUploadIntent.STATUS_COMPLETED,
            signature_timestamp=int(time.time()),
            expires_at=timezone.now() + timedelta(minutes=5),
            completed_at=timezone.now(),
        )

    def parent_denial(self, reason='contact_not_saved', status=403):
        return {
            'ok': False,
            'parent': {
                'response': {
                    'allowed': False,
                    'reason': reason,
                    'message': reason,
                },
                'status_code': status,
            },
        }, status

    def parent_allowed(self, delivery_blocked=False, sender_blocked_recipient=False):
        return {
            'ok': True,
            'parent': {
                'response': {
                    'allowed': True,
                    'sender_user_id': self.sender_user_id,
                    'sender_username': self.sender_username,
                    'sender_account_number': self.sender_account_number,
                    'recipient_user_id': self.recipient_user_id,
                    'recipient_username': 'recipient',
                    'recipient_account_number': self.recipient_account_number,
                    'delivery_blocked': delivery_blocked,
                    'block_context': {
                        'sender_blocked_recipient': sender_blocked_recipient,
                        'recipient_blocked_sender': delivery_blocked,
                    },
                    'contact': {
                        'alias_name': 'Recipient',
                        'display_name': 'Recipient',
                        'blocked': sender_blocked_recipient,
                    },
                },
                'status_code': 200,
            },
        }, 200

    @patch('messaging.services.cloudinary_uploader.destroy')
    def test_edit_direct_message_replacement_cleans_previous_encrypted_upload(self, cloudinary_destroy):
        cloudinary_destroy.return_value = {'result': 'ok'}
        room = self.create_direct_room()
        message = Message.objects.create(
            room=room,
            sender_user_id=self.sender_user_id,
            recipient_user_id=self.recipient_user_id,
            text='old encrypted payload',
            client_message_id='edit-direct-voice-note',
            status=Message.STATUS_READ,
        )
        self.create_completed_direct_upload_intent(
            client_message_id=message.client_message_id,
            attachment_client_id='old-voice-note',
            cloudinary_public_id='MAIN/e2ee/user-1/old-voice-note',
        )
        replacement_intent = self.create_completed_direct_upload_intent(
            client_message_id=message.client_message_id,
            attachment_client_id='new-voice-note',
            cloudinary_public_id='MAIN/e2ee/user-1/new-voice-note',
        )
        parent_authorization = self.parent_allowed()[0]['parent']['response']

        result, status = edit_direct_message(
            {
                'user_id': self.sender_user_id,
                'account_number': self.sender_account_number,
            },
            message.id,
            {
                'text': 'updated encrypted payload',
                'encrypted_upload_intent_ids': [str(replacement_intent.id)],
            },
            parent_authorization,
            replacement_upload_intents=[replacement_intent],
        )

        self.assertEqual(status, 200)
        self.assertEqual(result['status'], 'edited')
        message.refresh_from_db()
        self.assertEqual(message.text, 'updated encrypted payload')
        self.assertEqual(message.status, Message.STATUS_SENT)
        self.assertIsNotNone(message.edited_at)
        cloudinary_destroy.assert_called_once_with(
            'MAIN/e2ee/user-1/old-voice-note',
            resource_type='raw',
        )

    @patch('messaging.views.authorize_parent_messaging')
    def test_recipient_device_lookup_caches_authorization_but_send_refreshes_policy(
        self,
        authorize_parent_messaging,
    ):
        authorize_parent_messaging.return_value = self.parent_allowed()

        devices_response = self.client.get(
            f'/crypto/recipients/{self.recipient_account_number}/devices/',
            HTTP_AUTHORIZATION=self.auth_header(),
        )
        send_response = self.post_send_message(
            {
                'recipient_account_number': self.recipient_account_number,
                'text': 'Uses cached authorization.',
            }
        )

        self.assertEqual(devices_response.status_code, 200)
        self.assertEqual(send_response.status_code, 201)
        self.assertEqual(authorize_parent_messaging.call_count, 2)
        self.assertIsNotNone(
            get_cached_messaging_authorization(
                self.sender_user_id,
                self.recipient_account_number,
            )
        )

    @patch('messaging.views.broadcast_participant_event')
    @patch('messaging.views.broadcast_room_event')
    @patch('messaging.views.authorize_parent_messaging')
    def test_send_refreshes_stale_allowed_authorization_after_block(
        self,
        authorize_parent_messaging,
        broadcast_room_event,
        broadcast_participant_event,
    ):
        stale_parent_response = self.parent_allowed(delivery_blocked=False)[0]['parent']['response']
        cache_response = self.post_authorization_cache(
            {
                'authorizations': [
                    {
                        'sender_user_id': self.sender_user_id,
                        'recipient_account_number': self.recipient_account_number,
                        'status_code': 200,
                        'response': stale_parent_response,
                    },
                ],
            },
            token=settings.INTERNAL_SERVICE_TOKEN,
        )
        authorize_parent_messaging.return_value = self.parent_allowed(delivery_blocked=True)

        response = self.post_send_message(
            {
                'recipient_account_number': self.recipient_account_number,
                'text': 'Blocked by fresh policy.',
            }
        )

        self.assertEqual(cache_response.status_code, 200)
        self.assertEqual(cache_response.json()['updated'], 1)
        self.assertEqual(response.status_code, 201)
        authorize_parent_messaging.assert_called_once()

        message = Message.objects.get()
        self.assertTrue(message.delivery_blocked)
        broadcast_room_event.assert_not_called()
        broadcast_participant_event.assert_called_once()

    def test_receipt_visibility_cache_update_hides_existing_direct_receipts(self):
        room = self.create_direct_room()
        message = Message.objects.create(
            room=room,
            sender_user_id=self.sender_user_id,
            recipient_user_id=self.recipient_user_id,
            text='Already read before block.',
            status=Message.STATUS_READ,
        )

        response = self.post_receipt_visibility_cache(
            {
                'policies': [
                    {
                        'owner_user_id': self.recipient_user_id,
                        'candidate_user_id': self.sender_user_id,
                        'hidden': True,
                    },
                ],
            },
            token=settings.INTERNAL_SERVICE_TOKEN,
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['updated'], 1)
        self.assertEqual(body['applied_messages'], 1)
        message.refresh_from_db()
        self.assertTrue(message.receipt_hidden_from_sender)

        messages_result, messages_status = list_room_messages(
            self.sender_user_id,
            room.id,
        )
        self.assertEqual(messages_status, 200)
        self.assertEqual(messages_result['messages'][0]['status'], Message.STATUS_SENT)

    def test_receipt_visibility_cache_update_releases_hidden_read_receipts(self):
        room = self.create_direct_room()
        message = Message.objects.create(
            room=room,
            sender_user_id=self.sender_user_id,
            recipient_user_id=self.recipient_user_id,
            text='Hidden read before unblock.',
            status=Message.STATUS_SENT,
            receipt_hidden_from_sender=True,
        )
        participant = RoomParticipant.objects.get(
            room=room,
            user_id=self.recipient_user_id,
        )
        participant.last_read_at = timezone.now() + timedelta(seconds=1)
        participant.save(update_fields=['last_read_at'])

        response = self.post_receipt_visibility_cache(
            {
                'policies': [
                    {
                        'owner_user_id': self.recipient_user_id,
                        'candidate_user_id': self.sender_user_id,
                        'hidden': False,
                    },
                ],
            },
            token=settings.INTERNAL_SERVICE_TOKEN,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['applied_messages'], 1)
        message.refresh_from_db()
        self.assertEqual(message.status, Message.STATUS_READ)
        self.assertFalse(message.receipt_hidden_from_sender)

        messages_result, messages_status = list_room_messages(
            self.sender_user_id,
            room.id,
        )
        self.assertEqual(messages_status, 200)
        self.assertEqual(messages_result['messages'][0]['status'], Message.STATUS_READ)

    def test_internal_authorization_cache_requires_internal_token(self):
        response = self.post_authorization_cache({'authorizations': []})

        self.assertEqual(response.status_code, 401)

    def test_internal_direct_room_visibility_requires_internal_token(self):
        response = self.post_direct_room_visibility(
            {
                'owner_user_id': self.sender_user_id,
                'peer_user_id': self.recipient_user_id,
                'hidden': True,
            },
        )

        self.assertEqual(response.status_code, 401)

    def test_internal_direct_room_visibility_hides_owner_room_list_only(self):
        room = self.create_direct_room()
        Message.objects.create(
            room=room,
            sender_user_id=self.recipient_user_id,
            recipient_user_id=self.sender_user_id,
            text='Conversation stays stored.',
        )

        response = self.post_direct_room_visibility(
            {
                'owner_user_id': self.sender_user_id,
                'peer_user_id': self.recipient_user_id,
                'hidden': True,
            },
            token=settings.INTERNAL_SERVICE_TOKEN,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['result']['updated'], 1)
        self.assertTrue(
            RoomParticipant.objects.get(
                room=room,
                user_id=self.sender_user_id,
            ).room_list_hidden
        )
        sender_rooms, sender_status = list_user_rooms(self.sender_user_id)
        recipient_rooms, recipient_status = list_user_rooms(self.recipient_user_id)
        sender_messages, sender_messages_status = list_room_messages(
            self.sender_user_id,
            room.id,
        )
        self.assertEqual(sender_status, 200)
        self.assertEqual(recipient_status, 200)
        self.assertEqual(sender_messages_status, 200)
        self.assertEqual(sender_rooms['rooms'], [])
        self.assertEqual([recipient_rooms['rooms'][0]['id']], [room.id])
        self.assertEqual(sender_messages['messages'][0]['text'], 'Conversation stays stored.')

    @patch('messaging.views.broadcast_participant_event')
    @patch('messaging.views.broadcast_room_event')
    def test_message_reaction_creates_updates_and_removes_user_reaction(
        self,
        broadcast_room_event,
        broadcast_participant_event,
    ):
        room = self.create_direct_room()
        message = Message.objects.create(
            room=room,
            sender_user_id=self.sender_user_id,
            recipient_user_id=self.recipient_user_id,
            text='React to this.',
        )

        create_response = self.post_message_reaction(message.id, 'heart')
        self.assertEqual(create_response.status_code, 200)
        create_body = create_response.json()
        self.assertEqual(create_body['result']['action'], 'set')
        self.assertEqual(create_body['result']['my_reaction'], 'heart')
        self.assertEqual(create_body['result']['reactions'][0]['reaction'], 'heart')
        self.assertEqual(create_body['result']['reactions'][0]['count'], 1)
        self.assertEqual(MessageReaction.objects.count(), 1)

        update_response = self.post_message_reaction(message.id, 'laugh')
        self.assertEqual(update_response.status_code, 200)
        update_body = update_response.json()
        self.assertEqual(update_body['result']['action'], 'updated')
        self.assertEqual(update_body['result']['previous_reaction'], 'heart')
        self.assertEqual(update_body['result']['my_reaction'], 'laugh')
        self.assertEqual(MessageReaction.objects.count(), 1)
        self.assertEqual(MessageReaction.objects.get().reaction, 'laugh')

        remove_response = self.post_message_reaction(message.id, 'laugh')
        self.assertEqual(remove_response.status_code, 200)
        remove_body = remove_response.json()
        self.assertEqual(remove_body['result']['action'], 'removed')
        self.assertIsNone(remove_body['result']['my_reaction'])
        self.assertEqual(remove_body['result']['reactions'], [])
        self.assertEqual(MessageReaction.objects.count(), 0)

        self.assertEqual(broadcast_room_event.call_count, 3)
        self.assertEqual(broadcast_room_event.call_args.args[1], 'message.reaction_updated')
        last_event_payload = broadcast_room_event.call_args.args[2]
        self.assertEqual(last_event_payload['message_id'], message.id)
        self.assertEqual(broadcast_participant_event.call_count, 3)

    def test_message_reaction_rejects_unsupported_reaction(self):
        room = self.create_direct_room()
        message = Message.objects.create(
            room=room,
            sender_user_id=self.sender_user_id,
            recipient_user_id=self.recipient_user_id,
            text='React to this.',
        )

        response = self.post_message_reaction(message.id, 'fire')

        self.assertEqual(response.status_code, 400)
        self.assertEqual(MessageReaction.objects.count(), 0)
        self.assertIn('reaction', response.json()['result']['errors'])

    def test_message_reaction_requires_visible_message(self):
        room = self.create_direct_room()
        hidden_message = Message.objects.create(
            room=room,
            sender_user_id=self.sender_user_id,
            recipient_user_id=self.recipient_user_id,
            text='Hidden while blocked.',
            delivery_blocked=True,
            sent_while_blocked=True,
        )

        response = self.post_message_reaction(
            hidden_message.id,
            'heart',
            user_id=self.recipient_user_id,
            account_number=self.recipient_account_number,
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(MessageReaction.objects.count(), 0)

    def test_room_messages_include_reaction_summary_for_current_user(self):
        room = self.create_direct_room()
        message = Message.objects.create(
            room=room,
            sender_user_id=self.sender_user_id,
            recipient_user_id=self.recipient_user_id,
            text='Reacted message.',
        )
        MessageReaction.objects.create(
            message=message,
            user_id=self.sender_user_id,
            reaction='heart',
        )
        MessageReaction.objects.create(
            message=message,
            user_id=self.recipient_user_id,
            reaction='heart',
        )

        response = self.client.get(
            f'/rooms/{room.id}/messages/',
            HTTP_AUTHORIZATION=self.auth_header(),
        )

        self.assertEqual(response.status_code, 200)
        message_payload = response.json()['result']['messages'][0]
        self.assertEqual(message_payload['my_reaction'], 'heart')
        self.assertEqual(message_payload['reactions'][0]['reaction'], 'heart')
        self.assertEqual(message_payload['reactions'][0]['count'], 2)
        self.assertTrue(message_payload['reactions'][0]['reacted_by_me'])

    @patch('messaging.views.broadcast_participant_event')
    @patch('messaging.views.broadcast_room_event')
    @patch('messaging.views.authorize_parent_messaging')
    def test_send_accepts_reply_target_and_broadcasts_reply_preview(
        self,
        authorize_parent_messaging,
        broadcast_room_event,
        broadcast_participant_event,
    ):
        room = self.create_direct_room()
        original_message = Message.objects.create(
            room=room,
            sender_user_id=self.recipient_user_id,
            recipient_user_id=self.sender_user_id,
            text='Earlier message.',
        )
        authorize_parent_messaging.return_value = self.parent_allowed()

        response = self.post_send_message(
            {
                'recipient_account_number': self.recipient_account_number,
                'text': 'Replying to that.',
                'reply_to_message_id': original_message.id,
                'client_message_id': 'reply-message-1',
            }
        )

        self.assertEqual(response.status_code, 201)
        body = response.json()
        reply_message = Message.objects.order_by('-id').first()
        self.assertEqual(reply_message.reply_to_id, original_message.id)
        self.assertEqual(body['result']['message']['reply_to_message_id'], original_message.id)
        self.assertEqual(body['result']['message']['reply_to']['text'], 'Earlier message.')

        broadcast_room_event.assert_called_once()
        event_payload = broadcast_room_event.call_args.args[2]
        self.assertEqual(event_payload['message']['reply_to']['id'], original_message.id)
        broadcast_participant_event.assert_called_once()

    @patch('messaging.views.authorize_parent_messaging')
    def test_send_rejects_reply_target_outside_room(
        self,
        authorize_parent_messaging,
    ):
        self.create_direct_room()
        other_room = Room.objects.create(
            room_type=Room.TYPE_DIRECT,
            created_by_user_id=99,
        )
        Message.objects.create(
            room=other_room,
            sender_user_id=99,
            recipient_user_id=self.sender_user_id,
            text='Different room message.',
        )
        authorize_parent_messaging.return_value = self.parent_allowed()

        response = self.post_send_message(
            {
                'recipient_account_number': self.recipient_account_number,
                'text': 'This reply target is invalid.',
                'reply_to_message_id': Message.objects.get(room=other_room).id,
            }
        )

        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body['status'], 'error')
        self.assertIn('reply_to_message_id', body['result']['errors'])

    @patch('messaging.views.broadcast_participant_event')
    @patch('messaging.views.broadcast_room_event')
    @patch('messaging.views.authorize_parent_messaging')
    @patch('messaging.services.cloudinary_uploader.upload')
    def test_send_uploads_multiple_media_files_to_cloudinary(
        self,
        cloudinary_upload,
        authorize_parent_messaging,
        broadcast_room_event,
        broadcast_participant_event,
    ):
        authorize_parent_messaging.return_value = self.parent_allowed()
        cloudinary_upload.side_effect = [
            {
                'secure_url': 'https://res.cloudinary.com/demo/image/upload/main-pic.jpg',
                'public_id': 'MAIN/pics/main-pic',
                'asset_id': 'asset-pic',
                'resource_type': 'image',
                'bytes': 7,
                'width': 32,
                'height': 32,
            },
            {
                'secure_url': 'https://res.cloudinary.com/demo/raw/upload/main-file.pdf',
                'public_id': 'MAIN/pdfs/main-file',
                'asset_id': 'asset-pdf',
                'resource_type': 'raw',
                'bytes': 11,
            },
        ]
        image_file = SimpleUploadedFile(
            'photo.jpg',
            b'image-bytes',
            content_type='image/jpeg',
        )
        pdf_file = SimpleUploadedFile(
            'report.pdf',
            b'%PDF-bytes',
            content_type='application/pdf',
        )

        response = self.post_send_media_message(
            {
                'recipient_account_number': self.recipient_account_number,
                'text': 'Files attached.',
                'client_message_id': 'media-message-1',
                'attachments': [image_file, pdf_file],
            }
        )

        self.assertEqual(response.status_code, 201)
        body = response.json()
        attachments = body['result']['message']['attachments']
        self.assertEqual(len(attachments), 2)
        self.assertEqual(attachments[0]['file_type'], 'image')
        self.assertEqual(attachments[1]['file_type'], 'document')

        saved_attachments = list(Message.objects.get().attachments.order_by('sort_order'))
        self.assertEqual(saved_attachments[0].cloudinary_public_id, 'MAIN/pics/main-pic')
        expected_folder = 'MAIN/sender-7000000001/direct messages/Recipient-7000000002'
        self.assertEqual(saved_attachments[0].cloudinary_folder, expected_folder)
        self.assertEqual(saved_attachments[1].cloudinary_public_id, 'MAIN/pdfs/main-file')
        self.assertEqual(saved_attachments[1].cloudinary_folder, expected_folder)

        self.assertEqual(cloudinary_upload.call_args_list[0].kwargs['folder'], expected_folder)
        self.assertEqual(cloudinary_upload.call_args_list[1].kwargs['folder'], expected_folder)
        broadcast_room_event.assert_called_once()
        broadcast_participant_event.assert_called_once()

    @patch('messaging.views.authorize_parent_messaging')
    def test_create_encrypted_upload_intent_requires_authorized_message(
        self,
        authorize_parent_messaging,
    ):
        authorize_parent_messaging.return_value = self.parent_denial()

        response = self.post_upload_intents(
            {
                'recipient_account_number': self.recipient_account_number,
                'client_message_id': 'denied-upload-message-1',
                'attachments': [
                    {
                        'id': 'attachment-1',
                        'file_name': 'photo.jpg',
                        'mime_type': 'image/jpeg',
                        'file_size_bytes': 20,
                        'encrypted_file_size_bytes': 36,
                    },
                ],
            }
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(MessageEncryptedUploadIntent.objects.count(), 0)

    @patch('messaging.views.authorize_parent_messaging')
    def test_create_and_complete_encrypted_upload_intent(
        self,
        authorize_parent_messaging,
    ):
        authorize_parent_messaging.return_value = self.parent_allowed()

        create_response = self.post_upload_intents(
            {
                'recipient_account_number': self.recipient_account_number,
                'client_message_id': 'direct-upload-message-1',
                'attachments': [
                    {
                        'id': 'attachment-1',
                        'file_name': 'photo.jpg',
                        'mime_type': 'image/jpeg',
                        'file_size_bytes': 20,
                        'encrypted_file_size_bytes': 36,
                    },
                ],
            }
        )
        create_body = create_response.json()

        self.assertEqual(create_response.status_code, 201)
        upload_intent = create_body['result']['upload_intents'][0]
        self.assertEqual(upload_intent['api_key'], 'test-key')
        self.assertNotIn('api_secret', upload_intent)
        self.assertEqual(upload_intent['resource_type'], 'raw')
        self.assertEqual(
            upload_intent['parameters']['folder'],
            'MAIN/sender-7000000001/direct messages/Recipient-7000000002',
        )
        self.assertEqual(MessageEncryptedUploadIntent.objects.count(), 1)

        intent = MessageEncryptedUploadIntent.objects.get()
        version = '123'
        complete_response = self.post_complete_upload_intent(
            intent.id,
            {
                'public_id': intent.cloudinary_public_id,
                'resource_type': 'raw',
                'bytes': 36,
                'asset_id': 'asset-direct-upload',
                'version': version,
                'signature': self.cloudinary_response_signature(
                    intent.cloudinary_public_id,
                    version,
                ),
            },
        )
        complete_body = complete_response.json()

        self.assertEqual(complete_response.status_code, 200)
        self.assertEqual(complete_body['status'], 'ok')
        encoded_public_id = intent.cloudinary_public_id.replace(' ', '%20')
        self.assertEqual(
            complete_body['result']['file']['encrypted_file_url'],
            f'https://res.cloudinary.com/test-cloud/raw/upload/v{version}/{encoded_public_id}',
        )
        intent.refresh_from_db()
        self.assertEqual(intent.status, MessageEncryptedUploadIntent.STATUS_COMPLETED)
        self.assertEqual(intent.cloudinary_asset_id, 'asset-direct-upload')

    @patch('messaging.views.broadcast_participant_event')
    @patch('messaging.views.broadcast_room_event')
    @patch('messaging.views.authorize_parent_messaging')
    def test_send_consumes_completed_encrypted_upload_intent(
        self,
        authorize_parent_messaging,
        broadcast_room_event,
        broadcast_participant_event,
    ):
        authorize_parent_messaging.return_value = self.parent_allowed()
        intent = self.create_completed_upload_intent(
            client_message_id='direct-upload-message-2',
        )

        response = self.post_send_message(
            {
                'recipient_account_number': self.recipient_account_number,
                'text': 'Encrypted message envelope with attachment.',
                'client_message_id': 'direct-upload-message-2',
                'encrypted_upload_intent_ids': [str(intent.id)],
            }
        )

        self.assertEqual(response.status_code, 201)
        intent.refresh_from_db()
        self.assertEqual(intent.status, MessageEncryptedUploadIntent.STATUS_CONSUMED)
        self.assertIsNotNone(intent.consumed_at)
        broadcast_room_event.assert_called_once()
        broadcast_participant_event.assert_called_once()

    @patch('messaging.views.authorize_parent_messaging')
    def test_send_rejects_uncompleted_encrypted_upload_intent(
        self,
        authorize_parent_messaging,
    ):
        authorize_parent_messaging.return_value = self.parent_allowed()
        intent = self.create_completed_upload_intent(
            client_message_id='direct-upload-message-3',
        )
        intent.status = MessageEncryptedUploadIntent.STATUS_ISSUED
        intent.save(update_fields=['status', 'updated_at'])

        response = self.post_send_message(
            {
                'recipient_account_number': self.recipient_account_number,
                'text': 'Encrypted message envelope with attachment.',
                'client_message_id': 'direct-upload-message-3',
                'encrypted_upload_intent_ids': [str(intent.id)],
            }
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(Message.objects.count(), 0)

    @patch('messaging.views.authorize_parent_messaging')
    @patch('messaging.services.cloudinary_uploader.upload')
    def test_send_media_message_rejects_unsupported_file_type(
        self,
        cloudinary_upload,
        authorize_parent_messaging,
    ):
        authorize_parent_messaging.return_value = self.parent_allowed()

        response = self.post_send_media_message(
            {
                'recipient_account_number': self.recipient_account_number,
                'text': '',
                'attachments': [
                    SimpleUploadedFile(
                        'run.exe',
                        b'not-safe',
                        content_type='application/octet-stream',
                    ),
                ],
            }
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(Message.objects.count(), 0)
        cloudinary_upload.assert_not_called()

    @patch('messaging.views.broadcast_participant_event')
    @patch('messaging.views.broadcast_room_event')
    @patch('messaging.views.authorize_parent_messaging')
    def test_send_allows_unsaved_contact_when_existing_direct_room_is_shared(
        self,
        authorize_parent_messaging,
        broadcast_room_event,
        broadcast_participant_event,
    ):
        room = self.create_direct_room()
        authorize_parent_messaging.return_value = self.parent_denial()

        response = self.post_send_message(
            {
                'recipient_account_number': self.recipient_account_number,
                'text': 'Hello from an existing room.',
                'client_message_id': 'shared-room-message-1',
            }
        )

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body['status'], 'sent')
        self.assertEqual(body['authorization']['messenger']['reason'], 'shared_room')
        self.assertEqual(body['authorization']['messenger']['room_id'], room.id)
        self.assertEqual(Room.objects.count(), 1)

        message = Message.objects.get()
        self.assertEqual(message.room_id, room.id)
        self.assertEqual(message.sender_user_id, self.sender_user_id)
        self.assertEqual(message.recipient_user_id, self.recipient_user_id)
        broadcast_room_event.assert_called_once()
        broadcast_participant_event.assert_called_once()

    @patch('messaging.views.authorize_parent_messaging')
    def test_authorize_allows_unsaved_contact_when_existing_direct_room_is_shared(
        self,
        authorize_parent_messaging,
    ):
        room = self.create_direct_room()
        authorize_parent_messaging.return_value = self.parent_denial()

        response = self.post_authorize_message(
            {
                'recipient_account_number': self.recipient_account_number,
            }
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['status'], 'allowed')
        self.assertEqual(body['authorization']['messenger']['room_id'], room.id)
        self.assertEqual(Message.objects.count(), 0)

    @patch('messaging.views.authorize_parent_messaging')
    def test_send_keeps_contact_not_saved_denial_without_shared_direct_room(
        self,
        authorize_parent_messaging,
    ):
        authorize_parent_messaging.return_value = self.parent_denial()

        response = self.post_send_message(
            {
                'recipient_account_number': self.recipient_account_number,
                'text': 'This should not send.',
            }
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['status'], 'denied')
        self.assertEqual(Message.objects.count(), 0)
        self.assertEqual(Room.objects.count(), 0)

    @patch('messaging.views.broadcast_participant_event')
    @patch('messaging.views.broadcast_room_event')
    @patch('messaging.views.authorize_parent_messaging')
    def test_send_creates_sent_only_message_when_recipient_blocked_sender(
        self,
        authorize_parent_messaging,
        broadcast_room_event,
        broadcast_participant_event,
    ):
        authorize_parent_messaging.return_value = self.parent_allowed(delivery_blocked=True)

        response = self.post_send_message(
            {
                'recipient_account_number': self.recipient_account_number,
                'text': 'This should stay sent only.',
            }
        )

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body['status'], 'sent')
        self.assertNotIn('delivery_blocked', body['authorization']['parent']['response'])

        message = Message.objects.get()
        self.assertEqual(message.status, Message.STATUS_SENT)
        self.assertTrue(message.delivery_blocked)

        broadcast_room_event.assert_not_called()
        broadcast_participant_event.assert_called_once()
        participants = broadcast_participant_event.call_args.args[0]
        self.assertEqual([participant['user_id'] for participant in participants], [self.sender_user_id])

    @patch('messaging.views.authorize_parent_messaging')
    def test_send_requires_active_room_participants_for_shared_room_fallback(
        self,
        authorize_parent_messaging,
    ):
        self.create_direct_room(recipient_active=False)
        authorize_parent_messaging.return_value = self.parent_denial()

        response = self.post_send_message(
            {
                'recipient_account_number': self.recipient_account_number,
                'text': 'Inactive participants should not authorize.',
            }
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['status'], 'denied')
        self.assertEqual(Message.objects.count(), 0)

@override_settings(
    **TEST_JWT_SETTINGS,
    CHANNEL_LAYERS={
        'default': {
            'BACKEND': 'channels.layers.InMemoryChannelLayer',
        },
    },
)
class WebSocketRealtimeTests(TransactionTestCase):
    sender_user_id = 1
    recipient_user_id = 2
    sender_account_number = '7000000001'
    recipient_account_number = '7000000002'

    def auth_token(self, user_id=None, account_number=None):
        now = timezone.now()
        return jwt.encode(
            {
                'sub': str(user_id or self.sender_user_id),
                'user_id': user_id or self.sender_user_id,
                'account_number': account_number or self.sender_account_number,
                'iss': settings.MESSAGING_JWT_ISSUER,
                'aud': settings.MESSAGING_JWT_AUDIENCE,
                'iat': now,
                'exp': now + timedelta(minutes=5),
            },
            settings.MESSAGING_JWT_SECRET,
            algorithm='HS256',
        )

    def create_direct_room(self):
        room = Room.objects.create(
            room_type=Room.TYPE_DIRECT,
            created_by_user_id=self.sender_user_id,
        )
        RoomParticipant.objects.create(
            room=room,
            user_id=self.sender_user_id,
            account_number=self.sender_account_number,
            is_active=True,
        )
        RoomParticipant.objects.create(
            room=room,
            user_id=self.recipient_user_id,
            account_number=self.recipient_account_number,
            is_active=True,
        )

        return room

    async def test_room_websocket_receives_room_broadcasts(self):
        room = await sync_to_async(self.create_direct_room)()
        token = self.auth_token()
        communicator = WebsocketCommunicator(
            application,
            f'/ws/rooms/{room.id}/?token={token}',
        )

        connected, _ = await communicator.connect()
        self.assertTrue(connected)
        self.assertEqual(
            (await communicator.receive_json_from(timeout=5))['type'],
            'connection.accepted',
        )
        self.assertEqual(
            (await communicator.receive_json_from(timeout=5))['type'],
            'typing.snapshot',
        )

        await sync_to_async(broadcast_room_event)(
            room.id,
            'message.sent',
            {
                'room': {'id': room.id},
                'message': {'id': 1, 'room_id': room.id},
            },
        )

        event = await communicator.receive_json_from(timeout=1)
        self.assertEqual(event['type'], 'message.sent')
        self.assertEqual(event['message']['room_id'], room.id)
        await communicator.disconnect()

    async def test_inbox_websocket_receives_user_broadcasts(self):
        token = self.auth_token()
        communicator = WebsocketCommunicator(
            application,
            f'/ws/inbox/?token={token}',
        )

        connected, _ = await communicator.connect()
        self.assertTrue(connected)
        self.assertEqual(
            (await communicator.receive_json_from(timeout=5))['type'],
            'connection.accepted',
        )
        self.assertEqual(
            (await communicator.receive_json_from(timeout=5))['type'],
            'presence.snapshot',
        )

        await sync_to_async(broadcast_user_event)(
            self.sender_user_id,
            'message.delivered',
            {
                'room_id': 10,
                'user_id': self.recipient_user_id,
                'last_delivered_message_id': 99,
            },
        )

        event = await communicator.receive_json_from(timeout=1)
        self.assertEqual(event['type'], 'message.delivered')
        self.assertEqual(event['last_delivered_message_id'], 99)
        await communicator.disconnect()

    async def test_room_typing_event_is_broadcast_to_recipient_inbox(self):
        room = await sync_to_async(self.create_direct_room)()
        sender_token = self.auth_token()
        recipient_token = self.auth_token(
            user_id=self.recipient_user_id,
            account_number=self.recipient_account_number,
        )
        room_communicator = WebsocketCommunicator(
            application,
            f'/ws/rooms/{room.id}/?token={sender_token}',
        )
        inbox_communicator = WebsocketCommunicator(
            application,
            f'/ws/inbox/?token={recipient_token}',
        )

        room_connected, _ = await room_communicator.connect()
        inbox_connected, _ = await inbox_communicator.connect()
        self.assertTrue(room_connected)
        self.assertTrue(inbox_connected)
        self.assertEqual(
            (await room_communicator.receive_json_from(timeout=5))['type'],
            'connection.accepted',
        )
        self.assertEqual(
            (await room_communicator.receive_json_from(timeout=5))['type'],
            'typing.snapshot',
        )
        self.assertEqual(
            (await inbox_communicator.receive_json_from(timeout=5))['type'],
            'connection.accepted',
        )
        self.assertEqual(
            (await inbox_communicator.receive_json_from(timeout=5))['type'],
            'presence.snapshot',
        )

        await room_communicator.send_json_to({'type': 'typing.started'})

        event = await inbox_communicator.receive_json_from(timeout=5)
        self.assertEqual(event['type'], 'typing.started')
        self.assertEqual(int(event['room_id']), room.id)
        self.assertEqual(event['user_id'], self.sender_user_id)
        self.assertEqual(event['account_number'], self.sender_account_number)
        await room_communicator.disconnect()
        await inbox_communicator.disconnect()


@override_settings(**TEST_JWT_SETTINGS)
class PresenceVisibilityWebhookTests(TestCase):
    owner_user_id = 1
    viewer_user_id = 2
    owner_account_number = '7000000001'

    def setUp(self):
        cache.clear()

    def auth_header(self):
        now = timezone.now()
        token = jwt.encode(
            {
                'sub': str(self.owner_user_id),
                'user_id': self.owner_user_id,
                'account_number': self.owner_account_number,
                'iss': settings.MESSAGING_JWT_ISSUER,
                'aud': settings.MESSAGING_JWT_AUDIENCE,
                'iat': now,
                'exp': now + timedelta(minutes=5),
            },
            settings.MESSAGING_JWT_SECRET,
            algorithm='HS256',
        )

        return f'Bearer {token}'

    def post_presence_visibility(self, visible):
        return self.client.post(
            '/presence/internal/visibility/',
            data=json.dumps(
                {
                    'owner_user_id': self.owner_user_id,
                    'owner_account_number': self.owner_account_number,
                    'viewer_user_id': self.viewer_user_id,
                    'visible': visible,
                }
            ),
            content_type='application/json',
            HTTP_X_INTERNAL_SERVICE_TOKEN=settings.INTERNAL_SERVICE_TOKEN,
        )

    def post_presence_refresh(self):
        return self.client.post(
            '/presence/visibility/refresh/',
            data=json.dumps({'viewer_user_id': self.viewer_user_id}),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )

    @patch('messaging.views.broadcast_user_event')
    def test_presence_visibility_hidden_broadcasts_offline_immediately(
        self,
        broadcast_user_event,
    ):
        broadcast_user_event.return_value = True

        response = self.post_presence_visibility(False)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['event_type'], 'presence.offline')
        broadcast_user_event.assert_called_once_with(
            self.viewer_user_id,
            'presence.offline',
            {
                'user_id': self.owner_user_id,
                'account_number': self.owner_account_number,
                'expires_in': 0,
            },
        )

    @patch('messaging.views.broadcast_user_event')
    def test_presence_visibility_visible_broadcasts_online_when_owner_online(
        self,
        broadcast_user_event,
    ):
        broadcast_user_event.return_value = True
        cache.set(
            f'messaging:presence:user:{self.owner_user_id}:connections',
            ['connection-1'],
            timeout=settings.MESSAGING_PRESENCE_TTL_SECONDS * 2,
        )
        cache.set(
            f'messaging:presence:user:{self.owner_user_id}:connection:connection-1',
            True,
            timeout=settings.MESSAGING_PRESENCE_TTL_SECONDS,
        )

        response = self.post_presence_visibility(True)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['owner_online'])
        self.assertEqual(response.json()['event_type'], 'presence.online')
        broadcast_user_event.assert_called_once_with(
            self.viewer_user_id,
            'presence.online',
            {
                'user_id': self.owner_user_id,
                'account_number': self.owner_account_number,
                'expires_in': settings.MESSAGING_PRESENCE_TTL_SECONDS,
            },
        )

    @patch('messaging.views.broadcast_user_event')
    def test_presence_visibility_visible_does_not_broadcast_when_owner_offline(
        self,
        broadcast_user_event,
    ):
        response = self.post_presence_visibility(True)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['owner_online'])
        self.assertIsNone(response.json()['event_type'])
        broadcast_user_event.assert_not_called()

    @patch('messaging.views.resolve_parent_presence_visibility')
    @patch('messaging.views.broadcast_user_event')
    def test_authenticated_presence_refresh_uses_parent_policy_for_live_broadcast(
        self,
        broadcast_user_event,
        resolve_parent_presence_visibility,
    ):
        broadcast_user_event.return_value = True
        resolve_parent_presence_visibility.return_value = (
            {
                'parent': {
                    'response': {
                        'allowed': True,
                        'visible_user_ids': [],
                        'hidden_user_ids': [self.viewer_user_id],
                    },
                },
            },
            200,
        )

        response = self.post_presence_refresh()

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['visible'])
        self.assertEqual(response.json()['event_type'], 'presence.offline')
        resolve_parent_presence_visibility.assert_called_once_with(
            {
                'owner_user_id': self.owner_user_id,
                'candidate_user_ids': [self.viewer_user_id],
            }
        )
        broadcast_user_event.assert_called_once_with(
            self.viewer_user_id,
            'presence.offline',
            {
                'user_id': self.owner_user_id,
                'account_number': self.owner_account_number,
                'expires_in': 0,
            },
        )


@override_settings(**TEST_JWT_SETTINGS)
class MessageReadBroadcastTests(TestCase):
    sender_user_id = 1
    recipient_user_id = 2
    sender_username = 'sender'
    sender_account_number = '7000000001'
    recipient_account_number = '7000000002'

    def setUp(self):
        cache.clear()

    def auth_header(self, user_id=None, account_number=None):
        now = timezone.now()
        token = jwt.encode(
            {
                'sub': str(user_id or self.recipient_user_id),
                'user_id': user_id or self.recipient_user_id,
                'username': self.sender_username,
                'account_number': account_number or self.recipient_account_number,
                'iss': settings.MESSAGING_JWT_ISSUER,
                'aud': settings.MESSAGING_JWT_AUDIENCE,
                'iat': now,
                'exp': now + timedelta(minutes=5),
            },
            settings.MESSAGING_JWT_SECRET,
            algorithm='HS256',
        )

        return f'Bearer {token}'

    def create_direct_room(self):
        room = Room.objects.create(
            room_type=Room.TYPE_DIRECT,
            created_by_user_id=self.sender_user_id,
        )
        RoomParticipant.objects.create(
            room=room,
            user_id=self.sender_user_id,
            account_number=self.sender_account_number,
            is_active=True,
        )
        RoomParticipant.objects.create(
            room=room,
            user_id=self.recipient_user_id,
            account_number=self.recipient_account_number,
            is_active=True,
        )

        return room

    @patch('messaging.views.broadcast_user_event')
    @patch('messaging.views.broadcast_participant_event')
    @patch('messaging.views.broadcast_room_event')
    @patch('messaging.services.resolve_parent_receipt_visibility')
    def test_hidden_direct_read_broadcasts_reader_unread_clear(
        self,
        resolve_parent_receipt_visibility,
        broadcast_room_event,
        broadcast_participant_event,
        broadcast_user_event,
    ):
        room = self.create_direct_room()
        message = Message.objects.create(
            room=room,
            sender_user_id=self.sender_user_id,
            recipient_user_id=self.recipient_user_id,
            text='Hidden direct read should still clear unread.',
            status=Message.STATUS_DELIVERED,
        )
        resolve_parent_receipt_visibility.return_value = (
            {
                'parent': {
                    'response': {
                        'allowed': True,
                        'hidden_user_ids': [self.sender_user_id],
                        'visible_user_ids': [],
                    },
                },
            },
            200,
        )

        response = self.client.post(
            f'/rooms/{room.id}/read/',
            data=json.dumps({'last_read_message_id': message.id}),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )

        result = response.json()['result']
        self.assertEqual(response.status_code, 200)
        self.assertEqual(result['updated_messages'], 0)
        self.assertEqual(result['hidden_receipts'], 1)
        self.assertEqual(result['unread_count'], 0)
        broadcast_room_event.assert_not_called()
        broadcast_participant_event.assert_not_called()
        broadcast_user_event.assert_called_once()
        self.assertEqual(broadcast_user_event.call_args.args[0], self.recipient_user_id)
        self.assertEqual(broadcast_user_event.call_args.args[1], 'message.read')
        event_payload = broadcast_user_event.call_args.args[2]
        self.assertEqual(event_payload['user_id'], self.recipient_user_id)
        self.assertEqual(event_payload['room_id'], room.id)
        self.assertEqual(event_payload['last_read_message_id'], message.id)
        self.assertEqual(event_payload['unread_count'], 0)
        self.assertEqual(event_payload['message_statuses'], [])


class MessagingCacheTests(TestCase):
    sender_user_id = 1
    recipient_user_id = 2
    sender_account_number = '7000000001'
    recipient_account_number = '7000000002'

    def setUp(self):
        cache.clear()

    def create_direct_room(self):
        room = Room.objects.create(
            room_type=Room.TYPE_DIRECT,
            created_by_user_id=self.sender_user_id,
        )
        RoomParticipant.objects.create(
            room=room,
            user_id=self.sender_user_id,
            account_number=self.sender_account_number,
        )
        RoomParticipant.objects.create(
            room=room,
            user_id=self.recipient_user_id,
            account_number=self.recipient_account_number,
        )

        return room

    def parent_authorization(self, delivery_blocked=False):
        return {
            'sender_user_id': self.sender_user_id,
            'sender_account_number': self.sender_account_number,
            'recipient_user_id': self.recipient_user_id,
            'recipient_account_number': self.recipient_account_number,
            'delivery_blocked': delivery_blocked,
        }

    def sender(self):
        return {
            'user_id': self.sender_user_id,
            'account_number': self.sender_account_number,
        }

    def test_list_room_messages_uses_cache_until_invalidated(self):
        room = self.create_direct_room()
        message = Message.objects.create(
            room=room,
            sender_user_id=self.sender_user_id,
            recipient_user_id=self.recipient_user_id,
            text='Original cached message.',
        )

        messages_result, messages_status = list_room_messages(
            self.sender_user_id,
            room.id,
        )
        self.assertEqual(messages_status, 200)
        self.assertEqual(messages_result['messages'][0]['text'], 'Original cached message.')

        Message.objects.filter(id=message.id).update(text='Updated in database.')

        cached_messages_result, cached_messages_status = list_room_messages(
            self.sender_user_id,
            room.id,
        )
        self.assertEqual(cached_messages_status, 200)
        self.assertEqual(cached_messages_result['messages'][0]['text'], 'Original cached message.')

        invalidate_room_messages_cache(room.id)

        fresh_messages_result, fresh_messages_status = list_room_messages(
            self.sender_user_id,
            room.id,
        )
        self.assertEqual(fresh_messages_status, 200)
        self.assertEqual(fresh_messages_result['messages'][0]['text'], 'Updated in database.')

    def test_create_direct_message_invalidates_room_and_message_caches(self):
        room = self.create_direct_room()
        Message.objects.create(
            room=room,
            sender_user_id=self.sender_user_id,
            recipient_user_id=self.recipient_user_id,
            text='Old message.',
        )

        rooms_result, rooms_status = list_user_rooms(self.sender_user_id)
        messages_result, messages_status = list_room_messages(self.sender_user_id, room.id)
        self.assertEqual(rooms_status, 200)
        self.assertEqual(messages_status, 200)
        self.assertEqual(rooms_result['rooms'][0]['last_message']['text'], 'Old message.')
        self.assertEqual(len(messages_result['messages']), 1)

        send_result, send_status = create_direct_message(
            self.sender(),
            self.parent_authorization(),
            {
                'recipient_account_number': self.recipient_account_number,
                'text': 'New message.',
                'client_message_id': 'cache-invalidation-send-1',
            },
        )
        self.assertEqual(send_status, 201)
        self.assertEqual(send_result['status'], 'sent')

        fresh_rooms_result, fresh_rooms_status = list_user_rooms(self.sender_user_id)
        fresh_messages_result, fresh_messages_status = list_room_messages(
            self.sender_user_id,
            room.id,
        )
        self.assertEqual(fresh_rooms_status, 200)
        self.assertEqual(fresh_messages_status, 200)
        self.assertEqual(fresh_rooms_result['rooms'][0]['last_message']['text'], 'New message.')
        self.assertEqual(
            [message['text'] for message in fresh_messages_result['messages']],
            ['Old message.', 'New message.'],
        )

    def test_direct_room_list_hidden_is_owner_only_and_reversible(self):
        room = self.create_direct_room()
        Message.objects.create(
            room=room,
            sender_user_id=self.recipient_user_id,
            recipient_user_id=self.sender_user_id,
            text='Stored conversation.',
        )

        initial_sender_rooms, initial_sender_status = list_user_rooms(self.sender_user_id)
        self.assertEqual(initial_sender_status, 200)
        self.assertEqual([room_data['id'] for room_data in initial_sender_rooms['rooms']], [room.id])

        hidden_result, hidden_status = set_direct_room_list_hidden(
            self.sender_user_id,
            self.recipient_user_id,
            True,
        )

        self.assertEqual(hidden_status, 200)
        self.assertEqual(hidden_result['updated'], 1)
        hidden_sender_rooms, hidden_sender_status = list_user_rooms(self.sender_user_id)
        recipient_rooms, recipient_status = list_user_rooms(self.recipient_user_id)
        sender_messages, sender_messages_status = list_room_messages(
            self.sender_user_id,
            room.id,
        )
        self.assertEqual(hidden_sender_status, 200)
        self.assertEqual(recipient_status, 200)
        self.assertEqual(sender_messages_status, 200)
        self.assertEqual(hidden_sender_rooms['rooms'], [])
        self.assertEqual([room_data['id'] for room_data in recipient_rooms['rooms']], [room.id])
        self.assertEqual(sender_messages['messages'][0]['text'], 'Stored conversation.')

        visible_result, visible_status = set_direct_room_list_hidden(
            self.sender_user_id,
            self.recipient_user_id,
            False,
        )

        self.assertEqual(visible_status, 200)
        self.assertEqual(visible_result['updated'], 1)
        visible_sender_rooms, visible_sender_status = list_user_rooms(self.sender_user_id)
        self.assertEqual(visible_sender_status, 200)
        self.assertEqual([room_data['id'] for room_data in visible_sender_rooms['rooms']], [room.id])

    def test_visible_incoming_message_unhides_direct_room_for_recipient(self):
        room = self.create_direct_room()
        hidden_result, hidden_status = set_direct_room_list_hidden(
            self.recipient_user_id,
            self.sender_user_id,
            True,
        )
        self.assertEqual(hidden_status, 200)
        self.assertEqual(hidden_result['updated'], 1)
        hidden_rooms, hidden_rooms_status = list_user_rooms(self.recipient_user_id)
        self.assertEqual(hidden_rooms_status, 200)
        self.assertEqual(hidden_rooms['rooms'], [])

        send_result, send_status = create_direct_message(
            self.sender(),
            self.parent_authorization(delivery_blocked=False),
            {
                'recipient_account_number': self.recipient_account_number,
                'text': 'Visible after contact delete.',
            },
        )

        self.assertEqual(send_status, 201)
        self.assertEqual(send_result['status'], 'sent')
        visible_rooms, visible_rooms_status = list_user_rooms(self.recipient_user_id)
        self.assertEqual(visible_rooms_status, 200)
        self.assertEqual([room_data['id'] for room_data in visible_rooms['rooms']], [room.id])
        self.assertEqual(visible_rooms['rooms'][0]['last_message']['text'], 'Visible after contact delete.')

    def test_delivery_blocked_message_is_visible_only_to_sender_and_kept_sent(self):
        room = self.create_direct_room()

        send_result, send_status = create_direct_message(
            self.sender(),
            self.parent_authorization(delivery_blocked=True),
            {
                'recipient_account_number': self.recipient_account_number,
                'text': 'Blocked inbound message.',
            },
        )
        self.assertEqual(send_status, 201)
        self.assertEqual(send_result['status'], 'sent')

        message = Message.objects.get()
        self.assertEqual(message.status, Message.STATUS_SENT)
        self.assertTrue(message.delivery_blocked)
        self.assertTrue(message.sent_while_blocked)
        self.assertFalse(send_result['message']['sent_while_blocked'])

        sender_messages_result, sender_messages_status = list_room_messages(
            self.sender_user_id,
            room.id,
        )
        recipient_messages_result, recipient_messages_status = list_room_messages(
            self.recipient_user_id,
            room.id,
        )
        self.assertEqual(sender_messages_status, 200)
        self.assertEqual(recipient_messages_status, 200)
        self.assertEqual(
            [message['text'] for message in sender_messages_result['messages']],
            ['Blocked inbound message.'],
        )
        self.assertEqual(recipient_messages_result['messages'], [])

        recipient_rooms_result, recipient_rooms_status = list_user_rooms(self.recipient_user_id)
        self.assertEqual(recipient_rooms_status, 200)
        self.assertEqual(recipient_rooms_result['rooms'][0]['last_message'], None)
        self.assertEqual(recipient_rooms_result['rooms'][0]['unread_count'], 0)

        delivered_result, delivered_status = mark_room_delivered(
            self.recipient_user_id,
            room.id,
            {},
        )
        self.assertEqual(delivered_status, 200)
        self.assertEqual(delivered_result['updated_messages'], 0)

        read_result, read_status = mark_room_read(
            self.recipient_user_id,
            room.id,
            {},
        )
        self.assertEqual(read_status, 200)
        self.assertEqual(read_result['updated_messages'], 0)

        message.refresh_from_db()
        self.assertEqual(message.status, Message.STATUS_SENT)

    def test_released_blocked_message_is_delivered_and_marked_for_recipient(self):
        room = self.create_direct_room()

        create_direct_message(
            self.sender(),
            self.parent_authorization(delivery_blocked=True),
            {
                'recipient_account_number': self.recipient_account_number,
                'text': 'Blocked then released message.',
            },
        )
        message = Message.objects.get()

        release_result, release_status = release_room_blocked_messages(
            self.recipient_user_id,
            room.id,
        )
        self.assertEqual(release_status, 200)
        self.assertEqual(release_result['released_messages'], 1)
        self.assertEqual(release_result['updated_messages'], 1)
        self.assertEqual(release_result['last_delivered_message_id'], message.id)

        message.refresh_from_db()
        self.assertFalse(message.delivery_blocked)
        self.assertTrue(message.sent_while_blocked)
        self.assertEqual(message.status, Message.STATUS_DELIVERED)

        sender_messages_result, sender_messages_status = list_room_messages(
            self.sender_user_id,
            room.id,
        )
        recipient_messages_result, recipient_messages_status = list_room_messages(
            self.recipient_user_id,
            room.id,
        )
        self.assertEqual(sender_messages_status, 200)
        self.assertEqual(recipient_messages_status, 200)
        self.assertFalse(sender_messages_result['messages'][0]['sent_while_blocked'])
        self.assertTrue(recipient_messages_result['messages'][0]['sent_while_blocked'])
        self.assertEqual(recipient_messages_result['messages'][0]['status'], Message.STATUS_DELIVERED)

    def test_sender_blocked_recipient_message_can_still_be_delivered_and_read(self):
        room = self.create_direct_room()

        create_direct_message(
            self.sender(),
            self.parent_authorization(delivery_blocked=False),
            {
                'recipient_account_number': self.recipient_account_number,
                'text': 'Sender blocked recipient but sends anyway.',
            },
        )
        message = Message.objects.get()

        delivered_result, delivered_status = mark_room_delivered(
            self.recipient_user_id,
            room.id,
            {'last_delivered_message_id': message.id},
        )
        self.assertEqual(delivered_status, 200)
        self.assertEqual(delivered_result['updated_messages'], 1)

        message.refresh_from_db()
        self.assertEqual(message.status, Message.STATUS_DELIVERED)

        read_result, read_status = mark_room_read(
            self.recipient_user_id,
            room.id,
            {'last_read_message_id': message.id},
        )
        self.assertEqual(read_status, 200)
        self.assertEqual(read_result['updated_messages'], 1)

        message.refresh_from_db()
        self.assertEqual(message.status, Message.STATUS_READ)

    @patch('messaging.services.resolve_parent_receipt_visibility')
    def test_hidden_direct_receipts_release_after_ghosting_is_removed(
        self,
        resolve_parent_receipt_visibility,
    ):
        room = self.create_direct_room()
        first_message = Message.objects.create(
            room=room,
            sender_user_id=self.sender_user_id,
            recipient_user_id=self.recipient_user_id,
            text='Hidden while ghosted.',
        )
        latest_message = Message.objects.create(
            room=room,
            sender_user_id=self.sender_user_id,
            recipient_user_id=self.recipient_user_id,
            text='Latest hidden while ghosted.',
        )
        resolve_parent_receipt_visibility.return_value = (
            {
                'parent': {
                    'response': {
                        'allowed': True,
                        'hidden_user_ids': [self.sender_user_id],
                        'visible_user_ids': [],
                    },
                },
            },
            200,
        )

        hidden_result, hidden_status = mark_room_read(
            self.recipient_user_id,
            room.id,
            {'last_read_message_id': latest_message.id},
        )

        self.assertEqual(hidden_status, 200)
        self.assertEqual(hidden_result['updated_messages'], 0)
        self.assertEqual(hidden_result['hidden_receipts'], 2)
        first_message.refresh_from_db()
        latest_message.refresh_from_db()
        self.assertEqual(first_message.status, Message.STATUS_SENT)
        self.assertEqual(latest_message.status, Message.STATUS_SENT)
        self.assertTrue(first_message.receipt_hidden_from_sender)
        self.assertTrue(latest_message.receipt_hidden_from_sender)

        resolve_parent_receipt_visibility.return_value = (
            {
                'parent': {
                    'response': {
                        'allowed': True,
                        'hidden_user_ids': [],
                        'visible_user_ids': [self.sender_user_id],
                    },
                },
            },
            200,
        )

        released_result, released_status = mark_room_read(
            self.recipient_user_id,
            room.id,
            {'last_read_message_id': latest_message.id},
        )

        self.assertEqual(released_status, 200)
        self.assertEqual(released_result['updated_messages'], 2)
        self.assertEqual(released_result['hidden_receipts'], 0)
        self.assertEqual(
            released_result['message_statuses'],
            [
                {'message_id': first_message.id, 'status': Message.STATUS_READ},
                {'message_id': latest_message.id, 'status': Message.STATUS_READ},
            ],
        )
        first_message.refresh_from_db()
        latest_message.refresh_from_db()
        self.assertEqual(first_message.status, Message.STATUS_READ)
        self.assertEqual(latest_message.status, Message.STATUS_READ)
        self.assertFalse(first_message.receipt_hidden_from_sender)
        self.assertFalse(latest_message.receipt_hidden_from_sender)

    def test_delivery_blocked_is_rejected_for_group_room_messages(self):
        room = Room.objects.create(
            room_type=Room.TYPE_GROUP,
            created_by_user_id=self.sender_user_id,
        )

        with self.assertRaises(ValidationError) as error_context:
            Message.objects.create(
                room=room,
                sender_user_id=self.sender_user_id,
                recipient_user_id=self.recipient_user_id,
                text='Group messages cannot use direct-room block delivery.',
                delivery_blocked=True,
            )

        self.assertIn('delivery_blocked', error_context.exception.message_dict)

        with self.assertRaises(ValidationError) as blocked_marker_context:
            Message.objects.create(
                room=room,
                sender_user_id=self.sender_user_id,
                recipient_user_id=self.recipient_user_id,
                text='Group messages cannot use direct-room blocked markers.',
                sent_while_blocked=True,
            )

        self.assertIn('sent_while_blocked', blocked_marker_context.exception.message_dict)

    def test_group_room_messages_are_not_hidden_by_delivery_blocked_flag(self):
        room = Room.objects.create(
            room_type=Room.TYPE_GROUP,
            created_by_user_id=self.sender_user_id,
        )
        RoomParticipant.objects.create(
            room=room,
            user_id=self.sender_user_id,
            account_number=self.sender_account_number,
        )
        RoomParticipant.objects.create(
            room=room,
            user_id=self.recipient_user_id,
            account_number=self.recipient_account_number,
        )
        message = Message.objects.create(
            room=room,
            sender_user_id=self.sender_user_id,
            recipient_user_id=self.recipient_user_id,
            text='Future group message.',
        )
        Message.objects.filter(id=message.id).update(delivery_blocked=True)

        messages_result, messages_status = list_room_messages(self.recipient_user_id, room.id)
        self.assertEqual(messages_status, 200)
        self.assertEqual(
            [message['text'] for message in messages_result['messages']],
            ['Future group message.'],
        )
        self.assertEqual(get_room_unread_count(room.id, self.recipient_user_id), 1)

        delivered_result, delivered_status = mark_room_delivered(
            self.recipient_user_id,
            room.id,
            {'last_delivered_message_id': message.id},
        )
        self.assertEqual(delivered_status, 200)
        self.assertEqual(delivered_result['updated_messages'], 1)

        message.refresh_from_db()
        self.assertEqual(message.status, Message.STATUS_DELIVERED)

    def test_mark_room_read_invalidates_room_and_message_caches(self):
        room = self.create_direct_room()
        message = Message.objects.create(
            room=room,
            sender_user_id=self.recipient_user_id,
            recipient_user_id=self.sender_user_id,
            text='Unread message.',
            status=Message.STATUS_DELIVERED,
        )

        rooms_result, rooms_status = list_user_rooms(self.sender_user_id)
        messages_result, messages_status = list_room_messages(self.sender_user_id, room.id)
        self.assertEqual(rooms_status, 200)
        self.assertEqual(messages_status, 200)
        self.assertEqual(rooms_result['rooms'][0]['unread_count'], 1)
        self.assertEqual(messages_result['messages'][0]['status'], Message.STATUS_DELIVERED)

        read_result, read_status = mark_room_read(
            self.sender_user_id,
            room.id,
            {'last_read_message_id': message.id},
        )
        self.assertEqual(read_status, 200)
        self.assertEqual(read_result['status'], 'read')

        fresh_rooms_result, fresh_rooms_status = list_user_rooms(self.sender_user_id)
        fresh_messages_result, fresh_messages_status = list_room_messages(
            self.sender_user_id,
            room.id,
        )
        self.assertEqual(fresh_rooms_status, 200)
        self.assertEqual(fresh_messages_status, 200)
        self.assertEqual(fresh_rooms_result['rooms'][0]['unread_count'], 0)
        self.assertEqual(fresh_messages_result['messages'][0]['status'], Message.STATUS_READ)

    def test_list_room_messages_defaults_to_twenty_messages_per_page(self):
        room = self.create_direct_room()
        for index in range(25):
            Message.objects.create(
                room=room,
                sender_user_id=self.sender_user_id,
                recipient_user_id=self.recipient_user_id,
                text=f'Message {index + 1}',
            )

        first_page_result, first_page_status = list_room_messages(
            self.sender_user_id,
            room.id,
        )
        self.assertEqual(first_page_status, 200)
        self.assertEqual(len(first_page_result['messages']), 20)
        self.assertTrue(first_page_result['pagination']['has_more'])
        self.assertIsNotNone(first_page_result['pagination']['next_before_message_id'])

        second_page_result, second_page_status = list_room_messages(
            self.sender_user_id,
            room.id,
            before_message_id=first_page_result['pagination']['next_before_message_id'],
        )
        self.assertEqual(second_page_status, 200)
        self.assertEqual(len(second_page_result['messages']), 5)
        self.assertFalse(second_page_result['pagination']['has_more'])

    def test_list_room_messages_includes_reply_preview_for_unloaded_target(self):
        room = self.create_direct_room()
        original_message = Message.objects.create(
            room=room,
            sender_user_id=self.recipient_user_id,
            recipient_user_id=self.sender_user_id,
            text='Original reply target.',
        )
        for index in range(10):
            Message.objects.create(
                room=room,
                sender_user_id=self.sender_user_id,
                recipient_user_id=self.recipient_user_id,
                text=f'Filler message {index + 1}',
            )
        Message.objects.create(
            room=room,
            reply_to=original_message,
            sender_user_id=self.sender_user_id,
            recipient_user_id=self.recipient_user_id,
            text='Reply on latest page.',
        )

        messages_result, messages_status = list_room_messages(
            self.sender_user_id,
            room.id,
            limit=5,
        )

        self.assertEqual(messages_status, 200)
        self.assertNotIn(
            original_message.id,
            [message['id'] for message in messages_result['messages']],
        )
        reply_message = messages_result['messages'][-1]
        self.assertEqual(reply_message['text'], 'Reply on latest page.')
        self.assertEqual(reply_message['reply_to_message_id'], original_message.id)
        self.assertEqual(reply_message['reply_to']['text'], 'Original reply target.')

    def test_list_room_messages_around_message_returns_target_page(self):
        room = self.create_direct_room()
        target_message = None
        for index in range(15):
            message = Message.objects.create(
                room=room,
                sender_user_id=self.sender_user_id,
                recipient_user_id=self.recipient_user_id,
                text=f'Message {index + 1}',
            )
            if index == 3:
                target_message = message

        messages_result, messages_status = list_room_messages(
            self.sender_user_id,
            room.id,
            limit=5,
            around_message_id=target_message.id,
        )

        self.assertEqual(messages_status, 200)
        self.assertEqual(messages_result['pagination']['target_message_id'], target_message.id)
        self.assertIn(
            target_message.id,
            [message['id'] for message in messages_result['messages']],
        )
        self.assertTrue(messages_result['pagination']['has_newer'])
