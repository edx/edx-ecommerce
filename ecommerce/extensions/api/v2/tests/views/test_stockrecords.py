import json

from django.contrib.auth.models import Permission
from django.core.urlresolvers import reverse
from oscar.core.loading import get_model

from ecommerce.courses.models import Course
from ecommerce.extensions.api.v2.tests.views import JSON_CONTENT_TYPE, ProductSerializerMixin
from ecommerce.extensions.catalogue.tests.mixins import CourseCatalogTestMixin
from ecommerce.tests.mixins import ThrottlingMixin
from ecommerce.tests.testcases import TestCase

Product = get_model('catalogue', 'Product')
StockRecord = get_model('partner', 'StockRecord')


class StockRecordViewSetTests(ProductSerializerMixin, CourseCatalogTestMixin, ThrottlingMixin, TestCase):
    maxDiff = None
    list_path = reverse('api:v2:stockrecords-list')
    detail_path = 'api:v2:stockrecords-detail'

    def setUp(self):
        super(StockRecordViewSetTests, self).setUp()
        self.user = self.create_user()
        self.client.login(username=self.user.username, password=self.password)
        self.course = Course.objects.create(id='course-v1:edX+DemoX+Demo_Course', name='Demo Course')
        self.product = self.course.create_or_update_seat('honor', False, 0, self.partner)
        self.stockrecord = self.product.stockrecords.first()
        self.change_permission = Permission.objects.get(codename='change_stockrecord')

    def test_list(self):
        """ Verify a list of stock records is returned. """
        StockRecord.objects.create(partner=self.partner, product=self.product, partner_sku='dummy-sku',
                                   price_currency='USD', price_excl_tax=200.00)
        response = self.client.get(self.list_path)
        self.assertEqual(response.status_code, 200)
        results = [self.serialize_stockrecord(stockrecord) for stockrecord in
                   self.product.stockrecords.all()]
        expected = {'count': 2, 'next': None, 'previous': None, 'results': results}
        self.assertDictEqual(json.loads(response.content), expected)

    def test_list_with_no_stockrecords(self):
        """ Verify the endpoint returns an empty list. """
        StockRecord.objects.all().delete()
        response = self.client.get(self.list_path)
        self.assertEqual(response.status_code, 200)
        expected = {'count': 0, 'next': None, 'previous': None, 'results': []}
        self.assertDictEqual(json.loads(response.content), expected)

    def test_retrieve_with_invalid_id(self):
        """ Verify endpoint returns 404 if no stockrecord is available. """
        path = reverse(self.detail_path, kwargs={'pk': 999})
        response = self.client.get(path)
        self.assertEqual(response.status_code, 404)

    def test_retrieve(self):
        """ Verify a single stockrecord is returned. """
        path = reverse(self.detail_path, kwargs={'pk': self.stockrecord.id})
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        self.assertDictEqual(json.loads(response.content), self.serialize_stockrecord(self.stockrecord))

    def test_update(self):
        """ Verify update endpoint allows to update 'price_currency' and 'price_excl_tax'. """
        self.user.user_permissions.add(self.change_permission)
        self.user.save()

        data = {
            "price_currency": "PKR",
            "price_excl_tax": "500.00"
        }
        response = self.attempt_update(data)
        self.assertEqual(response.status_code, 200)

        stockrecord = StockRecord.objects.get(id=self.stockrecord.id)
        self.assertEqual(unicode(stockrecord.price_excl_tax), data['price_excl_tax'])
        self.assertEqual(stockrecord.price_currency, data['price_currency'])

    def test_update_without_permission(self):
        """ Verify only users with the change_stockrecord permission can update stock records. """
        self.user.user_permissions.clear()
        self.user.save()

        data = {
            "price_currency": "PKR",
            "price_excl_tax": "500.00"
        }
        response = self.attempt_update(data)
        self.assertEqual(response.status_code, 403)

    def test_allowed_fields_for_update(self):
        """ Verify the endpoint only allows the price_excl_tax and price_currency fields to be updated. """
        self.user.user_permissions.add(self.change_permission)
        self.user.save()

        data = {
            "partner_sku": "new_sku",
        }
        response = self.attempt_update(data)
        self.assertEqual(response.status_code, 400, response.content)
        stockrecord = StockRecord.objects.get(id=self.stockrecord.id)
        self.assertEqual(self.serialize_stockrecord(self.stockrecord), self.serialize_stockrecord(stockrecord))
        self.assertDictEqual(json.loads(response.content), {
            'message': 'Only the price_currency and price_excl_tax fields are allowed to be modified.'})

    def attempt_update(self, data):
        """ Helper method that attempts to update an existing StockRecord object.

        Arguments:
          data (dict): Data to be converted to JSON and sent to the API.

        Returns:
          Response: HTTP response from the API.
        """

        path = reverse(self.detail_path, kwargs={'pk': self.stockrecord.id})
        return self.client.put(path, json.dumps(data), JSON_CONTENT_TYPE)

    def test_create_stockrecord(self):
        """ Verify the endpoint supports the creation of new stock records. """

        self.user.user_permissions.add(Permission.objects.get(codename='add_stockrecord'))
        self.user.save()

        response = self.attempt_create()
        self.assertEqual(response.status_code, 201)

        # verify stock record exists
        self.assertTrue(StockRecord.objects.filter(product=self.product.id, partner_sku="new-sku").exists())

    def test_create_without_permission(self):
        """ Verify only users with the add_stockrecord permission can add stock records. """
        self.user.user_permissions.clear()
        self.user.save()

        response = self.attempt_create()
        self.assertEqual(response.status_code, 403)

    def attempt_create(self):
        """ Helping method that will try to create a new stockrecord. """
        data = {
            "product": self.product.id,
            "partner": self.partner.id,
            "partner_sku": "new-sku",
            "price_currency": "USD",
            "price_excl_tax": 50.00
        }

        return self.client.post(self.list_path, json.dumps(data), JSON_CONTENT_TYPE)
