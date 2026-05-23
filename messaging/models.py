import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


class Room(models.Model):
    TYPE_DIRECT = 'direct'
    TYPE_GROUP = 'group'
    ROOM_TYPE_CHOICES = [
        (TYPE_DIRECT, 'Direct'),
        (TYPE_GROUP, 'Group'),
    ]

    room_type = models.CharField(
        max_length=20,
        choices=ROOM_TYPE_CHOICES,
        default=TYPE_DIRECT,
        db_index=True,
    )
    title = models.CharField(max_length=120, blank=True)
    created_by_user_id = models.PositiveBigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['room_type', 'updated_at']),
            models.Index(fields=['created_by_user_id']),
        ]
        ordering = ['-updated_at', '-id']

    @property
    def is_group(self):
        return self.room_type == self.TYPE_GROUP

    @property
    def is_direct(self):
        return self.room_type == self.TYPE_DIRECT

    def active_participant_count(self):
        if not self.pk:
            return 0

        return self.participants.filter(is_active=True).count()

    def can_add_active_participant(self):
        return self.is_group or self.active_participant_count() < 2

    def __str__(self):
        return f'{self.room_type} room #{self.pk or "new"}'


class RoomParticipant(models.Model):
    ROLE_MEMBER = 'member'
    ROLE_ADMIN = 'admin'
    ROLE_CHOICES = [
        (ROLE_MEMBER, 'Member'),
        (ROLE_ADMIN, 'Admin'),
    ]

    room = models.ForeignKey(Room, related_name='participants', on_delete=models.CASCADE)
    user_id = models.PositiveBigIntegerField()
    account_number = models.CharField(max_length=10, blank=True)
    display_name = models.CharField(max_length=120, blank=True)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default=ROLE_MEMBER)
    joined_at = models.DateTimeField(auto_now_add=True)
    last_read_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['room', 'user_id'], name='uq_room_participant_user'),
        ]
        indexes = [
            models.Index(fields=['user_id', 'is_active']),
            models.Index(fields=['room', 'is_active']),
        ]
        ordering = ['joined_at', 'id']

    def clean(self):
        super().clean()

        if not self.is_active or not self.room_id:
            return

        if self.room.is_direct:
            active_count = (
                RoomParticipant.objects.filter(room_id=self.room_id, is_active=True)
                .exclude(pk=self.pk)
                .count()
            )
            if active_count >= 2:
                raise ValidationError(
                    {
                        'room': 'Direct rooms can have only two active participants.',
                    }
                )

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f'user {self.user_id} in room {self.room_id}'


class UserDeviceKey(models.Model):
    STATUS_ACTIVE = 'active'
    STATUS_REVOKED = 'revoked'
    STATUS_CHOICES = [
        (STATUS_ACTIVE, 'Active'),
        (STATUS_REVOKED, 'Revoked'),
    ]

    user_id = models.PositiveBigIntegerField(db_index=True)
    device_id = models.CharField(max_length=120)
    device_name = models.CharField(max_length=120, blank=True)
    public_key = models.TextField()
    encryption_public_key = models.TextField(blank=True)
    management_public_key = models.TextField(blank=True)
    is_default = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['user_id', 'device_id'], name='uq_user_device_key'),
            models.UniqueConstraint(
                fields=['user_id'],
                condition=Q(is_default=True),
                name='uq_user_default_device_key',
            ),
        ]
        indexes = [
            models.Index(fields=['user_id', 'last_seen_at']),
            models.Index(fields=['user_id', 'is_default']),
            models.Index(fields=['user_id', 'status']),
        ]
        ordering = ['-last_seen_at', '-id']

    def __str__(self):
        return f'device key {self.device_id} for user {self.user_id}'


class UserDeviceDefaultCredential(models.Model):
    user_id = models.PositiveBigIntegerField(unique=True, db_index=True)
    password_hash = models.CharField(max_length=256)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['user_id', 'updated_at']),
        ]
        ordering = ['-updated_at', '-id']

    def __str__(self):
        return f'default device credential for user {self.user_id}'


class UserE2EEKeyBackup(models.Model):
    user_id = models.PositiveBigIntegerField(unique=True, db_index=True)
    public_key = models.TextField()
    encrypted_private_key = models.TextField()
    salt = models.TextField()
    nonce = models.TextField()
    kdf_algorithm = models.CharField(max_length=40, default='PBKDF2-SHA256')
    kdf_iterations = models.PositiveIntegerField(default=600000)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['user_id', 'updated_at']),
        ]
        ordering = ['-updated_at', '-id']

    def __str__(self):
        return f'E2EE key backup for user {self.user_id}'


class Message(models.Model):
    STATUS_SENT = 'sent'
    STATUS_DELIVERED = 'delivered'
    STATUS_READ = 'read'
    STATUS_CHOICES = [
        (STATUS_SENT, 'Sent'),
        (STATUS_DELIVERED, 'Delivered'),
        (STATUS_READ, 'Read'),
    ]

    room = models.ForeignKey(Room, related_name='messages', on_delete=models.CASCADE)
    reply_to = models.ForeignKey(
        'self',
        related_name='replies',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    sender_user_id = models.PositiveBigIntegerField()
    recipient_user_id = models.PositiveBigIntegerField(null=True, blank=True)
    text = models.TextField(blank=True)
    client_message_id = models.CharField(max_length=120, blank=True)
    story_context = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_SENT)
    delivery_blocked = models.BooleanField(default=False)
    sent_while_blocked = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    edited_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['sender_user_id', 'client_message_id'],
                condition=~Q(client_message_id=''),
                name='uq_sender_client_message_id',
            ),
        ]
        indexes = [
            models.Index(fields=['room', 'created_at']),
            models.Index(fields=['reply_to']),
            models.Index(fields=['sender_user_id', 'created_at']),
            models.Index(fields=['recipient_user_id', 'created_at']),
            models.Index(fields=['status']),
        ]
        ordering = ['created_at', 'id']

    def clean(self):
        super().clean()

        if self.reply_to_id and self.room_id and self.reply_to.room_id != self.room_id:
            raise ValidationError({'reply_to': 'Reply target must be in the same room.'})

        if self.room_id and not self.room.is_direct:
            if self.delivery_blocked:
                raise ValidationError({'delivery_blocked': 'Delivery blocking is only supported for direct rooms.'})

            if self.sent_while_blocked:
                raise ValidationError({'sent_while_blocked': 'Blocked-send markers are only supported for direct rooms.'})

        if self.pk and not self.text.strip() and not self.attachments.exists():
            raise ValidationError({'text': 'Message must include text or at least one attachment.'})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f'message #{self.pk or "new"} in room {self.room_id}'


class MessageAttachment(models.Model):
    TYPE_IMAGE = 'image'
    TYPE_VIDEO = 'video'
    TYPE_AUDIO = 'audio'
    TYPE_DOCUMENT = 'document'
    TYPE_OTHER = 'other'
    FILE_TYPE_CHOICES = [
        (TYPE_IMAGE, 'Image'),
        (TYPE_VIDEO, 'Video'),
        (TYPE_AUDIO, 'Audio'),
        (TYPE_DOCUMENT, 'Document'),
        (TYPE_OTHER, 'Other'),
    ]

    message = models.ForeignKey(Message, related_name='attachments', on_delete=models.CASCADE)
    file_type = models.CharField(max_length=20, choices=FILE_TYPE_CHOICES, default=TYPE_OTHER)
    file_url = models.URLField(max_length=1000)
    thumbnail_url = models.URLField(max_length=1000, blank=True)
    file_name = models.CharField(max_length=255, blank=True)
    mime_type = models.CharField(max_length=120, blank=True)
    file_size_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    duration_seconds = models.PositiveIntegerField(null=True, blank=True)
    cloudinary_public_id = models.CharField(max_length=512, blank=True)
    cloudinary_asset_id = models.CharField(max_length=255, blank=True)
    cloudinary_resource_type = models.CharField(max_length=40, blank=True)
    cloudinary_folder = models.CharField(max_length=255, blank=True)
    sort_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['message', 'sort_order']),
            models.Index(fields=['file_type']),
        ]
        ordering = ['sort_order', 'id']

    def clean(self):
        super().clean()

        if not self.file_url:
            raise ValidationError({'file_url': 'Attachment file URL is required.'})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.file_type} attachment #{self.pk or "new"}'


class MessageReaction(models.Model):
    REACTION_THUMBS_UP = 'thumbs_up'
    REACTION_HEART = 'heart'
    REACTION_LAUGH = 'laugh'
    REACTION_SURPRISED = 'surprised'
    REACTION_SAD = 'sad'
    REACTION_CHOICES = [
        (REACTION_THUMBS_UP, 'Thumbs up'),
        (REACTION_HEART, 'Heart'),
        (REACTION_LAUGH, 'Laugh'),
        (REACTION_SURPRISED, 'Surprised'),
        (REACTION_SAD, 'Sad'),
    ]
    ALLOWED_REACTIONS = tuple(choice[0] for choice in REACTION_CHOICES)

    message = models.ForeignKey(Message, related_name='reactions', on_delete=models.CASCADE)
    user_id = models.PositiveBigIntegerField(db_index=True)
    reaction = models.CharField(max_length=20, choices=REACTION_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['message', 'user_id'], name='uq_message_reaction_user'),
        ]
        indexes = [
            models.Index(fields=['message', 'reaction']),
            models.Index(fields=['user_id', 'updated_at']),
        ]
        ordering = ['created_at', 'id']

    def __str__(self):
        return f'{self.reaction} reaction by user {self.user_id} on message {self.message_id}'


class MessageEncryptedUploadIntent(models.Model):
    STATUS_ISSUED = 'issued'
    STATUS_COMPLETED = 'completed'
    STATUS_CONSUMED = 'consumed'
    STATUS_EXPIRED = 'expired'
    STATUS_CHOICES = [
        (STATUS_ISSUED, 'Issued'),
        (STATUS_COMPLETED, 'Completed'),
        (STATUS_CONSUMED, 'Consumed'),
        (STATUS_EXPIRED, 'Expired'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sender_user_id = models.PositiveBigIntegerField(db_index=True)
    sender_account_number = models.CharField(max_length=10, blank=True)
    recipient_user_id = models.PositiveBigIntegerField(db_index=True)
    recipient_account_number = models.CharField(max_length=10, db_index=True)
    client_message_id = models.CharField(max_length=120, db_index=True)
    attachment_client_id = models.CharField(max_length=255, blank=True)
    attachment_index = models.PositiveIntegerField(default=0)
    original_file_name = models.CharField(max_length=255, blank=True)
    original_mime_type = models.CharField(max_length=120, blank=True)
    original_file_size_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    encrypted_file_size_bytes = models.PositiveBigIntegerField()
    cloudinary_public_id = models.CharField(max_length=512, unique=True)
    cloudinary_asset_id = models.CharField(max_length=255, blank=True)
    cloudinary_resource_type = models.CharField(max_length=40, default='raw')
    cloudinary_folder = models.CharField(max_length=255)
    secure_url = models.URLField(max_length=1000, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ISSUED)
    signature_timestamp = models.PositiveBigIntegerField()
    expires_at = models.DateTimeField(db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    consumed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['sender_user_id', 'client_message_id', 'status']),
            models.Index(fields=['recipient_user_id', 'status']),
            models.Index(fields=['status', 'expires_at']),
        ]
        ordering = ['created_at', 'id']

    def __str__(self):
        return f'encrypted upload intent {self.id} for user {self.sender_user_id}'
