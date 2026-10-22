from django.contrib import admin
from django import forms
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User
from django.contrib.auth.forms import UserChangeForm
from .models import InventoryItem, GardenDesign, GardenImage, Attendance, StaffProfile


@admin.register(InventoryItem)
class InventoryItemAdmin(admin.ModelAdmin):
    list_display = ['name', 'category', 'stock_quantity', 'unit_price']
    list_filter = ['category']
    search_fields = ['name']


@admin.register(GardenDesign)
class GardenDesignAdmin(admin.ModelAdmin):
    list_display = ['name', 'user', 'total_cost', 'created_at', 'updated_at']
    list_filter = ['status', 'created_at']
    readonly_fields = ['created_at', 'updated_at']


@admin.register(GardenImage)
class GardenImageAdmin(admin.ModelAdmin):
    list_display = ['pk', 'uploaded_at']


@admin.register(Attendance)
class AttendanceAdmin(admin.ModelAdmin):
    list_display = ['staff', 'booking', 'clock_in_time', 'clock_out_time', 'latitude', 'longitude']
    list_filter = ['clock_in_time', 'staff']
    search_fields = ['staff__username', 'booking__service_type']


class CustomUserChangeForm(UserChangeForm):
    role = forms.ChoiceField(
        choices=StaffProfile.ROLE_CHOICES + [('CUSTOMER', 'Customer')],
        required=False,
        help_text="Select the user's role. If Customer, no StaffProfile is maintained."
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            try:
                self.fields['role'].initial = self.instance.staff_profile.role
            except StaffProfile.DoesNotExist:
                self.fields['role'].initial = 'CUSTOMER'

    def save(self, commit=True):
        user = super().save(commit=commit)
        role = self.cleaned_data.get('role')
        if role and role != 'CUSTOMER':
            profile, created = StaffProfile.objects.get_or_create(user=user)
            profile.role = role
            profile.save()
        else:
            StaffProfile.objects.filter(user=user).delete()
        return user


# Unregister the default User admin
admin.site.unregister(User)


@admin.register(User)
class CustomUserAdmin(BaseUserAdmin):
    form = CustomUserChangeForm

    # Add role to the admin edit fieldsets
    fieldsets = list(BaseUserAdmin.fieldsets) + [
        ('Staff Role Info', {'fields': ('role',)}),
    ]

    # Add role to list_display
    list_display = list(BaseUserAdmin.list_display) + ['get_role']

    # Add role to list_filter
    list_filter = list(BaseUserAdmin.list_filter) + ['staff_profile__role']

    def get_role(self, obj):
        try:
            return obj.staff_profile.get_role_display()
        except StaffProfile.DoesNotExist:
            return 'Customer'
    get_role.short_description = 'Role'

