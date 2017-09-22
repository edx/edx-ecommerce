# -*- coding: utf-8 -*-
# Generated by Django 1.10.7 on 2017-08-10 15:16
from __future__ import unicode_literals

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sites', '0002_alter_domain_unique'),
        ('offer', '0013_auto_20170801_0742'),
    ]

    operations = [
        migrations.AddField(
            model_name='conditionaloffer',
            name='site',
            field=models.ForeignKey(blank=True, default=None, null=True, on_delete=django.db.models.deletion.CASCADE, to='sites.Site', verbose_name='Site'),
        ),
    ]
