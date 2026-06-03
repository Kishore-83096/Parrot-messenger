from django.urls import path

from .views import create_group_room, group_avatar, group_room_detail


urlpatterns = [
    path('', create_group_room, name='group-create'),
    path('<int:room_id>/', group_room_detail, name='group-detail'),
    path('<int:room_id>/avatar/', group_avatar, name='group-avatar'),
]
