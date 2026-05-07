from django.urls import path

from .views import (
    authorize_message,
    deliver_room,
    health_check,
    list_rooms,
    read_room,
    room_messages,
    send_message,
)

urlpatterns = [
    path('health/', health_check, name='messenger-health'),
    path('rooms/', list_rooms, name='room-list'),
    path('rooms/<int:room_id>/messages/', room_messages, name='room-messages'),
    path('rooms/<int:room_id>/delivered/', deliver_room, name='room-delivered'),
    path('rooms/<int:room_id>/read/', read_room, name='room-read'),
    path('messages/authorize/', authorize_message, name='message-authorization'),
    path('messages/send/', send_message, name='message-send'),
]
