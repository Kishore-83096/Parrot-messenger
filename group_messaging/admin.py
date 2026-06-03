from django.contrib import admin

from .models import GroupActionLog, GroupMembership, GroupProfile


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
