from posthog.test.base import BaseTest
from unittest.mock import patch

from parameterized import parameterized

from posthog.models.uploaded_media import UploadedMedia

from products.conversations.backend.services.attachments import save_file_to_uploaded_media


class TestSaveFileToUploadedMedia(BaseTest):
    @parameterized.expand(
        [
            ("text_html", "text/html"),
            ("text_html_charset", "text/html; charset=utf-8"),
            ("application_xhtml", "application/xhtml+xml"),
            ("image_svg", "image/svg+xml"),
            ("application_xml", "application/xml"),
            ("text_xml", "text/xml"),
            ("application_javascript", "application/javascript"),
            ("text_javascript", "text/javascript"),
            ("mixed_case_html", "Text/HTML"),
            ("empty", ""),
            ("missing_slash", "garbage"),
        ]
    )
    @patch("products.conversations.backend.services.attachments.save_content_to_object_storage")
    def test_rejects_unsafe_content_type(self, _name: str, content_type: str, mock_storage) -> None:
        with self.settings(OBJECT_STORAGE_ENABLED=True):
            url = save_file_to_uploaded_media(
                team=self.team,
                file_name="evil.html",
                content_type=content_type,
                content=b"<script>alert(1)</script>",
            )
        assert url is None
        assert UploadedMedia.objects.count() == 0
        mock_storage.assert_not_called()

    @parameterized.expand(
        [
            ("png", "image/png"),
            ("jpeg", "image/jpeg"),
            ("gif", "image/gif"),
            ("webp", "image/webp"),
            ("pdf", "application/pdf"),
            ("text_plain", "text/plain"),
            ("csv", "text/csv"),
            ("zip", "application/zip"),
            ("msword", "application/msword"),
        ]
    )
    @patch("products.conversations.backend.services.attachments.save_content_to_object_storage")
    @patch("products.conversations.backend.services.attachments.is_valid_image", return_value=True)
    def test_allows_safe_content_type(self, _name: str, content_type: str, _mock_image, _mock_storage) -> None:
        with self.settings(OBJECT_STORAGE_ENABLED=True):
            url = save_file_to_uploaded_media(
                team=self.team,
                file_name="file.bin",
                content_type=content_type,
                content=b"hello",
            )
        assert url is not None
        assert UploadedMedia.objects.filter(content_type=content_type).exists()
