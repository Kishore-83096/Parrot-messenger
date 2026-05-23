from django.contrib import admin

from .models import (
    Story,
    StoryAudience,
    StoryMedia,
    StoryReaction,
    StoryReply,
    StoryUploadIntent,
    StoryView,
)


class StoryMediaInline(admin.TabularInline):
    model = StoryMedia
    extra = 0
    readonly_fields = ('created_at',)


class StoryAudienceInline(admin.TabularInline):
    model = StoryAudience
    extra = 0
    readonly_fields = ('created_at',)


@admin.register(Story)
class StoryAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'owner_user_id',
        'owner_account_number',
        'visibility',
        'expiry_hours',
        'status',
        'expires_at',
        'created_at',
    )
    list_filter = ('visibility', 'expiry_hours', 'status', 'created_at')
    search_fields = ('id', 'owner_user_id', 'owner_account_number', 'client_story_id')
    readonly_fields = ('id', 'created_at', 'updated_at')
    inlines = [StoryMediaInline, StoryAudienceInline]


@admin.register(StoryUploadIntent)
class StoryUploadIntentAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'owner_user_id',
        'client_story_id',
        'media_type',
        'status',
        'expires_at',
        'created_at',
    )
    list_filter = ('media_type', 'status', 'created_at')
    search_fields = (
        'id',
        'owner_user_id',
        'owner_account_number',
        'client_story_id',
        'cloudinary_public_id',
    )
    readonly_fields = ('id', 'created_at', 'updated_at')


@admin.register(StoryMedia)
class StoryMediaAdmin(admin.ModelAdmin):
    list_display = ('id', 'story', 'media_type', 'mime_type', 'sort_order', 'created_at')
    list_filter = ('media_type', 'created_at')
    search_fields = ('story__id', 'file_name', 'mime_type', 'cloudinary_public_id')
    readonly_fields = ('created_at',)


@admin.register(StoryAudience)
class StoryAudienceAdmin(admin.ModelAdmin):
    list_display = ('id', 'story', 'viewer_user_id', 'viewer_account_number', 'created_at')
    search_fields = ('story__id', 'viewer_user_id', 'viewer_account_number')
    readonly_fields = ('created_at',)


@admin.register(StoryView)
class StoryViewAdmin(admin.ModelAdmin):
    list_display = ('id', 'story', 'viewer_user_id', 'viewer_account_number', 'viewed_at')
    search_fields = ('story__id', 'viewer_user_id', 'viewer_account_number')
    readonly_fields = ('viewed_at',)


@admin.register(StoryReaction)
class StoryReactionAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'story',
        'viewer_user_id',
        'viewer_account_number',
        'reaction',
        'message',
        'created_at',
    )
    list_filter = ('reaction', 'created_at')
    search_fields = ('story__id', 'viewer_user_id', 'viewer_account_number', 'message__id')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(StoryReply)
class StoryReplyAdmin(admin.ModelAdmin):
    list_display = ('id', 'story', 'viewer_user_id', 'viewer_account_number', 'message', 'created_at')
    search_fields = ('story__id', 'viewer_user_id', 'viewer_account_number', 'message__id')
    readonly_fields = ('created_at',)
