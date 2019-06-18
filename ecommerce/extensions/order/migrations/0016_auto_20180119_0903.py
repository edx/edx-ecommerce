# -*- coding: utf-8 -*-
# Generated by Django 1.10.7 on 2018-01-19 09:03
from __future__ import absolute_import, unicode_literals

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('order', '0015_create_disable_repeat_order_check_switch'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='historicalline',
            name='history_user',
        ),
        migrations.RemoveField(
            model_name='historicalline',
            name='order',
        ),
        migrations.RemoveField(
            model_name='historicalline',
            name='partner',
        ),
        migrations.RemoveField(
            model_name='historicalline',
            name='product',
        ),
        migrations.RemoveField(
            model_name='historicalline',
            name='stockrecord',
        ),
        migrations.RemoveField(
            model_name='historicalorder',
            name='basket',
        ),
        migrations.RemoveField(
            model_name='historicalorder',
            name='billing_address',
        ),
        migrations.RemoveField(
            model_name='historicalorder',
            name='history_user',
        ),
        migrations.RemoveField(
            model_name='historicalorder',
            name='shipping_address',
        ),
        migrations.RemoveField(
            model_name='historicalorder',
            name='site',
        ),
        migrations.RemoveField(
            model_name='historicalorder',
            name='user',
        ),
        migrations.DeleteModel(
            name='HistoricalLine',
        ),
        migrations.DeleteModel(
            name='HistoricalOrder',
        ),
    ]
