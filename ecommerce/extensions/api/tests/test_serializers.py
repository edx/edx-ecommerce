from __future__ import absolute_import

import datetime

import mock
from oscar.core.loading import get_model
from testfixtures import LogCapture

from ecommerce.coupons.tests.mixins import CouponMixin
from ecommerce.extensions.api.serializers import (
    CouponCodeAssignmentSerializer,
    CouponCodeRemindSerializer,
    CouponCodeRevokeSerializer
)
from ecommerce.extensions.test import factories
from ecommerce.tests.testcases import TestCase

OfferAssignment = get_model('offer', 'OfferAssignment')
Voucher = get_model('voucher', 'Voucher')


class CouponCodeSerializerTests(CouponMixin, TestCase):
    """ Test for coupon code serializers. """
    LOGGER_NAME = 'ecommerce.extensions.api.serializers'
    TEMPLATE = 'Text {PARAM} is fun'
    GREETING = 'Hello '
    CLOSING = ' Bye'

    def setUp(self):
        super(CouponCodeSerializerTests, self).setUp()

        self.coupon = self.create_coupon(
            benefit_value=25,
            code='serializeCouponTest',
            enterprise_customer='af4b351f-5f1c-4fc3-af41-48bb38fcb161',
            catalog=None,
            enterprise_customer_catalog='8212a8d8-c6b1-4023-8754-4d687c43d72f',
            end_datetime=(datetime.datetime.now() + datetime.timedelta(days=10))
        )
        self.code = self.coupon.attr.coupon_vouchers.vouchers.first().code
        self.email = 'serializeCoupon@test.org'
        self.data = {'codes': [self.code], 'emails': [self.email]}
        self.offer_assignments = factories.EnterpriseOfferFactory.create_batch(1)
        self.offer_assignment = OfferAssignment.objects.create(
            offer=self.offer_assignments[0],
            code=self.code,
            user_email=self.email,
        )

    @mock.patch('ecommerce.extensions.api.serializers.send_assigned_offer_email')
    def test_send_assigned_offer_email_args(self, mock_assign_email):
        """ Test that the code_expiration_date passed is equal to coupon batch end date """
        serializer = CouponCodeAssignmentSerializer(data=self.data, context={'coupon': self.coupon})
        serializer._trigger_email_sending_task(  # pylint: disable=protected-access
            greeting=self.GREETING,
            closing=self.CLOSING,
            assigned_offer=self.offer_assignment,
            voucher_usage_type=Voucher.MULTI_USE_PER_CUSTOMER
        )
        expected_expiration_date = self.coupon.attr.coupon_vouchers.vouchers.first().end_datetime
        mock_assign_email.assert_called_with(
            greeting=self.GREETING,
            closing=self.CLOSING,
            offer_assignment_id=self.offer_assignment.id,
            learner_email=self.offer_assignment.user_email,
            code=self.offer_assignment.code,
            redemptions_remaining=mock.ANY,
            code_expiration_date=expected_expiration_date.strftime('%d %B, %Y')
        )

    @mock.patch('ecommerce.extensions.api.serializers.send_assigned_offer_reminder_email')
    def test_send_assigned_offer_reminder_email_args(self, mock_remind_email):
        """ Test that the code_expiration_date passed is equal to coupon batch end date """
        serializer = CouponCodeRemindSerializer(data=self.data, context={'coupon': self.coupon})
        serializer._trigger_email_sending_task(  # pylint: disable=protected-access
            greeting=self.GREETING,
            closing=self.CLOSING,
            assigned_offer=self.offer_assignment,
            redeemed_offer_count=3,
            total_offer_count=5,
        )
        expected_expiration_date = self.coupon.attr.coupon_vouchers.vouchers.first().end_datetime
        mock_remind_email.assert_called_with(
            greeting=self.GREETING,
            closing=self.CLOSING,
            learner_email=self.offer_assignment.user_email,
            code=self.offer_assignment.code,
            redeemed_offer_count=mock.ANY,
            total_offer_count=mock.ANY,
            code_expiration_date=expected_expiration_date.strftime('%d %B, %Y')
        )

    @mock.patch('ecommerce.extensions.api.serializers.send_assigned_offer_email')
    def test_send_assignment_email_error(self, mock_email):
        """ Test that we log an appropriate message if the code assignment email cannot be sent. """
        mock_email.side_effect = Exception('Ignore me - assignment')
        serializer = CouponCodeAssignmentSerializer(data=self.data, context={'coupon': self.coupon})
        expected = [
            (
                self.LOGGER_NAME,
                'ERROR',
                '[Offer Assignment] Email for offer_assignment_id: {} with greeting \'{}\' and closing \'{}\' raised '
                'exception: Exception(\'Ignore me - assignment\',)'.format(
                    self.offer_assignment.id,
                    self.GREETING,
                    self.CLOSING,
                )
            ),
        ]

        with LogCapture(self.LOGGER_NAME) as log:
            serializer._trigger_email_sending_task(  # pylint: disable=protected-access
                greeting=self.GREETING,
                closing=self.CLOSING,
                assigned_offer=self.offer_assignment,
                voucher_usage_type=Voucher.MULTI_USE_PER_CUSTOMER
            )
            log.check_present(*expected)

    @mock.patch('ecommerce.extensions.api.serializers.send_assigned_offer_reminder_email')
    def test_send_reminder_email_error(self, mock_email):
        """ Test that we log an appropriate message if the code reminder email cannot be sent. """
        mock_email.side_effect = Exception('Ignore me - reminder')
        serializer = CouponCodeRemindSerializer(data=self.data, context={'coupon': self.coupon})
        expected = [
            (
                self.LOGGER_NAME,
                'ERROR',
                '[Offer Reminder] Email for offer_assignment_id: {} with greeting \'{}\' and closing \'{}\' raised '
                'exception: Exception(\'Ignore me - reminder\',)'.format(
                    self.offer_assignment.id,
                    self.GREETING,
                    self.CLOSING,
                )
            ),
        ]

        with self.assertRaises(Exception):
            with LogCapture(self.LOGGER_NAME) as log:
                serializer._trigger_email_sending_task(  # pylint: disable=protected-access
                    greeting=self.GREETING,
                    closing=self.CLOSING,
                    assigned_offer=self.offer_assignment,
                    redeemed_offer_count=3,
                    total_offer_count=5,
                )
        log.check_present(*expected)

    def test_send_revocation_email_error_no_greeting(self):
        """ Test that we log an appropriate message if the code revocation email cannot be sent. """
        serializer = CouponCodeRevokeSerializer(data=self.data, context={'coupon': self.coupon})

        expected = [
            (
                self.LOGGER_NAME,
                'ERROR',
                '[Offer Revocation] Encountered error when revoking code {} for user {} with greeting {} and '
                'closing {}'.format(
                    None,
                    None,
                    None,
                    None
                )
            ),
        ]
        with LogCapture(self.LOGGER_NAME) as log:
            serializer.create(validated_data={})
            log.check_present(*expected)

    @mock.patch('ecommerce.extensions.api.serializers.send_revoked_offer_email')
    def test_send_revocation_email_error(self, mock_email):
        """ Test that we log an appropriate message if the code revocation email cannot be sent. """
        mock_email.side_effect = Exception('Ignore me - revocation')
        validated_data = {
            'code': self.code,
            'email': self.email,
            'offer_assignments': self.offer_assignments,
        }
        context = {
            'coupon': self.coupon,
            'greeting': self.GREETING,
            'closing': self.CLOSING,
        }
        serializer = CouponCodeRevokeSerializer(data=self.data, context=context)

        expected = [
            (
                self.LOGGER_NAME,
                'ERROR',
                '[Offer Revocation] Encountered error when revoking code {} for user {} with greeting \'{}\' and '
                'closing \'{}\''.format(
                    self.code,
                    self.email,
                    self.GREETING,
                    self.CLOSING,
                )
            ),
        ]
        with LogCapture(self.LOGGER_NAME) as log:
            serializer.create(validated_data=validated_data)
            log.check_present(*expected)
