# Parrot Messenger Service

The Messenger service is the Django backend for direct rooms, group rooms, stories, messages, message replies, reactions, edit/delete state, encrypted attachments, encrypted media upload intents, E2EE device keys, recovery-key backups, presence, receipts, and WebSocket events.

Messenger does not own user accounts. It trusts short-lived Messenger JWTs issued by the Parent service and calls Parent's internal policy APIs before allowing sends, resolving group members, showing presence/receipts, or exposing story audiences.

## Tech Stack

- Python 3.12
- Django
- Django Channels
- Redis for channel layer/cache in production
- SQLite or PostgreSQL for persistence
- Cloudinary for encrypted attachment storage
- PyJWT
- cryptography for Ed25519 device-management signature verification

## Local Setup

Create and activate a virtual environment:

```powershell
python -m venv venv
venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt
```

Run migrations:

```powershell
venv\Scripts\python.exe manage.py migrate
```

Run the ASGI development server:

```powershell
venv\Scripts\python.exe manage.py runserver 0.0.0.0:8000
```

Base URL:

```text
http://127.0.0.1:8000
```

## Environment Variables

```env
DJANGO_ENV=development
SECRET_KEY=local_django_secret
DEBUG=True
DATABASE_URL=sqlite:///db.sqlite3
REDIS_URL=redis://127.0.0.1:6379/0
CORS_ALLOWED_ORIGINS=http://localhost:5173,http://127.0.0.1:5173
CSRF_TRUSTED_ORIGINS=http://localhost:5173,http://127.0.0.1:5173
PARENT_SERVICE_URL=http://127.0.0.1:5000
INTERNAL_SERVICE_TOKEN=shared_internal_service_token
PARENT_SERVICE_TIMEOUT_SECONDS=5
MESSAGING_JWT_SECRET=shared_messenger_jwt_secret
MESSAGING_JWT_ISSUER=parrot-parent
MESSAGING_JWT_AUDIENCE=parrot-messenger
MESSAGING_WS_TOKEN_TTL_SECONDS=300
CLOUDINARY_URL=cloudinary://key:secret@cloud
CLOUDINARY_MAIN_FOLDER=Parrot
MESSAGING_MAX_UPLOAD_FILE_SIZE_BYTES=26214400
```

Parent and Messenger must share:

- `INTERNAL_SERVICE_TOKEN`
- `MESSAGING_JWT_SECRET`
- `MESSAGING_JWT_ISSUER`
- `MESSAGING_JWT_AUDIENCE`

`CLOUDINARY_MAIN_FOLDER` is the root folder for new Messenger uploads. Direct-message upload intents use `Parrot/<sender-username-account>/direct messages/<contact-name-account>/`, group message upload intents use `Parrot/<sender-username-account>/groupmessages/<group-name>/`, and story media uses `Parrot/<sender-username-account>/stories/`.

## Project Layout

```text
Messenger/
|-- messaging/
|   |-- urls.py                    # HTTP API routes
|   |-- views.py                   # request handlers
|   |-- services.py                # room/message business logic
|   |-- consumers.py               # WebSocket consumers
|   |-- routing.py                 # WebSocket routes
|   |-- auth.py                    # Messenger JWT validation
|   |-- realtime.py                # Channels event broadcasting
|   |-- e2ee/
|   |   |-- devices/               # linked-device registration/signatures
|   |   |-- backups.py             # encrypted recovery-key backup metadata
|   |   |-- files.py               # encrypted attachment upload
|   |   `-- payloads.py
|   |-- models.py
|   |-- admin.py
|   `-- tests.py
|-- group_messaging/
|   |-- urls.py                    # group room, member, message, upload routes
|   |-- views.py
|   |-- services.py                # group roles/messages/receipts/uploads
|   |-- serializers.py
|   `-- models.py
|-- stories/
|   |-- urls.py                    # story feed, upload, reaction/reply routes
|   |-- views.py
|   |-- services.py                # encrypted story lifecycle and cleanup
|   |-- policy.py                  # Parent story visibility checks
|   |-- models.py
|   `-- management/commands/cleanup_stories.py
|-- messenger_service/
|   |-- asgi.py                    # HTTP + WebSocket ASGI app
|   |-- settings.py
|   |-- urls.py
|   `-- wsgi.py
|-- manage.py
|-- requirements.txt
`-- Dockerfile
```

## Authentication

All Messenger APIs except `GET /health/` require:

```text
Authorization: Bearer <messaging_token>
```

The React frontend obtains this token from:

```text
POST /parent/messaging/token
```

The token includes:

- `sub` / `user_id`
- `account_number`
- issuer `parrot-parent`
- audience `parrot-messenger`
- short expiry

## HTTP API Documentation

### `GET /health/`

Checks service, database, Redis/channel cache, and Parent reachability.

Success or partial response:

```json
{
  "status": "ok",
  "service": "messenger",
  "checks": {
    "database": {"ok": true},
    "redis": {"ok": true},
    "parents": {"ok": true}
  }
}
```

### `GET /rooms/`

Returns rooms visible to the authenticated user.

```json
{
  "status": "ok",
  "result": {
    "rooms": []
  }
}
```

### `GET /rooms/<room_id>/messages/`

Returns paginated room messages for a participant.

Query parameters:

| Parameter | Purpose |
|---|---|
| `limit` | message count, default from service logic |
| `before_message_id` | pagination before a message id |
| `around_message_id` | load a page centered around a message id |

### `POST /rooms/<room_id>/delivered/`

Marks messages in a room as delivered for the authenticated recipient.

Request:

```json
{
  "last_delivered_message_id": 123
}
```

### `POST /rooms/<room_id>/read/`

Marks messages in a room as read for the authenticated user.

Request:

```json
{
  "last_read_message_id": 123
}
```

### `POST /rooms/<room_id>/blocked-messages/release/`

Releases blocked messages after the user unblocks or permits delivery.

### `POST /messages/authorize/`

Checks whether the authenticated sender may message a recipient account number.

Request:

```json
{
  "recipient_account_number": "7XXXXXXXXX"
}
```

Messenger calls Parent internally and may also allow an existing shared room fallback when appropriate.

### `POST /messages/send/`

Sends a message to a recipient. The body can be JSON or multipart form-data.

JSON request:

```json
{
  "recipient_account_number": "7XXXXXXXXX",
  "text": "{\"type\":\"e2ee.message\",...}",
  "client_message_id": "client-generated-id",
  "reply_to_message_id": 12,
  "encrypted_upload_intent_ids": ["uuid-from-completed-upload-intent"]
}
```

Multipart request uses `attachments`, `files`, or `media` fields for legacy backend-uploaded media. React encrypts message text and E2EE attachments before calling this endpoint.

`client_message_id` is unique per sender. If the same sender repeats a non-empty `client_message_id`, Messenger returns the existing message instead of creating a duplicate. React uses this with its local FIFO send queue.

When E2EE attachments are uploaded directly to Cloudinary, Messenger requires the completed upload intent ids on the message send. Each intent must belong to the authenticated sender, recipient, and `client_message_id`, must be completed, and must not be expired or consumed.

Encrypted voice notes and encrypted audio/video attachments use the same send contract. React stores voice-note and media presentation metadata inside the frontend-encrypted message payload, so Messenger does not need to read waveform data, playable duration, captions, or media type hints to deliver the message.

### `POST /messages/<message_id>/edit/`

Edits a direct message sent by the authenticated user.

Request:

```json
{
  "text": "{\"type\":\"e2ee.message\",...}"
}
```

Rules:

- only the original sender can edit
- only direct-room messages can use this endpoint
- the message must not already be deleted
- the edit window is open only until `created_at + 15 minutes`
- Messenger re-checks Parent authorization before accepting the edited payload
- the edited message is reset to `sent`; delivery/read state is recalculated after the edit
- the response includes `edited_at` and `action_expires_at` in the serialized message

Success response:

```json
{
  "status": "ok",
  "result": {
    "status": "edited",
    "message": {"id": 123, "edited_at": "2026-06-06T10:00:00+00:00"}
  }
}
```

Expired edit response:

```json
{
  "status": "timeout",
  "action": "edit",
  "message": "Messages can only be edited within 15 minutes.",
  "can_edit": false,
  "can_delete": false
}
```

After success, Messenger broadcasts `message.edited` to the room and participant inbox sockets.

### `POST /messages/<message_id>/delete/`

Deletes a direct message for both sides.

Rules:

- only the original sender can delete
- the delete window is open only until `created_at + 15 minutes`
- Messenger creates a deleted-message tombstone by clearing text/story context and setting `deleted_at`
- reactions and attachment rows are removed
- related encrypted Cloudinary resources are deleted on a best-effort basis

Success response:

```json
{
  "status": "ok",
  "result": {
    "status": "deleted",
    "message": {"id": 123, "deleted_at": "2026-06-06T10:00:00+00:00"}
  }
}
```

After success, Messenger broadcasts `message.deleted` to the room and participant inbox sockets.

### `POST /messages/<message_id>/reaction/`

Adds, changes, or removes the authenticated user's reaction for a message in a room they participate in.

Request:

```json
{
  "reaction": "heart"
}
```

Supported reaction keys:

| Key | UI Meaning |
|---|---|
| `thumbs_up` | thumbs up |
| `heart` | heart |
| `laugh` | laugh |
| `surprised` | surprised |
| `sad` | sad |

Rules:

- one user can have only one reaction per message
- sending the same reaction again removes that user's reaction
- sending a different supported reaction replaces the previous one
- reactions are stored as constrained keys, not encrypted message text
- the response includes grouped `reactions` counts and `my_reaction` for the current user

Success response:

```json
{
  "status": "ok",
  "result": {
    "message_id": 123,
    "reaction": "heart",
    "reactions": [
      {
        "reaction": "heart",
        "count": 1,
        "reacted_by_me": true
      }
    ],
    "my_reaction": "heart"
  }
}
```

After a successful change, Messenger broadcasts `message.reaction_updated` to the room and inbox participants so React can update open conversations and room cache data in real time.

## Group Messaging API

Group routes are mounted under `/groups/` and use the same Messenger JWT as direct messaging. A group room is still a `Room`, but it has group profile, membership, message, receipt, reaction, upload-intent, and action-log records.

### Group Roles

| Role | Permissions |
|---|---|
| `admin` | update group, manage members, promote/remove sub-admins, transfer admin, delete group, send messages |
| `sub_admin` | update group, add/remove normal members, send messages |
| `member` | send messages, react, read, leave group |

Group members must be saved contacts resolved by Parent. The creator is added automatically and cannot be included in `member_account_numbers`.

### Group Room Routes

| Route | Method | Purpose |
|---|---|---|
| `/groups/` | `POST` | create a group with `title` and `member_account_numbers` |
| `/groups/<room_id>/` | `GET` | fetch group room detail |
| `/groups/<room_id>/` | `PATCH` | update group name; admin or sub-admin |
| `/groups/<room_id>/` | `DELETE` | delete group; admin only |
| `/groups/<room_id>/avatar/` | `POST` | upload/replace group picture; image up to 5 MB |
| `/groups/<room_id>/members/` | `POST` | add saved contacts as members; admin or sub-admin |
| `/groups/<room_id>/members/<user_id>/` | `DELETE` | remove a member |
| `/groups/<room_id>/members/<user_id>/sub-admin/` | `POST` | promote member to sub-admin; admin only |
| `/groups/<room_id>/members/<user_id>/sub-admin/` | `DELETE` | remove sub-admin role; admin only |
| `/groups/<room_id>/admin-transfer/` | `POST` | transfer admin to another active member |
| `/groups/<room_id>/leave/` | `POST` | leave group; admin must transfer admin first |

Group action logs are emitted for create, update, avatar update, member add/remove/leave, sub-admin changes, admin transfer, and group delete.

### Group Message Routes

| Route | Method | Purpose |
|---|---|---|
| `/groups/<room_id>/messages/` | `GET` | fetch paginated group messages |
| `/groups/<room_id>/messages/send/` | `POST` | send encrypted group text/reply/media |
| `/groups/<room_id>/messages/<message_id>/edit/` | `POST` | edit own group message within 15 minutes |
| `/groups/<room_id>/messages/<message_id>/delete/` | `POST` | delete own group message for everyone within 15 minutes |
| `/groups/<room_id>/messages/<message_id>/reaction/` | `POST` | add/change/remove group message reaction |
| `/groups/<room_id>/messages/delivered/` | `POST` | mark group messages delivered for the current member |
| `/groups/<room_id>/messages/read/` | `POST` | mark group messages read for the current member |
| `/groups/<room_id>/receipts/visibility/prewarm/` | `POST` | warm Parent receipt visibility policy for group members |

Group edit/delete rules match direct messages: sender-only, 15-minute window, not already deleted, and serialized messages expose `edited_at`, `deleted_at`, and `action_expires_at`. Edits reset group message status to `sent` and clear per-member delivered/read timestamps. Deletes clear text, remove reactions, reset receipts, keep a tombstone, and try to delete encrypted Cloudinary resources tied to completed upload intents.

### Group E2EE Upload Routes

| Route | Method | Purpose |
|---|---|---|
| `/groups/<room_id>/crypto/devices/` | `GET` | list active recipient devices for every active group member |
| `/groups/<room_id>/crypto/files/upload-intents/` | `POST` | create signed Cloudinary upload intents for encrypted group attachments |
| `/groups/<room_id>/crypto/files/upload-intents/<upload_intent_id>/complete/` | `POST` | verify Cloudinary response and mark an intent complete |

Group upload intents are bound to room, sender, `client_message_id`, attachment id/index, encrypted byte size, and Cloudinary public id. A group message may reference up to 10 completed encrypted upload intents.

## Stories API

Story routes are mounted under `/stories/`. Stories are encrypted in React, stored by Messenger, and filtered through Parent audience/visibility policy.

### Story Routes

| Route | Method | Purpose |
|---|---|---|
| `/stories/` | `POST` | create a text or media story |
| `/stories/feed/` | `GET` | list visible active stories from contacts |
| `/stories/mine/` | `GET` | list the authenticated user's active stories and view counts |
| `/stories/settings/` | `GET` | read default story expiry/audience settings |
| `/stories/settings/` | `PUT` | update default story expiry/audience settings |
| `/stories/upload-intents/` | `POST` | create a signed Cloudinary upload intent for encrypted story media |
| `/stories/upload-intents/<upload_intent_id>/complete/` | `POST` | verify Cloudinary story media upload response |
| `/stories/<story_id>/` | `DELETE` | delete own story |
| `/stories/<story_id>/view/` | `POST` | mark a visible story viewed |
| `/stories/<story_id>/viewers/` | `GET` | list viewers for own story |
| `/stories/<story_id>/reaction/` | `POST` | react to a contact story and create a direct story-context message |
| `/stories/<story_id>/reply/` | `POST` | reply to a contact story and create a direct story-context message |
| `/stories/internal/cleanup-expired/` | `POST` | internal cleanup for expired story media |

Story rules:

- story types are `media` or `text`
- expiry is 6, 12, or 24 hours
- visibility is `all_contacts` or `specific_contacts`
- media stories support one encrypted image or video upload intent
- text stories store encrypted text/theme payload only
- view records can be hidden from the owner when Parent ghost policy says the viewer ghosted the owner
- story reactions use the same five reaction keys as messages

The cleanup endpoint and `cleanup_stories` management command mark expired stories and remove expired story Cloudinary media according to the Django settings `STORIES_EXPIRED_MEDIA_RETENTION_DAYS` and `STORIES_EXPIRED_MEDIA_CLEANUP_LIMIT`, falling back to service defaults when those settings are not configured.

### `POST /crypto/devices/`

Registers or updates the current browser/device E2EE keys.

Request:

```json
{
  "device_id": "uuid-or-device-id",
  "device_name": "Chrome on Windows",
  "public_key": "<base64 libsodium 32-byte public key>",
  "encryption_public_key": "<same public key>",
  "management_public_key": "<base64 Ed25519 32-byte public key>"
}
```

Success:

```json
{
  "status": "ok",
  "result": {
    "device": {
      "device_id": "uuid-or-device-id",
      "is_default": false,
      "status": "active"
    }
  }
}
```

### `GET /crypto/users/<user_id>/devices/`

Lists active device public keys for the authenticated user or a user sharing a room with the authenticated user.

When the authenticated user lists their own devices, the response also includes `default_password_configured`.

### `GET /crypto/recipients/<recipient_account_number>/devices/`

Authorizes the recipient with Parent and returns active recipient device public keys for encryption.

### `POST /crypto/devices/<device_id>/default/`

Makes a linked device default. Requires a signed device action and the default-device password.

Request:

```json
{
  "acting_device_id": "current-device-id",
  "action_timestamp": 1710000000,
  "action_nonce": "client-generated-nonce",
  "action_signature": "<base64 Ed25519 signature>",
  "default_password": "user-entered password"
}
```

Rules:

- if no default exists, a device may only make itself default and the supplied password creates the default-device password hash
- if a default exists and no password hash exists yet, only the current default device can create it
- the current default device can make another active device default after password verification
- a non-default device can make only itself default after password verification
- a non-default device cannot make another device default
- failed password verification is rate-limited per acting device

### `POST /crypto/devices/default-password/`

Updates the default-device password. Requires a signed `device.default_password.update` action from the current default device, the current password, and a new password.

Request:

```json
{
  "acting_device_id": "current-default-device-id",
  "action_timestamp": 1710000000,
  "action_nonce": "client-generated-nonce",
  "action_signature": "<base64 Ed25519 signature>",
  "current_default_password": "current password",
  "new_default_password": "new password"
}
```

Rules:

- only the current default device can update this password
- the current password must verify against Messenger's stored hash
- the new password must be different
- failed current-password verification is rate-limited per acting device

### `POST /crypto/devices/<device_id>/revoke/`

Revokes or logs out a linked device. Requires a signed device action.

Rules:

- the default device may delete other non-default device rows
- a non-default device may delete its own device row during logout
- a default device logout is retained: Messenger returns `retained_default=true` and does not delete the default row
- deleted devices are no longer returned by device-list APIs

Non-default logout response:

```json
{
  "status": "ok",
  "result": {
    "revoked": true,
    "deleted": true,
    "retained_default": false,
    "local_device_should_clear": true,
    "device_id": "non-default-device-id"
  }
}
```

Default-device logout response:

```json
{
  "status": "ok",
  "result": {
    "revoked": false,
    "deleted": false,
    "retained_default": true,
    "local_device_should_clear": false,
    "device_id": "default-device-id"
  }
}
```

### `POST /crypto/files/`

Legacy endpoint that uploads an encrypted attachment blob through Messenger.

Multipart request:

```text
file=<encrypted blob>
```

Response includes an encrypted file URL and Cloudinary metadata.

### `POST /crypto/files/upload-intents/`

Creates short-lived signed Cloudinary upload intents for encrypted attachment blobs. Messenger authorizes the sender and recipient before issuing signatures.

Request:

```json
{
  "recipient_account_number": "7XXXXXXXXX",
  "client_message_id": "client-generated-id",
  "attachments": [
    {
      "id": "client-attachment-id",
      "file_name": "photo.jpg",
      "mime_type": "image/jpeg",
      "file_size_bytes": 12345,
      "encrypted_file_size_bytes": 12361,
      "sort_order": 0
    }
  ]
}
```

Response:

```json
{
  "status": "ok",
  "result": {
    "upload_intents": [
      {
        "id": "upload-intent-uuid",
        "upload_url": "https://api.cloudinary.com/v1_1/<cloud>/raw/upload",
        "api_key": "<cloudinary api key>",
        "resource_type": "raw",
        "parameters": {
          "folder": "Parrot/sender-7000000001/direct messages/Recipient-7000000002",
          "public_id": "server-generated-id.txt",
          "timestamp": 1710000000,
          "overwrite": "false",
          "unique_filename": "false",
          "use_filename": "false",
          "api_key": "<cloudinary api key>",
          "signature": "<server-generated signature>"
        }
      }
    ]
  }
}
```

The response never includes the Cloudinary API secret.

Upload intents validate ownership, recipient authorization, byte sizes, expiry, and completion state. They do not decode or classify encrypted media. Voice notes, inline audio files, and inline video files are all stored as encrypted Cloudinary `raw` resources from Messenger's point of view.

### `POST /crypto/files/upload-intents/<upload_intent_id>/complete/`

Completes a direct Cloudinary upload. Messenger verifies the Cloudinary response signature, public id, resource type, and uploaded byte count before marking the intent complete.

Request body is the Cloudinary upload response JSON. Success returns the finalized encrypted file URL and metadata for the frontend E2EE attachment payload.

### `GET /crypto/key-backup/`

Returns the authenticated user's encrypted recovery backup metadata.

The backup is encrypted in the browser. Messenger stores ciphertext and KDF metadata only.

### `POST /crypto/key-backup/`

Saves/replaces the encrypted recovery backup. Requires a default-device signed action.

Request includes:

```json
{
  "public_key": "<base64 public key>",
  "encrypted_private_key": "<base64 encrypted identity payload>",
  "salt": "<base64 PBKDF2 salt>",
  "nonce": "<base64 XChaCha20 nonce>",
  "kdf_algorithm": "PBKDF2-SHA256",
  "kdf_iterations": 600000,
  "acting_device_id": "default-device-id",
  "action_timestamp": 1710000000,
  "action_nonce": "unique-nonce",
  "action_signature": "<base64 Ed25519 signature>"
}
```

After a successful update, Messenger broadcasts:

```json
{
  "type": "recovery.key_updated",
  "backup_updated_at": "2026-05-18T00:00:00+00:00",
  "updated_by_device_id": "default-device-id"
}
```

## Signed Device Actions

Privileged device actions require a local Ed25519 signature from the acting device's management private key.

Signed message format:

```text
parrot-device-action-v1
{action}
{user_id}
{acting_device_id}
{target_device_id}
{timestamp}
{nonce}
```

Supported actions:

| Action | Target | Purpose |
|---|---|---|
| `device.default` | target device id | change default linked device |
| `device.default_password.update` | `default-password` | update default-device password |
| `device.revoke` | target device id | revoke/logout device |
| `recovery.backup.save` | `key-backup` | save recovery backup |

Replay protection:

- signatures expire after 5 minutes
- each nonce is accepted only once per user/device

## WebSocket API

WebSockets authenticate with the Messenger JWT as a query parameter:

```text
ws://127.0.0.1:8000/ws/inbox/?token=<messaging_token>
ws://127.0.0.1:8000/ws/rooms/<room_id>/?token=<messaging_token>
```

The backend broadcasts the same room/message status events to room groups and participant inbox groups. React uses the room socket for the active conversation and the inbox socket as a fallback path for room-message and status updates.

### Inbox Socket

Receives account-wide events:

- `connection.accepted`
- `presence.snapshot`
- `presence.online`
- `presence.offline`
- `message.sent`
- `message.edited`
- `message.deleted`
- `message.delivered`
- `message.read`
- `message.reaction_updated`
- `group.created`
- `group.updated`
- `group.avatar_updated`
- `group.member_added`
- `group.member_removed`
- `group.member_left`
- `group.sub_admin_added`
- `group.sub_admin_removed`
- `group.admin_transferred`
- `group.deleted`
- `group.message.sent`
- `group.message.edited`
- `group.message.deleted`
- `group.message.delivered`
- `group.message.read`
- `group.message.reaction_updated`
- `story.created`
- `story.deleted`
- `story.viewed`
- `device.revoked`
- `device.default_changed`
- `recovery.key_updated`

Client may send:

```json
{"type": "ping"}
```

### Room Socket

Receives room-scoped events and typing state.

Room participants receive direct and group message events including `message.reaction_updated`, `message.edited`, `message.deleted`, `group.message.reaction_updated`, `group.message.edited`, and `group.message.deleted` when those events apply to the open room.

Client may send:

```json
{"type": "ping"}
```

```json
{"type": "typing.started"}
```

```json
{"type": "typing.stopped"}
```

## Tests

Run focused E2EE/device tests:

```powershell
venv\Scripts\python.exe manage.py test messaging.tests.CryptoDeviceKeyTests
```

Run all tests:

```powershell
venv\Scripts\python.exe manage.py test
```

Run group or story tests:

```powershell
venv\Scripts\python.exe manage.py test group_messaging stories
```

Check migrations:

```powershell
venv\Scripts\python.exe manage.py makemigrations --check --dry-run
```

## Production Notes

- Use Redis for `CHANNEL_LAYERS` and cache in production.
- Keep `MESSAGING_JWT_SECRET` and `INTERNAL_SERVICE_TOKEN` out of source control.
- Run migrations before deploying code that changes models.
- Messenger stores encrypted payloads and public keys, not the user's recovery key or message private keys.
- Keep Cloudinary cleanup observable in production logs; message and story deletes are best-effort for remote media removal and should not fail the user-visible tombstone operation just because Cloudinary cleanup has a transient error.
