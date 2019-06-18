# -*- coding: utf-8 -*-
# Generated by Django 1.11.20 on 2019-06-06 19:17
from __future__ import unicode_literals

from __future__ import absolute_import
import django.db.models.deletion
import simple_history.models
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('sites', '0002_alter_domain_unique'),
        ('partner', '0014_historicalstockrecord'),
    ]

    operations = [
        migrations.CreateModel(
            name='HistoricalPartner',
            fields=[
                ('id', models.IntegerField(auto_created=True, blank=True, db_index=True, verbose_name='ID')),
                ('name', models.CharField(blank=True, max_length=128, verbose_name='Name')),
                ('short_code', models.CharField(db_index=True, max_length=8)),
                ('enable_sailthru', models.BooleanField(default=True, help_text=b'DEPRECATED: Use SiteConfiguration!', verbose_name='Enable Sailthru Reporting')),
                ('history_id', models.AutoField(primary_key=True, serialize=False)),
                ('history_date', models.DateTimeField()),
                ('history_change_reason', models.CharField(max_length=100, null=True)),
                ('history_type', models.CharField(choices=[('+', 'Created'), ('~', 'Changed'), ('-', 'Deleted')], max_length=1)),
                ('default_site', models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to='sites.Site')),
                ('history_user', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ('-history_date', '-history_id'),
                'get_latest_by': 'history_date',
                'verbose_name': 'historical Partner',
            },
            bases=(simple_history.models.HistoricalChanges, models.Model),
        ),
    ]
