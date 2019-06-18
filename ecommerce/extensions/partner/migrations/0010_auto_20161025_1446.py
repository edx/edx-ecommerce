# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from __future__ import absolute_import
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('partner', '0009_partner_enable_sailthru'),
    ]

    operations = [
        migrations.AlterField(
            model_name='partner',
            name='enable_sailthru',
            field=models.BooleanField(default=True, help_text='Determines if purchases/enrolls should be reported to Sailthru.', verbose_name='Enable Sailthru Reporting'),
        ),
    ]
