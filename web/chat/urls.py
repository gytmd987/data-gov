from django.urls import path

from . import views

urlpatterns = [
    path("", views.chat_page, name="chat"),
    path("chat/send", views.send, name="chat_send"),
    path("chat/history/<int:conversation_id>", views.history, name="chat_history"),
    path("chat/feedback", views.feedback, name="chat_feedback"),
    path("submit/", views.submit_document, name="submit_document"),
    path("docs/original/<str:doc_id>", views.original_file, name="original_file"),
]
