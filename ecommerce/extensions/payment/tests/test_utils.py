from ecommerce.extensions.payment.utils import clean_field_value, middle_truncate
from ecommerce.tests.testcases import TestCase


class UtilsTests(TestCase):
    def test_truncation(self):
        """Verify that the truncation utility behaves as expected."""
        length = 10
        string = 'x' * length

        # Verify that the original string is returned when no truncation is necessary.
        self.assertEqual(string, middle_truncate(string, length))
        self.assertEqual(string, middle_truncate(string, length + 1))

        # Verify that truncation occurs when expected.
        self.assertEqual('xxx...xxx', middle_truncate(string, length - 1))
        self.assertEqual('xx...xx', middle_truncate(string, length - 2))

        self.assertRaises(ValueError, middle_truncate, string, 0)

    def test_clean_field_value(self):
        """ Verify the passed value is cleaned of specific special characters. """
        value = 'Some^text:\'test-value'
        self.assertEqual(clean_field_value(value), 'Sometexttest-value')
