"""Serializers for data manipulated by ecommerce API endpoints."""
from decimal import Decimal
import logging

from dateutil.parser import parse
from django.db import transaction
from django.utils.translation import ugettext_lazy as _
from oscar.core.loading import get_model, get_class
from rest_framework import serializers
from rest_framework.reverse import reverse
import waffle

from ecommerce.core.constants import ISO_8601_FORMAT, COURSE_ID_REGEX
from ecommerce.courses.models import Course
from ecommerce.extensions.api.exceptions import LMSPublisherError


logger = logging.getLogger(__name__)

BillingAddress = get_model('order', 'BillingAddress')
Line = get_model('order', 'Line')
Order = get_model('order', 'Order')
Product = get_model('catalogue', 'Product')
ProductAttributeValue = get_model('catalogue', 'ProductAttributeValue')
Refund = get_model('refund', 'Refund')
Selector = get_class('partner.strategy', 'Selector')

COURSE_DETAIL_VIEW = 'api:v2:course-detail'
PRODUCT_DETAIL_VIEW = 'api:v2:product-detail'


class BillingAddressSerializer(serializers.ModelSerializer):
    """Serializes a Billing Address. """
    city = serializers.CharField(max_length=255, source='line4')

    class Meta(object):
        model = BillingAddress
        fields = ('first_name', 'last_name', 'line1', 'line2', 'postcode', 'state', 'country', 'city')


class ProductAttributeValueSerializer(serializers.ModelSerializer):
    """ Serializer for ProductAttributeValue objects. """
    name = serializers.SerializerMethodField()
    value = serializers.SerializerMethodField()

    def get_name(self, instance):
        return instance.attribute.name

    def get_value(self, obj):
        return obj.value

    class Meta(object):
        model = ProductAttributeValue
        fields = ('name', 'value',)


class ProductSerializer(serializers.HyperlinkedModelSerializer):
    """ Serializer for Products. """
    attribute_values = ProductAttributeValueSerializer(many=True, read_only=True)
    product_class = serializers.SerializerMethodField()
    price = serializers.SerializerMethodField()
    is_available_to_buy = serializers.SerializerMethodField()

    def get_product_class(self, product):
        return product.get_product_class().name

    def get_price(self, product):
        info = self._get_info(product)
        if info.availability.is_available_to_buy:
            return serializers.DecimalField(max_digits=10, decimal_places=2).to_representation(info.price.excl_tax)
        return None

    def _get_info(self, product):
        return Selector().strategy(
            request=self.context.get('request')
        ).fetch_for_product(product)

    def get_is_available_to_buy(self, product):
        info = self._get_info(product)
        return info.availability.is_available_to_buy

    class Meta(object):
        model = Product
        fields = ('id', 'url', 'structure', 'product_class', 'title', 'price', 'expires', 'attribute_values',
                  'is_available_to_buy',)
        extra_kwargs = {
            'url': {'view_name': PRODUCT_DETAIL_VIEW},
        }


class LineSerializer(serializers.ModelSerializer):
    """Serializer for parsing line item data."""
    product = ProductSerializer()

    class Meta(object):
        model = Line
        fields = ('title', 'quantity', 'description', 'status', 'line_price_excl_tax', 'unit_price_excl_tax', 'product')


class OrderSerializer(serializers.ModelSerializer):
    """Serializer for parsing order data."""
    date_placed = serializers.DateTimeField(format=ISO_8601_FORMAT)
    lines = LineSerializer(many=True)
    billing_address = BillingAddressSerializer(allow_null=True)

    class Meta(object):
        model = Order
        fields = ('number', 'date_placed', 'status', 'currency', 'total_excl_tax', 'lines', 'billing_address')


class PaymentProcessorSerializer(serializers.Serializer):  # pylint: disable=abstract-method
    """ Serializer to use with instances of processors.BasePaymentProcessor """

    def to_representation(self, instance):
        """ Serialize instances as a string instead of a mapping object. """
        return instance.NAME


class RefundSerializer(serializers.ModelSerializer):
    """ Serializer for Refund objects. """

    class Meta(object):
        model = Refund


class CourseSerializer(serializers.HyperlinkedModelSerializer):
    id = serializers.RegexField(COURSE_ID_REGEX, max_length=255)
    products = ProductSerializer(many=True)
    products_url = serializers.SerializerMethodField()
    last_edited = serializers.SerializerMethodField()

    def __init__(self, *args, **kwargs):
        super(CourseSerializer, self).__init__(*args, **kwargs)

        include_products = kwargs['context'].pop('include_products', False)
        if not include_products:
            self.fields.pop('products', None)

    def get_last_edited(self, obj):
        return obj.history.latest().history_date.strftime(ISO_8601_FORMAT)

    def get_products_url(self, obj):
        return reverse('api:v2:course-product-list', kwargs={'parent_lookup_course_id': obj.id},
                       request=self.context['request'])

    class Meta(object):
        model = Course
        fields = ('id', 'url', 'name', 'verification_deadline', 'type', 'products_url', 'last_edited', 'products')
        read_only_fields = ('type', 'products')
        extra_kwargs = {
            'url': {'view_name': COURSE_DETAIL_VIEW}
        }


class AtomicPublicationSerializer(serializers.Serializer):  # pylint: disable=abstract-method
    """Serializer for saving and publishing a Course and associated products.

    Using a ModelSerializer for the Course data makes it difficult to use this serializer to handle updates.
    The automatically applied validation logic rejects course IDs which already exist in the database.
    """
    id = serializers.RegexField(COURSE_ID_REGEX, max_length=255)
    name = serializers.CharField(max_length=255)
    # Verification deadline should only be required if the course actually requires verification.
    verification_deadline = serializers.DateTimeField(required=False, allow_null=True)
    products = serializers.ListField()

    def __init__(self, *args, **kwargs):
        super(AtomicPublicationSerializer, self).__init__(*args, **kwargs)

        self.access_token = kwargs['context'].pop('access_token')

    def validate_products(self, products):
        """Validate product data."""
        for product in products:
            # Verify that each product is intended to be a Seat.
            product_class = product.get('product_class')
            if product_class != 'Seat':
                raise serializers.ValidationError(
                    _(u"Invalid product class [{product_class}] requested.".format(product_class=product_class))
                )

            # Verify that attributes required to create a Seat are present.
            attrs = self._flatten(product['attribute_values'])
            if attrs.get('id_verification_required') is None:
                raise serializers.ValidationError(_(u"Products must indicate whether ID verification is required."))

            # Verify that a price is present.
            if product.get('price') is None:
                raise serializers.ValidationError(_(u"Products must have a price."))

        return products

    def save(self):
        """Save and publish Course and associated products."

        Returns:
            tuple: A Boolean indicating whether the Course was created, an Exception,
                if one was raised (else None), and a message for the user, if necessary (else None).
        """
        course_id = self.validated_data['id']
        course_name = self.validated_data['name']
        course_verification_deadline = self.validated_data.get('verification_deadline')
        products = self.validated_data['products']

        try:
            if not waffle.switch_is_active('publish_course_modes_to_lms'):
                message = _(
                    u'Course [{course_id}] was not published to LMS '
                    u'because the switch [publish_course_modes_to_lms] is disabled. '
                    u'To avoid ghost SKUs, data has not been saved.'
                ).format(course_id=course_id)

                raise Exception(message)

            # Explicitly delimit operations which will be rolled back if an exception is raised.
            with transaction.atomic():
                course, created = Course.objects.get_or_create(id=course_id)
                course.name = course_name
                course.verification_deadline = course_verification_deadline
                course.save()

                for product in products:
                    attrs = self._flatten(product['attribute_values'])

                    # Extract arguments required for Seat creation, deserializing as necessary.
                    certificate_type = attrs.get('certificate_type', '')
                    id_verification_required = attrs['id_verification_required']
                    price = Decimal(product['price'])

                    # Extract arguments which are optional for Seat creation, deserializing as necessary.
                    expires = product.get('expires')
                    expires = parse(expires) if expires else None
                    credit_provider = attrs.get('credit_provider')
                    credit_hours = attrs.get('credit_hours')
                    credit_hours = int(credit_hours) if credit_hours else None

                    course.create_or_update_seat(
                        certificate_type,
                        id_verification_required,
                        price,
                        expires=expires,
                        credit_provider=credit_provider,
                        credit_hours=credit_hours,
                    )

                resp_message = course.publish_to_lms(access_token=self.access_token)
                published = (resp_message is None)

                if published:
                    return created, None, None
                else:
                    raise LMSPublisherError(resp_message)

        except Exception as e:  # pylint: disable=broad-except
            logger.exception(u'Failed to save and publish [%s]: [%s]', course_id, e.message)
            return False, e, e.message

    def _flatten(self, attrs):
        """Transform a list of attribute names and values into a dictionary keyed on the names."""
        return {attr['name']: attr['value'] for attr in attrs}
