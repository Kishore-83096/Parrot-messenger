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
