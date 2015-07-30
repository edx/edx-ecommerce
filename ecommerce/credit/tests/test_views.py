"""
Tests for the checkout page.
"""
from django.conf import settings
from django.core.urlresolvers import reverse
from django.test import TestCase
from oscar.core.loading import get_model
from waffle import Switch

from ecommerce.courses.models import Course
from ecommerce.extensions.catalogue.tests.mixins import CourseCatalogTestMixin
from ecommerce.extensions.payment.helpers import get_processor_class
from ecommerce.tests.mixins import UserMixin

Partner = get_model('partner', 'Partner')


class CheckoutPageTest(UserMixin, CourseCatalogTestMixin, TestCase):
    """Test for Checkout page"""

    def setUp(self):
        super(CheckoutPageTest, self).setUp()
        self.switch, __ = Switch.objects.get_or_create(name='ENABLE_CREDIT_APP', active=True)
        user = self.create_user(is_superuser=False)
        self.client.login(username=user.username, password=self.password)
        self.course_name = 'credit course'
        self.provider = 'ASU'
        self.price = 100
        self.thumbnail_url = 'http://www.edx.org/course.jpg'
        self.credit_hours = 2
        # Create the course
        self.course = Course.objects.create(
            id=u'edx/Demo_Course/DemoX',
            name=self.course_name,
            thumbnail_url=self.thumbnail_url
        )

        # Create the credit seat
        self.seat = self.course.create_or_update_seat(
            'credit', True, self.price, self.provider, credit_hours=self.credit_hours
        )

    @property
    def path(self):
        return reverse('credit:checkout', args=[self.course.id])

    def test_get_with_enabled_flag(self):
        """
        Test checkout page accessibility. Page will appear only if feature
        flag is enabled.
        """
        response = self.client.get(self.path)

        self.assertEqual(response.status_code, 200)

    def test_get_with_disabled_flag(self):
        """
        Test checkout page accessibility. Page will return 404 if no flag is defined
        of it is disabled.
        """
        self.switch.active = False
        self.switch.save()
        response = self.client.get(self.path)

        self.assertEqual(response.status_code, 404)

    def test_get_checkout_page_with_credit_seats(self):
        """ Verify page loads and has the necessary context. """
        response = self.client.get(self.path)
        self.assertEqual(response.status_code, 200)
        expected = {
            'course': self.course,
            'credit_seats': [self.seat],
        }
        self.assertDictContainsSubset(expected, response.context)

        # Verify the payment processors are returned
        self.assertEqual(sorted(response.context['payment_processors'].keys()),
                         sorted([get_processor_class(path).NAME.lower() for path in settings.PAYMENT_PROCESSORS]))

        self.assertContains(
            response,
            'Purchase {} credits from'.format(self.credit_hours)
        )
