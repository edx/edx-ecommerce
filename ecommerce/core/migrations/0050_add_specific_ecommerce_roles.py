# -*- coding: utf-8 -*-
# Generated by Django 1.11.15 on 2019-03-14 11:08
from __future__ import absolute_import, unicode_literals

from django.db import migrations

from ecommerce.core.constants import ENTERPRISE_COUPON_ADMIN_ROLE


def create_roles(apps, schema_editor):
    """Create the ecommerce roles if they do not already exist."""
    EcommerceFeatureRole = apps.get_model('core', 'EcommerceFeatureRole')
    EcommerceFeatureRole.objects.update_or_create(name=ENTERPRISE_COUPON_ADMIN_ROLE)


def delete_roles(apps, schema_editor):
    """Delete the ecommerce roles."""
    EcommerceFeatureRole = apps.get_model('core', 'EcommerceFeatureRole')
    EcommerceFeatureRole.objects.filter(name=ENTERPRISE_COUPON_ADMIN_ROLE).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0049_ecommercefeaturerole_ecommercefeatureroleassignment'),
    ]

    operations = [
        migrations.RunPython(create_roles, delete_roles)
    ]
