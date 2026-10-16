from django.contrib import admin

from .models import Instrument


@admin.register(Instrument)
class InstrumentAdmin(admin.ModelAdmin):
    list_display = ('symbol', 'name', 'category', 'payout_percent', 'base_price', 'is_active', 'sort_order')
    list_filter = ('category', 'is_active')
    search_fields = ('symbol', 'name')
    list_editable = ('payout_percent', 'is_active', 'sort_order')
    ordering = ('category', 'sort_order', 'symbol')
