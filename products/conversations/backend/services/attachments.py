"""Shared attachment helpers for conversations channels (email, Slack, etc.)."""

from io import BytesIO

from django.conf import settings

import structlog
from PIL import Image

from posthog.models.team import Team
from posthog.models.uploaded_media import UploadedMedia, save_content_to_object_storage

logger = structlog.get_logger(__name__)

# Strict allowlist of MIME types that may be persisted as uploaded media.
# Anything outside this set — notably text/html, image/svg+xml, and other
# script-capable formats — would be stored and later served from the
# unauthenticated /uploaded_media endpoint, which is an XSS vector.
ALLOWED_ATTACHMENT_CONTENT_TYPES = frozenset(
    {
        # Raster images only — SVG is deliberately excluded because it can
        # contain executable script.
        "image/png",
        "image/jpeg",
        "image/jpg",
        "image/gif",
        "image/webp",
        "image/avif",
        "image/bmp",
        "image/heic",
        "image/heif",
        # Documents
        "application/pdf",
        "text/plain",
        "text/csv",
        "application/zip",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }
)


def _normalize_content_type(content_type: str) -> str:
    """Lowercase and strip any parameters (e.g. ``; charset=utf-8``)."""
    if not content_type:
        return ""
    return content_type.split(";", 1)[0].strip().lower()


def is_allowed_content_type(content_type: str) -> bool:
    return _normalize_content_type(content_type) in ALLOWED_ATTACHMENT_CONTENT_TYPES


def is_valid_image(content: bytes) -> bool:
    """Verify bytes are a real image (prevents serving disguised malicious content).

    Uses the same PIL open + transpose check as the frontend upload API.
    """
    try:
        image = Image.open(BytesIO(content))
        image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        image.close()
        return True
    except Exception:
        return False


def save_file_to_uploaded_media(
    team: Team,
    file_name: str,
    content_type: str,
    content: bytes,
    *,
    validate_images: bool = True,
) -> str | None:
    """Persist a file to object storage via UploadedMedia.

    Returns the absolute URL on success, None on failure.
    For image content types, validates the bytes are a real image unless
    validate_images is False.
    """
    if not settings.OBJECT_STORAGE_ENABLED:
        logger.warning("conversations_attachment_no_object_storage", team_id=team.id)
        return None

    normalized_content_type = _normalize_content_type(content_type)
    if not is_allowed_content_type(normalized_content_type):
        logger.warning(
            "conversations_attachment_disallowed_content_type",
            team_id=team.id,
            file_name=file_name,
            content_type=content_type,
        )
        return None

    if validate_images and normalized_content_type.startswith("image/") and not is_valid_image(content):
        logger.warning("conversations_attachment_invalid_image", team_id=team.id, file_name=file_name)
        return None

    uploaded_media = UploadedMedia.objects.create(
        team=team,
        file_name=file_name,
        content_type=content_type,
        created_by=None,
    )
    try:
        save_content_to_object_storage(uploaded_media, content)
    except Exception as e:
        logger.warning(
            "conversations_attachment_storage_failed",
            team_id=team.id,
            uploaded_media_id=str(uploaded_media.id),
            file_name=file_name,
            error=str(e),
        )
        uploaded_media.delete()
        return None

    logger.info(
        "conversations_attachment_saved",
        team_id=team.id,
        uploaded_media_id=str(uploaded_media.id),
        file_name=file_name,
        content_type=content_type,
        bytes_size=len(content),
    )
    return uploaded_media.get_absolute_url()
