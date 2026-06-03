from django.contrib import admin

from .models import (
    GroupActionLog,
    GroupMembership,
    GroupMessage,
    GroupMessageEncryptedUploadIntent,
    GroupMessageReaction,
    GroupMessageReceipt,
    GroupProfile,
)


@admin.register(GroupMembership)
class GroupMembershipAdmin(admin.ModelAdmin):
    list_display = ('id', 'room', 'user_id', 'role', 'is_active', 'updated_at')
    list_filter = ('role', 'is_active')
    search_fields = ('=room__id', '=user_id')


@admin.register(GroupProfile)
class GroupProfileAdmin(admin.ModelAdmin):
    list_display = ('id', 'room', 'title', 'created_by_user_id', 'updated_at')
    search_fields = ('title', '=created_by_user_id', '=room__id')


@admin.register(GroupActionLog)
class GroupActionLogAdmin(admin.ModelAdmin):
    list_display = ('id', 'room', 'action', 'actor_user_id', 'target_user_id', 'created_at')
    list_filter = ('action', 'created_at')
    search_fields = ('=room__id', '=actor_user_id', '=target_user_id')


class GroupMessageReceiptInline(admin.TabularInline):
    model = GroupMessageReceipt
    extra = 0
    fields = ('user_id', 'delivered_at', 'read_at', 'updated_at')
    readonly_fields = ('updated_at',)


class GroupMessageReactionInline(admin.TabularInline):
    model = GroupMessageReaction
    extra = 0
    fields = ('user_id', 'reaction', 'created_at', 'updated_at')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(GroupMessage)
class GroupMessageAdmin(admin.ModelAdmin):
    list_display = ('id', 'room', 'sender_user_id', 'status', 'created_at', 'updated_at')
    list_filter = ('status', 'created_at', 'updated_at')
    search_fields = ('=room__id', '=sender_user_id', 'client_message_id')
    readonly_fields = ('created_at', 'updated_at')
    inlines = [GroupMessageReceiptInline, GroupMessageReactionInline]


@admin.register(GroupMessageReceipt)
class GroupMessageReceiptAdmin(admin.ModelAdmin):
    list_display = ('id', 'message', 'room', 'user_id', 'delivered_at', 'read_at')
    list_filter = ('delivered_at', 'read_at', 'created_at')
    search_fields = ('=message__id', '=room__id', '=user_id')


@admin.register(GroupMessageReaction)
class GroupMessageReactionAdmin(admin.ModelAdmin):
    list_display = ('id', 'message', 'user_id', 'reaction', 'created_at', 'updated_at')
    list_filter = ('reaction', 'created_at', 'updated_at')
    search_fields = ('=message__id', '=user_id')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(GroupMessageEncryptedUploadIntent)
class GroupMessageEncryptedUploadIntentAdmin(admin.ModelAdmin):
    list_display = ('id', 'room', 'sender_user_id', 'client_message_id', 'status', 'expires_at')
    list_filter = ('status', 'created_at', 'expires_at')
    search_fields = ('=room__id', '=sender_user_id', 'client_message_id', 'cloudinary_public_id')
    readonly_fields = ('id', 'created_at', 'updated_at', 'completed_at', 'consumed_at')
