# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from __future__ import absolute_import
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('payment', '0008_remove_cybersource_level23_sample'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='paymentprocessorresponse',
            options={'get_latest_by': 'created', 'verbose_name': 'Payment Processor Response', 'verbose_name_plural': 'Payment Processor Responses'},
        ),
    ]
