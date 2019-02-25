"""
Middleware supporting JWT Authentication.
"""
import logging

from django.core.cache import cache

from django.contrib.auth.models import Group

from edx_rest_framework_extensions.auth.jwt.cookies import (
    jwt_cookie_name,
)
from edx_rest_framework_extensions.auth.jwt.decoder import jwt_decode_handler
from edx_rest_framework_extensions.auth.jwt.authentication import JwtAuthentication

log = logging.getLogger(__name__)
USE_JWT_COOKIE_HEADER = 'HTTP_USE_JWT_COOKIE'

ROLE_MAPPING = {
    'enterprise_learner': 'enterprise_learner',
    'enterprise_admin': 'enterprise_admin',
}


class JwtAuthCookieRoleMiddleware(object):
    """

    """

    def process_request(self, request):
        """
        Reconstitute the full JWT and add a new cookie on the request object.
        """
        user = request.user
        jwt_cookie = request.COOKIES.get(jwt_cookie_name(), None)
        if not jwt_cookie:
            return

        decoded_jwt = jwt_decode_handler(jwt_cookie)
        if not request.user.is_authenticated:
            user = JwtAuthentication().authenticate_credentials(decoded_jwt)

        roles_claim = decoded_jwt.get('roles')
        role_cache_key = '{user_id}:role_metadata'.format(user_id=user.id)
        previous_role_cache_data = cache.get(role_cache_key) or {}

        if not roles_claim:
            for role in previous_role_cache_data.keys():
                remove_user_role(role, user)
            cache.delete(role_cache_key)
            return

        role_cache_data = {}
        for role_data in roles_claim:
            role, object_type, object_key = role_data.split(':')
            add_user_role(role, user)

            if role not in role_cache_data:
                role_cache_data[role] = []

            role_cache_data[role].append(object_key)

        for role in previous_role_cache_data.keys():
            if role not in role_cache_data:
                remove_user_role(role, user)

        cache.set(role_cache_key, role_cache_data)


def add_user_role(role, user):
    mapped_role = ROLE_MAPPING[role]
    if mapped_role and not user.groups.filter(name=mapped_role).exists():
        group, _ = Group.objects.get_or_create(name=mapped_role)
        group.user_set.add(user)


def remove_user_role(role, user):
    mapped_role = ROLE_MAPPING[role]
    if mapped_role and user.groups.filter(name=mapped_role).exists():
        group, _ = Group.objects.get_or_create(name=mapped_role)
        group.user_set.remove(user)