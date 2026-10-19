from django.contrib import admin, messages
from django.template.response import TemplateResponse
from django.urls import path

from .models import KYCDocument, KYCProfile
from .services import approve_profile, reject_profile
from .views import mint_signed_url


class KYCDocumentInline(admin.TabularInline):
    model = KYCDocument
    extra = 0
    can_delete = False
    readonly_fields = ('preview', 'kind', 'doc_type', 'content_type', 'uploaded_at')
    fields = ('kind', 'doc_type', 'content_type', 'uploaded_at', 'preview')

    def preview(self, obj):
        if not obj.file:
            return '—'
        # Short-lived signed URL; only staff can fetch it (the download view
        # checks is_staff again, so a leaked URL is useless to others).
        url = mint_signed_url(self.request, obj.id)
        label = obj.get_kind_display()
        if obj.content_type.startswith('image/'):
            return (
                f'<a href="{url}" target="_blank" rel="noopener">'
                f'<img src="{url}" alt="{label}" style="max-height:120px;max-width:220px;'
                f'border-radius:6px;border:1px solid #272C47;" /></a>'
            )
        return f'<a href="{url}" target="_blank" rel="noopener">Open {label}</a>'

    preview.short_description = 'Preview'

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(KYCProfile)
class KYCProfileAdmin(admin.ModelAdmin):
    list_display = (
        'user_email',
        'status',
        'full_name',
        'country',
        'doc_type',
        'submitted_at',
        'reviewed_at',
        'reviewed_by',
        'reject_link',
    )
    list_filter = ('status', 'country', 'doc_type')
    search_fields = ('user__email', 'full_name', 'phone')
    readonly_fields = (
        'user',
        'submitted_at',
        'reviewed_at',
        'reviewed_by',
        'resubmit_count',
        'resubmit_window_start',
        'created_at',
        'updated_at',
    )
    inlines = (KYCDocumentInline,)
    actions = ('approve_selected',)

    def user_email(self, obj):
        return obj.user.email

    user_email.short_description = 'User'
    user_email.admin_order_field = 'user__email'

    def reject_link(self, obj):
        from django.urls import reverse
        from django.utils.html import format_html

        url = reverse('admin:kyc_kycprofile_reject', args=[obj.id])
        return format_html('<a class="button" href="{}">Reject…</a>', url)

    reject_link.short_description = 'Reject (with reason)'

    def get_inline_instances(self, request, obj=None):
        inline = super().get_inline_instances(request, obj)
        # Give the inline access to the request so it can mint signed URLs.
        for i in inline:
            i.request = request
        return inline

    def get_urls(self):
        custom = [
            path(
                '<path:object_id>/reject/',
                self.admin_site.admin_view(self.reject_view),
                name='kyc_kycprofile_reject',
            ),
        ]
        return custom + super().get_urls()

    @admin.action(description='Approve selected KYC profiles (grant live access)')
    def approve_selected(self, request, queryset):
        count = 0
        for profile in queryset.exclude(status='verified'):
            approve_profile(profile, reviewer=request.user)
            count += 1
        self.message_user(
            request,
            f'{count} profile(s) approved. Live access granted and users notified.',
            messages.SUCCESS,
        )

    def reject_view(self, request, object_id):
        """Two-step reject: ask for a reason, then apply it."""
        profile = self.get_object(request, object_id)
        if profile is None:
            messages.error(request, 'Profile not found.')
            return self.response_post_save_change(request, profile)

        if request.method == 'POST':
            if 'confirm' in request.POST:
                reason = (request.POST.get('reason') or '').strip()
                reject_profile(profile, reason, reviewer=request.user)
                messages.success(
                    request,
                    f'{profile.user.email} was rejected and notified.',
                )
                return self.response_post_save_change(request, profile)
            # Cancelled
            return self.response_post_save_change(request, profile)

        context = {
            **self.admin_site.each_context(request),
            'title': 'Reject KYC profile',
            'profile': profile,
            'opts': self.model._meta,
            'media': self.media,
        }
        return TemplateResponse(request, 'kyc/admin/reject_confirm.html', context)
