from django.urls import path

from . import views

urlpatterns = [
    path("review/", views.review, name="console_review"),
    path("review/<str:doc_id>/submit", views.review_submit, name="console_review_submit"),
    path("docs/", views.docs, name="console_docs"),
    path("docs/<str:doc_id>/action", views.docs_action, name="console_docs_action"),
    path("users/", views.users, name="console_users"),
]
