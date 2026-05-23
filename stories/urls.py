from django.urls import path

from .views import (
    complete_story_upload_intent,
    create_story,
    create_story_upload_intents,
    my_stories,
    story_delete,
    story_feed,
    story_reaction,
    story_reply,
    story_view,
    story_viewers,
)


urlpatterns = [
    path('', create_story, name='story-create'),
    path('feed/', story_feed, name='story-feed'),
    path('mine/', my_stories, name='story-mine'),
    path('upload-intents/', create_story_upload_intents, name='story-upload-intent-create'),
    path(
        'upload-intents/<uuid:upload_intent_id>/complete/',
        complete_story_upload_intent,
        name='story-upload-intent-complete',
    ),
    path('<uuid:story_id>/', story_delete, name='story-delete'),
    path('<uuid:story_id>/view/', story_view, name='story-view'),
    path('<uuid:story_id>/viewers/', story_viewers, name='story-viewers'),
    path('<uuid:story_id>/reaction/', story_reaction, name='story-reaction'),
    path('<uuid:story_id>/reply/', story_reply, name='story-reply'),
]
