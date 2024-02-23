from datetime import timedelta
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from testfixtures import LogCapture

from ecommerce.extensions.voucher.models import Voucher


class ManagementCommandTests(TestCase):
    def setUp(self):
        self.voucher_name = 'Test voucher'
        self.data = {
            'name': self.voucher_name,
            'start_datetime': timezone.now(),
            'end_datetime': timezone.now() + timedelta(days=7)
        }
        for item in range(3):
            code = 'TESTCODE' + str(item)
            Voucher.objects.create(code=code, **self.data)

        self.LOGGER_NAME = 'ecommerce.extensions.voucher.management.commands.update_voucher_names'

    @mock.patch('ecommerce.extensions.voucher.tasks.update_voucher_names.delay')
    def test_update_voucher_names_command(self, mock_delay):
        """
        Verify a celery task is spun off when the management command is run.
        """
        call_command('update_voucher_names', batch_size=1, run_async=True)

        assert mock_delay.called is True
        assert mock_delay.call_count == 3

    def test_update_voucher_names_task(self):
        """
        Verify task called in management command updates the voucher names correctly.
        """
        call_command('update_voucher_names', run_async=True)

        vouchers = Voucher.objects.all()
        assert vouchers.count() == 3

        for voucher in vouchers:
            assert voucher.name == f'{voucher.id} - {self.voucher_name}'

    @mock.patch('ecommerce.extensions.voucher.tasks.update_voucher_names.delay')
    def test_update_voucher_names_synchronous(self, mock_delay):
        """
        Verify task is NOT called in management command, but rather, function is
        directly called (and we expect it to still function)
        """
        call_command('update_voucher_names', run_async=False)

        vouchers = Voucher.objects.all()
        assert vouchers.count() == 3

        for voucher in vouchers:
            assert voucher.name == f'{voucher.id} - {self.voucher_name}'

        mock_delay.assert_not_called()

    def test_update_voucher_names_long_name(self):
        """
        Verify task will truncate long voucher names to 128 chars
        """

        # Make voucher have a long name
        voucher_with_long_name = Voucher.objects.first()
        voucher_with_long_name.name = 'a' * 128
        voucher_with_long_name.save()

        call_command('update_voucher_names', run_async=True)

        voucher_with_long_name.refresh_from_db()
        # Note that I have to trim the name here in the test too
        # because the ID of the voucher isn't guaranteed to be 1 char
        # long, so need to dynamically generate what we'd expect the name
        # to be
        expected_name = f'{voucher_with_long_name.id} - {"a" * 128}'[:128]
        assert voucher_with_long_name.name == expected_name
        assert len(voucher_with_long_name.name) == 128

    def test_voucher_name_update_idempotent(self):
        """
        Verify running the management command multiple times ultimately results
        in the same voucher names.
        """
        # Before we run the command
        vouchers = Voucher.objects.all()
        assert vouchers.count() == 3
        for voucher in vouchers:
            assert voucher.name == self.voucher_name

        # And after each time we run the command
        for _ in range(2):
            call_command('update_voucher_names', run_async=True)

            vouchers = Voucher.objects.all()
            for voucher in vouchers:
                assert voucher.name == f'{voucher.id} - {self.voucher_name}'

    @mock.patch('ecommerce.extensions.voucher.tasks.update_voucher_names.delay')
    def test_update_voucher_names_command_failure(self, mock_delay):
        """
        Verify a when celery task fails to spin off, we log an error and continue.
        """
        mock_delay.side_effect = [Exception]

        expected = (
            (
                self.LOGGER_NAME,
                "INFO",
                "Total number of vouchers: 3"
            ),
            (
                self.LOGGER_NAME,
                "ERROR",
                "Error updating voucher names: "
            ),
            (
                self.LOGGER_NAME,
                "INFO",
                "Processed 3 out of 3 vouchers"
            )
        )

        with LogCapture(self.LOGGER_NAME) as lc:
            call_command('update_voucher_names', run_async=True)
            lc.check(*expected)

        mock_delay.assert_called_once()
