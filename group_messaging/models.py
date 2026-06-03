from django.db import models

from messaging.models import Room


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
    ACTION_GROUP_UPDATED = 'group.updated'
    ACTION_AVATAR_UPDATED = 'group.avatar_updated'
    ACTION_CHOICES = [
        (ACTION_GROUP_CREATED, 'Group created'),
        (ACTION_MEMBER_ADDED, 'Member added'),
        (ACTION_GROUP_UPDATED, 'Group updated'),
        (ACTION_AVATAR_UPDATED, 'Group avatar updated'),
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
