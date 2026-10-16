from django.contrib import admin

from .models import Trade, Wallet


@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display = ('user', 'demo_balance', 'updated_at')
    search_fields = ('user__username', 'user__email')


@admin.register(Trade)
class TradeAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'user',
        'instrument',
        'direction',
        'status',
        'stake',
        'payout_percent',
        'open_price',
        'close_price',
        'expiry_time',
    )
    list_filter = ('status', 'direction', 'instrument__category')
    search_fields = ('user__username', 'user__email', 'instrument__symbol')
    readonly_fields = ('profit', 'gross_payout', 'open_price', 'close_price')
    ordering = ('-created_at',)
