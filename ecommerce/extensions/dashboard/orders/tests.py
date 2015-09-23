import os

from django.contrib.messages import constants as MSG
from django.core.urlresolvers import reverse
from django.test import TestCase, LiveServerTestCase, override_settings
from nose.plugins.skip import SkipTest
from oscar.core.loading import get_model
from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.firefox.webdriver import WebDriver
from selenium.webdriver.support.wait import WebDriverWait

from ecommerce.extensions.dashboard.tests import DashboardViewTestMixin
from ecommerce.extensions.fulfillment.signals import SHIPPING_EVENT_NAME
from ecommerce.extensions.fulfillment.status import ORDER, LINE
from ecommerce.extensions.refund.tests.mixins import RefundTestMixin
from ecommerce.extensions.test import factories
from ecommerce.tests.mixins import UserMixin

Order = get_model('order', 'Order')
Refund = get_model('refund', 'Refund')
ShippingEventType = get_model('order', 'ShippingEventType')


class OrderViewTestsMixin(UserMixin):
    def setUp(self):
        super(OrderViewTestsMixin, self).setUp()

        # Staff permissions are required for this view
        self.user = self.create_user(is_staff=True)

    def assert_successful_response(self, response, orders=None):
        self.assertEqual(response.status_code, 200)

        if orders:
            self.assertListEqual(list(response.context['orders']), orders)


class OrderListViewTests(OrderViewTestsMixin, RefundTestMixin, LiveServerTestCase):
    path = reverse('dashboard:order-list')

    @classmethod
    def setUpClass(cls):
        if os.environ.get('DISABLE_ACCEPTANCE_TESTS') == 'True':
            raise SkipTest

        cls.selenium = WebDriver()
        super(OrderListViewTests, cls).setUpClass()

    @classmethod
    def tearDownClass(cls):
        cls.selenium.quit()
        super(OrderListViewTests, cls).tearDownClass()

    def setUp(self):
        super(OrderListViewTests, self).setUp()

        self.btn_selector = '[data-action=retry-fulfillment]'
        self.password = 'test'
        self.user = factories.UserFactory(password=self.password, is_superuser=True, is_staff=True)

        self.order = factories.create_order(user=self.user)
        self.order.status = ORDER.FULFILLMENT_ERROR
        self.order.save()
        self.order.lines.all().update(status=LINE.FULFILLMENT_CONFIGURATION_ERROR)

        ShippingEventType.objects.get_or_create(name=SHIPPING_EVENT_NAME)

    def _login(self):
        """ Log into the service and navigate to the order list view. """
        self.selenium.get(self.live_server_url + reverse('auto_auth'))
        self.selenium.get(self.live_server_url + self.path)

    def _retry_fulfillment(self):
        """ Click the retry fulfillment button and wait for the AJAX call to finish. """
        button = self.selenium.find_element_by_css_selector(self.btn_selector)
        self.assertEqual(unicode(self.order.number), button.get_attribute('data-order-number'))
        button.click()

        # Wait for the AJAX call to finish and display an alert
        WebDriverWait(self.selenium, 1.0).until(lambda d: d.find_element_by_css_selector('#messages .alert'))

    def assertAlertDisplayed(self, alert_class, text):
        """ Verifies that the most recent alert has the given class and message. """
        alert = self.selenium.find_elements_by_css_selector('#messages .alert')[-1]
        classes = alert.get_attribute('class').split(' ')
        self.assertIn(alert_class, classes, )
        self.assertEqual(alert.find_element_by_css_selector('.message').text, text)

    @override_settings(FULFILLMENT_MODULES=['ecommerce.extensions.fulfillment.tests.modules.FakeFulfillmentModule', ])
    def test_retry_fulfillment(self):
        """
        The order list should display a "Retry Fulfillment" button beside orders in the Fulfillment Error state.
        Clicking the button should call the fulfillment API endpoint via AJAX. When successful, the order status
        should be updated, an alert displayed, and the button removed.
        """
        order_number = self.order.number

        self._login()
        self._retry_fulfillment()

        # Ensure the button is removed.
        self.assertRaises(NoSuchElementException, self.selenium.find_element_by_css_selector, self.btn_selector)

        # Ensure the status is updated.
        selector = 'tr[data-order-number="{}"] .order-status'.format(order_number)
        status = self.selenium.find_element_by_css_selector(selector)
        self.assertEqual(ORDER.COMPLETE, status.text)

        # Ensure an alert is displayed
        self.assertAlertDisplayed('alert-success', 'Order {} has been fulfilled.'.format(order_number))

    def test_fulfillment_failed(self):
        """ If fulfillment fails, an alert should be displayed, and the Retry Fulfillment button reactivated. """
        self._login()
        self._retry_fulfillment()

        # Ensure the status is unchanged.
        order_number = self.order.number
        selector = 'tr[data-order-number="{}"] .order-status'.format(order_number)
        status = self.selenium.find_element_by_css_selector(selector)
        self.assertEqual(ORDER.FULFILLMENT_ERROR, status.text)

        # Ensure the button is active
        button = self.selenium.find_element_by_css_selector(self.btn_selector)
        classes = button.get_attribute('class').split(' ')
        self.assertNotIn('disabled', classes, 'Button is disabled, but should have been re-enabled!')

        # Ensure an alert is displayed)
        self.assertAlertDisplayed('alert-error',
                                  'Failed to fulfill order {}: INTERNAL SERVER ERROR'.format(order_number))

    def test_filtering(self):
        """Verify that the view allows filtering by username."""
        self.create_order(user=self.user)

        new_user = self.create_user(username='hackerman')
        new_order = self.create_order(user=new_user)

        self.client.login(username=self.user.username, password=self.password)

        # Username filtering
        response = self.client.get('{path}?username={username}'.format(
            path=self.path,
            username=new_user.username
        ))
        self.assert_successful_response(response, [new_order])

        # Validate case-insensitive, starts-with username filtering
        response = self.client.get('{path}?username={username}'.format(
            path=self.path,
            # Cut the configured username in half, then invert the fragment's casing.
            username=new_user.username[:len(new_user.username) / 2].swapcase()
        ))
        self.assert_successful_response(response, [new_order])


class OrderDetailViewTests(DashboardViewTestMixin, OrderViewTestsMixin, RefundTestMixin, TestCase):
    def _request_refund(self, order):
        """POST to the view."""

        # Login
        self.client.login(username=self.user.username, password=self.password)

        # POST to the view, creating the Refund
        data = {
            'line_action': 'create_refund',
            'selected_line': order.lines.values_list('id', flat=True)
        }

        for line in order.lines.all():
            data['selected_line_qty_{}'.format(line.id)] = line.quantity

        response = self.client.post(reverse('dashboard:order-detail', kwargs={'number': order.number}), data=data,
                                    follow=True)
        self.assertEqual(response.status_code, 200)

        return response

    def test_create_refund(self):
        """Verify the view creates a Refund for the Order and selected Lines."""
        # Create Order and Lines
        order = self.create_order(user=self.user)
        self.assertFalse(order.refunds.exists())

        # Validate the Refund
        response = self._request_refund(order)
        refund = Refund.objects.latest()
        self.assert_refund_matches_order(refund, order)

        # Verify a message was passed for display
        data = {
            'link_start': '<a href="{}" target="_blank">'.format(
                reverse('dashboard:refunds:detail', kwargs={'pk': refund.pk})),
            'link_end': '</a>',
            'refund_id': refund.pk
        }
        expected = '{link_start}Refund #{refund_id}{link_end} created! ' \
                   'Click {link_start}here{link_end} to view it.'.format(**data)

        self.assert_message_equals(response, expected, MSG.SUCCESS)

    def test_create_refund_error(self):
        """Verify the view does not create a Refund if the selected Lines have already been refunded."""
        refund = self.create_refund()
        order = refund.order

        for line in order.lines.all():
            self.assertTrue(line.refund_lines.exists())

        # No new refunds should be created
        self.assertEqual(Refund.objects.count(), 1)
        response = self._request_refund(order)
        self.assertEqual(Refund.objects.count(), 1)

        # An error message should be displayed.
        self.assert_message_equals(response,
                                   'A refund cannot be created for these lines. They may have already been refunded.',
                                   MSG.ERROR)
