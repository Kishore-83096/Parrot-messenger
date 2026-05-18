# Parrot Messenger Service

The Messenger service is the Django backend for rooms, messages, encrypted attachments, E2EE device keys, recovery-key backups, presence, and WebSocket events.

Messenger does not own user accounts. It trusts short-lived Messenger JWTs issued by the Parent service and calls Parent's internal authorization API before allowing sends.

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
CLOUDINARY_MAIN_FOLDER=MAIN
MESSAGING_MAX_UPLOAD_FILE_SIZE_BYTES=26214400
```

Parent and Messenger must share:

- `INTERNAL_SERVICE_TOKEN`
- `MESSAGING_JWT_SECRET`
- `MESSAGING_JWT_ISSUER`
- `MESSAGING_JWT_AUDIENCE`

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
| `before_id` | pagination before a message id |
| `after_id` | pagination after a message id |

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
  "reply_to_message_id": 12
}
```

Multipart request uses `attachments`, `files`, or `media` fields. React encrypts message text and attachments before calling this endpoint.

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

### `GET /crypto/recipients/<recipient_account_number>/devices/`

Authorizes the recipient with Parent and returns active recipient device public keys for encryption.

### `POST /crypto/devices/<device_id>/default/`

Makes a linked device default. Requires a signed device action.

Rules:

- if no default exists, a device may only make itself default
- if a default exists, only the current default device can change the default
- recovered/non-default devices cannot promote themselves while another default exists

### `POST /crypto/devices/<device_id>/revoke/`

Revokes a linked device. Requires a signed device action.

Rules:

- the default device may revoke other non-default devices
- any active device may revoke itself
- revoked devices are marked `status=revoked` and no longer listed

### `POST /crypto/files/`

Uploads an encrypted attachment blob.

Multipart request:

```text
file=<encrypted blob>
```

Response includes an encrypted file URL and Cloudinary metadata.

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

### Inbox Socket

Receives account-wide events:

- `connection.accepted`
- `presence.snapshot`
- `presence.online`
- `presence.offline`
- `message.sent`
- `message.delivered`
- `message.read`
- `device.revoked`
- `device.default_changed`
- `recovery.key_updated`

Client may send:

```json
{"type": "ping"}
```

### Room Socket

Receives room-scoped events and typing state.

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

Check migrations:

```powershell
venv\Scripts\python.exe manage.py makemigrations --check --dry-run
```

## Production Notes

- Use Redis for `CHANNEL_LAYERS` and cache in production.
- Keep `MESSAGING_JWT_SECRET` and `INTERNAL_SERVICE_TOKEN` out of source control.
- Run migrations before deploying code that changes models.
- Messenger stores encrypted payloads and public keys, not the user's recovery key or message private keys.

