from django.contrib import admin

from .models import Message, MessageAttachment, Room, RoomParticipant


class RoomParticipantInline(admin.TabularInline):
    model = RoomParticipant
    extra = 0
    fields = ('user_id', 'account_number', 'display_name', 'role', 'is_active', 'joined_at')
    readonly_fields = ('joined_at',)


class MessageAttachmentInline(admin.TabularInline):
    model = MessageAttachment
    extra = 0
    fields = ('file_type', 'file_url', 'thumbnail_url', 'file_name', 'mime_type', 'sort_order')


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
        'created_at',
    )
    list_filter = ('status', 'delivery_blocked', 'created_at')
    search_fields = ('=sender_user_id', '=recipient_user_id', 'client_message_id', 'text')
    inlines = [MessageAttachmentInline]


@admin.register(MessageAttachment)
class MessageAttachmentAdmin(admin.ModelAdmin):
    list_display = ('id', 'message', 'file_type', 'file_name', 'mime_type', 'sort_order', 'created_at')
    list_filter = ('file_type', 'created_at')
    search_fields = ('file_url', 'thumbnail_url', 'file_name', 'mime_type')
