from cloudinary import config as cloudinary_config
from cloudinary import uploader as cloudinary_uploader
from cloudinary.exceptions import Error as CloudinaryError
from django.conf import settings


MAIN_CLOUDINARY_FOLDER = 'MAIN'
MAX_ENCRYPTED_FILE_SIZE_BYTES = 26 * 1024 * 1024


def validation_error(errors):
    return {
        'status': 'error',
        'errors': errors,
    }, 400


def upload_encrypted_file(uploaded_file):
    errors = validate_encrypted_file(uploaded_file)
    if errors:
        return validation_error(errors)

    if not getattr(settings, 'CLOUDINARY_URL', ''):
        return validation_error({
            'file': ['Cloudinary is not configured for encrypted uploads.'],
        })

    cloudinary_config(cloudinary_url=settings.CLOUDINARY_URL, secure=True)

    try:
        if hasattr(uploaded_file, 'seek'):
            uploaded_file.seek(0)
        upload_result = cloudinary_uploader.upload(
            uploaded_file,
            folder=get_encrypted_upload_folder(),
            resource_type='raw',
            use_filename=False,
            unique_filename=True,
            overwrite=False,
        )
    except CloudinaryError as error:
        return validation_error({'file': [str(error) or 'Unable to upload encrypted attachment.']})

    encrypted_file_url = upload_result.get('secure_url') or upload_result.get('url') or ''
    if not encrypted_file_url:
        cleanup_encrypted_upload(upload_result)
        return validation_error({'file': ['Encrypted upload did not return a file URL.']})

    return {
        'status': 'ok',
        'file': {
            'encrypted_file_url': encrypted_file_url,
            'encrypted_file_size_bytes': (
                upload_result.get('bytes')
                or int(getattr(uploaded_file, 'size', 0) or 0)
            ),
        },
    }, 200


def validate_encrypted_file(uploaded_file):
    if not uploaded_file:
        return {'file': ['Encrypted file is required.']}

    file_size = int(getattr(uploaded_file, 'size', 0) or 0)
    max_size = getattr(
        settings,
        'MESSAGING_MAX_ENCRYPTED_UPLOAD_FILE_SIZE_BYTES',
        MAX_ENCRYPTED_FILE_SIZE_BYTES,
    )

    if file_size <= 0:
        return {'file': ['Encrypted file is empty.']}

    if file_size > max_size:
        return {
            'file': [
                f'Encrypted attachment cannot exceed {max_size // (1024 * 1024)} MB.',
            ],
        }

    return None


def get_encrypted_upload_folder():
    root_folder = (
        getattr(settings, 'CLOUDINARY_MAIN_FOLDER', MAIN_CLOUDINARY_FOLDER).strip('/')
        or MAIN_CLOUDINARY_FOLDER
    )
    return f'{root_folder}/e2ee'


def cleanup_encrypted_upload(upload_result):
    public_id = upload_result.get('public_id')
    if not public_id:
        return

    try:
        cloudinary_uploader.destroy(
            public_id,
            resource_type=upload_result.get('resource_type') or 'raw',
        )
    except CloudinaryError:
        pass
