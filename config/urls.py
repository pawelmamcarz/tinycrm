from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.http import HttpResponse
from django.urls import include, path

urlpatterns = [
    path("healthz/", lambda request: HttpResponse("ok", content_type="text/plain"), name="healthz"),
    path("admin/", admin.site.urls),
    path("", include("apps.core.urls")),
    path("", include("apps.users.urls")),
    path("klienci/", include("apps.customers.urls")),
    path("zadania/", include("apps.tasks.urls")),
    path("notatki/", include("apps.voice.urls")),
    path("serwis/", include("apps.service.urls")),
    path("powiadomienia/", include("apps.notify.urls")),
    path("integracje/", include("apps.erp_sync.urls")),
    path("teren/", include("apps.pwa.urls")),
]
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
