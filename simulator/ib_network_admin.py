from django import forms
from django.contrib import admin
from django.core.exceptions import ValidationError
from .models import IBNetworkLink, IBNetworkExecution, IBNetworkAccrual, IBNetworkCarry
from .ib_network import link_sub_ib, ancestors

class LinkForm(forms.ModelForm):
    class Meta:
        model = IBNetworkLink
        fields = ('child', 'parent', 'reason')
    def clean(self):
        data = super().clean()
        child, parent = data.get('child'), data.get('parent')
        if child and parent:
            if child.pk == parent.pk or child.pk in {r.pk for r in ancestors(parent)}:
                raise ValidationError('No se permiten autorreferencias ni ciclos.')
            if IBNetworkLink.objects.filter(child=child).exists():
                raise ValidationError('Este IB ya tiene un superior.')
        return data

@admin.register(IBNetworkLink)
class NetworkLinkAdmin(admin.ModelAdmin):
    form = LinkForm
    list_display = ('child', 'parent', 'created_at', 'created_by', 'reason')
    raw_id_fields = ('child', 'parent')
    search_fields = ('child__code', 'parent__code', 'child__user__username', 'parent__user__username')
    def get_readonly_fields(self, request, obj=None):
        return ('created_at', 'created_by') if obj is None else tuple(f.name for f in self.model._meta.fields)
    def has_change_permission(self, request, obj=None):
        return False
    def has_delete_permission(self, request, obj=None):
        return False
    def save_model(self, request, obj, form, change):
        link = link_sub_ib(obj.child, obj.parent, actor=request.user, reason=obj.reason)
        obj.pk = link.pk
        obj.created_by = request.user
        obj.created_at = link.created_at

class ReadOnlyNetworkAdmin(admin.ModelAdmin):
    def get_readonly_fields(self, request, obj=None):
        return tuple(f.name for f in self.model._meta.fields)
    def has_add_permission(self, request): return False
    def has_change_permission(self, request, obj=None): return False
    def has_delete_permission(self, request, obj=None): return False

@admin.register(IBNetworkAccrual)
class AccrualAdmin(ReadOnlyNetworkAdmin):
    list_display = ('id', 'execution', 'referral', 'level', 'exact_amount', 'obligation')
    list_filter = ('level',)
    search_fields = ('referral__code',)

admin.site.register(IBNetworkExecution, ReadOnlyNetworkAdmin)
admin.site.register(IBNetworkCarry, ReadOnlyNetworkAdmin)
