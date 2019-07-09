# pylint: disable=no-else-return
from __future__ import absolute_import, unicode_literals

import logging
from datetime import datetime
from decimal import Decimal

import dateutil.parser
import newrelic.agent
import waffle
from django.http import HttpResponseBadRequest, HttpResponseRedirect
from django.shortcuts import redirect, render
from django.utils.html import escape
from django.utils.translation import ugettext as _
from opaque_keys.edx.keys import CourseKey
from oscar.apps.basket.views import VoucherAddView as BaseVoucherAddView
from oscar.apps.basket.views import *  # pylint: disable=wildcard-import, unused-wildcard-import
from oscar.core.prices import Price
from requests.exceptions import ConnectionError, Timeout
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from six.moves import range, zip
from six.moves.urllib.parse import urlencode
from slumber.exceptions import SlumberBaseException

from ecommerce.core.exceptions import SiteConfigurationError
from ecommerce.core.url_utils import get_lms_course_about_url, get_lms_url
from ecommerce.courses.utils import get_certificate_type_display_value, get_course_info_from_catalog
from ecommerce.enterprise.entitlements import get_enterprise_code_redemption_redirect
from ecommerce.enterprise.utils import CONSENT_FAILED_PARAM, get_enterprise_customer_from_voucher, has_enterprise_offer
from ecommerce.extensions.analytics.utils import (
    prepare_analytics_data,
    track_segment_event,
    translate_basket_line_for_segment
)
from ecommerce.extensions.basket import message_utils
from ecommerce.extensions.basket.constants import EMAIL_OPT_IN_ATTRIBUTE
from ecommerce.extensions.basket.exceptions import BadRequestException, RedirectException, VoucherException
from ecommerce.extensions.basket.middleware import BasketMiddleware
from ecommerce.extensions.basket.utils import (
    add_utm_params_to_url,
    apply_voucher_on_basket_and_check_discount,
    get_basket_switch_data,
    prepare_basket,
    validate_voucher
)
from ecommerce.extensions.offer.utils import format_benefit_value, render_email_confirmation_if_required
from ecommerce.extensions.order.exceptions import AlreadyPlacedOrderException
from ecommerce.extensions.partner.shortcuts import get_partner_for_site
from ecommerce.extensions.payment.constants import (
    CLIENT_SIDE_CHECKOUT_FLAG_NAME,
    ENABLE_MICROFRONTEND_FOR_BASKET_PAGE_FLAG_NAME
)
from ecommerce.extensions.payment.forms import PaymentForm

Basket = get_model('basket', 'basket')
BasketAttribute = get_model('basket', 'BasketAttribute')
BasketAttributeType = get_model('basket', 'BasketAttributeType')
Benefit = get_model('offer', 'Benefit')
logger = logging.getLogger(__name__)
Product = get_model('catalogue', 'Product')
StockRecord = get_model('partner', 'StockRecord')
Voucher = get_model('voucher', 'Voucher')
Selector = get_class('partner.strategy', 'Selector')


def _redirect_to_payment_microfrontend_if_configured(request):
    if waffle.flag_is_active(request, ENABLE_MICROFRONTEND_FOR_BASKET_PAGE_FLAG_NAME):
        if (
                request.site.siteconfiguration.enable_microfrontend_for_basket_page and
                request.site.siteconfiguration.payment_microfrontend_url
        ):
            url = add_utm_params_to_url(
                request.site.siteconfiguration.payment_microfrontend_url,
                list(request.GET.items()),
            )
            return HttpResponseRedirect(url)
    return None


class BasketAddItemsView(View):
    """
    View that adds multiple products to a user's basket.
    An additional coupon code can be supplied so the offer is applied to the basket.
    """
    def get(self, request):
        try:
            skus = self._get_skus(request)
            products = self._get_products(request, skus)
            voucher = self._get_voucher(request)

            logger.info('Starting payment flow for user [%s] for products [%s].', request.user.username, skus)

            self._redirect_for_enterprise_entitlement_if_needed(request, voucher, products, skus)
            available_products = self._get_available_products(request, products)
            self._set_email_preference_on_basket(request)

            try:
                prepare_basket(request, available_products, voucher)
            except AlreadyPlacedOrderException:
                return render(request, 'edx/error.html', {'error': _('You have already purchased these products')})

            self._redirect_to_microfrontend_if_needed(request, products)
            url = add_utm_params_to_url(reverse('basket:summary'), list(self.request.GET.items()))
            return HttpResponseRedirect(url, status=303)

        except BadRequestException as e:
            return HttpResponseBadRequest(e.message)
        except RedirectException as e:
            return e.response

    def _get_skus(self, request):
        skus = [escape(sku) for sku in request.GET.getlist('sku')]
        if not skus:
            raise BadRequestException(_('No SKUs provided.'))
        return skus

    def _get_products(self, request, skus):
        partner = get_partner_for_site(request)
        products = Product.objects.filter(stockrecords__partner=partner, stockrecords__partner_sku__in=skus)
        if not products:
            raise BadRequestException(_('Products with SKU(s) [{skus}] do not exist.').format(skus=', '.join(skus)))
        return products

    def _get_voucher(self, request):
        code = request.GET.get('code', None)
        return Voucher.objects.get(code=code) if code else None

    def _get_available_products(self, request, products):
        unavailable_product_ids = []
        for product in products:
            purchase_info = request.strategy.fetch_for_product(product)
            if not purchase_info.availability.is_available_to_buy:
                logger.warning('Product [%s] is not available to buy.', product.title)
                unavailable_product_ids.append(product.id)

        available_products = products.exclude(id__in=unavailable_product_ids)
        if not available_products:
            raise BadRequestException(_('No product is available to buy.'))
        return available_products

    def _set_email_preference_on_basket(self, request):
        """
        Associate the user's email opt in preferences with the basket in
        order to opt them in later as part of fulfillment
        """
        BasketAttribute.objects.update_or_create(
            basket=request.basket,
            attribute_type=BasketAttributeType.objects.get(name=EMAIL_OPT_IN_ATTRIBUTE),
            defaults={'value_text': request.GET.get('email_opt_in') == 'true'},
        )

    def _redirect_for_enterprise_entitlement_if_needed(self, request, voucher, products, skus):
        """
        If there is an Enterprise entitlement available for this basket,
        we redirect to the CouponRedeemView to apply the discount to the
        basket and handle the data sharing consent requirement.
        """
        if voucher is None:
            code_redemption_redirect = get_enterprise_code_redemption_redirect(
                request,
                products,
                skus,
                'basket:basket-add'
            )
            if code_redemption_redirect:
                raise RedirectException(response=code_redemption_redirect)

    def _redirect_to_microfrontend_if_needed(self, request, products):
        if self._is_single_course_purchase(products):
            redirect_response = _redirect_to_payment_microfrontend_if_configured(request)
            if redirect_response:
                raise RedirectException(response=redirect_response)

    def _is_single_course_purchase(self, products):
        return len(products) == 1 and products[0].is_seat_product


class BasketLogicMixin(object):
    """
    Business logic for determining basket contents and checkout/payment options.
    """

    @newrelic.agent.function_trace()
    def process_basket_lines(self, lines):
        """
        Processes the basket lines and extracts information for the view's context.
        In addition determines whether:
            * verification message should be displayed
            * voucher form should be displayed
            * switch link (for switching between seat and enrollment code products) should be displayed
        and returns that information for the basket view context to be updated with it.

        Args:
            lines (list): List of basket lines.
        Returns:
            context_updates (dict): Containing information with which the context needs to
                                    be updated with.
            lines_data (list): List of information about the basket lines.
        """
        context_updates = {
            'display_verification_message': False,
            'order_details_msg': None,
            'partner_sku': None,
            'switch_link_text': None,
            'show_voucher_form': True,
            'is_enrollment_code_purchase': False
        }

        lines_data = []
        for line in lines:
            product = line.product
            if product.is_seat_product or product.is_course_entitlement_product:
                line_data, _ = self._get_course_data(product)

                # TODO this is only used by hosted_checkout_basket template, which may no longer be
                # used. Consider removing both.
                if self._is_id_verification_required(product):
                    context_updates['display_verification_message'] = True
            elif product.is_enrollment_code_product:
                line_data, course = self._get_course_data(product)
                self._set_message_for_enrollment_code(product, course)
                context_updates['is_enrollment_code_purchase'] = True
                context_updates['show_voucher_form'] = False
            else:
                line_data = {
                    'product_title': product.title,
                    'image_url': None,
                    'product_description': product.description
                }

            context_updates['order_details_msg'] = self._get_order_details_message(product)
            context_updates['switch_link_text'], context_updates['partner_sku'] = get_basket_switch_data(product)

            line_data.update({
                'sku': product.stockrecords.first().partner_sku,
                'benefit_value': self._get_benefit_value(line),
                'enrollment_code': product.is_enrollment_code_product,
                'line': line,
                'seat_type': self._get_certificate_type_display_value(product),
            })
            lines_data.append(line_data)

        return context_updates, lines_data

    def process_totals(self, context):
        """
        Returns a Dictionary of data related to total price and discounts.
        """
        # Total benefit displayed in price summary.
        # Currently only one voucher per basket is supported.
        try:
            applied_voucher = self.request.basket.vouchers.first()
            total_benefit = (
                format_benefit_value(applied_voucher.best_offer.benefit)
                if applied_voucher else None
            )
        # TODO This ValueError handling no longer seems to be required and could probably be removed
        except ValueError:  # pragma: no cover
            total_benefit = None

        num_of_items = self.request.basket.num_items
        return {
            'total_benefit': total_benefit,
            'free_basket': context['order_total'].incl_tax == 0,
            'line_price': (self.request.basket.total_incl_tax_excl_discounts / num_of_items) if num_of_items > 0 else 0,
        }

    def fire_segment_events(self, request, basket):
        try:
            properties = {
                'cart_id': basket.id,
                'products': [translate_basket_line_for_segment(line) for line in basket.all_lines()],
            }
            track_segment_event(request.site, request.user, 'Cart Viewed', properties)

            properties = {
                'checkout_id': basket.order_number,
                'step': 1
            }
            track_segment_event(request.site, request.user, 'Checkout Step Viewed', properties)
        except Exception:  # pylint: disable=broad-except
            logger.exception('Failed to fire Cart Viewed event for basket [%d]', basket.id)

    def verify_enterprise_needs(self, basket):
        failed_enterprise_consent_code = self.request.GET.get(CONSENT_FAILED_PARAM)
        if failed_enterprise_consent_code:
            messages.error(
                self.request,
                _("Could not apply the code '{code}'; it requires data sharing consent.").format(
                    code=failed_enterprise_consent_code
                )
            )

        if has_enterprise_offer(basket) and basket.total_incl_tax == Decimal(0):
            raise RedirectException(response=redirect('checkout:free-checkout'))

    @newrelic.agent.function_trace()
    def _get_course_data(self, product):
        """
        Return course data.

        Args:
            product (Product): A product that has course_key as attribute (seat or bulk enrollment coupon)
        Returns:
            A dictionary containing product title, course key, image URL, description, and start and end dates.
            Also returns course information found from catalog.
        """
        course_data = {
            'product_title': None,
            'course_key': None,
            'image_url': None,
            'product_description': None,
            'course_start': None,
            'course_end': None,
        }
        course = None

        if product.is_seat_product:
            course_data['course_key'] = CourseKey.from_string(product.attr.course_key)

        try:
            course = get_course_info_from_catalog(self.request.site, product)
            try:
                course_data['image_url'] = course['image']['src']
            except (KeyError, TypeError):
                pass

            course_data['product_description'] = course.get('short_description', '')
            course_data['product_title'] = course.get('title', '')

            # The course start/end dates are not currently used
            # in the default basket templates, but we are adding
            # the dates to the template context so that theme
            # template overrides can make use of them.
            course_data['course_start'] = self._deserialize_date(course.get('start'))
            course_data['course_end'] = self._deserialize_date(course.get('end'))
        except (ConnectionError, SlumberBaseException, Timeout):
            logger.exception(
                'Failed to retrieve data from Discovery Service for course [%s].',
                course_data['course_key'],
            )

        return course_data, course

    @newrelic.agent.function_trace()
    def _get_order_details_message(self, product):
        if product.is_course_entitlement_product:
            return _(
                'After you complete your order you will be able to select course dates from your dashboard.'
            )
        elif product.is_seat_product:
            certificate_type = product.attr.certificate_type
            if certificate_type == 'verified':
                return _(
                    'After you complete your order you will be automatically enrolled '
                    'in the verified track of the course.'
                )
            elif certificate_type == 'credit':
                return _('After you complete your order you will receive credit for your course.')
            else:
                return _(
                    'After you complete your order you will be automatically enrolled in the course.'
                )
        elif product.is_enrollment_code_product:
            return _(
                '{paragraph_start}By purchasing, you and your organization agree to the following terms:'
                '{paragraph_end} {ul_start} {li_start}Each code is valid for the one course covered and can be '
                'used only one time.{li_end} '
                '{li_start}You are responsible for distributing codes to your learners in your organization.'
                '{li_end} {li_start}Each code will expire in one year from date of purchase or, if earlier, once '
                'the course is closed.{li_end} {li_start}If a course is not designated as self-paced, you should '
                'confirm that a course run is available before expiration. {li_end} {li_start}You may not resell '
                'codes to third parties.{li_end} '
                '{li_start}All edX for Business Sales are final and not eligible for refunds.{li_end}{ul_end} '
                '{paragraph_start}You will receive an email at {user_email} with your enrollment code(s). '
                '{paragraph_end}'
            ).format(
                paragraph_start='<p>',
                paragraph_end='</p>',
                ul_start='<ul>',
                li_start='<li>',
                li_end='</li>',
                ul_end='</ul>',
                user_email=self.request.user.email
            )
        else:
            return None

    @newrelic.agent.function_trace()
    def _set_message_for_enrollment_code(self, product, course):
        assert product.is_enrollment_code_product

        if self.request.basket.num_items == 1:
            course_key = CourseKey.from_string(product.attr.course_key)
            if course and course.get('marketing_url', None):
                course_about_url = course['marketing_url']
            else:
                course_about_url = get_lms_course_about_url(course_key=course_key)

            messages.info(
                self.request,
                _(
                    '{strong_start}Purchasing just for yourself?{strong_end}{paragraph_start}If you are '
                    'purchasing a single code for someone else, please continue with checkout. However, if you are the '
                    'learner {link_start}go back{link_end} to enroll directly.{paragraph_end}'
                ).format(
                    strong_start='<strong>',
                    strong_end='</strong>',
                    paragraph_start='<p>',
                    paragraph_end='</p>',
                    link_start='<a href="{course_about}">'.format(course_about=course_about_url),
                    link_end='</a>'
                ),
                extra_tags='safe enrollment-code-product-info'
            )

    @newrelic.agent.function_trace()
    def _is_id_verification_required(self, product):
        return (
            getattr(product.attr, 'id_verification_required', False) and
            product.attr.certificate_type != 'credit'
        )

    @newrelic.agent.function_trace()
    def _get_benefit_value(self, line):
        if line.has_discount:
            applied_offer_values = list(self.request.basket.applied_offers().values())
            if applied_offer_values:
                benefit = applied_offer_values[0].benefit
                return format_benefit_value(benefit)
        return None

    @newrelic.agent.function_trace()
    def _get_certificate_type(self, product):
        if product.is_seat_product or product.is_course_entitlement_product:
            return product.attr.certificate_type
        elif product.is_enrollment_code_product:
            return product.attr.seat_type
        return None

    @newrelic.agent.function_trace()
    def _get_certificate_type_display_value(self, product):
        certificate_type = self._get_certificate_type(product)
        if certificate_type:
            return get_certificate_type_display_value(certificate_type)
        return None

    @newrelic.agent.function_trace()
    def _deserialize_date(self, date_string):
        try:
            return dateutil.parser.parse(date_string)
        except (AttributeError, ValueError, TypeError):
            return None


class BasketSummaryView(BasketLogicMixin, BasketView):
    @newrelic.agent.function_trace()
    def get_context_data(self, **kwargs):
        context = super(BasketSummaryView, self).get_context_data(**kwargs)
        return self._add_to_context_data(context)

    @newrelic.agent.function_trace()
    def get(self, request, *args, **kwargs):
        basket = request.basket

        try:
            self.fire_segment_events(request, basket)
            self.verify_enterprise_needs(basket)
            self._redirect_to_microfrontend_if_needed(request, basket)
        except RedirectException as e:
            return e.response

        return super(BasketSummaryView, self).get(request, *args, **kwargs)

    @newrelic.agent.function_trace()
    def _add_to_context_data(self, context):
        formset = context.get('formset', [])
        lines = context.get('line_list', [])
        site_configuration = self.request.site.siteconfiguration

        context_updates, lines_data = self.process_basket_lines(lines)
        context.update(context_updates)
        context.update(self.process_totals(context))

        context.update({
            'analytics_data': prepare_analytics_data(
                self.request.user,
                site_configuration.segment_key,
            ),
            'enable_client_side_checkout': False,
            'sdn_check': site_configuration.enable_sdn_check
        })

        payment_processors = site_configuration.get_payment_processors()
        if (
                site_configuration.client_side_payment_processor and
                waffle.flag_is_active(self.request, CLIENT_SIDE_CHECKOUT_FLAG_NAME)
        ):
            payment_processors_data = self._get_payment_processors_data(payment_processors)
            context.update(payment_processors_data)

        context.update({
            'formset_lines_data': list(zip(formset, lines_data)),
            'homepage_url': get_lms_url(''),
            'min_seat_quantity': 1,
            'max_seat_quantity': 100,
            'payment_processors': payment_processors,
            'lms_url_root': site_configuration.lms_url_root,
        })
        return context

    @newrelic.agent.function_trace()
    def _get_payment_processors_data(self, payment_processors):
        """Retrieve information about payment processors for the client side checkout basket.

        Args:
            payment_processors (list): List of all available payment processors.
        Returns:
            A dictionary containing information about the payment processor(s) with which the
            basket view context needs to be updated with.
        """
        site_configuration = self.request.site.siteconfiguration
        payment_processor_class = site_configuration.get_client_side_payment_processor_class()

        if payment_processor_class:
            payment_processor = payment_processor_class(self.request.site)
            current_year = datetime.today().year

            return {
                'client_side_payment_processor': payment_processor,
                'enable_client_side_checkout': True,
                'months': list(range(1, 13)),
                'payment_form': PaymentForm(
                    user=self.request.user,
                    request=self.request,
                    initial={'basket': self.request.basket},
                    label_suffix=''
                ),
                'paypal_enabled': 'paypal' in (p.NAME for p in payment_processors),
                # Assumption is that the credit card duration is 15 years
                'years': list(range(current_year, current_year + 16)),
            }
        else:
            msg = 'Unable to load client-side payment processor [{processor}] for ' \
                  'site configuration [{sc}]'.format(processor=site_configuration.client_side_payment_processor,
                                                     sc=site_configuration.id)
            raise SiteConfigurationError(msg)

    def _redirect_to_microfrontend_if_needed(self, request, basket):
        if self._is_single_course_purchase(basket):
            redirect_response = _redirect_to_payment_microfrontend_if_configured(request)
            if redirect_response:
                raise RedirectException(response=redirect_response)

    def _is_single_course_purchase(self, basket):
        return (
            basket.num_items == 1 and
            basket.lines.count() == 1 and
            basket.lines.first().product.is_seat_product
        )


class PaymentApiLogicMixin(BasketLogicMixin):
    """
    Business logic for the various Payment APIs.
    """
    def get_payment_api_response(self):
        """
        Serializes the payment api response.
        """
        context, lines_data = self.process_basket_lines(self.request.basket.all_lines())

        context['order_total'] = self._get_order_total()
        context.update(self.process_totals(context))

        response = self._serialize_context(context, lines_data)
        self._add_messages(response, context)
        return Response(response, status=self._get_response_status(response))

    def _get_order_total(self):
        """
        Return the order_total in preparation for call to process_totals.
        See https://github.com/django-oscar/django-oscar/blob/1.5.4/src/oscar/apps/basket/views.py#L92-L132
        for reference in how this is calculated by Oscar.
        """
        shipping_charge = Price('USD', excl_tax=Decimal(0), tax=Decimal(0))
        return OrderTotalCalculator().calculate(self.request.basket, shipping_charge)

    def _serialize_context(self, context, lines_data):
        """
        Serializes the data in the given context.

        Args:
            context (dict): pre-calculated context data
        """
        response = {
            'basket_id': self.request.basket.id,
            'is_free_basket': context['free_basket'],
            'currency': self.request.basket.currency,
        }

        self._add_products(response, lines_data)
        self._add_total_summary(response, context)
        self._add_offers(response)
        self._add_coupons(response, context)
        return response

    def _add_products(self, response, lines_data):
        response['products'] = [
            {
                'sku': line_data['sku'],
                'title': line_data['product_title'],
                'product_type': line_data['line'].product.get_product_class().name,
                'image_url': line_data['image_url'],
                'certificate_type': self._get_certificate_type(line_data['line'].product),
            }
            for line_data in lines_data
        ]

    def _add_total_summary(self, response, context):
        if context['is_enrollment_code_purchase']:
            response['summary_price'] = context['line_price']
            response['summary_quantity'] = self.request.basket.num_items
            response['summary_subtotal'] = context['order_total'].incl_tax
        else:
            response['summary_price'] = self.request.basket.total_incl_tax_excl_discounts

        response['summary_discounts'] = self.request.basket.total_discount
        response['order_total'] = context['order_total'].incl_tax

    def _add_offers(self, response):
        response['offers'] = [
            {
                'provider': offer.condition.enterprise_customer_name,
                'benefit_value': format_benefit_value(offer.benefit),
            }
            for offer in self.request.basket.applied_offers().values()
            if offer.condition.enterprise_customer_name
        ]

    def _add_coupons(self, response, context):
        response['show_coupon_form'] = context['show_voucher_form']
        response['coupons'] = [
            {
                'id': voucher.id,
                'code': voucher.code,
                'benefit_value': context['total_benefit'],
            }
            for voucher in self.request.basket.vouchers.all()
            if response['show_coupon_form'] and self.request.basket.contains_a_voucher
        ]

    def _add_messages(self, response, context):
        response['messages'] = message_utils.serialize(self.request)
        response['switch_message'] = context['switch_link_text']

    def _get_response_status(self, response):
        return message_utils.get_response_status(response['messages'])


class PaymentApiView(PaymentApiLogicMixin, APIView):
    """
    Api for retrieving basket contents and checkout/payment options.

    GET:
        Retrieves basket contents and checkout/payment options.
    """
    permission_classes = (IsAuthenticated,)

    def get(self, request):  # pylint: disable=unused-argument
        basket = request.basket

        try:
            self.fire_segment_events(request, basket)
            self.verify_enterprise_needs(basket)
            return self.get_payment_api_response()
        except RedirectException as e:
            return Response({'redirect': e.response.url})


class VoucherAddLogicMixin(object):
    """
    VoucherAdd logic for adding a voucher.
    """
    def verify_and_apply_voucher(self, code):
        """
        Verifies the voucher for the given code before applying it to the basket.

        Raises:
            VoucherException in case of an error.
            RedirectException if a redirect is needed.
        """
        self._verify_basket_not_empty(code)
        self._verify_voucher_not_already_applied(code)

        stock_record = self._get_stock_record()
        voucher = self._get_voucher(code)

        self._verify_email_confirmation(voucher, stock_record.product)
        self._verify_enterprise_needs(voucher, code, stock_record)

        self.request.basket.clear_vouchers()
        self._validate_voucher(voucher)
        self._apply_voucher(voucher)

    def _verify_basket_not_empty(self, code):
        username = self.request.user and self.request.user.username
        if self.request.basket.is_empty:
            logger.warning(
                '[Code Redemption Failure] User attempted to apply a code to an empty basket. '
                'User: %s, Basket: %s, Code: %s',
                username, self.request.basket.id, code
            )
            raise VoucherException()

    def _verify_voucher_not_already_applied(self, code):
        username = self.request.user and self.request.user.username
        if self.request.basket.contains_voucher(code):
            logger.warning(
                '[Code Redemption Failure] User tried to apply a code that is already applied. '
                'User: %s, Basket: %s, Code: %s',
                username, self.request.basket.id, code
            )
            messages.error(
                self.request,
                _("You have already added coupon code '{code}' to your basket.").format(code=code),
            )
            raise VoucherException()

    def _verify_email_confirmation(self, voucher, product):
        offer = voucher.best_offer
        email_confirmation_response = render_email_confirmation_if_required(self.request, offer, product)
        if email_confirmation_response:
            # TODO (ARCH-956) support this for the API
            raise VoucherException(response=email_confirmation_response)

    def _verify_enterprise_needs(self, voucher, code, stock_record):
        if get_enterprise_customer_from_voucher(self.request.site, voucher) is not None:
            # The below lines only apply if the voucher that was entered is attached
            # to an EnterpriseCustomer. If that's the case, then rather than following
            # the standard redemption flow, we kick the user out to the `redeem` flow.
            # This flow will handle any additional information that needs to be gathered
            # due to the fact that the voucher is attached to an Enterprise Customer.
            params = urlencode(
                {
                    'code': code,
                    'sku': stock_record.partner_sku,
                    'failure_url': self.request.build_absolute_uri(
                        '{path}?{params}'.format(
                            path=reverse('basket:summary'),
                            params=urlencode(
                                {
                                    CONSENT_FAILED_PARAM: code
                                }
                            )
                        )
                    ),
                }
            )
            redirect_response = HttpResponseRedirect(
                '{path}?{params}'.format(
                    path=reverse('coupons:redeem'),
                    params=params
                )
            )
            raise RedirectException(response=redirect_response)

    def _validate_voucher(self, voucher):
        username = self.request.user and self.request.user.username
        is_valid, message = validate_voucher(voucher, self.request.user, self.request.basket, self.request.site)
        if not is_valid:
            logger.warning('[Code Redemption Failure] The voucher is not valid for this basket. '
                           'User: %s, Basket: %s, Code: %s, Message: %s',
                           username, self.request.basket.id, voucher.code, message)
            messages.error(self.request, message)
            self.request.basket.vouchers.remove(voucher)
            raise VoucherException()

    def _apply_voucher(self, voucher):
        username = self.request.user and self.request.user.username
        valid, message = apply_voucher_on_basket_and_check_discount(voucher, self.request, self.request.basket)
        if not valid:
            logger.warning('[Code Redemption Failure] The voucher could not be applied to this basket. '
                           'User: %s, Basket: %s, Code: %s, Message: %s',
                           username, self.request.basket.id, voucher.code, message)
            messages.warning(self.request, message)
            self.request.basket.vouchers.remove(voucher)
        else:
            messages.info(self.request, message)

    def _get_stock_record(self):
        # TODO: for multiline baskets, select the StockRecord for the product associated
        # specifically with the code that was submitted.
        basket_lines = self.request.basket.all_lines()
        return basket_lines[0].stockrecord

    def _get_voucher(self, code):
        try:
            return self.voucher_model._default_manager.get(code=code)  # pylint: disable=protected-access
        except self.voucher_model.DoesNotExist:
            messages.error(self.request, _("Coupon code '{code}' does not exist.").format(code=code))
            raise VoucherException()


class VoucherAddView(VoucherAddLogicMixin, BaseVoucherAddView):  # pylint: disable=function-redefined
    """
    Deprecated: Adds a voucher to the basket.

    Ensure any changes made here are also made to VoucherAddApiView.
    """
    def form_valid(self, form):
        code = form.cleaned_data['code']

        try:
            self.verify_and_apply_voucher(code)
        except RedirectException as e:
            return e.response
        except VoucherException as e:
            # If a response is provided, return it.
            # All other errors are passed via messages.
            if e.response:
                return e.response

        return redirect_to_referrer(self.request, 'basket:summary')


# TODO: ARCH-960: Remove "pragma: no cover"
class VoucherAddApiView(VoucherAddLogicMixin, PaymentApiLogicMixin, APIView):  # pragma: no cover
    """
    Api for adding voucher to a basket.

    POST:
    """
    permission_classes = (IsAuthenticated,)
    voucher_model = get_model('voucher', 'voucher')

    def post(self, request):  # pylint: disable=unused-argument
        """
        Adds voucher to a basket using the voucher's code.

        Parameters:
        {
            "code": "SUMMER20"
        }

        If successful, adds voucher and returns 200 and the same response as the payment api.
        If unsuccessful, returns 400 with the errors and the same response as the payment api.
        """
        code = request.data.get('code')
        code = code.strip()

        try:
            self.verify_and_apply_voucher(code)
        except RedirectException as e:
            return Response({'redirect': e.response.url})
        except VoucherException:
            # errors are passed via messages object and handled during serialization
            pass

        return self.get_payment_api_response()


class VoucherRemoveApiView(PaymentApiLogicMixin, APIView):
    """
    Api for removing voucher from a basket.

    DELETE /bff/payment/v0/vouchers/{voucherid}
    """
    permission_classes = (IsAuthenticated,)
    voucher_model = get_model('voucher', 'voucher')
    remove_signal = signals.voucher_removal

    def delete(self, request, voucherid):  # pylint: disable=unused-argument
        """
        If successful, removes voucher and returns 200 and the same response as the payment api.
        If unsuccessful, returns 400 with relevant errors and the same response as the payment api.
        """

        # Implementation is a copy of django-oscar's VoucherRemoveView without redirect, and other minor changes.
        # See: https://github.com/django-oscar/django-oscar/blob/3ee66877a2dbd49b2a0838c369205f4ffbc2a391/src/oscar/apps/basket/views.py#L389-L414  pylint: disable=line-too-long

        # Note: This comment is from original django-oscar code.
        # Hacking attempt - the basket must be saved for it to have a voucher in it.
        if self.request.basket.id:
            try:
                voucher = request.basket.vouchers.get(id=voucherid)
            except ObjectDoesNotExist:
                messages.error(self.request, _("No coupon found with id '%s'") % voucherid)
            else:
                self.request.basket.vouchers.remove(voucher)
                self.remove_signal.send(sender=self, basket=self.request.basket, voucher=voucher)

        self._reload_basket()
        return self.get_payment_api_response()

    def _reload_basket(self):
        """
        We need to reload the basket in order for the removal to take effect. There may be a better
        way than doing this.  If so, please update this code.
        """
        strategy = Selector().strategy(request=self.request, user=self.request.user)
        self.request.strategy = strategy
        self.request._basket_cache = None  # pylint: disable=protected-access
        basket_middleware = BasketMiddleware()
        self.request.basket = basket_middleware.get_basket(self.request)
        self.request.basket.strategy = self.request.strategy
        basket_middleware.apply_offers_to_basket(self.request, self.request.basket)
