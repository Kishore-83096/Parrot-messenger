from django.urls import path

from .views import (
    authorize_message,
    deliver_room,
    health_check,
    list_rooms,
    read_room,
    register_crypto_device,
    recipient_crypto_devices,
    release_blocked_messages,
    room_messages,
    send_message,
    user_crypto_devices,
)

urlpatterns = [
    path('health/', health_check, name='messenger-health'),
    path('rooms/', list_rooms, name='room-list'),
    path('rooms/<int:room_id>/messages/', room_messages, name='room-messages'),
    path('rooms/<int:room_id>/delivered/', deliver_room, name='room-delivered'),
    path('rooms/<int:room_id>/read/', read_room, name='room-read'),
    path('rooms/<int:room_id>/blocked-messages/release/', release_blocked_messages, name='room-blocked-messages-release'),
    path('crypto/devices/', register_crypto_device, name='crypto-device-register'),
    path('crypto/users/<int:user_id>/devices/', user_crypto_devices, name='crypto-user-devices'),
    path('crypto/recipients/<str:recipient_account_number>/devices/', recipient_crypto_devices, name='crypto-recipient-devices'),
    path('messages/authorize/', authorize_message, name='message-authorization'),
    path('messages/send/', send_message, name='message-send'),
]
