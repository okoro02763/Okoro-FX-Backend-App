from django.urls import re_path

from . import views


def route(route, view, name):
    return re_path(rf'^{route}/?$', view, name=name)


urlpatterns = [
    route('trades/settle', views.settle_view, name='bo-settle'),
    route('trades', views.trades, name='bo-trades'),
    route('pending/cancel', views.cancel_pending_view, name='bo-pending-cancel'),
    route('pending', views.pending_orders, name='bo-pending'),
    route('wallet/reset', views.reset_wallet_view, name='bo-wallet-reset'),
    route('wallet', views.get_wallet_view, name='bo-wallet'),
]
