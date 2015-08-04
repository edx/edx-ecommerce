from __future__ import unicode_literals
import logging
from optparse import make_option

from dateutil.parser import parse
from django.conf import settings
from django.core.management import BaseCommand
from django.db import transaction
import requests
import waffle

from ecommerce.courses.models import Course

logger = logging.getLogger(__name__)


class MigratedCourse(object):
    def __init__(self, course_id):
        self.course, _created = Course.objects.get_or_create(id=course_id)

    def load_from_lms(self, access_token):
        """
        Loads course products from the LMS.

        Loaded data is NOT persisted until the save() method is called.
        """
        name, verification_deadline, modes = self._retrieve_data_from_lms(access_token)

        self.course.name = name
        self.course.verification_deadline = verification_deadline
        self.course.save()

        self._get_products(modes)

    def _build_lms_url(self, path):
        # We avoid using urljoin here because it URL-encodes the path, and some LMS APIs
        # are not capable of decoding these values.
        host = settings.LMS_URL_ROOT.strip('/')
        return '{host}/{path}'.format(host=host, path=path)

    def _query_commerce_api(self, headers):
        """Get course name and verification deadline from the Commerce API."""
        if not settings.COMMERCE_API_URL:
            message = 'Aborting migration. COMMERCE_API_URL is not set.'
            logger.error(message)
            raise Exception(message)

        url = '{}/courses/{}/'.format(settings.COMMERCE_API_URL.rstrip('/'), self.course.id)
        timeout = settings.COMMERCE_API_TIMEOUT

        response = requests.get(url, headers=headers, timeout=timeout)
        if response.status_code != 200:
            raise Exception('Unable to retrieve course name and verification deadline: [{status}] - {body}'.format(
                status=response.status_code,
                body=response.content
            ))

        data = response.json()
        logger.debug(data)

        course_name = data['name']
        if course_name is None:
            message = u'Aborting migration. No name is available for {}.'.format(self.course.id)
            logger.error(message)
            raise Exception(message)

        course_verification_deadline = data['verification_deadline']
        course_verification_deadline = parse(course_verification_deadline) if course_verification_deadline else None

        return course_name, course_verification_deadline

    def _query_enrollment_api(self, headers):
        """Get modes and pricing from Enrollment API."""
        url = self._build_lms_url('api/enrollment/v1/course/{}'.format(self.course.id))
        response = requests.get(url, headers=headers)

        if response.status_code != 200:
            raise Exception('Unable to retrieve course modes: [{status}] - {body}'.format(
                status=response.status_code,
                body=response.content
            ))

        data = response.json()
        logger.debug(data)
        return data['course_modes']

    def _retrieve_data_from_lms(self, access_token):
        """
        Retrieves the course name and modes from the LMS.
        """
        headers = {
            'Accept': 'application/json',
            'Authorization': 'Bearer ' + access_token
        }

        course_name, course_verification_deadline = self._query_commerce_api(headers)
        modes = self._query_enrollment_api(headers)

        return course_name, course_verification_deadline, modes

    def _get_products(self, modes):
        """ Creates/updates course seat products. """
        for mode in modes:
            mode_slug = mode['slug'].lower()
            if mode_slug == 'audit':
                continue

            certificate_type = Course.certificate_type_for_mode(mode_slug)
            id_verification_required = Course.is_mode_verified(mode_slug)
            price = mode['min_price']
            expires = mode.get('expiration_datetime')
            expires = parse(expires) if expires else None
            self.course.create_or_update_seat(certificate_type, id_verification_required, price, expires=expires)


class Command(BaseCommand):
    help = 'Migrate course modes and pricing from LMS to Oscar.'

    option_list = BaseCommand.option_list + (
        make_option('--access_token',
                    action='store',
                    dest='access_token',
                    default=None,
                    help='OAuth2 access token used to authenticate against the LMS APIs.'),
        make_option('--commit',
                    action='store_true',
                    dest='commit',
                    default=False,
                    help='Save the migrated data to the database. If this is not set, '
                         'migrated data will NOT be saved to the database.'),
    )

    def handle(self, *args, **options):
        course_ids = args
        access_token = options.get('access_token')
        if not access_token:
            logger.error('Courses cannot be migrated if no access token is supplied.')
            return

        for course_id in course_ids:
            course_id = unicode(course_id)
            try:
                with transaction.atomic():
                    migrated_course = MigratedCourse(course_id)
                    migrated_course.load_from_lms(access_token)

                    course = migrated_course.course
                    msg = 'Retrieved info for {0} ({1}):\n'.format(course.id, course.name)
                    msg += '\t(cert. type, verified?, price, SKU, slug, expires)\n'

                    for seat in course.seat_products:
                        stock_record = seat.stockrecords.first()
                        data = (seat.attr.certificate_type, seat.attr.id_verification_required,
                                '{0} {1}'.format(stock_record.price_currency, stock_record.price_excl_tax),
                                stock_record.partner_sku, seat.slug, seat.expires)
                        msg += '\t{}\n'.format(data)

                    logger.info(msg)

                    if options.get('commit', False):
                        logger.info('Course [%s] was saved to the database.', course.id)
                        if waffle.switch_is_active('publish_course_modes_to_lms'):
                            course.publish_to_lms()
                        else:
                            logger.info('Data was not published to LMS because the switch '
                                        '[publish_course_modes_to_lms] is disabled.')
                    else:
                        logger.info('Course [%s] was NOT saved to the database.', course.id)
                        raise Exception('Forced rollback.')
            except Exception:  # pylint: disable=broad-except
                logger.exception('Failed to migrate [%s]!', course_id)
