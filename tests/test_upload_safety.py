import io
import os
os.environ.setdefault('FLASK_SECRET_KEY','test-only-secret-at-least-thirty-two-characters-long')
import unittest
from PIL import Image, PngImagePlugin
from pypdf import PdfWriter
from werkzeug.datastructures import FileStorage
from werkzeug.exceptions import BadRequest
from app import app
from portal import validated_file
from pdf_safety import sanitize_pdf

class UploadSafetyTests(unittest.TestCase):
    def pdf(self,js=False,encrypted=False):
        writer=PdfWriter(); writer.add_blank_page(width=100,height=100)
        if js: writer.add_js('app.alert("test")')
        if encrypted: writer.encrypt('test-password')
        output=io.BytesIO(); writer.write(output); return output.getvalue()

    def test_pdf_active_content_and_encryption_rejected(self):
        with app.test_request_context():
            for content in [self.pdf(js=True),self.pdf(encrypted=True),b'%PDF-1.7 fake %%EOF']:
                with self.assertRaises(BadRequest): sanitize_pdf(content)

    def test_plain_pdf_retains_pages(self):
        from pypdf import PdfReader
        with app.test_request_context(): result=sanitize_pdf(self.pdf())
        self.assertEqual(len(PdfReader(io.BytesIO(result)).pages),1)

    def test_pdf_timeout_is_rejected(self):
        from unittest.mock import patch
        import subprocess
        with app.test_request_context(),patch('pdf_safety.subprocess.run',side_effect=subprocess.TimeoutExpired('worker',10)):
            with self.assertRaises(BadRequest): sanitize_pdf(self.pdf())

    def test_photo_metadata_and_trailing_payload_removed(self):
        photo=io.BytesIO(); metadata=PngImagePlugin.PngInfo(); metadata.add_text('patient','private-exif-marker')
        Image.new('RGB',(32,32),'blue').save(photo,format='PNG',pnginfo=metadata)
        raw=photo.getvalue()+b'<script>polyglot-marker</script>'
        with app.test_request_context():
            name,kind,content=validated_file(FileStorage(stream=io.BytesIO(raw),filename='../photo.png'))
        self.assertEqual(kind,'image/jpeg'); self.assertEqual(name,'photo.jpg')
        self.assertNotIn(b'private-exif-marker',content); self.assertNotIn(b'polyglot-marker',content)
        with Image.open(io.BytesIO(content)) as img: self.assertEqual(img.size,(32,32))
