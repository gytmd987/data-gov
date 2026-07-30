from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/login/", auth_views.LoginView.as_view(
        template_name="login.html"), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    # 본인 비밀번호 변경(로그인 상태에서 현재 비밀번호 확인 후 변경)
    path("accounts/password/", auth_views.PasswordChangeView.as_view(
        template_name="password_change.html",
        success_url="/accounts/password/done/"), name="password_change"),
    path("accounts/password/done/", auth_views.PasswordChangeDoneView.as_view(
        template_name="password_change_done.html"), name="password_change_done"),
    path("console/", include("web.console.urls")),
    path("", include("web.chat.urls")),
]
