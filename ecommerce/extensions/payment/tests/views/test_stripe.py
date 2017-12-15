import json
from urllib import urlencode

import stripe
from django.conf import settings
from django.urls import reverse
from mock import mock
from oscar.core.loading import get_class, get_model
from oscar.test.factories import BillingAddressFactory

from ecommerce.extensions.checkout.utils import get_receipt_page_url
from ecommerce.extensions.order.constants import PaymentEventTypeName
from ecommerce.extensions.payment.processors.stripe import Stripe
from ecommerce.extensions.payment.tests.mixins import PaymentEventsMixin
from ecommerce.extensions.test.factories import create_basket
from ecommerce.tests.testcases import TestCase

Country = get_model('address', 'Country')
Order = get_model('order', 'Order')
PaymentEvent = get_model('order', 'PaymentEvent')
Selector = get_class('partner.strategy', 'Selector')
Source = get_model('payment', 'Source')


class BaseStripeViewTest(PaymentEventsMixin, TestCase):
    def setUp(self):
        super(BaseStripeViewTest, self).setUp()
        self.user = self.create_user()
        self.client.login(username=self.user.username, password=self.password)

    def assert_successful_order_response(self, response, order_number):
        assert response.status_code == 201
        receipt_url = get_receipt_page_url(self.site_configuration, order_number)
        assert json.loads(response.content) == {'url': receipt_url}

    def assert_order_created(self, basket, billing_address, card_type, label):
        order = Order.objects.get(number=basket.order_number, total_incl_tax=basket.total_incl_tax)
        total = order.total_incl_tax
        order.payment_events.get(event_type__code='paid', amount=total)
        Source.objects.get(
            source_type__name=Stripe.NAME,
            currency=order.currency,
            amount_allocated=total,
            amount_debited=total,
            card_type=card_type,
            label=label
        )
        PaymentEvent.objects.get(
            event_type__name=PaymentEventTypeName.PAID,
            amount=total,
            processor_name=Stripe.NAME
        )
        assert order.billing_address == billing_address

    def create_basket(self):
        basket = create_basket(owner=self.user, site=self.site)
        basket.strategy = Selector().strategy()
        basket.thaw()
        return basket


class StripeSubmitViewTests(BaseStripeViewTest):
    path = reverse('stripe:submit')

    def setUp(self):
        super(StripeSubmitViewTests, self).setUp()
        self.user = self.create_user()
        self.client.login(username=self.user.username, password=self.password)

    def generate_form_data(self, basket_id):
        return {
            'stripe_token': 'st_abc123',
            'basket': basket_id,
        }

    def test_login_required(self):
        self.client.logout()
        response = self.client.post(self.path)
        expected_url = '{base}?next={path}'.format(base=self.get_full_url(path=reverse(settings.LOGIN_URL)),
                                                   path=self.path)
        self.assertRedirects(response, expected_url, fetch_redirect_response=False)

    def test_payment_error(self):
        basket = self.create_basket()
        data = self.generate_form_data(basket.id)

        with mock.patch.object(Stripe, 'get_address_from_token', mock.Mock(return_value=BillingAddressFactory())):
            with mock.patch.object(Stripe, 'handle_processor_response', mock.Mock(side_effect=Exception)):
                response = self.client.post(self.path, data)

        assert response.status_code == 400
        assert response.content == '{}'

    def test_billing_address_error(self):
        basket = self.create_basket()
        data = self.generate_form_data(basket.id)
        card_type = 'American Express'
        label = '1986'
        charge = stripe.Charge.construct_from({
            'id': '2404',
            'source': {
                'brand': card_type,
                'last4': label,
                'object': 'card',
            },
        }, 'fake-key')

        with mock.patch.object(Stripe, 'get_address_from_token') as address_mock:
            address_mock.side_effect = Exception

            with mock.patch.object(stripe.Charge, 'create') as charge_mock:
                charge_mock.return_value = charge
                response = self.client.post(self.path, data)

            address_mock.assert_called_once_with(data['stripe_token'])

        self.assert_successful_order_response(response, basket.order_number)
        self.assert_order_created(basket, None, 'american_express', label)

    def test_successful_payment(self):
        basket = self.create_basket()
        data = self.generate_form_data(basket.id)
        card_type = 'American Express'
        label = '1986'
        charge = stripe.Charge.construct_from({
            'id': '2404',
            'source': {
                'brand': card_type,
                'last4': label,
                'object': 'card',
            },
        }, 'fake-key')

        billing_address = BillingAddressFactory()
        with mock.patch.object(Stripe, 'get_address_from_token') as address_mock:
            address_mock.return_value = billing_address

            with mock.patch.object(stripe.Charge, 'create') as charge_mock:
                charge_mock.return_value = charge
                response = self.client.post(self.path, data)

            address_mock.assert_called_once_with(data['stripe_token'])

        self.assert_successful_order_response(response, basket.order_number)
        self.assert_order_created(basket, billing_address, 'american_express', label)


class StripeSubmitSourceViewTests(BaseStripeViewTest):
    path = reverse('stripe:submit_source')

    def generate_form_data(self, basket_id):
        return {
            'source': 'src_abc123',
            'basket': basket_id,
            'client_secret': 'Setec Astronomy',
        }

    def test_successful_payment(self):
        basket = self.create_basket()
        data = self.generate_form_data(basket.id)
        charge = stripe.Charge.construct_from({
            'id': '2404',
            'source': {
                'type': 'alipay',
                'object': 'source',
            },
        }, 'fake-key')

        with mock.patch.object(stripe.Charge, 'create') as charge_mock:
            charge_mock.return_value = charge
            path = '{path}?{qs}'.format(path=self.path, qs=urlencode(data))
            response = self.client.get(path)

        expected_url = get_receipt_page_url(self.site_configuration, basket.order_number)
        self.assertRedirects(response, expected_url)
        self.assert_order_created(basket, None, 'Alipay', '')

    def test_declined_payment(self):
        basket = self.create_basket()
        data = self.generate_form_data(basket.id)

        with mock.patch.object(stripe.Charge, 'create') as charge_mock:
            charge_mock.side_effect = stripe.error.InvalidRequestError('fake-msg', 'fake-param')

            with mock.patch('stripe.Source.retrieve') as source_mock:
                source_mock.return_value = stripe.Source.construct_from({
                    'status': 'failed'
                }, 'fake-key')

                path = '{path}?{qs}'.format(path=self.path, qs=urlencode(data))
                response = self.client.get(path)

        expected_url = reverse('basket:summary')
        self.assertRedirects(response, expected_url)
