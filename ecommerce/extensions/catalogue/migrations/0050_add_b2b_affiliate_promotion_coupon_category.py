""" Add 'B2B Affiliate Promotion' category to the list of coupon categories"""

from django.db import migrations

from ecommerce.extensions.catalogue.utils import create_subcategories

COUPON_CATEGORY_NAME = 'Coupons'

NEW_CATEGORIES = [
    'B2B Affiliate Promotion',
]


def create_new_categories(apps, schema_editor):
    """ Create new coupon categories """
    Category = apps.get_model("catalogue", "Category")

    Category.skip_history_when_saving = True
    create_subcategories(Category, COUPON_CATEGORY_NAME, NEW_CATEGORIES)


def remove_new_categories(apps, schema_editor):
    """ Remove new coupon categories """
    Category = apps.get_model("catalogue", "Category")

    Category.skip_history_when_saving = True
    Category.objects.get(name=COUPON_CATEGORY_NAME).get_children().filter(
        name__in=NEW_CATEGORIES
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('catalogue', '0049_add_rap_and_orap_coupon_categories'),
    ]

    operations = [
        migrations.RunPython(create_new_categories, remove_new_categories)
    ]
