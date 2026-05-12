import os
from pathlib import Path
from urllib.parse import urlparse

import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / '.env')


def env_bool(name, default=False):
    value = os.getenv(name)

    if value is None:
        return default

    return value.strip().lower() in {'1', 'true', 'yes', 'on'}


def env_list(name, default=''):
    value = os.getenv(name, default)

    return [item.strip() for item in value.split(',') if item.strip()]


def env_host_list(name, default=''):
    hosts = []
    for item in env_list(name, default):
        parsed = urlparse(item)
        host = parsed.netloc or parsed.path
        host = host.split('/')[0]
        if host:
            hosts.append(host)

    return hosts


DJANGO_ENV = os.getenv('DJANGO_ENV', 'development')
IS_PRODUCTION = DJANGO_ENV == 'production' or env_bool('RENDER')
DEBUG = env_bool('DEBUG', not IS_PRODUCTION)

SECRET_KEY = os.getenv('SECRET_KEY')

if not SECRET_KEY:
    if IS_PRODUCTION:
        raise ImproperlyConfigured('SECRET_KEY must be set in production.')
    SECRET_KEY = 'django-insecure-local-development-only-change-me'

ALLOWED_HOSTS = env_host_list('ALLOWED_HOSTS', 'localhost,127.0.0.1')

render_external_hostname = os.getenv('RENDER_EXTERNAL_HOSTNAME')
if render_external_hostname:
    ALLOWED_HOSTS = [*ALLOWED_HOSTS, render_external_hostname]


# Application definition

INSTALLED_APPS = [
    'daphne',
    'corsheaders',
    'channels',
    'rest_framework',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'messaging',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'messenger_service.urls'
ASGI_APPLICATION = 'messenger_service.asgi.application'
WSGI_APPLICATION = 'messenger_service.wsgi.application'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]


# Database
# https://docs.djangoproject.com/en/6.0/ref/settings/#databases

DATABASES = {
    'default': dj_database_url.config(
        default=f'sqlite:///{BASE_DIR / "db.sqlite3"}',
        conn_max_age=600,
        conn_health_checks=True,
    )
}


# Django REST Framework

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
}


# Channels / Render Key Value / Valkey

REDIS_URL = os.getenv('REDIS_URL')

if REDIS_URL:
    CHANNEL_LAYERS = {
        'default': {
            'BACKEND': 'channels_redis.core.RedisChannelLayer',
            'CONFIG': {
                'hosts': [REDIS_URL],
            },
        },
    }
else:
    CHANNEL_LAYERS = {
        'default': {
            'BACKEND': 'channels.layers.InMemoryChannelLayer',
        },
    }


# Cache

if REDIS_URL:
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.redis.RedisCache',
            'LOCATION': REDIS_URL,
            'KEY_PREFIX': 'messenger',
        },
    }
else:
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'messenger-local-cache',
            'KEY_PREFIX': 'messenger',
        },
    }

MESSAGING_ROOM_LIST_CACHE_TTL_SECONDS = int(os.getenv('MESSAGING_ROOM_LIST_CACHE_TTL_SECONDS', '30'))
MESSAGING_ROOM_MESSAGES_CACHE_TTL_SECONDS = int(os.getenv('MESSAGING_ROOM_MESSAGES_CACHE_TTL_SECONDS', '60'))


# Browser and service integration

CORS_ALLOWED_ORIGINS = env_list(
    'CORS_ALLOWED_ORIGINS',
    'http://localhost:5173,http://127.0.0.1:5173',
)
CSRF_TRUSTED_ORIGINS = env_list(
    'CSRF_TRUSTED_ORIGINS',
    'http://localhost:5173,http://127.0.0.1:5173',
)

PARENT_SERVICE_URLS = [url.rstrip('/') for url in env_list('PARENT_SERVICE_URL', 'http://localhost:5000')]
PARENT_SERVICE_URL = PARENT_SERVICE_URLS[0] if PARENT_SERVICE_URLS else ''
INTERNAL_SERVICE_TOKEN = os.getenv('INTERNAL_SERVICE_TOKEN', '')
PARENT_SERVICE_TIMEOUT_SECONDS = int(os.getenv('PARENT_SERVICE_TIMEOUT_SECONDS', '5'))

MESSAGING_JWT_SECRET = os.getenv('MESSAGING_JWT_SECRET', '')
MESSAGING_JWT_ISSUER = os.getenv('MESSAGING_JWT_ISSUER', 'parrot-parent')
MESSAGING_JWT_AUDIENCE = os.getenv('MESSAGING_JWT_AUDIENCE', 'parrot-messenger')
MESSAGING_WS_TOKEN_TTL_SECONDS = int(os.getenv('MESSAGING_WS_TOKEN_TTL_SECONDS', '300'))
CLIENT_MESSAGE_DEDUPE_TTL_SECONDS = int(os.getenv('CLIENT_MESSAGE_DEDUPE_TTL_SECONDS', '600'))


# Password validation
# https://docs.djangoproject.com/en/6.0/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/6.0/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'Asia/Kolkata'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/6.0/howto/static-files/

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
