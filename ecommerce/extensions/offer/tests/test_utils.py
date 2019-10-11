from __future__ import absolute_import

from decimal import Decimal

import ddt
import mock
from django.conf import settings
from oscar.core.loading import get_model
from oscar.test.factories import StockRecord

from ecommerce.courses.tests.factories import CourseFactory
from ecommerce.extensions.catalogue.tests.mixins import DiscoveryTestMixin
from ecommerce.extensions.checkout.utils import add_currency
from ecommerce.extensions.offer.utils import (
    SafeDict,
    _remove_exponent_and_trailing_zeros,
    format_benefit_value,
    format_email,
    send_assigned_offer_email,
    send_assigned_offer_reminder_email,
    send_revoked_offer_email
)
from ecommerce.extensions.test.factories import (
    AbsoluteDiscountBenefitWithoutRangeFactory,
    BenefitFactory,
    PercentageDiscountBenefitWithoutRangeFactory,
    RangeFactory
)
from ecommerce.tests.testcases import TestCase

Benefit = get_model('offer', 'Benefit')


@ddt.ddt
class UtilTests(DiscoveryTestMixin, TestCase):
    _ASSIGNMENT_EMAIL_TEMPLATE = '''
        Your learning manager has provided you with a new access code to take a course at edX.
        You may redeem this code for {REDEMPTIONS_REMAINING} courses.

        edX login: {USER_EMAIL}
        Access Code: {CODE}
        Expiration date: {EXPIRATION_DATE}

        You can insert the access code at check out under "coupon code" for applicable courses.

        For any questions, please reach out to your Learning Manager.
    '''

    _REMINDER_EMAIL_TEMPLATE = '''
        This is a reminder email that your learning manager has provided you with a access code to take a course at edX.
        You have redeemed this code {REDEEMED_OFFER_COUNT} of times out of {TOTAL_OFFER_COUNT} number of available course redemptions.

        edX login: {USER_EMAIL}
        Access Code: {CODE}
        Expiration date: {EXPIRATION_DATE}

        You can insert the access code at check out under "coupon code" for applicable courses.

        For any questions, please reach out to your Learning Manager.
    '''

    _REVOKE_ASSIGNMENT_EMAIL_TEMPLATE = '''
        Your learning manager has revoked {CODE} and it is no longer assigned to your edX account {USER_EMAIL}.
        '''

    _BROKEN_EMAIL_TEMPLATE = '''
        Text
        {DOES_NOT_EXIST}
        code: {CODE}
        {}
        { abc d }
        More text.
        '''

    def setUp(self):
        super(UtilTests, self).setUp()
        self.course = CourseFactory(partner=self.partner)
        self.verified_seat = self.course.create_or_update_seat('verified', False, 100)
        self.stock_record = StockRecord.objects.filter(product=self.verified_seat).first()
        self.seat_price = self.stock_record.price_excl_tax
        self._range = RangeFactory(products=[self.verified_seat, ])

        self.percentage_benefit = BenefitFactory(type=Benefit.PERCENTAGE, range=self._range, value=35.00)
        self.value_benefit = BenefitFactory(type=Benefit.FIXED, range=self._range, value=self.seat_price - 10)

    def test_format_benefit_value(self):
        """ format_benefit_value(benefit) should format benefit value based on benefit type """
        benefit_value = format_benefit_value(self.percentage_benefit)
        self.assertEqual(benefit_value, '35%')

        benefit_value = format_benefit_value(self.value_benefit)
        expected_benefit = add_currency(Decimal((self.seat_price - 10)))
        self.assertEqual(benefit_value, '${expected_benefit}'.format(expected_benefit=expected_benefit))

    def test_format_program_benefit_value(self):
        """ format_benefit_value(program_benefit) should format benefit value based on proxy class. """
        percentage_benefit = PercentageDiscountBenefitWithoutRangeFactory()
        benefit_value = format_benefit_value(percentage_benefit)
        self.assertEqual(benefit_value, '{}%'.format(percentage_benefit.value))

        absolute_benefit = AbsoluteDiscountBenefitWithoutRangeFactory()
        benefit_value = format_benefit_value(absolute_benefit)
        expected_value = add_currency(Decimal(absolute_benefit.value))
        self.assertEqual(benefit_value, '${}'.format(expected_value))

    @ddt.data(
        ('1.0', '1'),
        ('5000.0', '5000'),
        ('1.45000', '1.45'),
        ('5000.40000', '5000.4'),
    )
    @ddt.unpack
    def test_remove_exponent_and_trailing_zeros(self, value, expected):
        """
        _remove_exponent_and_trailing_zeros(decimal) should remove exponent and trailing zeros
        from decimal number
        """
        decimal = _remove_exponent_and_trailing_zeros(Decimal(value))
        self.assertEqual(decimal, Decimal(expected))

    @mock.patch('ecommerce.extensions.offer.utils.send_offer_assignment_email')
    @ddt.data(
        (
            _ASSIGNMENT_EMAIL_TEMPLATE,
            {
                'offer_assignment_id': 555,
                'learner_email': 'johndoe@unknown.com',
                'code': 'GIL7RUEOU7VHBH7Q',
                'redemptions_remaining': 10,
                'code_expiration_date': '2018-12-19'
            },
            None,
        ),
        (
            _BROKEN_EMAIL_TEMPLATE,
            {
                'offer_assignment_id': 123,
                'learner_email': 'anotherdoe@unknown.com',
                'code': 'ABCD1234',
                'redemptions_remaining': 2,
                'code_expiration_date': '2020-12-19'
            },
            None,
        ),
    )
    @ddt.unpack
    def test_send_assigned_offer_email(
            self,
            template,
            tokens,
            side_effect,
            mock_sailthru_task,
    ):
        """ Test that the offer assignment email message is sent to async task. """
        email_subject = settings.OFFER_ASSIGNMENT_EMAIL_DEFAULT_SUBJECT
        mock_sailthru_task.delay.side_effect = side_effect
        send_assigned_offer_email(
            template,
            tokens.get('offer_assignment_id'),
            tokens.get('learner_email'),
            tokens.get('code'),
            tokens.get('redemptions_remaining'),
            tokens.get('code_expiration_date'),
        )
        mock_sailthru_task.delay.assert_called_once_with(
            tokens.get('learner_email'),
            tokens.get('offer_assignment_id'),
            email_subject,
            mock.ANY
        )

    @mock.patch('ecommerce.extensions.offer.utils.send_offer_update_email')
    @ddt.data(
        (
            _REMINDER_EMAIL_TEMPLATE,
            {
                'learner_email': 'johndoe@unknown.com',
                'code': 'GIL7RUEOU7VHBH7Q',
                'redeemed_offer_count': 0,
                'total_offer_count': 1,
                'code_expiration_date': '2018-12-19'
            },
            None,
        ),
        (
            _BROKEN_EMAIL_TEMPLATE,
            {
                'offer_assignment_id': 456,
                'learner_email': 'other@unknown.com',
                'code': 'QWERTY8',
                'redemptions_remaining': 3,
                'code_expiration_date': '2021-12-19'
            },
            None,
        ),
    )
    @ddt.unpack
    def test_send_assigned_offer_reminder_email(
            self,
            template,
            tokens,
            side_effect,
            mock_sailthru_task,
    ):
        """
        Test that the offer assignment reminder email message is sent to the async task in ecommerce-worker.
        """
        email_subject = settings.OFFER_ASSIGNMENT_EMAIL_REMINDER_DEFAULT_SUBJECT
        mock_sailthru_task.delay.side_effect = side_effect
        send_assigned_offer_reminder_email(
            template,
            tokens.get('learner_email'),
            tokens.get('code'),
            tokens.get('redeemed_offer_count'),
            tokens.get('total_offer_count'),
            tokens.get('code_expiration_date'),
        )
        mock_sailthru_task.delay.assert_called_once_with(
            tokens.get('learner_email'),
            email_subject,
            mock.ANY
        )

    @mock.patch('ecommerce.extensions.offer.utils.send_offer_update_email')
    @ddt.data(
        (
            _REVOKE_ASSIGNMENT_EMAIL_TEMPLATE,
            {
                'learner_email': 'johndoe@unknown.com',
                'code': 'GIL7RUEOU7VHBH7Q',
            },
            None,
        ),
        (
            _BROKEN_EMAIL_TEMPLATE,
            {
                'learner_email': 'tester@unknown.com',
                'code': 'PUMPKINS',
            },
            None,
        ),
    )
    @ddt.unpack
    def test_send_offer_revoked_email(
            self,
            template,
            tokens,
            side_effect,
            mock_sailthru_task,
    ):
        """
        Test that the offer revocation email message is sent to the async task in ecommerce-worker.
        """
        email_subject = settings.OFFER_REVOKE_EMAIL_DEFAULT_SUBJECT
        mock_sailthru_task.delay.side_effect = side_effect
        send_revoked_offer_email(
            template,
            tokens.get('learner_email'),
            tokens.get('code'),
        )
        mock_sailthru_task.delay.assert_called_once_with(
            tokens.get('learner_email'),
            email_subject,
            mock.ANY
        )

    @ddt.data(
        (
            _ASSIGNMENT_EMAIL_TEMPLATE,
            {
                'learner_email': 'johndoe@unknown.com',
                'code': 'GIL7RUEOU7VHBH7Q',
                'redemptions_remaining': 500,
                'code_expiration_date': '2018-12-19'
            },
        ),
    )
    @ddt.unpack
    def test_format_assigned_offer_email(
            self,
            template,
            tokens,
    ):
        """
        Test that the assigned offer email message is formatted correctly.
        """
        placeholder_dict = SafeDict(
            REDEMPTIONS_REMAINING=tokens.get('redemptions_remaining'),
            USER_EMAIL=tokens.get('learner_email'),
            CODE=tokens.get('code'),
            EXPIRATION_DATE=tokens.get('code_expiration_date'),
        )
        email = format_email(template, placeholder_dict)
        self.assertTrue(str(tokens.get('redemptions_remaining')) in email)
        self.assertTrue(tokens.get('learner_email') in email)
        self.assertTrue(tokens.get('code') in email)
        self.assertTrue(tokens.get('code_expiration_date') in email)

    def test_format_assigned_offer_broken_email(self):
        """
        Test that the assigned offer email message is formatted correctly if the template is broken.
        """
        code = 'GIL7RUEOU7VHBH7Q'
        placeholder_dict = SafeDict(
            REDEMPTIONS_REMAINING=500,
            USER_EMAIL='johndoe@unknown.com',
            CODE=code,
            EXPIRATION_DATE='2018-12-19',
        )
        email = format_email(self._BROKEN_EMAIL_TEMPLATE, placeholder_dict)
        self.assertTrue('{DOES_NOT_EXIST}' in email)
        self.assertTrue(code in email)

        # Compare strings, ignoring whitespace differences
        expected_email = """
            Text
            {DOES_NOT_EXIST}
            code: GIL7RUEOU7VHBH7Q
            {}
            { abc d }
            More text.
            """
        self.assertEqual(email.split(), expected_email.split())
