

from django.conf import settings
from rest_framework.throttling import UserRateThrottle


class ServiceUserThrottle(UserRateThrottle):
    """A throttle allowing service users to override rate limiting"""

    def allow_request(self, request, view):
        """Returns True if the request is coming from one of the service users
        and defaults to UserRateThrottle's configured setting otherwise.
        """
        if request.user.username in settings.SERVICE_USERS:
            return True
        return super(ServiceUserThrottle, self).allow_request(request, view)
