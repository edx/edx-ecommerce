from ecommerce.extensions.payment.processors import BasePaymentProcessor


class DummyProcessor(BasePaymentProcessor):
    NAME = 'dummy'

    def get_transaction_parameters(self, basket, request=None, use_client_side_checkout=False, **kwargs):
        pass

    def handle_processor_response(self, response, basket=None):
        pass

    def is_signature_valid(self, response):
        pass

    def issue_credit(self, transaction_id, amount, currency):
        pass


class AnotherDummyProcessor(DummyProcessor):
    NAME = 'another-dummy'
