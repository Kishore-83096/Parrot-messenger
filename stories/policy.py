import httpx
from django.conf import settings

from messaging.signals import build_parent_headers, decode_parent_response, label_parent_url


def post_parent_story_policy(path, payload):
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
        policy_url = f'{parent_base_url.rstrip("/")}{path}'

        try:
            response = httpx.post(
                policy_url,
                json=payload,
                headers=build_parent_headers(),
                timeout=settings.PARENT_SERVICE_TIMEOUT_SECONDS,
            )
        except httpx.RequestError as error:
            failed_parent_checks.append(
                {
                    'name': label_parent_url(parent_base_url),
                    'base_url': parent_base_url,
                    'url': policy_url,
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
                'url': policy_url,
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


def resolve_parent_story_audience(payload):
    return post_parent_story_policy('/parent/internal/stories/audience', payload)


def authorize_parent_story_visibility(payload):
    return post_parent_story_policy('/parent/internal/stories/visibility', payload)


def resolve_parent_receipt_visibility(payload):
    return post_parent_story_policy('/parent/internal/receipts/visibility', payload)
