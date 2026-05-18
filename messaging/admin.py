from django.contrib import admin

from .models import (
    Message,
    MessageAttachment,
    Room,
    RoomParticipant,
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
        'created_at',
    )
    list_filter = ('status', 'delivery_blocked', 'sent_while_blocked', 'created_at')
    search_fields = ('=sender_user_id', '=recipient_user_id', 'client_message_id', 'text')
    inlines = [MessageAttachmentInline]


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
