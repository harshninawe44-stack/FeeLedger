from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    BatchViewSet,
    DuesView,
    FeePlanViewSet,
    ImportStudentsView,
    MeView,
    PaymentViewSet,
    StudentViewSet,
)

router = DefaultRouter()
router.register("batches", BatchViewSet, basename="batch")
router.register("students", StudentViewSet, basename="student")
router.register("payments", PaymentViewSet, basename="payment")
router.register("fee-plans", FeePlanViewSet, basename="feeplan")

urlpatterns = [
    path("me/", MeView.as_view(), name="me"),
    path("dues/", DuesView.as_view(), name="dues"),
    path("imports/students/", ImportStudentsView.as_view(), name="import-students"),
] + router.urls