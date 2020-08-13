# -*- coding: utf-8 -*-

from datetime import datetime, timedelta

import six
from django.core.exceptions import ValidationError

from ecommerce.extensions.payment.models import EnterpriseContractMetadata, SDNCheckFailure, SDNFallbackMetadata
from ecommerce.tests.testcases import TestCase


class SDNCheckFailureTests(TestCase):
    def setUp(self):
        super(SDNCheckFailureTests, self).setUp()
        self.full_name = 'Keyser Söze'
        self.username = 'UnusualSuspect'
        self.country = 'US'
        self.sdn_check_response = {'description': 'Looks a bit suspicious.'}

    def test_unicode(self):
        """ Verify the __unicode__ method returns the correct value. """
        basket = SDNCheckFailure.objects.create(
            full_name=self.full_name,
            username=self.username,
            country=self.country,
            site=self.site,
            sdn_check_response=self.sdn_check_response
        )
        expected = 'SDN check failure [{username}]'.format(
            username=self.username
        )

        self.assertEqual(six.text_type(basket), expected)


class EnterpriseContractMetadataTests(TestCase):
    def setUp(self):
        super(EnterpriseContractMetadataTests, self).setUp()
        self.ecm = EnterpriseContractMetadata()

    def test_validate_fixed_value_good(self):
        """
        Verify expected good values do not throw errors on clean.
        """
        self.ecm.discount_type = EnterpriseContractMetadata.FIXED
        good_values = [
            '1234567890',
            '1234567890.23',
            '10000',
            '.45',
        ]
        for value in good_values:
            self.ecm.discount_value = value
            self.ecm.clean()

    def test_validate_fixed_value_bad(self):
        """
        Verify expected bad values throw errors on clean.
        """
        self.ecm.discount_type = EnterpriseContractMetadata.FIXED
        bad_values = [
            '12345678901',
            '12345678901.23',
            '.2345',
            '123.456',

        ]
        for value in bad_values:
            self.ecm.discount_value = value
            with self.assertRaises(ValidationError):
                self.ecm.clean()

    def test_validate_percentage_value_good(self):
        """
        Verify expected good values do not throw errors on clean.
        """
        self.ecm.discount_type = EnterpriseContractMetadata.PERCENTAGE
        good_values = [
            '10.12345',
            '95',
            '12.1',
            '100',
            '100.00000',
        ]
        for value in good_values:
            self.ecm.discount_value = value
            self.ecm.clean()

    def test_validate_percentage_value_bad(self):
        """
        Verify expected bad values throw errors on clean.
        """
        self.ecm.discount_type = EnterpriseContractMetadata.PERCENTAGE
        bad_values = [
            '145123',
            '100.01',
        ]
        for value in bad_values:
            self.ecm.discount_value = value
            with self.assertRaises(ValidationError):
                self.ecm.clean()


class SDNFallbackMetadataTests(TestCase):
    def setUp(self):
        super(SDNFallbackMetadataTests, self).setUp()
        self.file_checksum = 'foobar'
        self.import_timestamp = datetime.now() + timedelta(days=1)

    def test_minimum_requirements(self):
        """Make sure the row is created correctly with the minimum dataset + defaults."""
        new_metadata = SDNFallbackMetadata(
            file_checksum=self.file_checksum,
        )
        new_metadata.full_clean()
        new_metadata.save()

        self.assertEqual(len(SDNFallbackMetadata.objects.all()), 1)

        actual_metadata = SDNFallbackMetadata.objects.all()[0]
        self.assertEqual(actual_metadata.file_checksum, self.file_checksum)
        self.assertIsInstance(actual_metadata.download_timestamp, datetime)
        self.assertEqual(actual_metadata.import_timestamp, None)
        self.assertEqual(actual_metadata.import_state, 'New')

    def test_swap_non_existent_rows(self):
        """The swap function shouldn't break / do anything if there are no existing rows"""

        SDNFallbackMetadata.swap_states()
        self.assertEqual(len(SDNFallbackMetadata.objects.all()), 0)

    def test_swap_two_existing_rows(self):
        """Swap the rows if they already exist"""
        SDNFallbackMetadata.objects.create(
            file_checksum="A",
            import_state="New"
        )
        SDNFallbackMetadata.objects.create(
            file_checksum="B",
            import_state="Current"
        )

        SDNFallbackMetadata.swap_states()

        existing_a_metadata = SDNFallbackMetadata.objects.filter(file_checksum="A")[0]
        self.assertEqual(existing_a_metadata.import_state, 'Current')
        existing_b_metadata = SDNFallbackMetadata.objects.filter(file_checksum="B")[0]
        self.assertEqual(existing_b_metadata.import_state, 'Discard')

    def test_swap_all_rows_exist(self):
        """The swap function should break & no changes should be made if there is an existing Discard row"""
        SDNFallbackMetadata.objects.create(
            file_checksum="A",
            import_state="New"
        )
        SDNFallbackMetadata.objects.create(
            file_checksum="B",
            import_state="Current"
        )
        SDNFallbackMetadata.objects.create(
            file_checksum="C",
            import_state="Discard"
        )

        with self.assertRaises(ValidationError):
            SDNFallbackMetadata.swap_states()

        existing_a_metadata = SDNFallbackMetadata.objects.filter(file_checksum="A")[0]
        self.assertEqual(existing_a_metadata.import_state, 'New')
        existing_b_metadata = SDNFallbackMetadata.objects.filter(file_checksum="B")[0]
        self.assertEqual(existing_b_metadata.import_state, 'Current')
        existing_c_metadata = SDNFallbackMetadata.objects.filter(file_checksum="C")[0]
        self.assertEqual(existing_c_metadata.import_state, 'Discard')
