from django.test import override_settings
from threadlocals.threadlocals import get_current_request

from ecommerce.core.context_processors import core
from ecommerce.core.url_utils import get_lms_dashboard_url
from ecommerce.tests.testcases import TestCase

SUPPORT_URL = 'example.com'


class CoreContextProcessorTests(TestCase):
    @override_settings(SUPPORT_URL=SUPPORT_URL)
    def test_core(self):
        request = get_current_request()
        self.assertDictEqual(
            core(request),
            {
                'lms_dashboard_url': get_lms_dashboard_url(),
                'platform_name': request.site.name,
                'support_url': SUPPORT_URL
            }
        )
