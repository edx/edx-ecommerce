import logging

from django.utils.translation import ugettext_lazy as _
from oscar.core.loading import get_class, get_model

Applicator = get_class('offer.utils', 'Applicator')
Basket = get_model('basket', 'Basket')
logger = logging.getLogger(__name__)


def prepare_basket(request, product, voucher=None):
    """
    Create or get the basket, add the product, and apply a voucher.

    Existing baskets are merged. The specified product will
    be added to the remaining open basket. If a voucher is passed, all existing
    ones added to the basket are removed because we allow only one voucher per
    basket after the Voucher is applied to the basket.

    Arguments:
        request (Request): The request object made to the view.
        product (Product): Product to be added to the basket.
        voucher (Voucher): Voucher to apply to the basket.

    Returns:
        basket (Basket): Contains the product to be redeemed and the Voucher applied.
    """
    quantity = request.GET.get('qty', 1)
    basket = Basket.get_basket(request.user, request.site)
    basket.flush()
    basket.add_product(product, quantity)
    if voucher:
        for v in basket.vouchers.all():
            basket.vouchers.remove(v)
        basket.vouchers.add(voucher)
        Applicator().apply(basket, request.user, request)
        logger.info('Applied Voucher [%s] to basket [%s].', voucher.code, basket.id)

    return basket


def get_certificate_type_display_value(certificate_type):
    display_values = {
        'audit': _('Audit'),
        'verified': _('Verified'),
        'professional': _('Professional'),
        'honor': _('Honor')
    }

    if certificate_type not in display_values:
        raise ValueError('Certificate Type [%s] not found.', certificate_type)

    return display_values[certificate_type]
