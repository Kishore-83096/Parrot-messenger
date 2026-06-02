import json
from datetime import timedelta
from unittest.mock import patch

import jwt
from django.conf import settings
from django.test import TestCase, override_settings
from django.utils import timezone

from messaging.models import Message

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
from .services import (
    cleanup_stories,
    complete_story_media_upload_intent,
    create_story_from_upload_intents,
    create_story_media_upload_intents,
    mark_expired_stories,
)


TEST_STORY_SETTINGS = {
    'MESSAGING_JWT_SECRET': 'test-messenger-secret-at-least-32-bytes',
    'MESSAGING_JWT_ISSUER': 'parrot-parent',
    'MESSAGING_JWT_AUDIENCE': 'parrot-messenger',
    'CLOUDINARY_URL': 'cloudinary://test-key:test-secret@test-cloud',
    'CLOUDINARY_MAIN_FOLDER': 'MAIN',
    'CACHES': {
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'parrot-stories-tests',
        },
    },
}


@override_settings(**TEST_STORY_SETTINGS)
class StoryUploadIntentTests(TestCase):
    sender = {
        'user_id': 1,
        'account_number': '7000000001',
    }
    parent_audience = {
        'allowed': True,
        'owner_user_id': 1,
        'owner_account_number': '7000000001',
        'valid_contacts': [
            {
                'user_id': 2,
                'account_number': '7000000002',
            },
        ],
    }

    def auth_header(self, user_id=1, account_number='7000000001'):
        now = timezone.now()
        token = jwt.encode(
            {
                'sub': str(user_id),
                'user_id': user_id,
                'account_number': account_number,
                'iss': settings.MESSAGING_JWT_ISSUER,
                'aud': settings.MESSAGING_JWT_AUDIENCE,
                'iat': now,
                'exp': now + timedelta(minutes=5),
            },
            settings.MESSAGING_JWT_SECRET,
            algorithm='HS256',
        )

        return f'Bearer {token}'

    def story_media_payload(self):
        return {
            'client_story_id': 'story-client-1',
            'media': [
                {
                    'id': 'media-1',
                    'file_name': 'photo.jpg',
                    'mime_type': 'image/jpeg',
                    'file_size_bytes': 1024,
                    'encrypted_file_size_bytes': 1408,
                    'sort_order': 0,
                },
                {
                    'id': 'media-2',
                    'file_name': 'clip.mp4',
                    'mime_type': 'video/mp4',
                    'file_size_bytes': 2048,
                    'encrypted_file_size_bytes': 2496,
                    'sort_order': 1,
                },
            ],
        }

    def completed_upload_intent(self, client_story_id='story-client-1', media_type='image'):
        return StoryUploadIntent.objects.create(
            owner_user_id=self.sender['user_id'],
            owner_account_number=self.sender['account_number'],
            client_story_id=client_story_id,
            media_client_id=f'{media_type}-1',
            media_index=0,
            media_type=media_type,
            original_file_name='photo.jpg' if media_type == 'image' else 'clip.mp4',
            original_mime_type='image/jpeg' if media_type == 'image' else 'video/mp4',
            original_file_size_bytes=1024,
            encrypted_file_size_bytes=1408,
            cloudinary_public_id=f'MAIN/e2ee/stories/user-1/{client_story_id}-{media_type}.txt',
            cloudinary_asset_id=f'asset-{media_type}',
            cloudinary_resource_type='raw',
            cloudinary_folder='MAIN/e2ee/stories/user-1',
            secure_url=f'https://res.cloudinary.com/test/raw/upload/v1/{client_story_id}-{media_type}.txt',
            status=StoryUploadIntent.STATUS_COMPLETED,
            signature_timestamp=123,
            expires_at=timezone.now() + timedelta(minutes=10),
            completed_at=timezone.now(),
        )

    def allowed_visibility(self):
        return (
            {
                'ok': True,
                'parent': {
                    'response': {
                        'allowed': True,
                        'owner_user_id': 2,
                        'owner_account_number': '7000000002',
                        'viewer_user_id': 1,
                        'viewer_account_number': '7000000001',
                        'viewer_contact': {
                            'alias_name': 'Story Owner',
                        },
                    },
                    'status_code': 200,
                },
            },
            200,
        )

    def test_create_story_upload_intents_for_image_and_video(self):
        result, status_code = create_story_media_upload_intents(
            self.sender,
            self.parent_audience,
            self.story_media_payload(),
        )

        self.assertEqual(status_code, 201)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(len(result['upload_intents']), 2)
        self.assertEqual(StoryUploadIntent.objects.count(), 2)
        self.assertEqual(
            sorted(StoryUploadIntent.objects.values_list('media_type', flat=True)),
            ['image', 'video'],
        )

    def test_create_story_upload_intents_rejects_non_media_file(self):
        payload = {
            'client_story_id': 'story-client-2',
            'media': [
                {
                    'id': 'media-1',
                    'file_name': 'document.pdf',
                    'mime_type': 'application/pdf',
                    'file_size_bytes': 1024,
                    'encrypted_file_size_bytes': 1408,
                },
            ],
        }

        result, status_code = create_story_media_upload_intents(
            self.sender,
            self.parent_audience,
            payload,
        )

        self.assertEqual(status_code, 400)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(StoryUploadIntent.objects.count(), 0)

    @patch('messaging.e2ee.files.verify_api_response_signature', return_value=True)
    def test_complete_story_upload_intent_marks_intent_completed(self, _verify_signature):
        create_result, _ = create_story_media_upload_intents(
            self.sender,
            self.parent_audience,
            self.story_media_payload(),
        )
        upload_intent_id = create_result['upload_intents'][0]['id']
        intent = StoryUploadIntent.objects.get(id=upload_intent_id)

        result, status_code = complete_story_media_upload_intent(
            self.sender,
            upload_intent_id,
            {
                'public_id': intent.cloudinary_public_id,
                'resource_type': 'raw',
                'bytes': intent.encrypted_file_size_bytes,
                'version': '1234567890',
                'signature': 'valid-signature',
                'asset_id': 'asset-1',
            },
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(result['status'], 'ok')
        self.assertTrue(result['file']['encrypted_file_url'])
        intent.refresh_from_db()
        self.assertEqual(intent.status, StoryUploadIntent.STATUS_COMPLETED)
        self.assertEqual(intent.cloudinary_asset_id, 'asset-1')

    @patch('stories.views.resolve_parent_story_audience')
    def test_create_story_upload_intents_api_uses_parent_policy(self, resolve_policy):
        resolve_policy.return_value = (
            {
                'ok': True,
                'parent': {
                    'response': self.parent_audience,
                    'status_code': 200,
                },
            },
            200,
        )

        response = self.client.post(
            '/stories/upload-intents/',
            data=json.dumps(
                {
                    **self.story_media_payload(),
                    'audience_account_numbers': ['7000000002'],
                }
            ),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body['status'], 'ok')
        self.assertEqual(len(body['result']['upload_intents']), 2)
        resolve_policy.assert_called_once_with(
            {
                'owner_user_id': 1,
                'audience_account_numbers': ['7000000002'],
            }
        )

    def test_create_story_consumes_completed_upload_intent(self):
        intent = self.completed_upload_intent()
        encrypted_payload = json.dumps(
            {
                'type': 'parrot.story.media',
                'v': 1,
                'caption': 'Media caption',
                'media': [],
            }
        )

        result, status_code = create_story_from_upload_intents(
            self.sender,
            self.parent_audience,
            {
                'client_story_id': 'story-client-1',
                'expiry_hours': 24,
                'visibility': 'specific_contacts',
                'audience_account_numbers': ['7000000002'],
                'encrypted_payload': encrypted_payload,
                'encrypted_upload_intent_ids': [str(intent.id)],
            },
        )

        self.assertEqual(status_code, 201)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(Story.objects.count(), 1)
        self.assertEqual(StoryMedia.objects.count(), 1)
        self.assertEqual(StoryAudience.objects.count(), 1)
        self.assertEqual(result['story']['encrypted_payload'], encrypted_payload)
        self.assertEqual(Story.objects.get().encrypted_payload, encrypted_payload)
        intent.refresh_from_db()
        self.assertEqual(intent.status, StoryUploadIntent.STATUS_CONSUMED)

    @patch('stories.views.resolve_parent_story_audience')
    def test_create_story_api_uses_parent_audience_policy(self, resolve_policy):
        intent = self.completed_upload_intent(client_story_id='story-client-api')
        resolve_policy.return_value = (
            {
                'ok': True,
                'parent': {
                    'response': self.parent_audience,
                    'status_code': 200,
                },
            },
            200,
        )

        response = self.client.post(
            '/stories/',
            data=json.dumps(
                {
                    'client_story_id': 'story-client-api',
                    'expiry_hours': 12,
                    'visibility': 'specific_contacts',
                    'audience_account_numbers': ['7000000002'],
                    'encrypted_upload_intent_ids': [str(intent.id)],
                }
            ),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body['status'], 'ok')
        self.assertEqual(body['result']['story']['audience_count'], 1)
        resolve_policy.assert_called_once_with(
            {
                'owner_user_id': 1,
                'audience_account_numbers': ['7000000002'],
            }
        )

    @patch('stories.views.resolve_parent_story_audience')
    def test_story_settings_api_persists_owner_defaults(self, resolve_policy):
        resolve_policy.return_value = (
            {
                'ok': True,
                'parent': {
                    'response': self.parent_audience,
                    'status_code': 200,
                },
            },
            200,
        )

        response = self.client.put(
            '/stories/settings/',
            data=json.dumps(
                {
                    'expiry_hours': 6,
                    'visibility': 'specific_contacts',
                    'audience_account_numbers': ['7000000002'],
                }
            ),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['status'], 'ok')
        self.assertEqual(body['result']['settings']['expiry_hours'], 6)
        self.assertEqual(
            body['result']['settings']['audience_account_numbers'],
            ['7000000002'],
        )
        settings_row = StorySettings.objects.get(owner_user_id=1)
        self.assertEqual(settings_row.visibility, Story.VISIBILITY_SPECIFIC_CONTACTS)
        self.assertEqual(settings_row.expiry_hours, 6)
        self.assertEqual(settings_row.audience_account_numbers, ['7000000002'])
        resolve_policy.assert_called_once_with(
            {
                'owner_user_id': 1,
                'audience_account_numbers': ['7000000002'],
            }
        )

        get_response = self.client.get(
            '/stories/settings/',
            HTTP_AUTHORIZATION=self.auth_header(),
        )
        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(
            get_response.json()['result']['settings']['visibility'],
            Story.VISIBILITY_SPECIFIC_CONTACTS,
        )
        self.assertTrue(get_response.json()['result']['has_saved_settings'])

    def test_get_story_settings_returns_unsaved_defaults_without_creating_row(self):
        response = self.client.get(
            '/stories/settings/',
            HTTP_AUTHORIZATION=self.auth_header(),
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn('no-cache', response.headers['Cache-Control'])
        body = response.json()
        self.assertEqual(body['status'], 'ok')
        self.assertFalse(body['result']['has_saved_settings'])
        self.assertEqual(
            body['result']['settings'],
            {
                'expiry_hours': 24,
                'visibility': Story.VISIBILITY_ALL_CONTACTS,
                'audience_account_numbers': [],
            },
        )
        self.assertFalse(StorySettings.objects.filter(owner_user_id=1).exists())

    @patch('stories.views.resolve_parent_story_audience')
    def test_create_story_api_uses_saved_settings_when_fields_are_missing(
        self,
        resolve_policy,
    ):
        StorySettings.objects.create(
            owner_user_id=1,
            owner_account_number='7000000001',
            expiry_hours=6,
            visibility=Story.VISIBILITY_SPECIFIC_CONTACTS,
            audience_account_numbers=['7000000002'],
        )
        intent = self.completed_upload_intent(client_story_id='story-client-settings')
        resolve_policy.return_value = (
            {
                'ok': True,
                'parent': {
                    'response': self.parent_audience,
                    'status_code': 200,
                },
            },
            200,
        )

        response = self.client.post(
            '/stories/',
            data=json.dumps(
                {
                    'client_story_id': 'story-client-settings',
                    'encrypted_upload_intent_ids': [str(intent.id)],
                }
            ),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )

        self.assertEqual(response.status_code, 201)
        story = Story.objects.get(client_story_id='story-client-settings')
        self.assertEqual(story.expiry_hours, 6)
        self.assertEqual(story.visibility, Story.VISIBILITY_SPECIFIC_CONTACTS)
        self.assertEqual(StoryAudience.objects.filter(story=story).count(), 1)
        resolve_policy.assert_called_once_with(
            {
                'owner_user_id': 1,
                'audience_account_numbers': ['7000000002'],
            }
        )

    @patch('stories.views.resolve_parent_story_audience')
    def test_create_text_story_api_without_upload_intents(self, resolve_policy):
        resolve_policy.return_value = (
            {
                'ok': True,
                'parent': {
                    'response': self.parent_audience,
                    'status_code': 200,
                },
            },
            200,
        )

        response = self.client.post(
            '/stories/',
            data=json.dumps(
                {
                    'client_story_id': 'story-client-text',
                    'story_type': 'text',
                    'expiry_hours': 12,
                    'visibility': 'specific_contacts',
                    'audience_account_numbers': ['7000000002'],
                    'encrypted_payload': json.dumps(
                        {
                            'type': 'parrot.story.text',
                            'v': 1,
                            'text': 'Text board story',
                            'background': 'lavender',
                        }
                    ),
                    'encrypted_upload_intent_ids': [],
                }
            ),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body['status'], 'ok')
        self.assertEqual(body['result']['story']['story_type'], Story.STORY_TYPE_TEXT)
        story = Story.objects.get(client_story_id='story-client-text')
        self.assertEqual(story.story_type, Story.STORY_TYPE_TEXT)
        self.assertEqual(StoryMedia.objects.filter(story=story).count(), 0)
        self.assertEqual(StoryAudience.objects.filter(story=story).count(), 1)
        resolve_policy.assert_called_once_with(
            {
                'owner_user_id': 1,
                'audience_account_numbers': ['7000000002'],
            }
        )

    @patch('stories.views.broadcast_user_event')
    @patch('stories.views.resolve_parent_story_audience')
    def test_create_story_api_broadcasts_story_created_to_audience(
        self,
        resolve_policy,
        broadcast_user_event,
    ):
        intent = self.completed_upload_intent(client_story_id='story-client-broadcast')
        resolve_policy.return_value = (
            {
                'ok': True,
                'parent': {
                    'response': self.parent_audience,
                    'status_code': 200,
                },
            },
            200,
        )

        response = self.client.post(
            '/stories/',
            data=json.dumps(
                {
                    'client_story_id': 'story-client-broadcast',
                    'expiry_hours': 12,
                    'visibility': 'specific_contacts',
                    'audience_account_numbers': ['7000000002'],
                    'encrypted_upload_intent_ids': [str(intent.id)],
                }
            ),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )

        self.assertEqual(response.status_code, 201)
        broadcast_user_event.assert_called_once()
        user_id, event_type, payload = broadcast_user_event.call_args.args
        self.assertEqual(user_id, 2)
        self.assertEqual(event_type, 'story.created')
        self.assertEqual(payload['owner']['user_id'], 1)
        self.assertEqual(payload['story']['client_story_id'], 'story-client-broadcast')

    @patch('stories.services.authorize_parent_story_visibility')
    def test_story_feed_groups_visible_contacts_and_marks_viewed(self, authorize_visibility):
        visible_story = self.create_feed_story(
            owner_user_id=2,
            owner_account_number='7000000002',
            client_story_id='visible-story',
        )
        denied_story = self.create_feed_story(
            owner_user_id=3,
            owner_account_number='7000000003',
            client_story_id='denied-story',
        )
        StoryView.objects.create(
            story=visible_story,
            viewer_user_id=1,
            viewer_account_number='7000000001',
        )

        def visibility_side_effect(payload):
            if payload['owner_user_id'] == denied_story.owner_user_id:
                return (
                    {
                        'ok': False,
                        'parent': {
                            'response': {
                                'allowed': False,
                                'reason': 'viewer_blocked_owner',
                            },
                            'status_code': 403,
                        },
                    },
                    403,
                )

            return (
                {
                    'ok': True,
                    'parent': {
                        'response': {
                            'allowed': True,
                            'viewer_contact': {
                                'alias_name': 'Visible Contact',
                                'profile_picture': 'https://cdn.example.test/visible.png',
                            },
                        },
                        'status_code': 200,
                    },
                },
                200,
            )

        authorize_visibility.side_effect = visibility_side_effect

        response = self.client.get(
            '/stories/feed/',
            HTTP_AUTHORIZATION=self.auth_header(),
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['status'], 'ok')
        contacts = body['result']['contacts']
        self.assertEqual(len(contacts), 1)
        self.assertEqual(contacts[0]['user_id'], 2)
        self.assertEqual(contacts[0]['contact']['alias_name'], 'Visible Contact')
        self.assertEqual(
            contacts[0]['contact']['profile_picture'],
            'https://cdn.example.test/visible.png',
        )
        self.assertEqual(contacts[0]['unviewed_count'], 0)
        self.assertTrue(contacts[0]['stories'][0]['viewed'])

    @patch('stories.services.authorize_parent_story_visibility')
    def test_story_feed_orders_latest_story_first(self, authorize_visibility):
        now = timezone.now()
        first_story = self.create_feed_story(
            owner_user_id=2,
            owner_account_number='7000000002',
            client_story_id='first-story',
        )
        latest_story = self.create_feed_story(
            owner_user_id=2,
            owner_account_number='7000000002',
            client_story_id='latest-story',
        )
        Story.objects.filter(id=first_story.id).update(
            created_at=now - timedelta(minutes=2),
        )
        Story.objects.filter(id=latest_story.id).update(
            created_at=now - timedelta(minutes=1),
        )
        authorize_visibility.return_value = self.allowed_visibility()

        response = self.client.get(
            '/stories/feed/',
            HTTP_AUTHORIZATION=self.auth_header(),
        )

        self.assertEqual(response.status_code, 200)
        contact = response.json()['result']['contacts'][0]
        self.assertEqual(
            [story['client_story_id'] for story in contact['stories']],
            ['latest-story', 'first-story'],
        )
        self.assertEqual(contact['latest_story_at'], contact['stories'][0]['created_at'])

    def test_my_stories_returns_active_owner_stories_with_view_count(self):
        active_story = self.create_feed_story(
            owner_user_id=1,
            owner_account_number='7000000001',
            client_story_id='my-active-story',
        )
        expired_story = self.create_feed_story(
            owner_user_id=1,
            owner_account_number='7000000001',
            client_story_id='my-expired-story',
        )
        expired_story.expires_at = timezone.now() - timedelta(minutes=1)
        expired_story.save(update_fields=['expires_at', 'updated_at'])
        deleted_story = self.create_feed_story(
            owner_user_id=1,
            owner_account_number='7000000001',
            client_story_id='my-deleted-story',
        )
        deleted_story.status = Story.STATUS_DELETED
        deleted_story.save(update_fields=['status', 'updated_at'])
        StoryView.objects.create(
            story=active_story,
            viewer_user_id=2,
            viewer_account_number='7000000002',
        )
        StoryView.objects.create(
            story=active_story,
            viewer_user_id=3,
            viewer_account_number='7000000003',
        )

        response = self.client.get(
            '/stories/mine/',
            HTTP_AUTHORIZATION=self.auth_header(),
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['status'], 'ok')
        stories = body['result']['stories']
        self.assertEqual(len(stories), 1)
        self.assertEqual(stories[0]['client_story_id'], 'my-active-story')
        self.assertEqual(stories[0]['view_count'], 2)
        self.assertGreater(stories[0]['expires_in_seconds'], 0)
        self.assertEqual(stories[0]['media_preview'][0]['media_type'], 'image')

    @patch('stories.views.broadcast_user_event')
    def test_delete_story_marks_owner_story_deleted_and_broadcasts(
        self,
        broadcast_user_event,
    ):
        story = self.create_feed_story(
            owner_user_id=1,
            owner_account_number='7000000001',
            client_story_id='delete-my-story',
        )
        StoryAudience.objects.filter(story=story).delete()
        StoryAudience.objects.create(
            story=story,
            viewer_user_id=2,
            viewer_account_number='7000000002',
        )

        response = self.client.delete(
            f'/stories/{story.id}/',
            HTTP_AUTHORIZATION=self.auth_header(),
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['status'], 'ok')
        self.assertTrue(body['result']['deleted'])
        story.refresh_from_db()
        self.assertEqual(story.status, Story.STATUS_DELETED)
        self.assertIsNotNone(story.deleted_at)
        broadcast_user_event.assert_called_once()
        user_id, event_type, payload = broadcast_user_event.call_args.args
        self.assertEqual(user_id, 2)
        self.assertEqual(event_type, 'story.deleted')
        self.assertEqual(payload['story_id'], str(story.id))

    def test_delete_story_requires_owner(self):
        story = self.create_feed_story(
            owner_user_id=1,
            owner_account_number='7000000001',
            client_story_id='delete-other-owner-story',
        )

        response = self.client.delete(
            f'/stories/{story.id}/',
            HTTP_AUTHORIZATION=self.auth_header(
                user_id=2,
                account_number='7000000002',
            ),
        )

        self.assertEqual(response.status_code, 403)
        story.refresh_from_db()
        self.assertEqual(story.status, Story.STATUS_ACTIVE)

    def test_mark_expired_stories_updates_only_expired_active_stories(self):
        expired_story = self.create_feed_story(
            owner_user_id=1,
            owner_account_number='7000000001',
            client_story_id='expired-status-story',
        )
        active_story = self.create_feed_story(
            owner_user_id=1,
            owner_account_number='7000000001',
            client_story_id='active-status-story',
        )
        expired_story.expires_at = timezone.now() - timedelta(minutes=5)
        expired_story.save(update_fields=['expires_at', 'updated_at'])

        expired_count = mark_expired_stories()

        self.assertEqual(expired_count, 1)
        expired_story.refresh_from_db()
        active_story.refresh_from_db()
        self.assertEqual(expired_story.status, Story.STATUS_EXPIRED)
        self.assertEqual(active_story.status, Story.STATUS_ACTIVE)

    @patch('stories.services.cloudinary_uploader.destroy')
    def test_cleanup_stories_marks_expired_and_cleans_retained_media(
        self,
        cloudinary_destroy,
    ):
        story = self.create_feed_story(
            owner_user_id=1,
            owner_account_number='7000000001',
            client_story_id='cleanup-status-story',
        )
        story.expires_at = timezone.now() - timedelta(days=8)
        story.save(update_fields=['expires_at', 'updated_at'])
        media = story.media.first()
        StoryMedia.objects.filter(id=media.id).update(
            cloudinary_public_id='MAIN/e2ee/stories/user-1/cleanup-status-story.txt',
            cloudinary_resource_type='raw',
        )

        result = cleanup_stories(retention_days=7, media_limit=10)

        story.refresh_from_db()
        media.refresh_from_db()
        self.assertEqual(result['expired_stories'], 1)
        self.assertEqual(result['media_candidates'], 1)
        self.assertEqual(result['media_cleaned'], 1)
        self.assertEqual(story.status, Story.STATUS_EXPIRED)
        self.assertEqual(media.encrypted_file_url, '')
        cloudinary_destroy.assert_called_once_with(
            'MAIN/e2ee/stories/user-1/cleanup-status-story.txt',
            resource_type='raw',
        )

    @patch('stories.services.cloudinary_uploader.destroy')
    def test_cleanup_stories_deletes_expired_media_immediately_by_default(
        self,
        cloudinary_destroy,
    ):
        story = self.create_feed_story(
            owner_user_id=1,
            owner_account_number='7000000001',
            client_story_id='cleanup-immediate-status-story',
        )
        story.expires_at = timezone.now() - timedelta(seconds=1)
        story.save(update_fields=['expires_at', 'updated_at'])
        media = story.media.first()
        StoryMedia.objects.filter(id=media.id).update(
            cloudinary_public_id='MAIN/e2ee/stories/user-1/cleanup-immediate-status-story.txt',
            cloudinary_resource_type='raw',
        )

        result = cleanup_stories(media_limit=10)

        media.refresh_from_db()
        self.assertEqual(result['expired_stories'], 1)
        self.assertEqual(result['media_cleaned'], 1)
        self.assertEqual(media.encrypted_file_url, '')
        cloudinary_destroy.assert_called_once_with(
            'MAIN/e2ee/stories/user-1/cleanup-immediate-status-story.txt',
            resource_type='raw',
        )

    @patch('stories.services.authorize_parent_story_visibility')
    def test_mark_story_viewed_records_view_once(self, authorize_visibility):
        story = self.create_feed_story(
            owner_user_id=2,
            owner_account_number='7000000002',
            client_story_id='viewable-story',
        )
        authorize_visibility.return_value = (
            {
                'ok': True,
                'parent': {
                    'response': {
                        'allowed': True,
                        'viewer_user_id': 1,
                        'viewer_account_number': '7000000001',
                    },
                    'status_code': 200,
                },
            },
            200,
        )

        first_response = self.client.post(
            f'/stories/{story.id}/view/',
            HTTP_AUTHORIZATION=self.auth_header(),
        )
        second_response = self.client.post(
            f'/stories/{story.id}/view/',
            HTTP_AUTHORIZATION=self.auth_header(),
        )

        self.assertEqual(first_response.status_code, 201)
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(StoryView.objects.filter(story=story, viewer_user_id=1).count(), 1)
        self.assertTrue(first_response.json()['result']['created'])
        self.assertFalse(second_response.json()['result']['created'])

    @patch('stories.views.broadcast_user_event')
    @patch('stories.services.authorize_parent_story_visibility')
    def test_mark_story_viewed_broadcasts_owner_event(
        self,
        authorize_visibility,
        broadcast_user_event,
    ):
        story = self.create_feed_story(
            owner_user_id=2,
            owner_account_number='7000000002',
            client_story_id='view-broadcast-story',
        )
        authorize_visibility.return_value = (
            {
                'ok': True,
                'parent': {
                    'response': {
                        'allowed': True,
                        'viewer_user_id': 1,
                        'viewer_account_number': '7000000001',
                    },
                    'status_code': 200,
                },
            },
            200,
        )

        response = self.client.post(
            f'/stories/{story.id}/view/',
            HTTP_AUTHORIZATION=self.auth_header(),
        )

        self.assertEqual(response.status_code, 201)
        broadcast_user_event.assert_called_once()
        owner_user_id, event_type, payload = broadcast_user_event.call_args.args
        self.assertEqual(owner_user_id, 2)
        self.assertEqual(event_type, 'story.viewed')
        self.assertEqual(payload['story_id'], str(story.id))
        self.assertEqual(payload['viewer']['user_id'], 1)

    @patch('stories.services.authorize_parent_story_visibility')
    def test_mark_story_viewed_requires_story_audience(self, authorize_visibility):
        story = self.create_feed_story(
            owner_user_id=2,
            owner_account_number='7000000002',
            client_story_id='not-my-audience-story',
        )
        StoryAudience.objects.filter(story=story).delete()

        response = self.client.post(
            f'/stories/{story.id}/view/',
            HTTP_AUTHORIZATION=self.auth_header(),
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(StoryView.objects.filter(story=story).count(), 0)
        authorize_visibility.assert_not_called()

    def test_story_viewers_requires_owner(self):
        story = self.create_feed_story(
            owner_user_id=1,
            owner_account_number='7000000001',
            client_story_id='viewers-story',
        )
        StoryView.objects.create(
            story=story,
            viewer_user_id=2,
            viewer_account_number='7000000002',
        )

        owner_response = self.client.get(
            f'/stories/{story.id}/viewers/',
            HTTP_AUTHORIZATION=self.auth_header(),
        )
        other_response = self.client.get(
            f'/stories/{story.id}/viewers/',
            HTTP_AUTHORIZATION=self.auth_header(user_id=2, account_number='7000000002'),
        )

        self.assertEqual(owner_response.status_code, 200)
        self.assertEqual(owner_response.json()['result']['view_count'], 1)
        self.assertEqual(owner_response.json()['result']['viewers'][0]['user_id'], 2)
        self.assertEqual(other_response.status_code, 403)

    @patch('stories.views.broadcast_participant_event')
    @patch('stories.views.broadcast_room_event')
    @patch('stories.services.authorize_parent_story_visibility')
    def test_story_reaction_creates_chat_message_with_story_context(
        self,
        authorize_visibility,
        broadcast_room_event,
        broadcast_participant_event,
    ):
        story = self.create_feed_story(
            owner_user_id=2,
            owner_account_number='7000000002',
            client_story_id='reaction-story',
        )
        authorize_visibility.return_value = self.allowed_visibility()

        response = self.client.post(
            f'/stories/{story.id}/reaction/',
            data=json.dumps(
                {
                    'client_message_id': 'story-reaction-message-1',
                    'reaction': 'heart',
                }
            ),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(Message.objects.count(), 1)
        message = Message.objects.get()
        self.assertEqual(message.sender_user_id, 1)
        self.assertEqual(message.recipient_user_id, 2)
        self.assertEqual(message.text, '\u2764\ufe0f')
        self.assertEqual(message.story_context['type'], 'reaction')
        self.assertEqual(message.story_context['story_id'], str(story.id))
        self.assertEqual(message.story_context['preview_label'], 'Story')
        self.assertEqual(StoryReaction.objects.get().message_id, message.id)
        body = response.json()
        self.assertEqual(body['result']['message_result']['message']['story_context']['type'], 'reaction')
        broadcast_room_event.assert_called_once()
        broadcast_participant_event.assert_called_once()

    @patch('stories.views.broadcast_participant_event')
    @patch('stories.views.broadcast_room_event')
    @patch('stories.services.authorize_parent_story_visibility')
    def test_story_reply_creates_chat_message_with_story_context(
        self,
        authorize_visibility,
        broadcast_room_event,
        broadcast_participant_event,
    ):
        story = self.create_feed_story(
            owner_user_id=2,
            owner_account_number='7000000002',
            client_story_id='reply-story',
        )
        authorize_visibility.return_value = self.allowed_visibility()

        response = self.client.post(
            f'/stories/{story.id}/reply/',
            data=json.dumps(
                {
                    'client_message_id': 'story-reply-message-1',
                    'text': 'Nice story',
                }
            ),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth_header(),
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(Message.objects.count(), 1)
        message = Message.objects.get()
        self.assertEqual(message.text, 'Nice story')
        self.assertEqual(message.story_context['type'], 'reply')
        self.assertEqual(message.story_context['media_type'], 'image')
        self.assertEqual(StoryReply.objects.get().message_id, message.id)
        body = response.json()
        self.assertEqual(body['result']['message_result']['message']['story_context']['type'], 'reply')
        broadcast_room_event.assert_called_once()
        broadcast_participant_event.assert_called_once()

    def create_feed_story(self, owner_user_id, owner_account_number, client_story_id):
        story = Story.objects.create(
            owner_user_id=owner_user_id,
            owner_account_number=owner_account_number,
            client_story_id=client_story_id,
            visibility=Story.VISIBILITY_ALL_CONTACTS,
            expiry_hours=24,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        StoryMedia.objects.create(
            story=story,
            media_type=StoryMedia.MEDIA_IMAGE,
            encrypted_file_url=f'https://res.cloudinary.com/test/raw/upload/v1/{client_story_id}.txt',
            file_name='photo.jpg',
            mime_type='image/jpeg',
            file_size_bytes=1024,
            encrypted_file_size_bytes=1408,
            sort_order=0,
        )
        StoryAudience.objects.create(
            story=story,
            viewer_user_id=1,
            viewer_account_number='7000000001',
        )
        return story
