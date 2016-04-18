from __future__ import unicode_literals

import logging
from decimal import Decimal

import dateutil.parser

from django.conf import settings
from django.db import transaction
from django.db.utils import IntegrityError
from django.http import Http404
from django.shortcuts import get_object_or_404
from oscar.core.loading import get_model
from rest_framework import filters, generics, status, viewsets
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.response import Response

from ecommerce.core.models import BusinessClient, User
from ecommerce.extensions.api import data as data_api
from ecommerce.extensions.api.constants import APIConstants as AC
from ecommerce.extensions.api.filters import ProductFilter
from ecommerce.extensions.api.serializers import CategorySerializer, CouponSerializer, CouponListSerializer
from ecommerce.extensions.basket.utils import prepare_basket
from ecommerce.extensions.catalogue.utils import generate_coupon_slug, generate_sku, get_or_create_catalog
from ecommerce.extensions.checkout.mixins import EdxOrderPlacementMixin
from ecommerce.extensions.payment.processors.invoice import InvoicePayment
from ecommerce.extensions.voucher.models import CouponVouchers
from ecommerce.extensions.voucher.utils import (
    create_vouchers,
    update_voucher_offer,
    create_vouchers as utils_create_vouchers
)
from ecommerce.invoice.models import Invoice

Basket = get_model('basket', 'Basket')
Catalog = get_model('catalogue', 'Catalog')
Category = get_model('catalogue', 'Category')
ProductCategory = get_model('catalogue', 'ProductCategory')
logger = logging.getLogger(__name__)
Order = get_model('order', 'Order')
Product = get_model('catalogue', 'Product')
ProductClass = get_model('catalogue', 'ProductClass')
StockRecord = get_model('partner', 'StockRecord')
Voucher = get_model('voucher', 'Voucher')


class CouponViewSet(EdxOrderPlacementMixin, viewsets.ModelViewSet):
    """Endpoint for creating coupons.

    Creates a new coupon product, adds it to a basket and creates a
    new order from that basket.
    """
    queryset = Product.objects.filter(product_class__name='Coupon')
    permission_classes = (IsAuthenticated, IsAdminUser)
    filter_backends = (filters.DjangoFilterBackend, )
    filter_class = ProductFilter

    def get_serializer_class(self):
        if self.action == 'list':
            return CouponListSerializer
        return CouponSerializer

    def create(self, request, *args, **kwargs):
        """Adds coupon to the user's basket.

        Expects request array to contain all the necessary data (listed out below).
        This information is then used to create a coupon product, add to a
        basket and create an order from it.

        Arguments:
            request (HttpRequest): With parameters title, client_username,
            stock_record_ids, start_date, end_date, code, benefit_type, benefit_value,
            voucher_type, quantity, price, category and note in the body.

        Returns:
            200 if the order was created successfully; the basket ID is included in the response
                body along with the order ID and payment information.
            401 if an unauthenticated request is denied permission to access the endpoint.
            429 if the client has made requests at a rate exceeding that allowed by the configured rate limit.
            500 if an error occurs when attempting to create a coupon.
        """
        with transaction.atomic():
            title = request.data[AC.KEYS.TITLE]
            client_username = request.data[AC.KEYS.CLIENT_USERNAME]
            stock_record_ids = request.data[AC.KEYS.STOCK_RECORD_IDS]
            start_date = dateutil.parser.parse(request.data[AC.KEYS.START_DATE])
            end_date = dateutil.parser.parse(request.data[AC.KEYS.END_DATE])
            code = request.data[AC.KEYS.CODE]
            benefit_type = request.data[AC.KEYS.BENEFIT_TYPE]
            benefit_value = request.data[AC.KEYS.BENEFIT_VALUE]
            voucher_type = request.data[AC.KEYS.VOUCHER_TYPE]
            quantity = request.data[AC.KEYS.QUANTITY]
            price = request.data[AC.KEYS.PRICE]
            partner = request.site.siteconfiguration.partner
            categories = Category.objects.filter(id__in=request.data[AC.KEYS.CATEGORY_IDS])
            client, __ = BusinessClient.objects.get_or_create(name=client_username)
            note = request.data.get('note', None)
            max_uses = request.data.get('max_uses', None)
            create_vouchers = request.data.get('create_vouchers', False)

            if max_uses:
                max_uses = int(max_uses)

            # We currently do not support multi-use voucher types.
            if voucher_type == Voucher.MULTI_USE:
                raise NotImplementedError('Multi-use voucher types are not supported')

            # When a black-listed course mode is received raise an exception.
            # Audit modes do not have a certificate type and therefor will raise
            # an AttributeError exception.
            seats = Product.objects.filter(stockrecords__id__in=stock_record_ids)
            for seat in seats:
                try:
                    if seat.attr.certificate_type in settings.BLACK_LIST_COUPON_COURSE_MODES:
                        raise Exception('Course mode not supported')
                except AttributeError:
                    raise Exception('Course mode not supported')

            stock_records_string = ' '.join(str(id) for id in stock_record_ids)

            coupon_catalog, __ = get_or_create_catalog(
                name='Catalog for stock records: {}'.format(stock_records_string),
                partner=partner,
                stock_record_ids=stock_record_ids
            )

            data = {
                'partner': partner,
                'title': title,
                'benefit_type': benefit_type,
                'benefit_value': benefit_value,
                'catalog': coupon_catalog,
                'end_date': end_date,
                'code': code,
                'quantity': quantity,
                'start_date': start_date,
                'voucher_type': voucher_type,
                'categories': categories,
                'note': note,
                'max_uses': max_uses,
                'create_vouchers': create_vouchers,
            }

            coupon_product = self.create_coupon_product(title, price, data)

            basket = prepare_basket(request, coupon_product)

            # Create an order now since payment is handled out of band via an invoice.
            response_data = self.create_order_for_invoice(basket, coupon_id=coupon_product.id, client=client)

            return Response(response_data, status=status.HTTP_200_OK)

    def create_coupon_product(self, title, price, data):
        """Creates a coupon product and a stock record for it.

        Arguments:
            title (str): The name of the coupon.
            price (int): The price of the coupon(s).
            data (dict): Contains data needed to create vouchers,SKU and UPC:
                - partner (User)
                - benefit_type (str)
                - benefit_value (int)
                - catalog (Catalog)
                - end_date (Datetime)
                - code (str)
                - quantity (int)
                - start_date (Datetime)
                - voucher_type (str)
                - categories (list of Category objects)
                - note (str)
                - max_uses (int)

        Returns:
            A coupon product object.

        Raises:
            IntegrityError: An error occured when create_vouchers method returns
                            an IntegrityError exception
        """
        coupon_slug = generate_coupon_slug(title=title, catalog=data['catalog'], partner=data['partner'])

        product_class = ProductClass.objects.get(slug='coupon')
        coupon_product, __ = Product.objects.get_or_create(
            title=title,
            product_class=product_class,
            slug=coupon_slug
        )

        self.assign_categories_to_coupon(coupon=coupon_product, categories=data['categories'])

        # Vouchers are created during order and not fulfillment like usual
        # because we want vouchers to be part of the line in the order.
        if data.get('create_vouchers', False):
            try:
                utils_create_vouchers(
                    name=title,
                    benefit_type=data['benefit_type'],
                    benefit_value=Decimal(data['benefit_value']),
                    catalog=data['catalog'],
                    coupon=coupon_product,
                    end_datetime=data['end_date'],
                    code=data['code'] or None,
                    quantity=int(data['quantity']),
                    start_datetime=data['start_date'],
                    voucher_type=data['voucher_type'],
                    max_uses=data['max_uses'],
                    coupon_id=coupon_product.id
                )
                coupon_vouchers = CouponVouchers.objects.get(coupon=coupon_product)
                coupon_product.attr.coupon_vouchers = coupon_vouchers

            except IntegrityError as ex:
                logger.exception('Failed to create vouchers for [%s] coupon.', coupon_product.title)
                raise IntegrityError(ex)  # pylint: disable=nonstandard-exception

        coupon_product.attr.note = data['note']
        coupon_product.save()

        sku = generate_sku(
            product=coupon_product,
            partner=data['partner'],
            catalog=data['catalog'],
        )

        stock_record, __ = StockRecord.objects.get_or_create(
            product=coupon_product,
            partner=data['partner'],
            partner_sku=sku
        )
        stock_record.price_currency = 'USD'
        stock_record.price_excl_tax = price
        stock_record.save()

        return coupon_product

    def assign_categories_to_coupon(self, coupon, categories):
        """
        Assign categories to a coupon. If a category is already assigned, it will be fetch instead.
        Arguments:
            coupon (Product): Coupon product
            categories (List): List of categories to be assigned to a coupon
        """
        for category in categories:
            ProductCategory.objects.get_or_create(product=coupon, category=category)

    def create_order_for_invoice(self, basket, coupon_id, client):
        """Creates an order from the basket and invokes the invoice payment processor."""
        order_metadata = data_api.get_order_metadata(basket)

        response_data = {
            AC.KEYS.COUPON_ID: coupon_id,
            AC.KEYS.BASKET_ID: basket.id,
            AC.KEYS.ORDER: None,
            AC.KEYS.PAYMENT_DATA: None,
        }
        basket.freeze()

        order = self.handle_order_placement(
            order_number=order_metadata[AC.KEYS.ORDER_NUMBER],
            user=basket.owner,
            basket=basket,
            shipping_address=None,
            shipping_method=order_metadata[AC.KEYS.SHIPPING_METHOD],
            shipping_charge=order_metadata[AC.KEYS.SHIPPING_CHARGE],
            billing_address=None,
            order_total=order_metadata[AC.KEYS.ORDER_TOTAL]
        )

        # Invoice payment processor invocation.
        payment_processor = InvoicePayment
        payment_processor().handle_processor_response(response={}, order=order, business_client=client)
        response_data[AC.KEYS.PAYMENT_DATA] = {
            AC.KEYS.PAYMENT_PROCESSOR_NAME: 'Invoice'
        }

        response_data[AC.KEYS.ORDER] = order.id
        logger.info(
            'Created new order number [%s] from basket [%d]',
            order_metadata[AC.KEYS.ORDER_NUMBER],
            basket.id
        )

        return response_data

    def update(self, request, *args, **kwargs):
        """Update start and end dates of all vouchers associated with the coupon."""
        super(CouponViewSet, self).update(request, *args, **kwargs)

        coupon = self.get_object()
        vouchers = coupon.attr.coupon_vouchers.vouchers
        baskets = Basket.objects.filter(lines__product_id=coupon.id, status=Basket.SUBMITTED)
        data = {}

        for field in AC.UPDATEABLE_VOUCHER_FIELDS:
            self.create_update_data_dict(
                request_data=request.data,
                request_data_key=field['request_data_key'],
                update_dict=data,
                update_dict_key=field['attribute']
            )

        if data:
            vouchers.all().update(**data)

        benefit_value = request.data.get(AC.KEYS.BENEFIT_VALUE, '')
        if benefit_value:
            self.update_coupon_benefit_value(benefit_value=benefit_value, vouchers=vouchers)

        category_ids = request.data.get(AC.KEYS.CATEGORY_IDS, '')
        if category_ids:
            self.update_coupon_category(category_ids=category_ids, coupon=coupon)

        client_username = request.data.get(AC.KEYS.CLIENT_USERNAME, '')
        if client_username:
            self.update_coupon_client(baskets=baskets, client_username=client_username)

        coupon_price = request.data.get(AC.KEYS.PRICE, '')
        if coupon_price:
            StockRecord.objects.filter(product=coupon).update(price_excl_tax=coupon_price)

        note = request.data.get(AC.KEYS.NOTE, None)
        if note is not None:
            coupon.attr.note = note
            coupon.save()

        serializer = self.get_serializer(coupon)
        return Response(serializer.data)

    def create_update_data_dict(self, request_data, request_data_key, update_dict, update_dict_key):
        """
        Adds the value from request data to the update data dictionary
        Arguments:
            request_data (QueryDict): Request data
            request_data_key (str): Request data dictionary key
            update_dict (dict): Dictionary containing the coupon update data
            update_dict_key (str): Update data dictionary key
        """
        value = request_data.get(request_data_key, '')
        if value:
            update_dict[update_dict_key] = value

    def update_coupon_benefit_value(self, benefit_value, vouchers):
        """
        Remove all offers from the vouchers and add a new offer
        Arguments:
            benefit_value (Decimal): Benefit value associated with a new offer
            vouchers (ManyRelatedManager): Vouchers associated with the coupon to be updated
        """
        voucher_offers = vouchers.first().offers
        voucher_offer = voucher_offers.first()

        new_offer = update_voucher_offer(
            offer=voucher_offer,
            benefit_value=benefit_value,
            benefit_type=voucher_offer.benefit.type
        )
        for voucher in vouchers.all():
            voucher.offers.clear()
            voucher.offers.add(new_offer)

    def update_coupon_category(self, category_ids, coupon):
        """
        Remove categories currently assigned to a coupon and assigned new categories
        Arguments:
            category_ids (list): List of category IDs
            coupon (Product): Coupon product to be updated
        """
        new_categories = Category.objects.filter(id__in=category_ids)

        ProductCategory.objects.filter(product=coupon).exclude(category__in=new_categories).delete()

        self.assign_categories_to_coupon(coupon=coupon, categories=new_categories)

    def update_coupon_client(self, baskets, client_username):
        """
        Update Invoice client for new coupons or Basket owner for old coupons
        Arguments:
            baskets (QuerySet): Baskets associated with the coupons
            client_username (str): Client username
        """
        try:
            client, __ = BusinessClient.objects.get_or_create(name=client_username)
            order = get_object_or_404(Order, basket=baskets.first())
            invoices = Invoice.objects.filter(order=order)
            if invoices:
                invoices.update(business_client=client)
            else:
                raise Http404
        except Http404:
            user, __ = User.objects.get_or_create(username=client_username)
            baskets.update(owner=user)

    def destroy(self, request, pk):  # pylint: disable=unused-argument
        try:
            coupon = get_object_or_404(Product, pk=pk)
            self.perform_destroy(coupon)
        except Http404:
            return Response(status=404)
        return Response(status=204)

    def perform_destroy(self, coupon):
        Voucher.objects.filter(coupon_vouchers__coupon=coupon).delete()
        StockRecord.objects.filter(product=coupon).delete()
        coupon.delete()


class CouponCategoriesListView(generics.ListAPIView):
    serializer_class = CategorySerializer

    def get_queryset(self):
        parent_category = Category.objects.get(slug='coupons')
        return parent_category.get_children()
