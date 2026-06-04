from django.conf import settings
from django.db import connection
import httpx
import redis


def check_database():
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
            cursor.fetchone()
    except Exception as error:
        return {
            'ok': False,
            'error': error.__class__.__name__,
        }

    return {
        'ok': True,
        'engine': connection.vendor,
    }


def check_redis():
    if not settings.REDIS_URL:
        return {
            'ok': False,
            'configured': False,
            'error': 'REDIS_URL missing',
        }

    try:
        client = redis.from_url(
            settings.REDIS_URL,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        client.ping()
        client.close()
    except Exception as error:
        return {
            'ok': False,
            'configured': True,
            'error': error.__class__.__name__,
        }

    return {
        'ok': True,
        'configured': True,
    }


def decode_parent_response(response):
    try:
        return response.json()
    except ValueError:
        return {
            'raw': response.text[:500],
        }


def label_parent_url(base_url):
    if base_url.startswith(('http://localhost', 'http://127.0.0.1')):
        return 'local'

    return 'deployed'


def check_single_parent_service(base_url, headers):
    health_url = f'{base_url.rstrip("/")}/health'

    try:
        response = httpx.get(
            health_url,
            headers=headers,
            timeout=settings.PARENT_SERVICE_TIMEOUT_SECONDS,
        )
    except httpx.RequestError as error:
        return {
            'name': label_parent_url(base_url),
            'base_url': base_url,
            'url': health_url,
            'ok': False,
            'error': error.__class__.__name__,
        }

    return {
        'name': label_parent_url(base_url),
        'base_url': base_url,
        'url': health_url,
        'ok': response.is_success,
        'status_code': response.status_code,
        'response': decode_parent_response(response),
    }


def build_parent_headers():
    headers = {}

    if settings.INTERNAL_SERVICE_TOKEN:
        headers['X-Internal-Service-Token'] = settings.INTERNAL_SERVICE_TOKEN

    return headers


def check_parent_services():
    urls = getattr(settings, 'PARENT_SERVICE_URLS', None) or [settings.PARENT_SERVICE_URL]
    headers = build_parent_headers()

    parent_checks = [
        check_single_parent_service(base_url, headers)
        for base_url in urls
        if base_url
    ]
    connected_count = sum(1 for parent in parent_checks if parent['ok'])

    return {
        'ok': bool(parent_checks) and connected_count == len(parent_checks),
        'any_connected': connected_count > 0,
        'configured_count': len(parent_checks),
        'connected_count': connected_count,
        'checks': parent_checks,
    }


def authorize_parent_messaging(payload):
    parent_base_urls = getattr(settings, 'PARENT_SERVICE_URLS', None) or [settings.PARENT_SERVICE_URL]
    parent_base_urls = [url for url in parent_base_urls if url]
    if not parent_base_urls:
        return {
            'ok': False,
            'message': 'PARENT_SERVICE_URL is not configured.',
        }, 503

    failed_parent_checks = []
    last_result = None
    last_status = 503

    for parent_base_url in parent_base_urls:
        authorization_url = f'{parent_base_url.rstrip("/")}/parent/internal/messaging/authorize'

        try:
            response = httpx.post(
                authorization_url,
                json=payload,
                headers=build_parent_headers(),
                timeout=settings.PARENT_SERVICE_TIMEOUT_SECONDS,
            )
        except httpx.RequestError as error:
            failed_parent_checks.append(
                {
                    'name': label_parent_url(parent_base_url),
                    'base_url': parent_base_url,
                    'url': authorization_url,
                    'ok': False,
                    'error': error.__class__.__name__,
                }
            )
            continue

        result = {
            'ok': response.is_success,
            'parent': {
                'name': label_parent_url(parent_base_url),
                'base_url': parent_base_url,
                'url': authorization_url,
                'ok': response.is_success,
                'status_code': response.status_code,
                'response': decode_parent_response(response),
            },
        }
        last_result = result
        last_status = response.status_code

        if response.is_success or response.status_code < 500:
            if failed_parent_checks:
                result['failed_parents'] = failed_parent_checks

            return result, response.status_code

        failed_parent_checks.append(result['parent'])

    if last_result is not None:
        last_result['failed_parents'] = failed_parent_checks
        return last_result, last_status

    return {
        'ok': False,
        'parent': failed_parent_checks[0] if failed_parent_checks else {},
        'failed_parents': failed_parent_checks,
    }, 503


def resolve_parent_presence_visibility(payload):
    parent_base_urls = getattr(settings, 'PARENT_SERVICE_URLS', None) or [settings.PARENT_SERVICE_URL]
    parent_base_urls = [url for url in parent_base_urls if url]
    if not parent_base_urls:
        return {
            'ok': False,
            'message': 'PARENT_SERVICE_URL is not configured.',
        }, 503

    failed_parent_checks = []
    last_result = None
    last_status = 503

    for parent_base_url in parent_base_urls:
        visibility_url = f'{parent_base_url.rstrip("/")}/parent/internal/presence/visibility'

        try:
            response = httpx.post(
                visibility_url,
                json=payload,
                headers=build_parent_headers(),
                timeout=settings.PARENT_SERVICE_TIMEOUT_SECONDS,
            )
        except httpx.RequestError as error:
            failed_parent_checks.append(
                {
                    'name': label_parent_url(parent_base_url),
                    'base_url': parent_base_url,
                    'url': visibility_url,
                    'ok': False,
                    'error': error.__class__.__name__,
                }
            )
            continue

        result = {
            'ok': response.is_success,
            'parent': {
                'name': label_parent_url(parent_base_url),
                'base_url': parent_base_url,
                'url': visibility_url,
                'ok': response.is_success,
                'status_code': response.status_code,
                'response': decode_parent_response(response),
            },
        }
        last_result = result
        last_status = response.status_code

        if response.is_success or response.status_code < 500:
            if failed_parent_checks:
                result['failed_parents'] = failed_parent_checks

            return result, response.status_code

        failed_parent_checks.append(result['parent'])

    if last_result is not None:
        last_result['failed_parents'] = failed_parent_checks
        return last_result, last_status

    return {
        'ok': False,
        'parent': failed_parent_checks[0] if failed_parent_checks else {},
        'failed_parents': failed_parent_checks,
    }, 503
