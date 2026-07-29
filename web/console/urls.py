from django.urls import path

from . import views

urlpatterns = [
    path("review/", views.review, name="console_review"),
    path("review/<str:doc_id>/submit", views.review_submit, name="console_review_submit"),
    path("review/<str:doc_id>/cancel", views.review_cancel, name="console_review_cancel"),
    path("docs/", views.docs, name="console_docs"),
    path("docs/sweep", views.docs_sweep, name="console_docs_sweep"),
    path("docs/<str:doc_id>/action", views.docs_action, name="console_docs_action"),
    path("docs/<str:doc_id>/request", views.docs_request, name="console_docs_request"),
    path("docs/<str:doc_id>/relate", views.docs_relate, name="console_docs_relate"),
    path("requests/", views.requests_queue, name="console_requests"),
    path("requests/<int:req_id>/resolve", views.request_resolve, name="console_request_resolve"),
    path("users/", views.users, name="console_users"),
    path("org/", views.org_console, name="console_org"),
]
