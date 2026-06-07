from django.conf import settings
import jwt


def validate_messaging_token(authorization_header):
    if not authorization_header:
        return {
            'ok': False,
            'message': 'Missing Authorization header.',
        }, 401

    scheme, _, token = authorization_header.partition(' ')
    if scheme.lower() != 'bearer' or not token:
        return {
            'ok': False,
            'message': 'Authorization header must be Bearer token.',
        }, 401

    if not settings.MESSAGING_JWT_SECRET:
        return {
            'ok': False,
            'message': 'Messaging JWT secret is not configured.',
        }, 500

    try:
        claims = jwt.decode(
            token,
            settings.MESSAGING_JWT_SECRET,
            algorithms=['HS256'],
            issuer=settings.MESSAGING_JWT_ISSUER,
            audience=settings.MESSAGING_JWT_AUDIENCE,
        )
    except jwt.ExpiredSignatureError:
        return {
            'ok': False,
            'message': 'Messaging token has expired.',
        }, 401
    except jwt.InvalidTokenError:
        return {
            'ok': False,
            'message': 'Messaging token is invalid.',
        }, 401

    sender_user_id = claims.get('sub') or claims.get('user_id')
    try:
        sender_user_id = int(sender_user_id)
    except (TypeError, ValueError):
        return {
            'ok': False,
            'message': 'Messaging token is missing a valid sender.',
        }, 401

    return {
        'ok': True,
        'sender_user_id': sender_user_id,
        'username': claims.get('username'),
        'account_number': claims.get('account_number'),
        'expires_at': claims.get('exp'),
    }, 200
