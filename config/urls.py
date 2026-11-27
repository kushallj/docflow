from django.contrib import admin
from django.urls import include, path
from rest_framework.authtoken.views import obtain_auth_token
from rest_framework.routers import DefaultRouter

from apps.dashboard.views import protected_file
from apps.documents.views import DocumentTypeViewSet, DocumentViewSet, VendorViewSet
from apps.rules.views import RuleSetViewSet

router = DefaultRouter()
router.register("documents", DocumentViewSet, basename="document")
router.register("document-types", DocumentTypeViewSet, basename="document-type")
router.register("rulesets", RuleSetViewSet, basename="ruleset")
router.register("vendors", VendorViewSet, basename="vendor")

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include(router.urls)),
    path("api/auth/token/", obtain_auth_token),
    path("files/<path:path>", protected_file, name="protected-file"),
    path("", include("apps.dashboard.urls")),
]
