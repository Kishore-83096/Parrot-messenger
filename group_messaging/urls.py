from django.urls import path

from .views import (
    create_group_room,
    group_admin_transfer,
    group_avatar,
    group_leave,
    group_member_detail,
    group_member_sub_admin,
    group_members,
    group_room_detail,
)


urlpatterns = [
    path('', create_group_room, name='group-create'),
    path('<int:room_id>/', group_room_detail, name='group-detail'),
    path('<int:room_id>/avatar/', group_avatar, name='group-avatar'),
    path('<int:room_id>/members/', group_members, name='group-members'),
    path('<int:room_id>/members/<int:user_id>/', group_member_detail, name='group-member-detail'),
    path('<int:room_id>/members/<int:user_id>/sub-admin/', group_member_sub_admin, name='group-member-sub-admin'),
    path('<int:room_id>/admin-transfer/', group_admin_transfer, name='group-admin-transfer'),
    path('<int:room_id>/leave/', group_leave, name='group-leave'),
]
