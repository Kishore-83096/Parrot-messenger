from django.contrib import admin

from .models import (
    Message,
    MessageAttachment,
    MessageEncryptedUploadIntent,
    MessageReaction,
    Room,
    RoomParticipant,
    SavedMessage,
    UserDeviceKey,
    UserE2EEKeyBackup,
)


class RoomParticipantInline(admin.TabularInline):
    model = RoomParticipant
    extra = 0
    fields = ('user_id', 'account_number', 'display_name', 'role', 'is_active', 'joined_at')
    readonly_fields = ('joined_at',)


class MessageAttachmentInline(admin.TabularInline):
    model = MessageAttachment
    extra = 0
    fields = (
        'file_type',
        'file_url',
        'thumbnail_url',
        'file_name',
        'mime_type',
        'cloudinary_public_id',
        'cloudinary_resource_type',
        'cloudinary_folder',
        'sort_order',
    )


class MessageReactionInline(admin.TabularInline):
    model = MessageReaction
    extra = 0
    fields = ('user_id', 'reaction', 'created_at', 'updated_at')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    list_display = ('id', 'room_type', 'title', 'created_by_user_id', 'created_at', 'updated_at')
    list_filter = ('room_type', 'created_at')
    search_fields = ('title', '=created_by_user_id')
    inlines = [RoomParticipantInline]


@admin.register(RoomParticipant)
class RoomParticipantAdmin(admin.ModelAdmin):
    list_display = ('id', 'room', 'user_id', 'account_number', 'role', 'is_active', 'joined_at')
    list_filter = ('role', 'is_active', 'joined_at')
    search_fields = ('=user_id', 'account_number', 'display_name')


@admin.register(UserDeviceKey)
class UserDeviceKeyAdmin(admin.ModelAdmin):
    list_display = ('id', 'user_id', 'device_name', 'device_id', 'status', 'is_default', 'created_at', 'last_seen_at')
    list_filter = ('status', 'is_default', 'created_at', 'last_seen_at')
    search_fields = ('=user_id', 'device_name', 'device_id', 'public_key', 'encryption_public_key', 'management_public_key')
    readonly_fields = ('created_at', 'last_seen_at')


@admin.register(UserE2EEKeyBackup)
class UserE2EEKeyBackupAdmin(admin.ModelAdmin):
    list_display = ('id', 'user_id', 'kdf_algorithm', 'kdf_iterations', 'created_at', 'updated_at')
    search_fields = ('=user_id', 'public_key')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'room',
        'sender_user_id',
        'recipient_user_id',
        'reply_to',
        'status',
        'delivery_blocked',
        'sent_while_blocked',
        'story_context',
        'created_at',
    )
    list_filter = ('status', 'delivery_blocked', 'sent_while_blocked', 'created_at')
    search_fields = ('=sender_user_id', '=recipient_user_id', 'client_message_id', 'text')
    inlines = [MessageAttachmentInline, MessageReactionInline]


@admin.register(MessageAttachment)
class MessageAttachmentAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'message',
        'file_type',
        'file_name',
        'mime_type',
        'cloudinary_resource_type',
        'cloudinary_folder',
        'sort_order',
        'created_at',
    )
    list_filter = ('file_type', 'cloudinary_resource_type', 'cloudinary_folder', 'created_at')
    search_fields = (
        'file_url',
        'thumbnail_url',
        'file_name',
        'mime_type',
        'cloudinary_public_id',
        'cloudinary_asset_id',
    )


@admin.register(MessageReaction)
class MessageReactionAdmin(admin.ModelAdmin):
    list_display = ('id', 'message', 'user_id', 'reaction', 'created_at', 'updated_at')
    list_filter = ('reaction', 'created_at', 'updated_at')
    search_fields = ('=message__id', '=user_id')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(SavedMessage)
class SavedMessageAdmin(admin.ModelAdmin):
    list_display = ('id', 'user_id', 'message_kind', 'direct_message', 'group_message', 'created_at')
    list_filter = ('created_at', 'updated_at')
    search_fields = ('=user_id', '=direct_message__id', '=group_message__id')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(MessageEncryptedUploadIntent)
class MessageEncryptedUploadIntentAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'sender_user_id',
        'recipient_user_id',
        'client_message_id',
        'status',
        'encrypted_file_size_bytes',
        'expires_at',
        'created_at',
    )
    list_filter = ('status', 'cloudinary_resource_type', 'created_at', 'expires_at')
    search_fields = (
        '=sender_user_id',
        '=recipient_user_id',
        'recipient_account_number',
        'client_message_id',
        'cloudinary_public_id',
        'cloudinary_asset_id',
    )
    readonly_fields = ('created_at', 'updated_at', 'completed_at', 'consumed_at')
