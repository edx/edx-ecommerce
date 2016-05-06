import httpretty

from django.db import IntegrityError
from django.test import override_settings
from django.utils.translation import ugettext_lazy as _
from oscar.templatetags.currency_filters import currency
from oscar.test.factories import *  # pylint:disable=wildcard-import,unused-wildcard-import

from ecommerce.core.url_utils import get_ecommerce_url
from ecommerce.courses.tests.factories import CourseFactory
from ecommerce.extensions.catalogue.tests.mixins import CourseCatalogTestMixin
from ecommerce.extensions.fulfillment.modules import CouponFulfillmentModule
from ecommerce.extensions.fulfillment.status import LINE
from ecommerce.extensions.voucher.utils import (
    create_vouchers, generate_coupon_report, get_voucher_discount_info, update_voucher_offer
)
from ecommerce.tests.mixins import CouponMixin, LmsApiMockMixin
from ecommerce.tests.testcases import TestCase


Basket = get_model('basket', 'Basket')
Benefit = get_model('offer', 'Benefit')
Catalog = get_model('catalogue', 'Catalog')
CouponVouchers = get_model('voucher', 'CouponVouchers')
Order = get_model('order', 'Order')
Product = get_model('catalogue', 'Product')
ProductCategory = get_model('catalogue', 'ProductCategory')
ProductClass = get_model('catalogue', 'ProductClass')
StockRecord = get_model('partner', 'StockRecord')
Voucher = get_model('voucher', 'Voucher')

VOUCHER_CODE = "XMASC0DE"
VOUCHER_CODE_LENGTH = 1


class UtilTests(CouponMixin, CourseCatalogTestMixin, LmsApiMockMixin, TestCase):

    course_id = 'edX/DemoX/Demo_Course'
    certificate_type = 'test-certificate-type'
    provider = None

    def setUp(self):
        super(UtilTests, self).setUp()

        self.user = self.create_user(full_name="Test User", is_staff=True)
        self.client.login(username=self.user.username, password=self.password)

        self.course = CourseFactory(id='course-v1:test-org+course+run')
        self.verified_seat = self.course.create_or_update_seat('verified', False, 100, self.partner)

        self.catalog = Catalog.objects.create(partner=self.partner)

        self.stock_record = StockRecord.objects.filter(product=self.verified_seat).first()
        self.seat_price = self.stock_record.price_excl_tax
        self.catalog.stock_records.add(self.stock_record)

        self.coupon = self.create_coupon(
            title='Test product',
            catalog=self.catalog,
            note='Test note',
            quantity=1,
            max_uses=1
        )
        self.coupon.history.all().update(history_user=self.user)
        self.coupon_vouchers = CouponVouchers.objects.filter(coupon=self.coupon)

    def create_benefits(self):
        """
        Create all Benefit permutations
            - Benefit type: Percentage, Benefit value: 100%
            - Benefit type: Percentage, Benefit value: 50%
            - Benefit type: Value, Benefit value: seat price
            - Benefit type: Value, Benefit value: half the seat price
        """
        _range = RangeFactory(products=[self.verified_seat, ])

        benefit_percentage_all = BenefitFactory(type=Benefit.PERCENTAGE, range=_range, value=100.00)
        benefit_percentage_half = BenefitFactory(type=Benefit.PERCENTAGE, range=_range, value=50.00)
        benefit_value_all = BenefitFactory(type=Benefit.FIXED, range=_range, value=self.seat_price)
        benefit_value_half = BenefitFactory(type=Benefit.FIXED, range=_range, value=self.seat_price / 2)

        return [benefit_percentage_all, benefit_percentage_half, benefit_value_all, benefit_value_half]

    def setup_coupons_for_report(self):
        """ Create specific coupons to test report generation """
        create_vouchers(
            benefit_type=Benefit.PERCENTAGE,
            benefit_value=50.00,
            catalog=self.catalog,
            coupon=self.coupon,
            end_datetime=datetime.date(2099, 10, 30),
            name='Enrollment',
            quantity=1,
            start_datetime=datetime.date(2015, 10, 1),
            voucher_type=Voucher.ONCE_PER_CUSTOMER,
            code=VOUCHER_CODE,
            max_uses=1
        )

        create_vouchers(
            benefit_type=Benefit.FIXED,
            benefit_value=100.00,
            catalog=self.catalog,
            coupon=self.coupon,
            end_datetime=datetime.date.today() + datetime.timedelta(10),
            name='Discount',
            quantity=1,
            start_datetime=datetime.date.today() - datetime.timedelta(1),
            voucher_type=Voucher.SINGLE_USE
        )

    def use_voucher(self, order_num, voucher, user):
        """
        Mark voucher as used by provided users

        Args:
            voucher (Voucher): voucher to be marked as used
            users (list): list of users
        """
        order = OrderFactory(number=order_num)
        voucher.record_usage(order, user)
        voucher.offers.first().record_usage(discount={'freq': 1, 'discount': 1})

    def validate_report_of_redeemed_vouchers(self, row, username, order_num):
        """ Helper method for validating coupon report data for when a coupon was redeemed. """
        self.assertEqual(row['Status'], _('Redeemed'))
        self.assertEqual(row['Redeemed By Username'], username)
        self.assertEqual(row['Order Number'], order_num)

    def test_create_vouchers(self):
        """
        Test voucher creation
        """
        vouchers = create_vouchers(
            benefit_type=Benefit.PERCENTAGE,
            benefit_value=100.00,
            catalog=self.catalog,
            coupon=self.coupon,
            end_datetime=datetime.date(2015, 10, 30),
            name="Test voucher",
            quantity=10,
            start_datetime=datetime.date(2015, 10, 1),
            voucher_type=Voucher.SINGLE_USE
        )

        self.assertEqual(len(vouchers), 10)

        voucher = vouchers[0]
        voucher_offer = voucher.offers.first()
        coupon_voucher = CouponVouchers.objects.get(coupon=self.coupon)

        self.assertEqual(voucher_offer.benefit.type, Benefit.PERCENTAGE)
        self.assertEqual(voucher_offer.benefit.value, 100.00)
        self.assertEqual(voucher_offer.benefit.range.catalog, self.catalog)
        self.assertEqual(len(coupon_voucher.vouchers.all()), 11)
        self.assertEqual(voucher.end_datetime, datetime.date(2015, 10, 30))
        self.assertEqual(voucher.start_datetime, datetime.date(2015, 10, 1))
        self.assertEqual(voucher.usage, Voucher.SINGLE_USE)

    @override_settings(VOUCHER_CODE_LENGTH=VOUCHER_CODE_LENGTH)
    def test_regenerate_voucher_code(self):
        """
        Test that voucher code will be regenerated if it already exists
        """
        for code in 'BCDFGHJKL':
            create_vouchers(
                benefit_type=Benefit.PERCENTAGE,
                benefit_value=100.00,
                catalog=self.catalog,
                coupon=self.coupon,
                end_datetime=datetime.date(2015, 10, 30),
                name="Test voucher",
                quantity=1,
                start_datetime=datetime.date(2015, 10, 1),
                voucher_type=Voucher.SINGLE_USE,
                code=code
            )

        for _ in range(20):
            voucher = create_vouchers(
                benefit_type=Benefit.PERCENTAGE,
                benefit_value=100.00,
                catalog=self.catalog,
                coupon=self.coupon,
                end_datetime=datetime.date(2015, 10, 30),
                name="Test voucher",
                quantity=1,
                start_datetime=datetime.date(2015, 10, 1),
                voucher_type=Voucher.SINGLE_USE
            )
            self.assertTrue(Voucher.objects.filter(code__iexact=voucher[0].code).exists())

    @override_settings(VOUCHER_CODE_LENGTH=0)
    def test_nonpositive_voucher_code_length(self):
        """
        Test that setting a voucher code length to a nonpositive integer value
        raises a ValueError
        """
        with self.assertRaises(ValueError):
            create_vouchers(
                benefit_type=Benefit.PERCENTAGE,
                benefit_value=100.00,
                catalog=self.catalog,
                coupon=self.coupon,
                end_datetime=datetime.date(2015, 10, 30),
                name="Test voucher",
                quantity=1,
                start_datetime=datetime.date(2015, 10, 1),
                voucher_type=Voucher.SINGLE_USE
            )

    def test_create_discount_coupon(self):
        """
        Test discount voucher creation with specified code
        """
        discount_vouchers = create_vouchers(
            benefit_type=Benefit.PERCENTAGE,
            benefit_value=25.00,
            catalog=self.catalog,
            coupon=self.coupon,
            end_datetime=datetime.date(2015, 10, 30),
            name="Discount code",
            quantity=1,
            start_datetime=datetime.date(2015, 10, 1),
            voucher_type=Voucher.SINGLE_USE,
            code=VOUCHER_CODE
        )

        self.assertEqual(len(discount_vouchers), 1)
        self.assertEqual(discount_vouchers[0].code, "XMASC0DE")

        with self.assertRaises(IntegrityError):
            create_vouchers(
                benefit_type=Benefit.PERCENTAGE,
                benefit_value=35.00,
                catalog=self.catalog,
                coupon=self.coupon,
                end_datetime=datetime.date(2015, 10, 30),
                name="Discount name",
                quantity=1,
                start_datetime=datetime.date(2015, 10, 1),
                voucher_type=Voucher.SINGLE_USE,
                code=VOUCHER_CODE
            )

    def assert_report_row(self, row, coupon, voucher):
        """ Verify that the row fields contain the right data. """
        offer = voucher.offers.all().first()
        discount_data = get_voucher_discount_info(
            offer.benefit,
            offer.condition.range.catalog.stock_records.first().price_excl_tax
        )
        coupon_type = _('Discount') if discount_data['is_discounted'] else _('Enrollment')
        discount_percentage = _("{percentage} %").format(percentage=discount_data['discount_percentage'])
        discount_amount = currency(discount_data['discount_value'])

        self.assertEqual(row['Coupon Type'], coupon_type)
        self.assertEqual(row['Category'], ProductCategory.objects.get(product=coupon).category.name)
        self.assertEqual(row['Discount Percentage'], discount_percentage)
        self.assertEqual(row['Discount Amount'], discount_amount)
        self.assertEqual(row['Client'], coupon.client.name)
        self.assertEqual(
            row['URL'],
            get_ecommerce_url() + self.REDEMPTION_URL.format(voucher.code)
        )
        self.assertEqual(row['Note'], coupon.attr.note)
        self.assertEqual(row['Created By'], coupon.history.first().history_user.full_name)
        self.assertEqual(row['Create Date'], coupon.history.latest().history_date.strftime("%b %d, %y"))
        self.assertEqual(row['Coupon Start Date'], voucher.start_datetime.strftime("%b %d, %y"))
        self.assertEqual(row['Coupon Expiry Date'], voucher.end_datetime.strftime("%b %d, %y"))

    @httpretty.activate
    def test_generate_coupon_report(self):
        """ Verify the coupon report is generated properly. """
        self.setup_coupons_for_report()
        client = UserFactory()
        basket = Basket.get_basket(client, self.site)
        basket.add_product(self.coupon)

        vouchers = self.coupon_vouchers.first().vouchers.all()
        self.use_voucher('TESTORDER1', vouchers[1], self.user)

        user2 = UserFactory()
        self.use_voucher('TESTORDER2', vouchers[2], self.user)
        self.use_voucher('TESTORDER3', vouchers[2], user2)

        self.mock_course_api_response(course=self.course)
        field_names, rows = generate_coupon_report(self.coupon_vouchers)

        self.assertEqual(field_names, [
            'Coupon Name',
            'Code',
            'Maximum Coupon Usage',
            'Redemption Count',
            'Coupon Type',
            'URL',
            'Course ID',
            'Organization',
            'Client',
            'Category',
            'Note',
            'Price',
            'Invoiced Amount',
            'Discount Percentage',
            'Discount Amount',
            'Status',
            'Order Number',
            'Redeemed By Username',
            'Created By',
            'Create Date',
            'Coupon Start Date',
            'Coupon Expiry Date',
        ])

        for row in rows:
            voucher = Voucher.objects.get(name=row['Coupon Name'])
            self.assert_report_row(row, self.coupon, voucher)

    @httpretty.activate
    def test_report_for_inactive_coupons(self):
        """ Verify the coupon report show correct status for inactive coupons. """
        create_vouchers(
            benefit_type=Benefit.FIXED,
            benefit_value=100.00,
            catalog=self.catalog,
            coupon=self.coupon,
            end_datetime=datetime.date(2015, 10, 30),
            name="Inactive code",
            quantity=1,
            start_datetime=datetime.date(2015, 10, 30),
            voucher_type=Voucher.SINGLE_USE
        )

        __, rows = generate_coupon_report(self.coupon_vouchers)

        inactive_coupon_row = rows[1]
        self.assertEqual(inactive_coupon_row['Coupon Name'], 'Inactive code')
        self.assertEqual(inactive_coupon_row['Status'], _('Inactive'))

    @httpretty.activate
    def test_generate_coupon_report_for_old_coupons(self):
        """ Verify that the client info is present for old coupons. """
        self.setup_coupons_for_report()

        Order.objects.get(basket=self.basket).delete()
        ProductCategory.objects.all().delete()

        self.mock_course_api_response(course=self.course)
        __, rows = generate_coupon_report(self.coupon_vouchers)

        for row in rows:
            self.assertEqual(row['Client'], self.basket.owner.username)
            self.assertEqual(row['Category'], '')

    def test_get_voucher_discount_info(self):
        """ Verify that get_voucher_discount_info() returns correct info. """
        benefits = self.create_benefits()

        for benefit in benefits:
            discount_info = get_voucher_discount_info(benefit, self.seat_price)
            if (
                    benefit.type == "Percentage" and benefit.value == 100.00 or
                    benefit.type == "Absolute" and benefit.value == self.seat_price
            ):
                self.assertEqual(discount_info['discount_percentage'], 100.00)
                self.assertEqual(discount_info['discount_value'], 100.00)
                self.assertFalse(discount_info['is_discounted'])
            else:
                self.assertEqual(discount_info['discount_percentage'], 50.00)
                self.assertEqual(discount_info['discount_value'], 50.00)
                self.assertTrue(discount_info['is_discounted'])

            discount_info = get_voucher_discount_info(benefit, 0.0)
            self.assertEqual(discount_info['discount_percentage'], 0.00)
            self.assertEqual(discount_info['discount_value'], 0.00)
            self.assertFalse(discount_info['is_discounted'])

            discount_info = get_voucher_discount_info(None, 0.0)
            self.assertEqual(discount_info['discount_percentage'], 0.00)
            self.assertEqual(discount_info['discount_value'], 0.00)
            self.assertFalse(discount_info['is_discounted'])

            discount_info = get_voucher_discount_info(None, self.seat_price)
            self.assertEqual(discount_info['discount_percentage'], 0.00)
            self.assertEqual(discount_info['discount_value'], 0.00)
            self.assertFalse(discount_info['is_discounted'])

        discount_info = get_voucher_discount_info(benefits[-1], 20.00)
        self.assertEqual(discount_info['discount_percentage'], 100.00)
        self.assertEqual(discount_info['discount_value'], 20.00)
        self.assertFalse(discount_info['is_discounted'])

    def test_multiple_usage_coupon(self):
        """Test that multiple-usage coupon is created and the usage number decreased on usage."""
        # Verify that the created voucher has two possible applications.
        voucher = self.coupon.attr.coupon_vouchers.vouchers.first()
        self.assertEqual(voucher.offers.first().get_max_applications(), 1)

        # Verify that the voucher now has been applied and usage number decreased.
        basket = self.apply_voucher(self.user, self.site, voucher)
        order = create_order(basket=basket, user=self.user)
        lines = order.lines.all()
        order, completed_lines = CouponFulfillmentModule().fulfill_product(order, lines)
        self.assertEqual(completed_lines[0].status, LINE.COMPLETE)
        self.assertEqual(len(basket.applied_offers()), 1)
        self.assertEqual(voucher.offers.first().get_max_applications(), 0)

        # Verify that the voucher with now 0 usage number wasn't applied to the basket.
        new_basket = self.apply_voucher(self.user, self.site, voucher)
        self.assertEqual(len(new_basket.applied_offers()), 0)

    def test_update_voucher_offer(self):
        vouchers = create_vouchers(
            benefit_type=Benefit.PERCENTAGE,
            benefit_value=100.00,
            catalog=self.catalog,
            coupon=self.coupon,
            end_datetime=datetime.date(2015, 10, 30),
            name="Test voucher",
            quantity=10,
            start_datetime=datetime.date(2015, 10, 1),
            voucher_type=Voucher.SINGLE_USE
        )

        voucher = vouchers[0]
        voucher_offer = voucher.offers.first()
        self.assertEqual(voucher_offer.benefit.type, Benefit.PERCENTAGE)
        self.assertEqual(voucher_offer.benefit.value, 100.00)
        self.assertEqual(voucher_offer.benefit.range.catalog, self.catalog)

        new_offer = update_voucher_offer(voucher_offer, 50.00, Benefit.PERCENTAGE)
        self.assertEqual(new_offer.benefit.type, Benefit.PERCENTAGE)
        self.assertEqual(new_offer.benefit.value, 50.00)
        self.assertEqual(new_offer.benefit.range.catalog, self.catalog)
