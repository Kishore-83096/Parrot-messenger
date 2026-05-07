from django.urls import path

from .consumers import InboxConsumer, RoomConsumer


websocket_urlpatterns = [
    path('ws/inbox/', InboxConsumer.as_asgi()),
    path('ws/rooms/<int:room_id>/', RoomConsumer.as_asgi()),
]
