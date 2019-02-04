# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import datetime
import json
from uuid import uuid4

import ddt
import httpretty
import mock
from django.conf import settings
from django.urls import reverse
from django.utils.timezone import now
from oscar.core.loading import get_model
from oscar.test import factories
from rest_framework import status
from waffle.models import Switch

from ecommerce.coupons.tests.mixins import CouponMixin, DiscoveryMockMixin
from ecommerce.courses.tests.factories import CourseFactory
from ecommerce.enterprise.benefits import BENEFIT_MAP as ENTERPRISE_BENEFIT_MAP
from ecommerce.enterprise.conditions import AssignableEnterpriseCustomerCondition
from ecommerce.enterprise.constants import ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH
from ecommerce.enterprise.tests.mixins import EnterpriseServiceMockMixin
from ecommerce.extensions.catalogue.tests.mixins import DiscoveryTestMixin
from ecommerce.extensions.checkout.mixins import EdxOrderPlacementMixin
from ecommerce.extensions.offer.constants import (
    OFFER_ASSIGNMENT_REVOKED,
    OFFER_REDEEMED,
    VOUCHER_PARTIAL_REDEEMED,
    VOUCHER_REDEEMED,
    VOUCHER_UNASSIGNED,
    VOUCHER_UNREDEEMED,
)
from ecommerce.invoice.models import Invoice
from ecommerce.programs.custom import class_path
from ecommerce.tests.mixins import ThrottlingMixin
from ecommerce.tests.testcases import TestCase

Basket = get_model('basket', 'Basket')
Benefit = get_model('offer', 'Benefit')
OfferAssignment = get_model('offer', 'OfferAssignment')
Product = get_model('catalogue', 'Product')
Voucher = get_model('voucher', 'Voucher')
VoucherApplication = get_model('voucher', 'VoucherApplication')

ENTERPRISE_COUPONS_LINK = reverse('api:v2:enterprise-coupons-list')


class TestEnterpriseCustomerView(EnterpriseServiceMockMixin, TestCase):

    dummy_enterprise_customer_data = {
        'results': [
            {
                'name': 'Starfleet Academy',
                'uuid': '5113b17bf79f4b5081cf3be0009bc96f',
                'hypothetical_private_info': 'seriously, very private',
            },
            {
                'name': 'Millennium Falcon',
                'uuid': 'd1fb990fa2784a52a44cca1118ed3993',
            }
        ]
    }

    @mock.patch('ecommerce.enterprise.utils.EdxRestApiClient')
    @httpretty.activate
    def test_get_customers(self, mock_client):
        self.mock_access_token_response()
        instance = mock_client.return_value
        setattr(
            instance,
            'enterprise-customer',
            mock.MagicMock(
                get=mock.MagicMock(
                    return_value=self.dummy_enterprise_customer_data
                )
            ),
        )
        url = reverse('api:v2:enterprise:enterprise_customers')
        result = self.client.get(url)
        self.assertEqual(result.status_code, status.HTTP_401_UNAUTHORIZED)

        user = self.create_user(is_staff=True)

        self.client.login(username=user.username, password=self.password)

        result = self.client.get(url)
        self.assertEqual(result.status_code, status.HTTP_200_OK)
        self.assertJSONEqual(
            result.content.decode('utf-8'),
            {
                'results': [
                    {
                        'name': 'Millennium Falcon',
                        'id': 'd1fb990fa2784a52a44cca1118ed3993'
                    },
                    {
                        'name': 'Starfleet Academy',
                        'id': '5113b17bf79f4b5081cf3be0009bc96f'
                    }  # Note that the private information from the API has been stripped
                ]
            }
        )


@ddt.ddt
class EnterpriseCouponViewSetTest(CouponMixin, DiscoveryTestMixin, DiscoveryMockMixin, ThrottlingMixin, TestCase):
    """
    Test the enterprise coupon order functionality.
    """
    def setUp(self):
        super(EnterpriseCouponViewSetTest, self).setUp()
        self.user = self.create_user(is_staff=True)
        self.client.login(username=self.user.username, password=self.password)

        self.data = {
            'benefit_type': Benefit.PERCENTAGE,
            'benefit_value': 100,
            'category': {'name': self.category.name},
            'code': '',
            'end_datetime': str(now() + datetime.timedelta(days=10)),
            'price': 100,
            'quantity': 2,
            'start_datetime': str(now() - datetime.timedelta(days=10)),
            'title': 'Tešt Enterprise čoupon',
            'voucher_type': Voucher.SINGLE_USE,
            'enterprise_customer': {'name': 'test enterprise', 'id': str(uuid4()).decode('utf-8')},
            'enterprise_customer_catalog': str(uuid4()).decode('utf-8'),
            'notify_email': 'batman@gotham.comics',
        }

        self.course = CourseFactory(id='course-v1:test-org+course+run', partner=self.partner)
        self.verified_seat = self.course.create_or_update_seat('verified', False, 100)
        self.enterprise_slug = 'batman'

        patcher = mock.patch('ecommerce.extensions.api.v2.utils.send_mail')
        self.send_mail_patcher = patcher.start()
        self.addCleanup(patcher.stop)

    def get_coupon_voucher(self, coupon):
        """
        Helper method to get coupon voucher.
        """
        return coupon.attr.coupon_vouchers.vouchers.first()

    def get_coupon_data(self, coupon_title):
        """
        Helper method to return coupon data by coupon title.
        """
        coupon = Product.objects.get(title=coupon_title)
        return {
            'end_date': self.get_coupon_voucher_end_date(coupon),
            'has_error': False,
            'id': coupon.id,
            'max_uses': None,
            'num_codes': 2,
            'num_unassigned': 0,
            'num_uses': 0,
            'start_date': self.get_coupon_voucher_start_date(coupon),
            'title': coupon.title,
            'usage_limitation': 'Single use'
        }

    def get_coupon_voucher_start_date(self, coupon):
        """
        Helper method to return coupon voucher start date.
        """
        return self.get_coupon_voucher(coupon).start_datetime.isoformat().replace('+00:00', 'Z')

    def get_coupon_voucher_end_date(self, coupon):
        """
        Helper method to return coupon voucher end date.
        """
        return self.get_coupon_voucher(coupon).end_datetime.isoformat().replace('+00:00', 'Z')

    def get_response(self, method, path, data=None):
        """
        Helper method for sending requests and returning the response.
        """
        enterprise_id = ''
        enterprise_name = 'ToyX'
        if data and isinstance(data, dict) and data.get('enterprise_customer'):
            enterprise_id = data['enterprise_customer']['id']
            enterprise_name = data['enterprise_customer']['name']

        with mock.patch('ecommerce.extensions.voucher.utils.get_enterprise_customer') as patch1:
            with mock.patch('ecommerce.extensions.api.v2.utils.get_enterprise_customer') as patch2:
                patch1.return_value = patch2.return_value = {
                    'name': enterprise_name,
                    'enterprise_customer_uuid': enterprise_id,
                    'slug': self.enterprise_slug,
                }
                if method == 'GET':
                    return self.client.get(path)
                elif method == 'POST':
                    return self.client.post(path, json.dumps(data), 'application/json')
                elif method == 'PUT':
                    return self.client.put(path, json.dumps(data), 'application/json')
        return None

    def get_response_json(self, method, path, data=None):
        """
        Helper method for sending requests and returning JSON response content.
        """
        response = self.get_response(method, path, data)
        if response:
            return json.loads(response.content)
        return None

    def assert_new_codes_email(self):
        """
        Verify that new codes email is sent as expected
        """
        self.send_mail_patcher.assert_called_with(
            subject=settings.NEW_CODES_EMAIL_CONFIG['email_subject'],
            message=settings.NEW_CODES_EMAIL_CONFIG['email_body'].format(enterprise_slug=self.enterprise_slug),
            from_email=settings.NEW_CODES_EMAIL_CONFIG['from_email'],
            recipient_list=[self.data['notify_email']],
            fail_silently=False
        )

    def test_new_codes_email_for_enterprise_coupon(self):
        """"
        Test that new codes emails is sent with correct data upon enterprise coupon creation.
        """
        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': True})
        self.get_response('POST', ENTERPRISE_COUPONS_LINK, self.data)
        self.assert_new_codes_email()

    def test_list_enterprise_coupons(self):
        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': True})
        self.get_response('POST', ENTERPRISE_COUPONS_LINK, self.data)
        self.create_coupon()
        self.assertEqual(Product.objects.filter(product_class__name='Coupon').count(), 2)

        response = self.client.get(ENTERPRISE_COUPONS_LINK)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        coupon_data = json.loads(response.content)['results']
        self.assertEqual(len(coupon_data), 1)
        self.assertEqual(coupon_data[0]['title'], self.data['title'])
        self.assertEqual(coupon_data[0]['client'], self.data['enterprise_customer']['name'])
        self.assertEqual(coupon_data[0]['enterprise_customer'], self.data['enterprise_customer']['id'])
        self.assertEqual(coupon_data[0]['enterprise_customer_catalog'], self.data['enterprise_customer_catalog'])
        self.assertEqual(coupon_data[0]['code_status'], 'ACTIVE')

    def test_create_ent_offers_switch_off(self):
        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': False})
        response = self.get_response('POST', ENTERPRISE_COUPONS_LINK, self.data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_ent_offers_switch_on(self):
        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': True})
        response = self.get_response('POST', ENTERPRISE_COUPONS_LINK, self.data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        coupon = Product.objects.get(title=self.data['title'])
        enterprise_customer_id = self.data['enterprise_customer']['id']
        enterprise_name = self.data['enterprise_customer']['name']
        enterprise_catalog_id = self.data['enterprise_customer_catalog']
        vouchers = coupon.attr.coupon_vouchers.vouchers.all()
        for voucher in vouchers:
            all_offers = voucher.offers.all()
            self.assertEqual(len(all_offers), 1)
            offer = all_offers[0]
            self.assertEqual(str(offer.condition.enterprise_customer_uuid), enterprise_customer_id)
            self.assertEqual(str(offer.condition.enterprise_customer_catalog_uuid), enterprise_catalog_id)
            self.assertEqual(offer.condition.proxy_class, class_path(AssignableEnterpriseCustomerCondition))
            self.assertIsNone(offer.condition.range)
            self.assertEqual(offer.benefit.proxy_class, class_path(ENTERPRISE_BENEFIT_MAP[self.data['benefit_type']]))
            self.assertEqual(offer.benefit.value, self.data['benefit_value'])
            self.assertIsNone(offer.benefit.range)

        # Check that the enterprise name took precedence as the client name
        basket = Basket.objects.filter(lines__product_id=coupon.id).first()
        invoice = Invoice.objects.get(order__basket=basket)
        self.assertEqual(invoice.business_client.name, enterprise_name)
        self.assertEqual(str(invoice.business_client.enterprise_customer_uuid), enterprise_customer_id)

    def test_update_ent_offers_switch_off(self):
        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': True})
        self.get_response('POST', ENTERPRISE_COUPONS_LINK, self.data)
        coupon = Product.objects.get(title=self.data['title'])

        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': False})
        response = self.get_response(
            'PUT',
            reverse('api:v2:enterprise-coupons-detail', kwargs={'pk': coupon.id}),
            data={
                'title': 'Updated Enterprise Coupon',
            }
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_update_ent_offers_switch_on(self):
        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': True})
        self.get_response('POST', ENTERPRISE_COUPONS_LINK, self.data)
        coupon = Product.objects.get(title=self.data['title'])

        self.get_response(
            'PUT',
            reverse('api:v2:enterprise-coupons-detail', kwargs={'pk': coupon.id}),
            data={
                'title': 'Updated Enterprise Coupon',
            }
        )
        updated_coupon = Product.objects.get(title='Updated Enterprise Coupon')
        self.assertEqual(coupon.id, updated_coupon.id)

    def test_update_non_ent_coupon(self):
        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': True})
        coupon = self.create_coupon()
        response = self.get_response(
            'PUT',
            reverse('api:v2:enterprise-coupons-detail', kwargs={'pk': coupon.id}),
            data={
                'title': 'Updated Enterprise Coupon',
            }
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_update_migrated_ent_coupon(self):
        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': False})
        self.data.update({
            'catalog_query': '*:*',
            'course_seat_types': ['verified'],
            'benefit_value': 20,
            'title': 'Migrated Enterprise Coupon',
        })
        self.get_response('POST', reverse('api:v2:coupons-list'), self.data)
        coupon = Product.objects.get(title='Migrated Enterprise Coupon')

        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': True})
        new_catalog = str(uuid4()).decode('utf-8')
        self.get_response(
            'PUT',
            reverse('api:v2:enterprise-coupons-detail', kwargs={'pk': coupon.id}),
            data={
                'enterprise_customer_catalog': new_catalog,
                'benefit_value': 50,
                'title': 'Updated Enterprise Coupon',
            }
        )
        updated_coupon = Product.objects.get(title='Updated Enterprise Coupon')
        self.assertEqual(coupon.id, updated_coupon.id)
        vouchers = updated_coupon.attr.coupon_vouchers.vouchers.all()
        for voucher in vouchers:
            all_offers = voucher.offers.all()
            self.assertEqual(len(all_offers), 2)
            original_offer = all_offers[0]
            self.assertEqual(original_offer.benefit.value, 50)
            self.assertEqual(str(original_offer.condition.range.enterprise_customer_catalog), new_catalog)
            enterprise_offer = all_offers[1]
            self.assertEqual(enterprise_offer.benefit.value, 50)
            self.assertEqual(str(enterprise_offer.condition.enterprise_customer_catalog_uuid), new_catalog)

    def test_update_max_uses_single_use(self):
        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': True})
        self.get_response('POST', ENTERPRISE_COUPONS_LINK, self.data)
        coupon = Product.objects.get(title=self.data['title'])
        response = self.get_response(
            'PUT',
            reverse('api:v2:enterprise-coupons-detail', kwargs={'pk': coupon.id}),
            data={
                'max_uses': 5,
            }
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_update_max_uses_invalid_value(self):
        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': True})
        self.data.update({
            'voucher_type': Voucher.MULTI_USE,
            'max_uses': 5,
        })
        self.get_response('POST', ENTERPRISE_COUPONS_LINK, self.data)
        coupon = Product.objects.get(title=self.data['title'])
        response = self.get_response(
            'PUT',
            reverse('api:v2:enterprise-coupons-detail', kwargs={'pk': coupon.id}),
            data={
                'max_uses': -5,
            }
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def assert_coupon_codes_response(self, response, coupon_id, max_uses=1, results_count=0, pagination=None, is_csv=False):
        """
        Verify response received from `/api/v2/enterprise/coupons/{coupon_id}/codes/` endpoint
        """
        coupon = Product.objects.get(id=coupon_id)

        all_coupon_codes = coupon.attr.coupon_vouchers.vouchers.values_list('code', flat=True)
        all_coupon_codes = [code for code in all_coupon_codes]

        if is_csv:
            total_result_count = len(response)
            all_received_codes = [result.split(',')[1] for result in response if result]
            all_received_code_max_uses = [
                int(result.split(',')[3])
                for result in response if result
            ]
        else:
            total_result_count = len(response['results'])
            all_received_codes = [result['code'] for result in response['results']]
            all_received_code_max_uses = [
                result['redemptions']['total']
                for result in response['results']
            ]

        voucher_type = coupon.attr.coupon_vouchers.vouchers.first().usage
        if results_count and voucher_type != Voucher.SINGLE_USE:
            # `max_uses` should be same for all codes
            max_uses = max_uses or 1
            self.assertEqual(set(all_received_code_max_uses), set([max_uses]))

        # total count of results returned is correct
        self.assertEqual(total_result_count, results_count)

        # all received codes must be equals to coupon codes
        self.assertTrue(set(all_received_codes).issubset(all_coupon_codes))

        if pagination:
            self.assertEqual(response['count'], pagination['count'])
            self.assertEqual(response['next'], pagination['next'])
            self.assertEqual(response['previous'], pagination['previous'])

    def use_voucher(self, voucher, user):
        """
        Mark voucher as used by provided user
        """
        order = factories.OrderFactory()
        order_line = factories.OrderLineFactory(product=self.verified_seat)
        order.lines.add(order_line)
        voucher.record_usage(order, user)
        offer = voucher.offers.first()
        offer.record_usage(discount={'freq': 1, 'discount': 1})
        # Update assignment if exists.
        self.update_assigned_voucher_offer_assignment(voucher, user, order, offer)

    def update_assigned_voucher_offer_assignment(self, voucher, user, order, offer):
        """
        Mark assignment as redeemed by provided user.
        """
        assignments = offer.offerassignment_set.filter(
            code=voucher.code, user_email=user.email
        ).exclude(
            status__in=[OFFER_REDEEMED, OFFER_ASSIGNMENT_REVOKED]
        )

        for assignment in assignments:
            assignment.voucher_application = voucher.applications.filter(
                user=user,
                order=order
            ).order_by('-date_created').first()
            assignment.status = OFFER_REDEEMED
            assignment.save()

    def create_enterprise_coupon(self, coupon_data, with_applications=False):
        """
        Create coupon and voucher applications(redemeptions) if required.
        """
        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': True})

        # create coupon
        coupon = self.get_response('POST', ENTERPRISE_COUPONS_LINK, coupon_data)
        coupon = coupon.json()
        coupon_id = coupon['coupon_id']

        # create some voucher applications
        if with_applications:
            max_uses = coupon_data.get('max_uses', 1)
            coupon = Product.objects.get(id=coupon_id)
            coupon_vouchers = coupon.attr.coupon_vouchers.vouchers.all()
            self.create_coupon_vouchers(coupon_vouchers=coupon_vouchers, max_uses=max_uses)

        return coupon_id

    def create_coupon_vouchers(self, coupon_vouchers, voucher_redemptions=None, max_uses=None, redeem_completely=True):
        for i, voucher in enumerate(coupon_vouchers):
            # How many uses for a voucher to create.
            if not redeem_completely:
                voucher_usage = voucher_redemptions[i] if voucher_redemptions is not None else 0
            else:
                voucher_usage = max_uses if max_uses else 1

            for usage_index in range(voucher_usage):
                self.use_voucher(voucher, self.create_user(
                    email='user{email_index}@example.com'.format(email_index=usage_index))
                )

    def assign_coupon_codes(self, coupon_id, vouchers, voucher_assignments=None):
        """
        Assigns codes.
        """
        # for _ in range(voucher_assignmentsmax_uses or 1)):
        for i, voucher in enumerate(vouchers):
            if voucher_assignments[i] == 0:
                continue

            # For multi-use-per-customer case, email list should be same.
            if voucher.usage == Voucher.MULTI_USE_PER_CUSTOMER:
                emails = ['user0@example.com' for _ in range(voucher_assignments[i])]
            else:
                emails = [
                    'user{email_index}@example.com'.format(email_index=email_index)
                    for email_index in range(voucher_assignments[i])
                ]

            self.get_response(
                'POST',
                '/api/v2/enterprise/coupons/{}/assign/'.format(coupon_id),
                {'emails': emails, 'codes': [voucher.code], 'template': 'Test template'}
            )

    @ddt.data(
        {
            'voucher_type': Voucher.SINGLE_USE,
            'quantity': 2,
            'max_uses': None,
            'expected_results_count': 2
        },
        {
            'voucher_type': Voucher.ONCE_PER_CUSTOMER,
            'quantity': 2,
            'max_uses': 2,
            'expected_results_count': 4
        },
        {
            'voucher_type': Voucher.MULTI_USE,
            'quantity': 2,
            'max_uses': 3,
            'expected_results_count': 6
        },
    )
    def test_coupon_codes_detail(self, data):
        """
        Verify that `/api/v2/enterprise/coupons/{coupon_id}/codes/` endpoint returns correct data for different coupons
        """
        endpoint = '/api/v2/enterprise/coupons/{}/codes/'
        pagination = {
            'count': data['expected_results_count'],
            'next': None,
            'previous': None,
        }

        coupon_id = self.create_enterprise_coupon(
            dict(self.data, voucher_type=data['voucher_type'], quantity=data['quantity'], max_uses=data['max_uses']),
            with_applications=True
        )

        # Get coupon codes usage details
        response = self.get_response('GET', endpoint.format(coupon_id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response = response.json()
        self.assert_coupon_codes_response(
            response,
            coupon_id,
            max_uses=data['max_uses'],
            results_count=data['expected_results_count'],
            pagination=pagination
        )

    @ddt.data(
        {
            'page': 1,
            'page_size': 2,
            'pagination': {
                'count': 6,
                'next': 'http://testserver.fake/api/v2/enterprise/coupons/{}/codes/?page=2&page_size=2',
                'previous': None,
            },
            'expected_results_count': 2,
        },
        {
            'page': 2,
            'page_size': 4,
            'pagination': {
                'count': 6,
                'next': None,
                'previous': 'http://testserver.fake/api/v2/enterprise/coupons/{}/codes/?page_size=4',
            },
            'expected_results_count': 2,
        },
        {
            'page': 2,
            'page_size': 3,
            'pagination': {
                'count': 6,
                'next': None,
                'previous': 'http://testserver.fake/api/v2/enterprise/coupons/{}/codes/?page_size=3',
            },
            'expected_results_count': 3,
        },
    )
    @ddt.unpack
    def test_coupon_codes_detail_with_pagination(self, page, page_size, pagination, expected_results_count):
        """
        Verify that `/api/v2/enterprise/coupons/{coupon_id}/codes/` endpoint pagination works
        """
        coupon_data = {
            'voucher_type': Voucher.MULTI_USE,
            'quantity': 2,
            'max_uses': 3,
        }
        coupon_id = self.create_enterprise_coupon(dict(self.data, **coupon_data), with_applications=True)

        # update the coupon id in `previous` and next urls
        pagination['previous'] = pagination['previous'] and pagination['previous'].format(coupon_id)
        pagination['next'] = pagination['next'] and pagination['next'].format(coupon_id)

        endpoint = '/api/v2/enterprise/coupons/{}/codes/?page={}&page_size={}'.format(coupon_id, page, page_size)

        # get coupon codes usage details
        response = self.get_response('GET', endpoint)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response = response.json()
        self.assert_coupon_codes_response(
            response,
            coupon_id,
            max_uses=coupon_data['max_uses'],
            results_count=expected_results_count,
            pagination=pagination,
        )

    def test_coupon_codes_detail_with_invalid_coupon_id(self):
        """
        Verify that `/api/v2/enterprise/coupons/{coupon_id}/codes/` endpoint returns 400 on invalid coupon id
        """
        response = self.get_response('GET', '/api/v2/enterprise/coupons/1212121212/codes/')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(
            response.json(),
            {'detail': 'Not found.'}
        )

    def test_coupon_codes_detail_with_max_coupon_usage(self):
        """
        Verify that `/api/v2/enterprise/coupons/{coupon_id}/codes/` endpoint returns correct default max coupon usage
        """
        coupon_data = {
            'voucher_type': Voucher.MULTI_USE,
            'quantity': 1,
            'max_uses': None
        }
        coupon_id = self.create_enterprise_coupon(
            dict(self.data, **coupon_data),
            with_applications=True
        )

        response = self.get_response('GET', '/api/v2/enterprise/coupons/{}/codes/'.format(coupon_id))
        response = response.json()
        self.assert_coupon_codes_response(
            response,
            coupon_id,
            max_uses=10000,
            results_count=1
        )

    @ddt.data(
        # VOUCHER_UNASSIGNED: SINGLE_USE
        {
            'code_filter': VOUCHER_UNASSIGNED,
            'voucher_type': Voucher.SINGLE_USE,
            'quantity': 3,
            'voucher_assignments': [0, 0, 0],    # No assignment
            'voucher_redemptions': [0, 0, 0],    # No redeemption.
            'expected_results_count': 3
        },
        {
            'code_filter': VOUCHER_UNASSIGNED,
            'voucher_type': Voucher.SINGLE_USE,
            'quantity': 3,
            'voucher_assignments': [1, 1, 1],
            'voucher_redemptions': [0, 0, 0],
            'expected_results_count': 0
        },
        {
            'code_filter': VOUCHER_UNASSIGNED,
            'voucher_type': Voucher.SINGLE_USE,
            'quantity': 3,
            'voucher_assignments': [0, 0, 0],
            'voucher_redemptions': [1, 1, 1],
            'expected_results_count': 0
        },
        {
            'code_filter': VOUCHER_UNASSIGNED,
            'voucher_type': Voucher.SINGLE_USE,
            'quantity': 3,
            'voucher_assignments': [1, 0, 0],
            'voucher_redemptions': [0, 1, 0],
            'expected_results_count': 1
        },
        {
            'code_filter': VOUCHER_UNASSIGNED,
            'voucher_type': Voucher.SINGLE_USE,
            'quantity': 3,
            'voucher_assignments': [1, 0, 0],
            'voucher_redemptions': [1, 0, 0],
            'expected_results_count': 2
        },
        {
            'code_filter': VOUCHER_UNASSIGNED,
            'voucher_type': Voucher.SINGLE_USE,
            'quantity': 3,
            'voucher_assignments': [1, 1, 0],
            'voucher_redemptions': [1, 0, 0],
            'expected_results_count': 1
        },
        {
            'code_filter': VOUCHER_UNASSIGNED,
            'voucher_type': Voucher.SINGLE_USE,
            'quantity': 3,
            'voucher_assignments': [1, 0, 0],
            'voucher_redemptions': [1, 1, 0],
            'expected_results_count': 1
        },
        {
            'code_filter': VOUCHER_UNASSIGNED,
            'voucher_type': Voucher.SINGLE_USE,
            'quantity': 3,
            'voucher_assignments': [1, 1, 0],
            'voucher_redemptions': [0, 0, 1],
            'expected_results_count': 0
        },
        # VOUCHER_UNASSIGNED: MULTI_USE
        {
            'code_filter': VOUCHER_UNASSIGNED,
            'voucher_type': Voucher.MULTI_USE,
            'quantity': 3,
            'max_uses': 10,
            'voucher_assignments': [0, 0, 0],
            'voucher_redemptions': [0, 0, 0],
            'expected_results_count': 3
        },
        {
            'code_filter': VOUCHER_UNASSIGNED,
            'voucher_type': Voucher.MULTI_USE,
            'quantity': 3,
            'max_uses': 10,
            'voucher_assignments': [10, 0, 0],
            'voucher_redemptions': [10, 0, 0],
            'expected_results_count': 2
        },
        {
            'code_filter': VOUCHER_UNASSIGNED,
            'voucher_type': Voucher.MULTI_USE,
            'quantity': 3,
            'max_uses': 10,
            'voucher_assignments': [10, 0, 0],
            'voucher_redemptions': [0, 10, 0],
            'expected_results_count': 1
        },
        {
            'code_filter': VOUCHER_UNASSIGNED,
            'voucher_type': Voucher.MULTI_USE,
            'quantity': 3,
            'max_uses': 10,
            'voucher_assignments': [10, 0, 0],
            'voucher_redemptions': [10, 10, 0],
            'expected_results_count': 1
        },
        # VOUCHER_UNASSIGNED: ONCE_PER_CUSTOMER
        {
            'code_filter': VOUCHER_UNASSIGNED,
            'voucher_type': Voucher.ONCE_PER_CUSTOMER,
            'quantity': 3,
            'max_uses': 10,
            'voucher_assignments': [10, 0, 0],
            'voucher_redemptions': [0, 10, 10],
            'expected_results_count': 0
        },
        # VOUCHER_UNASSIGNED: MULTI_USE_PER_CUSTOMER
        # FIXME: result count is 3.
        # {
        #     'code_filter': VOUCHER_UNASSIGNED,
        #     'voucher_type': Voucher.MULTI_USE_PER_CUSTOMER,
        #     'quantity': 3,
        #     'max_uses': 10,
        #     'voucher_assignments': [10, 0, 0],
        #     'voucher_redemptions': [0, 1, 4],
        #     'expected_results_count': 2
        # },
        # FIXME: result count is 3.
        # {
        #     'code_filter': VOUCHER_UNASSIGNED,
        #     'voucher_type': Voucher.MULTI_USE_PER_CUSTOMER,
        #     'quantity': 3,
        #     'max_uses': 10,
        #     'voucher_assignments': [10, 10, 10],
        #     'voucher_redemptions': [0, 0, 0],
        #     'expected_results_count': 0
        # },
        {
            'code_filter': VOUCHER_UNASSIGNED,
            'voucher_type': Voucher.MULTI_USE_PER_CUSTOMER,
            'quantity': 3,
            'max_uses': 10,
            'voucher_assignments': [0, 0, 0],
            'voucher_redemptions': [10, 0, 0],
            'expected_results_count': 2
        },
        {
            'code_filter': VOUCHER_UNASSIGNED,
            'voucher_type': Voucher.MULTI_USE_PER_CUSTOMER,
            'quantity': 3,
            'max_uses': 10,
            'voucher_assignments': [0, 0, 0],
            'voucher_redemptions': [5, 0, 0],
            'expected_results_count': 3
        },
        # VOUCHER_UNREDEEMED: SINGLE_USE
        {
            'code_filter': VOUCHER_UNREDEEMED,
            'voucher_type': Voucher.SINGLE_USE,
            'quantity': 3,
            'voucher_assignments': [0, 0, 0],
            'voucher_redemptions': [0, 0, 0],
            'expected_results_count': 0
        },
        {
            'code_filter': VOUCHER_UNREDEEMED,
            'voucher_type': Voucher.SINGLE_USE,
            'quantity': 3,
            'voucher_assignments': [0, 0, 0],
            'voucher_redemptions': [1, 1, 1],
            'expected_results_count': 0
        },
        {
            'code_filter': VOUCHER_UNREDEEMED,
            'voucher_type': Voucher.SINGLE_USE,
            'quantity': 3,
            'voucher_assignments': [1, 1, 1],
            'voucher_redemptions': [0, 0, 0],
            'expected_results_count': 3
        },
        {
            'code_filter': VOUCHER_UNREDEEMED,
            'voucher_type': Voucher.SINGLE_USE,
            'quantity': 3,
            'voucher_assignments': [1, 1, 1],
            'voucher_redemptions': [1, 1, 1],
            'expected_results_count': 0
        },
        {
            'code_filter': VOUCHER_UNREDEEMED,
            'voucher_type': Voucher.SINGLE_USE,
            'quantity': 3,
            'voucher_assignments': [1, 0, 0],
            'voucher_redemptions': [0, 1, 1],
            'expected_results_count': 1
        },
        {
            'code_filter': VOUCHER_UNREDEEMED,
            'voucher_type': Voucher.SINGLE_USE,
            'quantity': 3,
            'assign_slice': slice(0, 1),
            'voucher_assignments': [1, 0, 0],
            'voucher_redemptions': [1, 1, 1],
            'expected_results_count': 0
        },
        {
            'code_filter': VOUCHER_UNREDEEMED,
            'voucher_type': Voucher.SINGLE_USE,
            'quantity': 3,
            'voucher_assignments': [1, 0, 0],
            'voucher_redemptions': [1, 0, 0],
            'expected_results_count': 0
        },
        # VOUCHER_UNREDEEMED: MULTI_USE
        {
            'code_filter': VOUCHER_UNREDEEMED,
            'voucher_type': Voucher.MULTI_USE,
            'quantity': 3,
            'max_uses': 10,
            'voucher_assignments': [10, 10, 10],
            'voucher_redemptions': [10, 10, 10],
            'expected_results_count': 0
        },
        {
            'code_filter': VOUCHER_UNREDEEMED,
            'voucher_type': Voucher.MULTI_USE,
            'quantity': 3,
            'max_uses': 10,
            'voucher_assignments': [10, 10, 10],
            'voucher_redemptions': [0, 0, 0],
            'expected_results_count': 3
        },
        {
            'code_filter': VOUCHER_UNREDEEMED,
            'voucher_type': Voucher.MULTI_USE,
            'quantity': 3,
            'max_uses': 10,
            'voucher_assignments': [0, 0, 0],
            'voucher_redemptions': [10, 10, 10],
            'expected_results_count': 0
        },
        {
            'code_filter': VOUCHER_UNREDEEMED,
            'voucher_type': Voucher.MULTI_USE,
            'quantity': 3,
            'max_uses': 10,
            'voucher_assignments': [10, 10, 10],
            'voucher_redemptions': [0, 10, 0],
            'expected_results_count': 2
        },
        {
            'code_filter': VOUCHER_UNREDEEMED,
            'voucher_type': Voucher.MULTI_USE,
            'quantity': 3,
            'max_uses': 10,
            'voucher_assignments': [10, 0, 0],
            'voucher_redemptions': [0, 10, 10],
            'expected_results_count': 1
        },
        {
            'code_filter': VOUCHER_UNREDEEMED,
            'voucher_type': Voucher.MULTI_USE,
            'quantity': 3,
            'max_uses': 10,
            'voucher_assignments': [10, 10, 10],
            'voucher_redemptions': [2, 3, 5],
            'expected_results_count': 3
        },
        # VOUCHER_PARTIAL_REDEEMED: SINGLE_USE
        {
            'code_filter': VOUCHER_PARTIAL_REDEEMED,
            'voucher_type': Voucher.SINGLE_USE,
            'quantity': 3,
            'voucher_assignments': [0, 0, 0],
            'voucher_redemptions': [0, 0, 0],
            'expected_results_count': 0
        },
        {
            'code_filter': VOUCHER_PARTIAL_REDEEMED,
            'voucher_type': Voucher.SINGLE_USE,
            'quantity': 3,
            'voucher_assignments': [10, 0, 0],
            'voucher_redemptions': [0, 1, 1],
            'expected_results_count': 0
        },
        # VOUCHER_PARTIAL_REDEEMED: MULTI_USE
        {
            'code_filter': VOUCHER_PARTIAL_REDEEMED,
            'voucher_type': Voucher.MULTI_USE,
            'quantity': 3,
            'max_uses': 10,
            'voucher_assignments': [0, 0, 0],
            'voucher_redemptions': [1, 4, 1],
            'expected_results_count': 0
        },
        {
            'code_filter': VOUCHER_PARTIAL_REDEEMED,
            'voucher_type': Voucher.MULTI_USE,
            'quantity': 3,
            'max_uses': 10,
            'voucher_assignments': [10, 0, 0],
            'voucher_redemptions': [1, 4, 1],
            'expected_results_count': 1
        },
        {
            'code_filter': VOUCHER_PARTIAL_REDEEMED,
            'voucher_type': Voucher.MULTI_USE,
            'quantity': 3,
            'max_uses': 10,
            'voucher_assignments': [10, 10, 10],
            'voucher_redemptions': [1, 4, 1],
            'expected_results_count': 3
        },
        {
            'code_filter': VOUCHER_PARTIAL_REDEEMED,
            'voucher_type': Voucher.MULTI_USE,
            'quantity': 3,
            'max_uses': 10,
            'voucher_assignments': [10, 10, 10],
            'voucher_redemptions': [10, 4, 1],
            'expected_results_count': 2
        },
        # VOUCHER_PARTIAL_REDEEMED: ONCE_PER_CUSTOMER
        {
            'code_filter': VOUCHER_PARTIAL_REDEEMED,
            'voucher_type': Voucher.ONCE_PER_CUSTOMER,
            'quantity': 3,
            'max_uses': 10,
            'assign_slice': slice(0, 3),
            'voucher_assignments': [10, 10, 10],
            'voucher_redemptions': [0, 1, 4],
            'expected_results_count': 2
        },
        # VOUCHER_PARTIAL_REDEEMED: MULTI_USE_PER_CUSTOMER
        # # FIXME: result count is 0.
        # {
        #     'code_filter': VOUCHER_PARTIAL_REDEEMED,
        #     'voucher_type': Voucher.MULTI_USE_PER_CUSTOMER,
        #     'quantity': 3,
        #     'max_uses': 10,
        #     'voucher_assignments': [10, 10, 10],
        #     'voucher_redemptions': [0, 1, 4],
        #     'expected_results_count': 2
        # },
        # VOUCHER_REDEEMED: SINGLE_USE
        {
            'code_filter': VOUCHER_REDEEMED,
            'voucher_type': Voucher.SINGLE_USE,
            'quantity': 3,
            'voucher_assignments': [0, 0, 0],
            'voucher_redemptions': [0, 0, 0],
            'expected_results_count': 0
        },
        {
            'code_filter': VOUCHER_REDEEMED,
            'voucher_type': Voucher.SINGLE_USE,
            'quantity': 3,
            'voucher_assignments': [0, 0, 0],
            'voucher_redemptions': [1, 1, 1],
            'expected_results_count': 3
        },
        {
            'code_filter': VOUCHER_REDEEMED,
            'voucher_type': Voucher.SINGLE_USE,
            'quantity': 3,
            'voucher_assignments': [1, 0, 0],
            'voucher_redemptions': [0, 1, 1],
            'expected_results_count': 2
        },
        # VOUCHER_REDEEMED: MULTI_USE
        {
            'code_filter': VOUCHER_REDEEMED,
            'voucher_type': Voucher.MULTI_USE,
            'quantity': 3,
            'max_uses': 10,
            'voucher_assignments': [10, 0, 0],
            'voucher_redemptions': [10, 10, 10],
            'expected_results_count': 20
        },
        {
            'code_filter': VOUCHER_REDEEMED,
            'voucher_type': Voucher.MULTI_USE,
            'quantity': 3,
            'max_uses': 10,
            'voucher_assignments': [10, 2, 5],
            'voucher_redemptions': [10, 10, 10],
            'expected_results_count': 20
        },
        {
            'code_filter': VOUCHER_REDEEMED,
            'voucher_type': Voucher.MULTI_USE,
            'quantity': 3,
            'max_uses': 10,
            'voucher_assignments': [10, 0, 0],
            'voucher_redemptions': [0, 1, 4],
            'expected_results_count': 0
        },
        {
            'code_filter': VOUCHER_REDEEMED,
            'voucher_type': Voucher.MULTI_USE,
            'quantity': 3,
            'max_uses': 10,
            'voucher_assignments': [10, 0, 0],
            'voucher_redemptions': [0, 10, 4],
            'expected_results_count': 10
        },
        # VOUCHER_REDEEMED: ONCE_PER_CUSTOMER
        {
            'code_filter': VOUCHER_REDEEMED,
            'voucher_type': Voucher.ONCE_PER_CUSTOMER,
            'quantity': 3,
            'max_uses': 10,
            'voucher_assignments': [10, 0, 0],
            'voucher_redemptions': [0, 10, 10],
            'expected_results_count': 20
        },
        # VOUCHER_REDEEMED: MULTI_USE_PER_CUSTOMER
        {
            'code_filter': VOUCHER_REDEEMED,
            'voucher_type': Voucher.MULTI_USE_PER_CUSTOMER,
            'quantity': 3,
            'max_uses': 10,
            'voucher_assignments': [0, 0, 0],
            'voucher_redemptions': [10, 10, 10],
            'expected_results_count': 3
        },
        {
            'code_filter': VOUCHER_REDEEMED,
            'voucher_type': Voucher.MULTI_USE_PER_CUSTOMER,
            'quantity': 3,
            'max_uses': 10,
            'voucher_assignments': [10, 0, 0],
            'voucher_redemptions': [0, 10, 10],
            'expected_results_count': 2
        },
        {
            'code_filter': VOUCHER_REDEEMED,
            'voucher_type': Voucher.MULTI_USE_PER_CUSTOMER,
            'quantity': 3,
            'max_uses': 10,
            'voucher_assignments': [10, 0, 0],
            'voucher_redemptions': [0, 1, 4],
            'expected_results_count': 0
        },
        {
            'code_filter': VOUCHER_REDEEMED,
            'voucher_type': Voucher.MULTI_USE_PER_CUSTOMER,
            'quantity': 3,
            'max_uses': 10,
            'voucher_assignments': [10, 0, 0],
            'voucher_redemptions': [0, 10, 4],
            'expected_results_count': 1
        },
    )
    def test_coupon_codes_filters(self, data):
        """
        Verify that `/api/v2/enterprise/coupons/{coupon_id}/codes/` endpoint returns correct data when filtered.
        """
        max_uses = data.get('max_uses', None)
        coupon_data = {
            'quantity': data['quantity'],
            'voucher_type': data['voucher_type'],
            'max_uses': max_uses
        }
        coupon_id = self.create_enterprise_coupon(dict(self.data, **coupon_data))

        vouchers = Product.objects.get(id=coupon_id).attr.coupon_vouchers.vouchers.all()

        # Assign coupon codes.
        self.assign_coupon_codes(coupon_id, vouchers, data.get('voucher_assignments'))

        # create voucher applications.
        self.create_coupon_vouchers(
            coupon_vouchers=vouchers,
            max_uses=max_uses,
            redeem_completely=False,
            voucher_redemptions=data.get('voucher_redemptions'),
        )

        # Get coupon codes.
        response = self.get_response(
            'GET',
            '/api/v2/enterprise/coupons/{}/codes/?code_filter={}'.format(coupon_id, data['code_filter'])
        )
        response = response.json()

        try:
            self.assert_coupon_codes_response(
                response,
                coupon_id,
                max_uses=max_uses,
                results_count=data['expected_results_count']
            )
        except:
            from nose.tools import set_trace; set_trace();

        for response_voucher in response['results']:
            voucher = [voucher for voucher in vouchers if voucher.code == response_voucher['code']][0]
            available_slots = response_voucher['redemptions']['total'] - response_voucher['redemptions']['used']
            assert voucher.slots_available_for_assignment == available_slots

    def test_coupon_codes_detail_csv(self):
        """
        Verify that `/api/v2/enterprise/coupons/{coupon_id}/codes/` endpoint returns correct csv data.
        """
        data = {
            'voucher_type': Voucher.MULTI_USE,
            'quantity': 2,
            'max_uses': 3
        }

        coupon_id = self.create_enterprise_coupon(
            dict(self.data, voucher_type=data['voucher_type'], quantity=data['quantity'], max_uses=data['max_uses']),
            with_applications=True
        )

        response = self.get_response('GET', '/api/v2/enterprise/coupons/{}/codes.csv'.format(coupon_id))
        csv_content = response.content.split('\r\n')
        csv_header = csv_content[0]
        # Strip out first row (headers) and last row (extra csv line)
        csv_data = csv_content[1:-1]

        # Verify headers.
        self.assertEqual(csv_header, 'assigned_to,code,redeem_url,redemptions.total,redemptions.used')

        # Verify csv data.
        self.assert_coupon_codes_response(
            csv_data,
            coupon_id,
            data['max_uses'],
            6,
            is_csv=True
        )

    @ddt.data(
        (
            '85b08dde-0877-4474-a4e9-8408fe47ce88',
            ['coupon-1', 'coupon-2']
        ),
        (
            'f5c9149f-8dce-4410-bb0f-85c0f2dda864',
            ['coupon-3']
        ),
        (
            'f5c9149f-8dce-4410-bb0f-85c0f2dda860',
            []
        ),
    )
    @ddt.unpack
    def test_get_enterprise_coupon_overview_data(self, enterprise_id, expected_coupons):
        """
        Test if we get correct enterprise coupon overview data.
        """
        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': True})
        coupons_data = [{
            'title': 'coupon-1',
            'enterprise_customer': {'name': 'LOTRx', 'id': '85b08dde-0877-4474-a4e9-8408fe47ce88'}
        }, {
            'title': 'coupon-2',
            'enterprise_customer': {'name': 'LOTRx', 'id': '85b08dde-0877-4474-a4e9-8408fe47ce88'}
        }, {
            'title': 'coupon-3',
            'enterprise_customer': {'name': 'HPx', 'id': 'f5c9149f-8dce-4410-bb0f-85c0f2dda864'}
        }]

        # Create coupons.
        for coupon_data in coupons_data:
            self.get_response('POST', ENTERPRISE_COUPONS_LINK, dict(self.data, **coupon_data))

        # Build expected results.
        expected_results = []
        for coupon_title in expected_coupons:
            expected_results.append(self.get_coupon_data(coupon_title))

        overview_response = self.get_response_json(
            'GET',
            reverse(
                'api:v2:enterprise-coupons-(?P<enterprise-id>.+)/overview-list',
                kwargs={'enterprise_id': enterprise_id}
            )
        )

        # Verify that we get correct number of results related enterprise id.
        self.assertEqual(overview_response['count'], len(expected_results))

        # Verify that we get correct results.
        for actual_result in overview_response['results']:
            self.assertIn(actual_result, expected_results)

    @ddt.data(
        (Voucher.SINGLE_USE, 2, None, ['test1@example.com', 'test2@example.com'], [1]),
        (Voucher.MULTI_USE_PER_CUSTOMER, 2, 3, ['test1@example.com', 'test2@example.com'], [3]),
        (Voucher.MULTI_USE, 1, None, ['test1@example.com', 'test2@example.com'], [2]),
        (Voucher.MULTI_USE, 2, 3, ['t1@example.com', 't2@example.com', 't3@example.com', 't4@example.com'], [3, 1]),
        (Voucher.ONCE_PER_CUSTOMER, 2, 2, ['test1@example.com', 'test2@example.com'], [2]),
    )
    @ddt.unpack
    def test_coupon_codes_assign_success(self, voucher_type, quantity, max_uses, emails, assignments_per_code):
        """Test assigning codes to users."""
        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': True})

        coupon_post_data = dict(self.data, voucher_type=voucher_type, quantity=quantity, max_uses=max_uses)
        coupon = self.get_response('POST', ENTERPRISE_COUPONS_LINK, coupon_post_data)
        coupon = coupon.json()
        coupon_id = coupon['coupon_id']
        with mock.patch('ecommerce.extensions.offer.utils.send_offer_assignment_email.delay') as mock_send_email:
            response = self.get_response(
                'POST',
                '/api/v2/enterprise/coupons/{}/assign/'.format(coupon_id),
                {'template': 'Test template', 'emails': emails}
            )
        response = response.json()
        assert mock_send_email.call_count == len(emails)
        for i, email in enumerate(emails):
            if voucher_type != Voucher.MULTI_USE_PER_CUSTOMER:
                assert response['offer_assignments'][i]['user_email'] == email
            else:
                for j in range(max_uses):
                    assert response['offer_assignments'][(i * max_uses) + j]['user_email'] == email

        assigned_codes = []
        for assignment in response['offer_assignments']:
            if assignment['code'] not in assigned_codes:
                assigned_codes.append(assignment['code'])

        for code in assigned_codes:
            assert OfferAssignment.objects.filter(code=code).count() in assignments_per_code

    def test_coupon_codes_assign_success_with_codes_filter(self):
        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': True})
        coupon_post_data = dict(self.data, voucher_type=Voucher.SINGLE_USE, quantity=5)
        coupon = self.get_response('POST', ENTERPRISE_COUPONS_LINK, coupon_post_data)
        coupon = coupon.json()
        coupon_id = coupon['coupon_id']

        vouchers = Product.objects.get(id=coupon_id).attr.coupon_vouchers.vouchers.all()
        codes = [voucher.code for voucher in vouchers]
        codes_param = codes[3:]

        emails = ['t1@example.com', 't2@example.com']
        with mock.patch('ecommerce.extensions.offer.utils.send_offer_assignment_email.delay') as mock_send_email:
            response = self.get_response(
                'POST',
                '/api/v2/enterprise/coupons/{}/assign/'.format(coupon_id),
                {'template': 'Test template', 'emails': emails, 'codes': codes_param}
            )
        response = response.json()
        assert mock_send_email.call_count == len(emails)
        for i, email in enumerate(emails):
            assert response['offer_assignments'][i]['user_email'] == email
            assert response['offer_assignments'][i]['code'] in codes_param

        for code in codes:
            if code not in codes_param:
                assert OfferAssignment.objects.filter(code=code).count() == 0
            else:
                assert OfferAssignment.objects.filter(code=code).count() == 1

    def test_coupon_codes_assign_success_exclude_used_codes(self):
        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': True})
        coupon_post_data = dict(self.data, voucher_type=Voucher.SINGLE_USE, quantity=5)
        coupon = self.get_response('POST', ENTERPRISE_COUPONS_LINK, coupon_post_data)
        coupon = coupon.json()
        coupon_id = coupon['coupon_id']

        vouchers = Product.objects.get(id=coupon_id).attr.coupon_vouchers.vouchers.all()
        # Use some of the vouchers
        used_codes = []
        for voucher in vouchers[:3]:
            self.use_voucher(voucher, self.create_user())
            used_codes.append(voucher.code)
        unused_codes = [voucher.code for voucher in vouchers[3:]]
        emails = ['t1@example.com', 't2@example.com']
        with mock.patch('ecommerce.extensions.offer.utils.send_offer_assignment_email.delay') as mock_send_email:
            response = self.get_response(
                'POST',
                '/api/v2/enterprise/coupons/{}/assign/'.format(coupon_id),
                {'template': 'Test template', 'emails': emails}
            )
        response = response.json()
        assert mock_send_email.call_count == len(emails)
        for i, email in enumerate(emails):
            assert response['offer_assignments'][i]['user_email'] == email
            assert response['offer_assignments'][i]['code'] in unused_codes

        for code in used_codes:
            assert OfferAssignment.objects.filter(code=code).count() == 0
        for code in unused_codes:
            assert OfferAssignment.objects.filter(code=code).count() == 1

    def test_coupon_codes_assign_once_per_customer_with_used_codes(self):
        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': True})
        coupon_post_data = dict(self.data, voucher_type=Voucher.ONCE_PER_CUSTOMER, quantity=3)
        coupon = self.get_response('POST', ENTERPRISE_COUPONS_LINK, coupon_post_data)
        coupon = coupon.json()
        coupon_id = coupon['coupon_id']

        vouchers = Product.objects.get(id=coupon_id).attr.coupon_vouchers.vouchers.all()
        # Redeem and assign two of the vouchers
        already_redeemed_voucher = vouchers[0]
        already_assigned_voucher = vouchers[1]
        unused_voucher = vouchers[2]
        redeemed_user = self.create_user(email='t1@example.com')
        self.use_voucher(already_redeemed_voucher, redeemed_user)
        OfferAssignment.objects.create(
            code=already_assigned_voucher.code,
            offer=already_assigned_voucher.enterprise_offer,
            user_email='t2@example.com',
        )
        emails = ['t1@example.com', 't2@example.com', 't3@example.com']
        with mock.patch('ecommerce.extensions.offer.utils.send_offer_assignment_email.delay') as mock_send_email:
            response = self.get_response(
                'POST',
                '/api/v2/enterprise/coupons/{}/assign/'.format(coupon_id),
                {'template': 'Test template', 'emails': emails}
            )
        response = response.json()
        assert mock_send_email.call_count == len(emails)
        for i, email in enumerate(emails):
            assert response['offer_assignments'][i]['user_email'] == email
            assert response['offer_assignments'][i]['code'] == unused_voucher.code

        assert OfferAssignment.objects.filter(code=unused_voucher.code).count() == 3
        assert OfferAssignment.objects.filter(code=already_assigned_voucher.code).count() == 1
        assert OfferAssignment.objects.filter(code=already_redeemed_voucher.code).count() == 0

    @ddt.data(
        (Voucher.SINGLE_USE, 1, None, ['test1@example.com', 'test2@example.com']),
        (Voucher.MULTI_USE_PER_CUSTOMER, 1, 3, ['test1@example.com', 'test2@example.com']),
        (Voucher.MULTI_USE, 1, 3, ['t1@example.com', 't2@example.com', 't3@example.com', 't4@example.com']),
    )
    @ddt.unpack
    def test_coupon_codes_assign_failure(self, voucher_type, quantity, max_uses, emails):
        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': True})

        coupon_post_data = dict(self.data, voucher_type=voucher_type, quantity=quantity, max_uses=max_uses)
        coupon = self.get_response('POST', ENTERPRISE_COUPONS_LINK, coupon_post_data)
        coupon = coupon.json()
        coupon_id = coupon['coupon_id']
        with mock.patch('ecommerce.extensions.offer.utils.send_offer_assignment_email.delay') as mock_send_email:
            response = self.get_response(
                'POST',
                '/api/v2/enterprise/coupons/{}/assign/'.format(coupon_id),
                {'template': 'Test template', 'emails': emails}
            )
        response = response.json()
        assert response['non_field_errors'] == ['Not enough available codes for assignment!']
        assert mock_send_email.call_count == 0

    @ddt.data(
        (Voucher.SINGLE_USE, 2, None, ['test1@example.com', 'test2@example.com'], [1]),
        (Voucher.MULTI_USE_PER_CUSTOMER, 2, 3, ['test1@example.com', 'test2@example.com'], [3]),
        (Voucher.MULTI_USE, 1, None, ['test1@example.com', 'test2@example.com'], [2]),
        (Voucher.MULTI_USE, 2, 3, ['t1@example.com', 't2@example.com', 't3@example.com', 't4@example.com'], [3, 1]),
        (Voucher.ONCE_PER_CUSTOMER, 2, 2, ['test1@example.com', 'test2@example.com'], [2]),
    )
    @ddt.unpack
    def test_codes_assignment_email_failure(self, voucher_type, quantity, max_uses, emails, assignments_per_code):
        """Test assigning codes to users."""
        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': True})

        coupon_post_data = dict(self.data, voucher_type=voucher_type, quantity=quantity, max_uses=max_uses)
        coupon = self.get_response('POST', ENTERPRISE_COUPONS_LINK, coupon_post_data)
        coupon = coupon.json()
        coupon_id = coupon['coupon_id']
        with mock.patch(
            'ecommerce.extensions.offer.utils.send_offer_assignment_email.delay', side_effect=Exception()
        ) as mock_send_email:
            response = self.get_response(
                'POST',
                '/api/v2/enterprise/coupons/{}/assign/'.format(coupon_id),
                {'template': 'Test template', 'emails': emails}
            )
        response = response.json()
        assert mock_send_email.call_count == len(emails)
        for i, email in enumerate(emails):
            if voucher_type != Voucher.MULTI_USE_PER_CUSTOMER:
                assert response['offer_assignments'][i]['user_email'] == email
            else:
                for j in range(max_uses):
                    assert response['offer_assignments'][(i * max_uses) + j]['user_email'] == email

        assigned_codes = []
        for assignment in response['offer_assignments']:
            if assignment['code'] not in assigned_codes:
                assigned_codes.append(assignment['code'])

        for code in assigned_codes:
            assert OfferAssignment.objects.filter(code=code).count() in assignments_per_code

    @ddt.data(
        (Voucher.SINGLE_USE, 2, None, True),
        (Voucher.MULTI_USE_PER_CUSTOMER, 2, 3, False),
        (Voucher.MULTI_USE, 1, None, True),
        (Voucher.ONCE_PER_CUSTOMER, 2, 2, False),
    )
    @ddt.unpack
    def test_coupon_codes_revoke_success(self, voucher_type, quantity, max_uses, send_email):
        """Test revoking codes from users."""
        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': True})

        email = 'test1@example.com'
        coupon_post_data = dict(self.data, voucher_type=voucher_type, quantity=quantity, max_uses=max_uses)
        coupon = self.get_response('POST', ENTERPRISE_COUPONS_LINK, coupon_post_data)
        coupon = coupon.json()
        coupon_id = coupon['coupon_id']
        with mock.patch('ecommerce.extensions.offer.utils.send_offer_assignment_email.delay'):
            self.get_response(
                'POST',
                '/api/v2/enterprise/coupons/{}/assign/'.format(coupon_id),
                {'template': 'Test template', 'emails': [email]}
            )

        offer_assignment = OfferAssignment.objects.filter(user_email=email).first()
        payload = {'assignments': [{'email': email, 'code': offer_assignment.code}]}
        if send_email:
            payload['template'] = 'Test template'
        with mock.patch('ecommerce.extensions.offer.utils.send_offer_update_email.delay') as mock_send_email:
            response = self.get_response(
                'POST',
                '/api/v2/enterprise/coupons/{}/revoke/'.format(coupon_id),
                payload
            )

        response = response.json()
        assert response == [{'code': offer_assignment.code, 'email': email, 'detail': 'success'}]
        assert mock_send_email.call_count == (1 if send_email else 0)
        for offer_assignment in OfferAssignment.objects.filter(user_email=email):
            assert offer_assignment.status == OFFER_ASSIGNMENT_REVOKED

    def test_coupon_codes_revoke_invalid_request(self):
        """Test that revoke fails when the request format is incorrect."""
        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': True})
        email = 'test1@example.com'
        coupon_post_data = dict(self.data, voucher_type=Voucher.SINGLE_USE, quantity=1)
        coupon = self.get_response('POST', ENTERPRISE_COUPONS_LINK, coupon_post_data)
        coupon = coupon.json()
        coupon_id = coupon['coupon_id']

        response = self.get_response(
            'POST',
            '/api/v2/enterprise/coupons/{}/revoke/'.format(coupon_id),
            {'template': 'Test template', 'assignments': {'email': email, 'code': 'RANDOMCODE'}}
        )

        response = response.json()
        assert response == {'non_field_errors': ['Expected a list of items but got type "dict".']}

    def test_coupon_codes_revoke_code_not_in_coupon(self):
        """Test that revoke fails when the specified code is not associated with the Coupon."""
        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': True})
        email = 'test1@example.com'
        coupon_post_data = dict(self.data, voucher_type=Voucher.SINGLE_USE, quantity=1)
        coupon = self.get_response('POST', ENTERPRISE_COUPONS_LINK, coupon_post_data)
        coupon = coupon.json()
        coupon_id = coupon['coupon_id']

        response = self.get_response(
            'POST',
            '/api/v2/enterprise/coupons/{}/revoke/'.format(coupon_id),
            {'template': 'Test template', 'assignments': [{'email': email, 'code': 'RANDOMCODE'}]}
        )

        response = response.json()
        assert response == [
            {'non_field_errors': ['Code RANDOMCODE is not associated with this Coupon']}
        ]

    def test_coupon_codes_revoke_no_assignment_exists(self):
        """Test that revoke fails when the user has no existing assignments for the code."""
        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': True})
        email = 'test1@example.com'
        coupon_post_data = dict(self.data, voucher_type=Voucher.SINGLE_USE, quantity=1)
        coupon = self.get_response('POST', ENTERPRISE_COUPONS_LINK, coupon_post_data)
        coupon = coupon.json()
        coupon_id = coupon['coupon_id']

        voucher = Product.objects.get(id=coupon_id).attr.coupon_vouchers.vouchers.first()
        response = self.get_response(
            'POST',
            '/api/v2/enterprise/coupons/{}/revoke/'.format(coupon_id),
            {'template': 'Test template', 'assignments': [{'email': email, 'code': voucher.code}]}
        )

        response = response.json()
        assert response == [
            {'non_field_errors': ['No assignments exist for user {} and code {}'.format(email, voucher.code)]}
        ]

    def test_coupon_codes_revoke_email_failure(self):
        """Test revoking a code for a user with an email failure."""
        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': True})

        email = 'test1@example.com'
        coupon_post_data = dict(self.data, voucher_type=Voucher.SINGLE_USE, quantity=1)
        coupon = self.get_response('POST', ENTERPRISE_COUPONS_LINK, coupon_post_data)
        coupon = coupon.json()
        coupon_id = coupon['coupon_id']
        with mock.patch('ecommerce.extensions.offer.utils.send_offer_assignment_email.delay'):
            self.get_response(
                'POST',
                '/api/v2/enterprise/coupons/{}/assign/'.format(coupon_id),
                {'template': 'Test template', 'emails': [email]}
            )

        offer_assignment = OfferAssignment.objects.filter(user_email=email).first()
        with mock.patch(
            'ecommerce.extensions.offer.utils.send_offer_update_email.delay',
            side_effect=Exception('email_dispatch_failed')
        ) as mock_send_email:
            response = self.get_response(
                'POST',
                '/api/v2/enterprise/coupons/{}/revoke/'.format(coupon_id),
                {'template': 'Test template', 'assignments': [{'email': email, 'code': offer_assignment.code}]}
            )

        response = response.json()
        assert response == [{'email': email, 'code': offer_assignment.code, 'detail': 'email_dispatch_failed'}]
        assert mock_send_email.call_count == 1
        for offer_assignment in OfferAssignment.objects.filter(user_email=email):
            assert offer_assignment.status == OFFER_ASSIGNMENT_REVOKED

    def test_coupon_codes_revoke_bulk(self):
        """Test sending multiple revoke requests (bulk use case)."""
        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': True})

        emails = ['test1@example.com', 'test2@example.com']
        coupon_post_data = dict(self.data, voucher_type=Voucher.SINGLE_USE, quantity=2)
        coupon = self.get_response('POST', ENTERPRISE_COUPONS_LINK, coupon_post_data)
        coupon = coupon.json()
        coupon_id = coupon['coupon_id']
        with mock.patch('ecommerce.extensions.offer.utils.send_offer_update_email.delay'):
            self.get_response(
                'POST',
                '/api/v2/enterprise/coupons/{}/assign/'.format(coupon_id),
                {'template': 'Test template', 'emails': emails}
            )

        offer_assignment = OfferAssignment.objects.filter(user_email__in=emails).first()
        with mock.patch('ecommerce.extensions.offer.utils.send_offer_update_email.delay') as mock_send_email:
            response = self.get_response(
                'POST',
                '/api/v2/enterprise/coupons/{}/revoke/'.format(coupon_id),
                {'template': 'Test template', 'assignments': [
                    {'email': offer_assignment.user_email, 'code': offer_assignment.code},
                    {'email': 'test3@example.com', 'code': 'RANDOMCODE'},
                ]}
            )

        response = response.json()
        assert response == [
            {'email': offer_assignment.user_email, 'code': offer_assignment.code, 'detail': 'success'},
            {'non_field_errors': ['Code RANDOMCODE is not associated with this Coupon']},
        ]
        assert mock_send_email.call_count == 1
        for offer_assignment in OfferAssignment.objects.filter(user_email=offer_assignment.user_email):
            assert offer_assignment.status == OFFER_ASSIGNMENT_REVOKED

    @ddt.data(
        (Voucher.SINGLE_USE, 2, None),
        (Voucher.MULTI_USE_PER_CUSTOMER, 2, 3),
        (Voucher.MULTI_USE, 1, None),
        (Voucher.ONCE_PER_CUSTOMER, 2, 2),
    )
    @ddt.unpack
    def test_coupon_codes_remind_success(self, voucher_type, quantity, max_uses):
        """Test sending reminder emails for codes."""
        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': True})

        email = 'test1@example.com'
        coupon_post_data = dict(self.data, voucher_type=voucher_type, quantity=quantity, max_uses=max_uses)
        coupon = self.get_response('POST', ENTERPRISE_COUPONS_LINK, coupon_post_data)
        coupon = coupon.json()
        coupon_id = coupon['coupon_id']
        with mock.patch('ecommerce.extensions.offer.utils.send_offer_assignment_email.delay'):
            self.get_response(
                'POST',
                '/api/v2/enterprise/coupons/{}/assign/'.format(coupon_id),
                {'template': 'Test template', 'emails': [email]}
            )
        offer_assignment = OfferAssignment.objects.filter(user_email=email).first()
        payload = {'assignments': [{'email': email, 'code': offer_assignment.code}]}
        payload['template'] = 'Test template'
        with mock.patch('ecommerce.extensions.offer.utils.send_offer_update_email.delay') as mock_send_email:
            response = self.get_response(
                'POST',
                '/api/v2/enterprise/coupons/{}/remind/'.format(coupon_id),
                payload
            )
        response = response.json()
        assert response == [{'code': offer_assignment.code, 'email': email, 'detail': 'success'}]
        assert mock_send_email.call_count == 1

    def test_coupon_codes_remind_code_not_in_coupon(self):
        """Test that remind fails when the specified code is not associated with the Coupon."""
        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': True})
        email = 'test1@example.com'
        coupon_post_data = dict(self.data, voucher_type=Voucher.SINGLE_USE, quantity=1)
        coupon = self.get_response('POST', ENTERPRISE_COUPONS_LINK, coupon_post_data)
        coupon = coupon.json()
        coupon_id = coupon['coupon_id']
        response = self.get_response(
            'POST',
            '/api/v2/enterprise/coupons/{}/remind/'.format(coupon_id),
            {'template': 'Test template', 'assignments': [{'email': email, 'code': 'RANDOMCODE'}]}
        )
        response = response.json()
        assert response == [
            {'non_field_errors': ['Code RANDOMCODE is not associated with this Coupon']}
        ]

    def test_coupon_codes_remind_no_assignment_exists(self):
        """Test that remind fails when the user has no existing assignments for the code."""
        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': True})
        email = 'test1@example.com'
        coupon_post_data = dict(self.data, voucher_type=Voucher.SINGLE_USE, quantity=1)
        coupon = self.get_response('POST', ENTERPRISE_COUPONS_LINK, coupon_post_data)
        coupon = coupon.json()
        coupon_id = coupon['coupon_id']
        voucher = Product.objects.get(id=coupon_id).attr.coupon_vouchers.vouchers.first()
        response = self.get_response(
            'POST',
            '/api/v2/enterprise/coupons/{}/remind/'.format(coupon_id),
            {'template': 'Test template', 'assignments': [{'email': email, 'code': voucher.code}]}
        )
        response = response.json()
        assert response == [
            {'non_field_errors': ['No assignments exist for user {} and code {}'.format(email, voucher.code)]}
        ]

    def test_coupon_codes_remind_email_failure(self):
        """Test sending a reminder for a code with an email failure."""
        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': True})

        email = 'test1@example.com'
        coupon_post_data = dict(self.data, voucher_type=Voucher.SINGLE_USE, quantity=1)
        coupon = self.get_response('POST', ENTERPRISE_COUPONS_LINK, coupon_post_data)
        coupon = coupon.json()
        coupon_id = coupon['coupon_id']
        with mock.patch('ecommerce.extensions.offer.utils.send_offer_assignment_email.delay'):
            self.get_response(
                'POST',
                '/api/v2/enterprise/coupons/{}/assign/'.format(coupon_id),
                {'template': 'Test template', 'emails': [email]}
            )
        offer_assignment = OfferAssignment.objects.filter(user_email=email).first()
        with mock.patch(
            'ecommerce.extensions.offer.utils.send_offer_update_email.delay',
            side_effect=Exception('email_dispatch_failed')
        ) as mock_send_email:
            response = self.get_response(
                'POST',
                '/api/v2/enterprise/coupons/{}/remind/'.format(coupon_id),
                {'template': 'Test template', 'assignments': [{'email': email, 'code': offer_assignment.code}]}
            )
        response = response.json()
        assert response == [{'email': email, 'code': offer_assignment.code, 'detail': 'email_dispatch_failed'}]
        assert mock_send_email.call_count == 1

    def test_coupon_codes_remind_bulk(self):
        """Test sending multiple remind requests (bulk use case)."""
        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': True})

        emails = ['test1@example.com', 'test2@example.com']
        coupon_post_data = dict(self.data, voucher_type=Voucher.SINGLE_USE, quantity=2)
        coupon = self.get_response('POST', ENTERPRISE_COUPONS_LINK, coupon_post_data)
        coupon = coupon.json()
        coupon_id = coupon['coupon_id']
        with mock.patch('ecommerce.extensions.offer.utils.send_offer_update_email.delay'):
            self.get_response(
                'POST',
                '/api/v2/enterprise/coupons/{}/assign/'.format(coupon_id),
                {'template': 'Test template', 'emails': emails}
            )
        offer_assignment = OfferAssignment.objects.filter(user_email__in=emails).first()
        with mock.patch('ecommerce.extensions.offer.utils.send_offer_update_email.delay') as mock_send_email:
            response = self.get_response(
                'POST',
                '/api/v2/enterprise/coupons/{}/remind/'.format(coupon_id),
                {'template': 'Test template', 'assignments': [
                    {'email': offer_assignment.user_email, 'code': offer_assignment.code},
                    {'email': 'test3@example.com', 'code': 'RANDOMCODE'},
                ]}
            )
        response = response.json()
        assert response == [
            {'email': offer_assignment.user_email, 'code': offer_assignment.code, 'detail': 'success'},
            {'non_field_errors': ['Code RANDOMCODE is not associated with this Coupon']},
        ]
        assert mock_send_email.call_count == 1
