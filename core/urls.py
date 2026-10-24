from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import BatchViewSet, DuesView, MeView, StudentViewSet

router = DefaultRouter()
router.register("batches", BatchViewSet, basename="batch")
router.register("students", StudentViewSet, basename="student")

urlpatterns = [
    path("me/", MeView.as_view(), name="me"),
    path("dues/", DuesView.as_view(), name="dues"),
] + router.urls