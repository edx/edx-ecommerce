

from contextlib import contextmanager
import logging

import requests
from typing import Optional
import waffle
from django.contrib import messages
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import redirect
from django.utils.decorators import method_decorator
from django.utils.functional import cached_property
from django.utils.translation import ugettext as _
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import View
from edx_django_utils import monitoring as monitoring_utils
from oscar.apps.partner import strategy
from oscar.apps.payment.exceptions import GatewayError, PaymentError, TransactionDeclined, UserCancelled
from oscar.core.loading import get_class, get_model
from rest_framework import permissions, status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from ecommerce.core.url_utils import absolute_redirect
from ecommerce.extensions.api.serializers import OrderSerializer
from ecommerce.extensions.basket.utils import (
    basket_add_organization_attribute,
    get_payment_microfrontend_or_basket_url
)
from ecommerce.extensions.checkout.mixins import EdxOrderPlacementMixin
from ecommerce.extensions.checkout.utils import get_receipt_page_url
from ecommerce.extensions.payment.exceptions import (
    AuthorizationError,
    DuplicateReferenceNumber,
    ExcessivePaymentForOrderError,
    InvalidBasketError,
    InvalidSignatureError,
    RedundantPaymentNotificationError
)
from ecommerce.extensions.payment.processors.cybersource import Cybersource, CybersourceREST
from ecommerce.extensions.payment.utils import checkSDN, clean_field_value
from ecommerce.extensions.payment.views import BasePaymentSubmitView

logger = logging.getLogger(__name__)

Applicator = get_class('offer.applicator', 'Applicator')
Basket = get_model('basket', 'Basket')
BasketAttribute = get_model('basket', 'BasketAttribute')
BasketAttributeType = get_model('basket', 'BasketAttributeType')
BillingAddress = get_model('order', 'BillingAddress')
BUNDLE = 'bundle_identifier'
Country = get_model('address', 'Country')
NoShippingRequired = get_class('shipping.methods', 'NoShippingRequired')
Order = get_model('order', 'Order')
OrderNumberGenerator = get_class('order.utils', 'OrderNumberGenerator')
OrderTotalCalculator = get_class('checkout.calculators', 'OrderTotalCalculator')
PaymentProcessorResponse = get_model('payment', 'PaymentProcessorResponse')


class CyberSourceProcessorMixin:
    @cached_property
    def payment_processor(self):
        return Cybersource(self.request.site)


class CybersourceOrderInitiationView:
    """
    A baseclass that includes pre-work before submitting an order to cybersource for
    payment validation.
    """

    def check_sdn(self, request, data):
        """
        Check that the supplied request and form data passes SDN checks.

        Returns:
            JsonResponse with an error if the SDN check fails, or None if it succeeds.
        """
        hit_count = checkSDN(
            request,
            data['first_name'] + ' ' + data['last_name'],
            data['city'],
            data['country'])

        if hit_count > 0:
            logger.info(
                'SDNCheck function called for basket [%d]. It received %d hit(s).',
                request.basket.id,
                hit_count,
            )
            response_to_return = {
                'error': 'There was an error submitting the basket',
                'sdn_check_failure': {'hit_count': hit_count}}

            return JsonResponse(response_to_return, status=403)

        logger.info(
            'SDNCheck function called for basket [%d]. It did not receive a hit.',
            request.basket.id,
        )


class CybersourceSubmitView(BasePaymentSubmitView, CybersourceOrderInitiationView):
    """ Starts CyberSource payment process.

    This view is intended to be called asynchronously by the payment form. The view expects POST data containing a
    `Basket` ID. The specified basket is frozen, and CyberSource parameters are returned as a JSON object.
    """
    FIELD_MAPPINGS = {
        'city': 'bill_to_address_city',
        'country': 'bill_to_address_country',
        'address_line1': 'bill_to_address_line1',
        'address_line2': 'bill_to_address_line2',
        'postal_code': 'bill_to_address_postal_code',
        'state': 'bill_to_address_state',
        'first_name': 'bill_to_forename',
        'last_name': 'bill_to_surname',
    }

    def form_valid(self, form):
        data = form.cleaned_data
        basket = data['basket']
        request = self.request
        user = request.user

        sdn_check_failure = self.check_sdn(request, data)
        if sdn_check_failure is not None:
            return sdn_check_failure

        # Add extra parameters for Silent Order POST
        extra_parameters = {
            'payment_method': 'card',
            'unsigned_field_names': ','.join(Cybersource.PCI_FIELDS),
            'bill_to_email': user.email,
            # Fall back to order number when there is no session key (JWT auth)
            'device_fingerprint_id': request.session.session_key or basket.order_number,
        }

        for source, destination in self.FIELD_MAPPINGS.items():
            extra_parameters[destination] = clean_field_value(data[source])

        parameters = Cybersource(self.request.site).get_transaction_parameters(
            basket,
            use_client_side_checkout=True,
            extra_parameters=extra_parameters
        )

        logger.info(
            'Parameters signed for CyberSource transaction [%s], associated with basket [%d].',
            # TODO: transaction_id is None in logs. This should be fixed.
            parameters.get('transaction_id'),
            basket.id
        )

        # This parameter is only used by the Web/Mobile flow. It is not needed for for Silent Order POST.
        parameters.pop('payment_page_url', None)

        # Ensure that the response can be properly rendered so that we
        # don't have to deal with thawing the basket in the event of an error.
        response = JsonResponse({'form_fields': parameters})

        basket_add_organization_attribute(basket, data)

        # Freeze the basket since the user is paying for it now.
        basket.freeze()

        return response


class CybersourceSubmitAPIView(APIView, CybersourceSubmitView):
    # DRF APIView wrapper which allows clients to use
    # JWT authentication when making Cybersource submit
    # requests.
    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request):
        logger.info(
            '%s called for basket [%d]. It is in the [%s] state.',
            self.__class__.__name__,
            request.basket.id,
            request.basket.status
        )
        return super(CybersourceSubmitAPIView, self).post(request)


class CybersourceOrderCompletionView(EdxOrderPlacementMixin):
    """
    A baseclass that includes error handling and financial reporting for orders placed via
    CyberSource.
    """

    transaction_id: Optional[str] = None
    order_number: Optional[str] = None
    basket_id: Optional[int] = None

    def _log_cybersource_payment_failure(
            self, exception, basket, order_number, transaction_id, ppr, notification_msg=None,
            message_prefix=None, logger_function=None
    ):
        """ Logs standard payment response as exception log unless logger_function supplied. """
        message_prefix = message_prefix + ' ' if message_prefix else ''
        logger_function = logger_function if logger_function else logger.exception
        # pylint: disable=logging-not-lazy
        logger_function(
            message_prefix +
            'CyberSource payment failed due to [%s] for transaction [%s], order [%s], and basket [%d]. '
            'The complete payment response [%s] was recorded in entry [%d].',
            exception.__class__.__name__,
            transaction_id,
            order_number,
            basket.id,
            notification_msg or "Unknown Error",
            ppr.id
        )

    @contextmanager
    def log_payment_exceptions(self, basket, order_number, transaction_id, ppr, notification_msg=None):
        try:
            yield
        except (UserCancelled, TransactionDeclined, AuthorizationError) as exception:
            self._log_cybersource_payment_failure(
                exception, basket, order_number, transaction_id, ppr, notification_msg,
                logger_function=logger.info,
            )
            exception.unlogged = False
            raise
        except DuplicateReferenceNumber as exception:
            logger.info(
                'Received CyberSource payment notification for basket [%d] which is associated '
                'with existing order [%s]. No payment was collected, and no new order will be created.',
                basket.id,
                order_number
            )
            exception.unlogged = False
            raise
        except RedundantPaymentNotificationError as exception:
            logger.info(
                'Received redundant CyberSource payment notification with same transaction ID for basket [%d] '
                'which is associated with an existing order [%s]. No payment was collected.',
                basket.id,
                order_number
            )
            exception.unlogged = False
            raise
        except ExcessivePaymentForOrderError as exception:
            logger.info(
                'Received duplicate CyberSource payment notification with different transaction ID for basket '
                '[%d] which is associated with an existing order [%s]. Payment collected twice, request a '
                'refund.',
                basket.id,
                order_number
            )
            exception.unlogged = False
            raise
        except InvalidSignatureError as exception:
            self._log_cybersource_payment_failure(
                exception, basket, order_number, transaction_id, ppr, notification_msg,
                message_prefix='CyberSource response was invalid.',
            )
            exception.unlogged = False
            raise
        except (PaymentError, Exception) as exception:
            self._log_cybersource_payment_failure(
                exception, basket, order_number, transaction_id, ppr, notification_msg,
            )
            exception.unlogged = False
            raise

    def set_payment_response_custom_metrics(self, basket, notification, order_number, ppr, transaction_id):
        # IMPORTANT: Do not set metric for the entire `notification`, because it includes PII.
        #   It is accessible using the `payment_response_record_id` if needed.
        monitoring_utils.set_custom_metric('payment_response_processor_name', 'cybersource')
        monitoring_utils.set_custom_metric('payment_response_basket_id', basket.id)
        monitoring_utils.set_custom_metric('payment_response_order_number', order_number)
        monitoring_utils.set_custom_metric('payment_response_transaction_id', transaction_id)
        monitoring_utils.set_custom_metric('payment_response_record_id', ppr.id)
        # For reason_code, see https://support.cybersource.com/s/article/What-does-this-response-code-mean#code_table
        reason_code = notification.get("reason_code", "not-found")
        monitoring_utils.set_custom_metric('payment_response_reason_code', reason_code)
        payment_response_message = notification.get("message", 'Unknown Error')
        monitoring_utils.set_custom_metric('payment_response_message', payment_response_message)

    # Note: method has too-many-statements, but it enables tracking that all exception handling gets logged
    def validate_order_completion(self, order_completion_message):  # pylint: disable=too-many-statements
        # Note (CCB): Orders should not be created until the payment processor has validated the response's signature.
        # This validation is performed in the handle_payment method. After that method succeeds, the response can be
        # safely assumed to have originated from CyberSource.
        basket = None
        order_completion_message = order_completion_message or {}

        try:

            try:
                logger.info(
                    'Received CyberSource payment notification for transaction [%s], associated with order [%s]'
                    ' and basket [%d].',
                    self.transaction_id,
                    self.order_number,
                    self.basket_id
                )

                basket = self._get_basket(self.basket_id)

                if not basket:
                    error_message = (
                        'Received CyberSource payment notification for non-existent basket [%s].' % self.basket_id
                    )
                    logger.error(error_message)
                    exception = InvalidBasketError(error_message)
                    exception.unlogged = False
                    raise exception

                if basket.status != Basket.FROZEN:
                    # We don't know how serious this situation is at this point, hence
                    # the INFO level logging. This notification is most likely CyberSource
                    # telling us that they've declined an attempt to pay for an existing order.
                    logger.info(
                        'Received CyberSource payment notification for basket [%d] which is in a non-frozen state,'
                        ' [%s]',
                        basket.id, basket.status
                    )
            finally:
                # Store the response in the database regardless of its authenticity.
                ppr = self.payment_processor.record_processor_response(
                    order_completion_message, transaction_id=self.transaction_id, basket=basket
                )
                self.set_payment_response_custom_metrics(basket, order_completion_message, self.order_number, ppr, self.transaction_id)

            # Explicitly delimit operations which will be rolled back if an exception occurs.
            with transaction.atomic():
                with self.log_payment_exceptions(
                        basket,
                        self.order_number,
                        self.transaction_id,
                        ppr,
                        order_completion_message.get("message")
                ):
                    self.handle_payment(order_completion_message, basket)

        except Exception as exception:  # pylint: disable=bare-except
            if getattr(exception, 'unlogged', True):
                logger.exception(
                    'Unhandled exception processing CyberSource payment notification for transaction [%s], order [%s], '
                    'and basket [%d].',
                    self.transaction_id,
                    self.order_number,
                    self.basket_id
                )
            raise

        return basket


class CyberSourceRESTProcessorMixin:
    @cached_property
    def payment_processor(self):
        return CybersourceREST(
            self.request.site,
            self.request.POST['payment_token'],
            # We save the capture context in the session and recall it here since we can't trust the front-end
            self.request.session['capture_context']
        )


class CybersourceAuthorizeAPIView(
        APIView,
        BasePaymentSubmitView,
        CyberSourceRESTProcessorMixin,
        CybersourceOrderCompletionView,
        CybersourceOrderInitiationView
):
    # DRF APIView wrapper which allows clients to use
    # JWT authentication when making Cybersource submit
    # requests.
    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request):
        logger.info(
            '%s called for basket [%d]. It is in the [%s] state.',
            self.__class__.__name__,
            request.basket.id,
            request.basket.status
        )
        return super(CybersourceAuthorizeAPIView, self).post(request)

    def form_valid(self, form):
        data = form.cleaned_data
        basket = data['basket']
        request = self.request

        sdn_check_failure = self.check_sdn(request, data)
        if sdn_check_failure is not None:
            return sdn_check_failure

        try:
            payment_processor_response, transaction_id = self.payment_processor.initiate_payment(basket, request, data)
            self.record_processor_response(
                payment_processor_response.to_dict(),
                transaction_id=transaction_id,
                basket=basket
            )
            handled_processor_response = self.payment_processor.handle_processor_response(
                payment_processor_response,
                basket
            )
        except GatewayError:
            return JsonResponse({}, status=400)
        except TransactionDeclined:
            return JsonResponse({
                'errors': [
                    {'error_code': 'transaction-declined-message'}
                ]
            }, status=400)

        billing_address = BillingAddress(
            first_name=data['first_name'],
            last_name=data['last_name'],
            line1=data['address_line1'],
            line2=data['address_line2'],
            line4=data['city'],
            postcode=data['postal_code'],
            state=data['state'],
            country=Country.objects.get(iso_3166_1_a2=data['country'])
        )

        with transaction.atomic():
            basket.freeze()
            self.record_payment(basket, handled_processor_response)
            order = self.create_order(request, basket, billing_address)
            self.handle_post_order(order)

        receipt_page_url = get_receipt_page_url(
            request.site.siteconfiguration,
            order_number=basket.order_number,
            disable_back_button=True,
        )
        return JsonResponse({
            'receipt_page_url': receipt_page_url,
        }, status=201)


class CybersourceInterstitialView(CyberSourceProcessorMixin, CybersourceOrderCompletionView, View):
    """
    Interstitial view for Cybersource Payments.

    Side effect:
        Sets the custom metric ``payment_response_validation`` to one of the following:
            'success', 'redirect-to-receipt', 'redirect-to-payment-page', 'redirect-to-error-page'

    """

    # Disable atomicity for the view. Otherwise, we'd be unable to commit to the database
    # until the request had concluded; Django will refuse to commit when an atomic() block
    # is active, since that would break atomicity. Without an order present in the database
    # at the time fulfillment is attempted, asynchronous order fulfillment tasks will fail.
    @method_decorator(transaction.non_atomic_requests)
    @method_decorator(csrf_exempt)
    def dispatch(self, request, *args, **kwargs):
        return super(CybersourceInterstitialView, self).dispatch(request, *args, **kwargs)

    def _get_billing_address(self, cybersource_response):
        field = 'req_bill_to_address_line1'
        # Address line 1 is optional if flag is enabled
        line1 = (cybersource_response.get(field, '')
                 if waffle.switch_is_active('optional_location_fields')
                 else cybersource_response[field])
        return BillingAddress(
            first_name=cybersource_response['req_bill_to_forename'],
            last_name=cybersource_response['req_bill_to_surname'],
            line1=line1,

            # Address line 2 is optional
            line2=cybersource_response.get('req_bill_to_address_line2', ''),

            # Oscar uses line4 for city
            line4=cybersource_response['req_bill_to_address_city'],
            # Postal code is optional
            postcode=cybersource_response.get('req_bill_to_address_postal_code', ''),
            # State is optional
            state=cybersource_response.get('req_bill_to_address_state', ''),
            country=Country.objects.get(
                iso_3166_1_a2=cybersource_response['req_bill_to_address_country']))

    def _get_basket(self, basket_id):
        if not basket_id:
            return None

        try:
            basket_id = int(basket_id)
            basket = Basket.objects.get(id=basket_id)
            basket.strategy = strategy.Default()

            Applicator().apply(basket, basket.owner, self.request)
            logger.info(
                'Applicator applied, basket id: [%s]',
                basket.id)
            return basket
        except (ValueError, ObjectDoesNotExist) as error:
            logger.warning(
                'Could not get basket--error: [%s]',
                str(error))
            return None

    def post(self, request, *args, **kwargs):  # pylint: disable=unused-argument
        """Process a CyberSource merchant notification and place an order for paid products as appropriate."""
        notification = request.POST.dict()
        self.transaction_id = notification.get('transaction_id')
        self.order_number = notification.get('req_reference_number')

        try:
            self.basket_id = OrderNumberGenerator().basket_id(self.order_number)
        except:  # pylint: disable=bare-except
            logger.exception(
                'Error generating basket_id from CyberSource notification with transaction [%s] and order [%s].',
                self.transaction_id,
                self.order_number,
            )
            return self.redirect_to_payment_error()

        try:
            basket = self.validate_order_completion(notification)
            monitoring_utils.set_custom_metric('payment_response_validation', 'success')
        except DuplicateReferenceNumber:
            # CyberSource has told us that they've declined an attempt to pay
            # for an existing order. If this happens, we can redirect the browser
            # to the receipt page for the existing order.
            monitoring_utils.set_custom_metric('payment_response_validation', 'redirect-to-receipt')
            return self.redirect_to_receipt_page(notification)
        except TransactionDeclined:
            # Declined transactions are the most common cause of errors during payment
            # processing and tend to be easy to correct (e.g., an incorrect CVV may have
            # been provided). The recovery path is not as clear for other exceptions,
            # so we let those drop through to the payment error page.
            self._merge_old_basket_into_new(request)

            messages.error(self.request, _('transaction declined'), extra_tags='transaction-declined-message')

            monitoring_utils.set_custom_metric('payment_response_validation', 'redirect-to-payment-page')
            # TODO:
            # 1. There are sometimes messages from CyberSource that would make a more helpful message for users.
            # 2. We could have similar handling of other exceptions like UserCancelled and AuthorizationError

            redirect_url = get_payment_microfrontend_or_basket_url(self.request)
            return HttpResponseRedirect(redirect_url)

        except:  # pylint: disable=bare-except
            # logging handled by validate_order_completion, because not all exceptions are problematic
            monitoring_utils.set_custom_metric('payment_response_validation', 'redirect-to-error-page')
            return absolute_redirect(request, 'payment_error')

        try:
            order = self.create_order(request, basket, self._get_billing_address(notification))
            self.handle_post_order(order)
            return self.redirect_to_receipt_page(notification)
        except:  # pylint: disable=bare-except
            logger.exception(
                'Error processing order for transaction [%s], with order [%s] and basket [%d].',
                self.transaction_id,
                self.order_number,
                self.basket_id
            )
            return absolute_redirect(request, 'payment_error')

    def _merge_old_basket_into_new(self, request):
        """
        Upon declined transaction merge old basket into new one and also copy bundle attibute
        over to new basket if any.
        """
        order_number = request.POST.get('req_reference_number')
        old_basket_id = OrderNumberGenerator().basket_id(order_number)
        old_basket = Basket.objects.get(id=old_basket_id)

        bundle_attributes = BasketAttribute.objects.filter(
            basket=old_basket,
            attribute_type=BasketAttributeType.objects.get(name=BUNDLE)
        )
        bundle = bundle_attributes.first().value_text if bundle_attributes.count() > 0 else None

        new_basket = Basket.objects.create(owner=old_basket.owner, site=request.site)

        # We intentionally avoid thawing the old basket here to prevent order
        # numbers from being reused. For more, refer to commit a1efc68.
        new_basket.merge(old_basket, add_quantities=False)
        if bundle:
            BasketAttribute.objects.update_or_create(
                basket=new_basket,
                attribute_type=BasketAttributeType.objects.get(name=BUNDLE),
                defaults={'value_text': bundle}
            )

        logger.info(
            'Created new basket [%d] from old basket [%d] for declined transaction with bundle [%s].',
            new_basket.id,
            old_basket_id,
            bundle
        )

    def redirect_to_receipt_page(self, notification):
        receipt_page_url = get_receipt_page_url(
            self.request.site.siteconfiguration,
            order_number=notification.get('req_reference_number'),
            disable_back_button=True,
        )

        return redirect(receipt_page_url)


class ApplePayStartSessionView(CyberSourceProcessorMixin, APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request):
        url = request.data.get('url')
        if not url:
            raise ValidationError({'error': 'url is required'})

        # The domain name sent to Apple Pay needs to match the domain name of the frontend.
        # We use a URL parameter to indicate whether the new Payment microfrontend was used to
        # make this request - since the use of the new microfrontend is request-specific - depends
        # on the state of the waffle toggle, the user's A/B test bucket, and user's toggle choice.
        #
        # As an alternative implementation, one can look at the domain of the requesting client,
        # instead of relying on this boolean URL parameter. We are going with a URL parameter since
        # it is simplest for testing at this time.
        if request.data.get('is_payment_microfrontend'):
            domain_name = request.site.siteconfiguration.payment_domain_name
        else:
            domain_name = request.site.domain

        data = {
            'merchantIdentifier': self.payment_processor.apple_pay_merchant_identifier,
            'domainName': domain_name,
            'displayName': request.site.name,
        }

        response = requests.post(url, json=data, cert=self.payment_processor.apple_pay_merchant_id_certificate_path)

        if response.status_code > 299:
            logger.warning('Failed to start Apple Pay session. [%s] returned status [%d] with content %s',
                           url, response.status_code, response.content)

        return JsonResponse(response.json(), status=response.status_code)


class CybersourceApplePayAuthorizationView(CyberSourceProcessorMixin, EdxOrderPlacementMixin, APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def _get_billing_address(self, apple_pay_payment_contact):
        """ Converts ApplePayPaymentContact object to BillingAddress.

        See https://developer.apple.com/documentation/applepayjs/applepaypaymentcontact.
        """
        address_lines = apple_pay_payment_contact['addressLines']
        address_line_2 = address_lines[1] if len(address_lines) > 1 else ''
        country_code = apple_pay_payment_contact.get('countryCode')

        try:
            country = Country.objects.get(iso_3166_1_a2__iexact=country_code)
        except Country.DoesNotExist:
            logger.warning('Country matching code [%s] does not exist.', country_code)
            raise

        return BillingAddress(
            first_name=apple_pay_payment_contact['givenName'],
            last_name=apple_pay_payment_contact['familyName'],
            line1=address_lines[0],

            # Address line 2 is optional
            line2=address_line_2,

            # Oscar uses line4 for city
            line4=apple_pay_payment_contact['locality'],
            # Postal code is optional
            postcode=apple_pay_payment_contact.get('postalCode', ''),
            # State is optional
            state=apple_pay_payment_contact.get('administrativeArea', ''),
            country=country)

    def post(self, request):
        basket = request.basket

        if not request.data.get('token'):
            raise ValidationError({'error': 'token_missing'})

        try:
            billing_address = self._get_billing_address(request.data.get('billingContact'))
        except Exception:
            logger.exception(
                'Failed to authorize Apple Pay payment. An error occurred while parsing the billing address.')
            raise ValidationError({'error': 'billing_address_invalid'})

        try:
            self.handle_payment(None, basket)
        except GatewayError:
            return Response({'error': 'payment_failed'}, status=status.HTTP_502_BAD_GATEWAY)

        order = self.create_order(request, basket, billing_address=billing_address)
        return Response(OrderSerializer(order, context={'request': request}).data, status=status.HTTP_201_CREATED)

    def handle_payment(self, response, basket):
        request = self.request
        basket = request.basket
        billing_address = self._get_billing_address(request.data.get('billingContact'))
        token = request.data['token']

        handled_processor_response = self.payment_processor.request_apple_pay_authorization(
            basket, billing_address, token)
        self.record_payment(basket, handled_processor_response)
