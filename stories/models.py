import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


class Story(models.Model):
    VISIBILITY_ALL_CONTACTS = 'all_contacts'
    VISIBILITY_SPECIFIC_CONTACTS = 'specific_contacts'
    VISIBILITY_CHOICES = [
        (VISIBILITY_ALL_CONTACTS, 'All contacts'),
        (VISIBILITY_SPECIFIC_CONTACTS, 'Specific contacts'),
    ]

    EXPIRY_6_HOURS = 6
    EXPIRY_12_HOURS = 12
    EXPIRY_24_HOURS = 24
    EXPIRY_CHOICES = [
        (EXPIRY_6_HOURS, '6 hours'),
        (EXPIRY_12_HOURS, '12 hours'),
        (EXPIRY_24_HOURS, '24 hours'),
    ]

    STATUS_ACTIVE = 'active'
    STATUS_EXPIRED = 'expired'
    STATUS_DELETED = 'deleted'
    STATUS_CHOICES = [
        (STATUS_ACTIVE, 'Active'),
        (STATUS_EXPIRED, 'Expired'),
        (STATUS_DELETED, 'Deleted'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner_user_id = models.PositiveBigIntegerField(db_index=True)
    owner_account_number = models.CharField(max_length=10, db_index=True)
    client_story_id = models.CharField(max_length=120, blank=True, db_index=True)
    visibility = models.CharField(
        max_length=30,
        choices=VISIBILITY_CHOICES,
        default=VISIBILITY_ALL_CONTACTS,
    )
    expiry_hours = models.PositiveSmallIntegerField(
        choices=EXPIRY_CHOICES,
        default=EXPIRY_24_HOURS,
    )
    encrypted_payload = models.TextField(blank=True)
    payload_version = models.PositiveSmallIntegerField(default=1)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    expires_at = models.DateTimeField(db_index=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['owner_user_id', 'client_story_id'],
                condition=~Q(client_story_id=''),
                name='uq_story_owner_client_story_id',
            ),
            models.CheckConstraint(
                condition=Q(expiry_hours__in=[6, 12, 24]),
                name='ck_story_expiry_hours_allowed',
            ),
        ]
        indexes = [
            models.Index(fields=['owner_user_id', 'created_at']),
            models.Index(fields=['owner_user_id', 'expires_at']),
            models.Index(fields=['owner_account_number', 'expires_at']),
            models.Index(fields=['status', 'expires_at']),
            models.Index(fields=['visibility', 'status']),
        ]
        ordering = ['-created_at', '-id']

    def __str__(self):
        return f'story {self.id} by user {self.owner_user_id}'


class StoryUploadIntent(models.Model):
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

    MEDIA_IMAGE = 'image'
    MEDIA_VIDEO = 'video'
    MEDIA_TYPE_CHOICES = [
        (MEDIA_IMAGE, 'Image'),
        (MEDIA_VIDEO, 'Video'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner_user_id = models.PositiveBigIntegerField(db_index=True)
    owner_account_number = models.CharField(max_length=10, db_index=True)
    client_story_id = models.CharField(max_length=120, db_index=True)
    media_client_id = models.CharField(max_length=255, blank=True)
    media_index = models.PositiveIntegerField(default=0)
    media_type = models.CharField(max_length=20, choices=MEDIA_TYPE_CHOICES)
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
            models.Index(fields=['owner_user_id', 'client_story_id', 'status']),
            models.Index(fields=['owner_account_number', 'status']),
            models.Index(fields=['status', 'expires_at']),
            models.Index(fields=['media_type', 'status']),
        ]
        ordering = ['created_at', 'id']

    def __str__(self):
        return f'story upload intent {self.id} for user {self.owner_user_id}'


class StoryMedia(models.Model):
    MEDIA_IMAGE = StoryUploadIntent.MEDIA_IMAGE
    MEDIA_VIDEO = StoryUploadIntent.MEDIA_VIDEO
    MEDIA_TYPE_CHOICES = StoryUploadIntent.MEDIA_TYPE_CHOICES

    story = models.ForeignKey(Story, related_name='media', on_delete=models.CASCADE)
    upload_intent = models.ForeignKey(
        StoryUploadIntent,
        related_name='media_items',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    media_type = models.CharField(max_length=20, choices=MEDIA_TYPE_CHOICES)
    encrypted_file_url = models.URLField(max_length=1000)
    thumbnail_url = models.URLField(max_length=1000, blank=True)
    file_name = models.CharField(max_length=255, blank=True)
    mime_type = models.CharField(max_length=120, blank=True)
    file_size_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    encrypted_file_size_bytes = models.PositiveBigIntegerField(null=True, blank=True)
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
            models.Index(fields=['story', 'sort_order']),
            models.Index(fields=['media_type']),
            models.Index(fields=['cloudinary_public_id']),
        ]
        ordering = ['sort_order', 'id']

    def clean(self):
        super().clean()

        if self.media_type not in dict(self.MEDIA_TYPE_CHOICES):
            raise ValidationError({'media_type': 'Stories support image and video media only.'})

        if not self.encrypted_file_url:
            raise ValidationError({'encrypted_file_url': 'Story media file URL is required.'})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.media_type} story media #{self.pk or "new"}'


class StoryAudience(models.Model):
    story = models.ForeignKey(Story, related_name='audience', on_delete=models.CASCADE)
    viewer_user_id = models.PositiveBigIntegerField(db_index=True)
    viewer_account_number = models.CharField(max_length=10, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['story', 'viewer_user_id'], name='uq_story_audience_viewer'),
        ]
        indexes = [
            models.Index(fields=['viewer_user_id', 'created_at']),
            models.Index(fields=['viewer_account_number', 'created_at']),
            models.Index(fields=['story', 'created_at']),
        ]
        ordering = ['created_at', 'id']

    def __str__(self):
        return f'user {self.viewer_user_id} audience for story {self.story_id}'


class StoryView(models.Model):
    story = models.ForeignKey(Story, related_name='views', on_delete=models.CASCADE)
    viewer_user_id = models.PositiveBigIntegerField(db_index=True)
    viewer_account_number = models.CharField(max_length=10, db_index=True)
    viewed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['story', 'viewer_user_id'], name='uq_story_view_viewer'),
        ]
        indexes = [
            models.Index(fields=['story', 'viewed_at']),
            models.Index(fields=['viewer_user_id', 'viewed_at']),
            models.Index(fields=['viewer_account_number', 'viewed_at']),
        ]
        ordering = ['viewed_at', 'id']

    def __str__(self):
        return f'user {self.viewer_user_id} viewed story {self.story_id}'


class StoryReaction(models.Model):
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

    story = models.ForeignKey(Story, related_name='reactions', on_delete=models.CASCADE)
    viewer_user_id = models.PositiveBigIntegerField(db_index=True)
    viewer_account_number = models.CharField(max_length=10, db_index=True)
    reaction = models.CharField(max_length=20, choices=REACTION_CHOICES)
    message = models.ForeignKey(
        'messaging.Message',
        related_name='story_reactions',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['story', 'viewer_user_id'], name='uq_story_reaction_viewer'),
        ]
        indexes = [
            models.Index(fields=['story', 'reaction']),
            models.Index(fields=['viewer_user_id', 'updated_at']),
            models.Index(fields=['viewer_account_number', 'updated_at']),
            models.Index(fields=['message']),
        ]
        ordering = ['created_at', 'id']

    def __str__(self):
        return f'{self.reaction} reaction by user {self.viewer_user_id} on story {self.story_id}'


class StoryReply(models.Model):
    story = models.ForeignKey(Story, related_name='replies', on_delete=models.CASCADE)
    viewer_user_id = models.PositiveBigIntegerField(db_index=True)
    viewer_account_number = models.CharField(max_length=10, db_index=True)
    message = models.ForeignKey(
        'messaging.Message',
        related_name='story_replies',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['story', 'created_at']),
            models.Index(fields=['viewer_user_id', 'created_at']),
            models.Index(fields=['viewer_account_number', 'created_at']),
            models.Index(fields=['message']),
        ]
        ordering = ['created_at', 'id']

    def __str__(self):
        return f'story reply by user {self.viewer_user_id} on story {self.story_id}'
