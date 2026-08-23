from __future__ import annotations

import hashlib
import io
import unittest

from PIL import Image

from image_processing import ImageInputError, prepare_uploads


class FakeUpload:
    def __init__(self, name: str, data: bytes):
        self.name = name
        self._data = data

    def getvalue(self) -> bytes:
        return self._data


def _jpeg_bytes(
    *,
    size: tuple[int, int] = (96, 64),
    color: tuple[int, int, int] = (20, 80, 140),
    exif_description: str | None = None,
) -> bytes:
    image = Image.new("RGB", size, color)
    output = io.BytesIO()
    save_options = {"format": "JPEG", "quality": 92}
    if exif_description is not None:
        exif = Image.Exif()
        exif[270] = exif_description
        save_options["exif"] = exif
    image.save(output, **save_options)
    return output.getvalue()


class ImageProcessingTests(unittest.TestCase):
    def test_accepts_many_images_without_a_fixed_count_cap(self):
        uploads = [
            FakeUpload(
                f"photo-{index}.jpg",
                _jpeg_bytes(
                    size=(96 + index, 64),
                    color=((index * 11) % 256, (index * 37) % 256, (index * 73) % 256),
                ),
            )
            for index in range(24)
        ]

        batch = prepare_uploads(uploads)

        self.assertEqual(len(batch.images), 24)
        self.assertEqual(batch.duplicate_names, ())
        self.assertTrue(all(image.mime_type == "image/jpeg" for image in batch.images))

    def test_resizes_to_4k_and_removes_exif(self):
        raw = _jpeg_bytes(
            size=(5000, 400),
            exif_description="private medical photo metadata",
        )

        batch = prepare_uploads([FakeUpload("vitiligo.jpg", raw)])
        prepared = batch.images[0]

        self.assertLessEqual(prepared.width, 4096)
        self.assertLessEqual(prepared.height, 2160)
        with Image.open(io.BytesIO(prepared.data)) as output:
            self.assertFalse(output.getexif())

    def test_skips_duplicates_in_batch_and_existing_session(self):
        first = _jpeg_bytes(color=(1, 2, 3))
        second = _jpeg_bytes(color=(4, 5, 6))
        existing_hash = hashlib.sha256(first).hexdigest()

        batch = prepare_uploads(
            [
                FakeUpload("existing.jpg", first),
                FakeUpload("new.jpg", second),
                FakeUpload("new-copy.jpg", second),
            ],
            existing_source_hashes={existing_hash},
        )

        self.assertEqual([image.name for image in batch.images], ["new.jpg"])
        self.assertEqual(batch.duplicate_names, ("existing.jpg", "new-copy.jpg"))

    def test_rejects_non_image_content(self):
        with self.assertRaisesRegex(ImageInputError, "אינו תמונה"):
            prepare_uploads([FakeUpload("not-an-image.jpg", b"plain text")])


if __name__ == "__main__":
    unittest.main()
