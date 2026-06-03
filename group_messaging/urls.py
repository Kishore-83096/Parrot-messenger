from django.urls import path

from .views import (
    create_group_room,
    group_admin_transfer,
    group_avatar,
    group_complete_crypto_file_upload_intent,
    group_create_crypto_file_upload_intents,
    group_crypto_devices,
    group_deliver_room,
    group_leave,
    group_message_reaction,
    group_member_detail,
    group_member_sub_admin,
    group_members,
    group_read_room,
    group_room_detail,
    group_room_messages,
    group_send_message,
)


urlpatterns = [
    path('', create_group_room, name='group-create'),
    path('<int:room_id>/', group_room_detail, name='group-detail'),
    path('<int:room_id>/avatar/', group_avatar, name='group-avatar'),
    path('<int:room_id>/crypto/devices/', group_crypto_devices, name='group-crypto-devices'),
    path('<int:room_id>/crypto/files/upload-intents/', group_create_crypto_file_upload_intents, name='group-crypto-upload-intents'),
    path('<int:room_id>/crypto/files/upload-intents/<uuid:upload_intent_id>/complete/', group_complete_crypto_file_upload_intent, name='group-crypto-upload-intent-complete'),
    path('<int:room_id>/messages/', group_room_messages, name='group-messages'),
    path('<int:room_id>/messages/send/', group_send_message, name='group-message-send'),
    path('<int:room_id>/messages/delivered/', group_deliver_room, name='group-message-delivered'),
    path('<int:room_id>/messages/read/', group_read_room, name='group-message-read'),
    path('<int:room_id>/messages/<int:message_id>/reaction/', group_message_reaction, name='group-message-reaction'),
    path('<int:room_id>/members/', group_members, name='group-members'),
    path('<int:room_id>/members/<int:user_id>/', group_member_detail, name='group-member-detail'),
    path('<int:room_id>/members/<int:user_id>/sub-admin/', group_member_sub_admin, name='group-member-sub-admin'),
    path('<int:room_id>/admin-transfer/', group_admin_transfer, name='group-admin-transfer'),
    path('<int:room_id>/leave/', group_leave, name='group-leave'),
]
