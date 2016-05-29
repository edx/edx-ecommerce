import httpretty
import mock
from django.conf import settings
from django.test import RequestFactory
from edx_rest_api_client.client import EdxRestApiClient
from oscar.core.loading import get_model
from oscar.test import factories

from ecommerce.coupons.tests.mixins import CatalogPreviewMockMixin, CouponMixin
from ecommerce.extensions.catalogue.tests.mixins import CourseCatalogTestMixin
from ecommerce.tests.testcases import TestCase

Catalog = get_model('catalogue', 'Catalog')


class RangeTests(CouponMixin, CourseCatalogTestMixin, CatalogPreviewMockMixin, TestCase):
    def setUp(self):
        super(RangeTests, self).setUp()

        self.range = factories.RangeFactory()
        self.range_with_catalog = factories.RangeFactory()

        self.catalog = Catalog.objects.create(partner=self.partner)
        self.product = factories.create_product()

        self.range.add_product(self.product)
        self.range_with_catalog.catalog = self.catalog
        self.stock_record = factories.create_stockrecord(self.product, num_in_stock=2)
        self.catalog.stock_records.add(self.stock_record)

    def test_range_contains_product(self):
        """
        contains_product(product) should return Boolean value
        """
        self.assertTrue(self.range.contains_product(self.product))
        self.assertTrue(self.range_with_catalog.contains_product(self.product))

        not_in_range_product = factories.create_product()
        self.assertFalse(self.range.contains_product(not_in_range_product))
        self.assertFalse(self.range.contains_product(not_in_range_product))

    def test_range_number_of_products(self):
        """
        num_products() should return number of num_of_products
        """
        self.assertEqual(self.range.num_products(), 1)
        self.assertEqual(self.range_with_catalog.num_products(), 1)

    def test_range_all_products(self):
        """
        all_products() should return a list of products in range
        """
        self.assertIn(self.product, self.range.all_products())
        self.assertEqual(len(self.range.all_products()), 1)

        self.assertIn(self.product, self.range_with_catalog.all_products())
        self.assertEqual(len(self.range_with_catalog.all_products()), 1)

    @mock.patch('ecommerce.core.url_utils.get_current_request', mock.Mock(return_value=None))
    def test_run_catalog_query_no_request(self):
        """
        run_course_query() should return status 400 response when no request is present.
        """
        with self.assertRaises(Exception):
            self.range.run_catalog_query(self.product)

    @httpretty.activate
    @mock.patch(
        'ecommerce.extensions.offer.models.get_course_catalog_api_client',
        mock.Mock(return_value=EdxRestApiClient(
            settings.COURSE_CATALOG_API_URL,
            jwt='auth-token'
        ))
    )
    def test_run_catalog_query(self):
        """
        run_course_query() should return True for included course run ID's.
        """
        course, seat = self.create_course_and_seat()
        self.mock_dynamic_catalog_contains_api(query='key:*', course_run_ids=[course.id])
        request = RequestFactory()
        request.site = self.site

        with mock.patch('ecommerce.core.url_utils.get_current_request', mock.Mock(return_value=request)):
            response = self.range.run_catalog_query(seat)
            self.assertTrue(response['course_runs'][course.id])

    @httpretty.activate
    @mock.patch(
        'ecommerce.extensions.offer.models.get_course_catalog_api_client',
        mock.Mock(return_value=EdxRestApiClient(
            settings.COURSE_CATALOG_API_URL,
            jwt='auth-token'
        ))
    )
    def test_query_range_contains_product(self):
        """
        contains_product() should return the correct boolean if a product is in it's range.
        """
        course, seat = self.create_course_and_seat()
        self.mock_dynamic_catalog_contains_api(query='key:*', course_run_ids=[course.id])
        false_response = self.range.contains_product(seat)
        self.assertFalse(false_response)

        self.range.catalog_query = 'key:*'
        self.range.course_seat_types = 'verified'
        response = self.range.contains_product(seat)
        self.assertTrue(response)

    @httpretty.activate
    @mock.patch(
        'ecommerce.coupons.utils.get_course_catalog_api_client',
        mock.Mock(return_value=EdxRestApiClient(
            settings.COURSE_CATALOG_API_URL,
            jwt='auth-token'
        ))
    )
    def test_query_range_all_products(self):
        """
        all_products() should return seats from the query.
        """
        course, seat = self.create_course_and_seat()
        self.assertEqual(len(self.range.all_products()), 1)
        self.assertFalse(seat in self.range.all_products())

        self.mock_dynamic_catalog_course_runs_api(query='key:*', course_run=course)
        self.range.catalog_query = 'key:*'
        self.range.course_seat_types = 'verified'
        self.assertEqual(len(self.range.all_products()), 2)
        self.assertTrue(seat in self.range.all_products())
