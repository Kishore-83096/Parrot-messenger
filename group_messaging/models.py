import uuid

from django.db import models
from django.db.models import Q

from messaging.models import Room


class GroupMembership(models.Model):
    ROLE_ADMIN = 'admin'
    ROLE_SUB_ADMIN = 'sub_admin'
    ROLE_MEMBER = 'member'
    ROLE_CHOICES = [
        (ROLE_ADMIN, 'Admin'),
        (ROLE_SUB_ADMIN, 'Sub admin'),
        (ROLE_MEMBER, 'Member'),
    ]

    room = models.ForeignKey(Room, related_name='group_memberships', on_delete=models.CASCADE)
    user_id = models.PositiveBigIntegerField()
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default=ROLE_MEMBER)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['room', 'user_id'], name='uq_group_membership_user'),
        ]
        indexes = [
            models.Index(fields=['room', 'is_active'], name='group_messa_room_id_a7f98c_idx'),
            models.Index(fields=['user_id', 'is_active'], name='group_messa_user_id_18b3d2_idx'),
            models.Index(fields=['role', 'is_active'], name='group_messa_role_8f95ce_idx'),
        ]
        ordering = ['created_at', 'id']

    def __str__(self):
        return f'group role {self.role} for user {self.user_id} in room {self.room_id}'


class GroupProfile(models.Model):
    room = models.OneToOneField(Room, related_name='group_profile', on_delete=models.CASCADE)
    title = models.CharField(max_length=120)
    avatar_url = models.URLField(max_length=1000, blank=True)
    avatar_cloudinary_public_id = models.CharField(max_length=512, blank=True)
    avatar_cloudinary_asset_id = models.CharField(max_length=255, blank=True)
    created_by_user_id = models.PositiveBigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['created_by_user_id', 'updated_at'], name='group_messa_created_0051e8_idx'),
        ]
        ordering = ['-updated_at', '-id']

    def __str__(self):
        return f'group profile for room {self.room_id}'


class GroupActionLog(models.Model):
    ACTION_GROUP_CREATED = 'group.created'
    ACTION_MEMBER_ADDED = 'group.member_added'
    ACTION_MEMBER_REMOVED = 'group.member_removed'
    ACTION_MEMBER_LEFT = 'group.member_left'
    ACTION_GROUP_UPDATED = 'group.updated'
    ACTION_AVATAR_UPDATED = 'group.avatar_updated'
    ACTION_SUB_ADMIN_ADDED = 'group.sub_admin_added'
    ACTION_SUB_ADMIN_REMOVED = 'group.sub_admin_removed'
    ACTION_ADMIN_TRANSFERRED = 'group.admin_transferred'
    ACTION_GROUP_DELETED = 'group.deleted'
    ACTION_CHOICES = [
        (ACTION_GROUP_CREATED, 'Group created'),
        (ACTION_MEMBER_ADDED, 'Member added'),
        (ACTION_MEMBER_REMOVED, 'Member removed'),
        (ACTION_MEMBER_LEFT, 'Member left'),
        (ACTION_GROUP_UPDATED, 'Group updated'),
        (ACTION_AVATAR_UPDATED, 'Group avatar updated'),
        (ACTION_SUB_ADMIN_ADDED, 'Sub admin added'),
        (ACTION_SUB_ADMIN_REMOVED, 'Sub admin removed'),
        (ACTION_ADMIN_TRANSFERRED, 'Admin transferred'),
        (ACTION_GROUP_DELETED, 'Group deleted'),
    ]

    room = models.ForeignKey(Room, related_name='group_action_logs', on_delete=models.CASCADE)
    actor_user_id = models.PositiveBigIntegerField()
    target_user_id = models.PositiveBigIntegerField(null=True, blank=True)
    action = models.CharField(max_length=40, choices=ACTION_CHOICES)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['room', 'created_at'], name='group_messa_room_id_2a99f9_idx'),
            models.Index(fields=['action', 'created_at'], name='group_messa_action_44da4b_idx'),
            models.Index(fields=['target_user_id', 'created_at'], name='group_messa_target__728a37_idx'),
        ]
        ordering = ['created_at', 'id']

    def __str__(self):
        return f'{self.action} in room {self.room_id}'


class GroupMessage(models.Model):
    STATUS_SENT = 'sent'
    STATUS_DELIVERED = 'delivered'
    STATUS_READ = 'read'
    STATUS_CHOICES = [
        (STATUS_SENT, 'Sent'),
        (STATUS_DELIVERED, 'Delivered'),
        (STATUS_READ, 'Read'),
    ]

    room = models.ForeignKey(Room, related_name='group_messages', on_delete=models.CASCADE)
    reply_to = models.ForeignKey(
        'self',
        related_name='group_replies',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    sender_user_id = models.PositiveBigIntegerField(db_index=True)
    text = models.TextField(blank=True)
    client_message_id = models.CharField(max_length=120, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_SENT)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    edited_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['sender_user_id', 'client_message_id'],
                condition=~Q(client_message_id=''),
                name='uq_group_sender_client_message_id',
            ),
        ]
        indexes = [
            models.Index(fields=['room', 'created_at'], name='group_messa_room_id_f819d0_idx'),
            models.Index(fields=['reply_to'], name='group_messa_reply_t_810e63_idx'),
            models.Index(fields=['sender_user_id', 'created_at'], name='group_messa_sender__0c14ec_idx'),
            models.Index(fields=['status'], name='group_messa_status_71a02b_idx'),
        ]
        ordering = ['created_at', 'id']

    def clean(self):
        super().clean()

        if self.room_id and not self.room.is_group:
            from django.core.exceptions import ValidationError

            raise ValidationError({'room': 'Group messages can only be stored in group rooms.'})

        if self.reply_to_id and self.room_id and self.reply_to.room_id != self.room_id:
            from django.core.exceptions import ValidationError

            raise ValidationError({'reply_to': 'Reply target must be in the same group.'})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f'group message #{self.pk or "new"} in room {self.room_id}'


class GroupMessageReceipt(models.Model):
    message = models.ForeignKey(GroupMessage, related_name='receipts', on_delete=models.CASCADE)
    room = models.ForeignKey(Room, related_name='group_message_receipts', on_delete=models.CASCADE)
    user_id = models.PositiveBigIntegerField(db_index=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['message', 'user_id'], name='uq_group_message_receipt_user'),
        ]
        indexes = [
            models.Index(fields=['room', 'user_id', 'read_at'], name='group_messa_room_id_82ec2c_idx'),
            models.Index(fields=['room', 'user_id', 'delivered_at'], name='group_messa_room_id_39a6de_idx'),
            models.Index(fields=['message', 'delivered_at'], name='group_messa_message_25b23b_idx'),
            models.Index(fields=['message', 'read_at'], name='group_messa_message_71d010_idx'),
        ]
        ordering = ['created_at', 'id']

    def __str__(self):
        return f'group receipt for message {self.message_id} and user {self.user_id}'


class GroupMessageReaction(models.Model):
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

    message = models.ForeignKey(GroupMessage, related_name='reactions', on_delete=models.CASCADE)
    user_id = models.PositiveBigIntegerField(db_index=True)
    reaction = models.CharField(max_length=20, choices=REACTION_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['message', 'user_id'], name='uq_group_message_reaction_user'),
        ]
        indexes = [
            models.Index(fields=['message', 'reaction'], name='group_messa_message_99cf93_idx'),
            models.Index(fields=['user_id', 'updated_at'], name='group_messa_user_id_ce21ce_idx'),
        ]
        ordering = ['created_at', 'id']

    def __str__(self):
        return f'{self.reaction} group reaction by user {self.user_id} on message {self.message_id}'


class GroupMessageEncryptedUploadIntent(models.Model):
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
    room = models.ForeignKey(Room, related_name='group_encrypted_upload_intents', on_delete=models.CASCADE)
    sender_user_id = models.PositiveBigIntegerField(db_index=True)
    sender_account_number = models.CharField(max_length=10, blank=True)
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
            models.Index(fields=['room', 'sender_user_id', 'client_message_id', 'status'], name='group_messa_room_id_d0bcd2_idx'),
            models.Index(fields=['sender_user_id', 'status'], name='group_messa_sender__5457f6_idx'),
            models.Index(fields=['status', 'expires_at'], name='group_messa_status_376d63_idx'),
        ]
        ordering = ['created_at', 'id']

    def __str__(self):
        return f'group encrypted upload intent {self.id} for room {self.room_id}'
