"""
Caching utility middleware.
"""

from ecommerce.cache_utils.utils import RequestCache, TieredCache


class CacheUtilsMiddleware(object):
    """
    Middleware to clear the request cache as appropriate for new requests.
    """
    def process_request(self, request):
        """
        Stores whether or not 'force_django_cache_miss' was supplied in the
        request. Also, clears the request cache.
        """
        RequestCache.clear()
        TieredCache._get_and_set_force_django_cache_miss(request)  # pylint: disable=protected-access

    def process_response(self, request, response):  # pylint: disable=unused-argument
        """
         Clear the request cache after processing a response.
         """
        RequestCache.clear()
        return response

    def process_exception(self, request, exception):  # pylint: disable=unused-argument
        """
        Clear the request cache after a failed request.
        """
        RequestCache.clear()
        return None
