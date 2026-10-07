"""Bounded, isolated PDF validation. This is not an antivirus scanner."""
import io
import subprocess
import sys
from pathlib import Path

MAX_PDF = 5 * 1024 * 1024


def sanitize_pdf(content):
    from flask import abort
    try:
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--worker'],
                                input=content, capture_output=True, timeout=10, check=False)
        if result.returncode or not result.stdout.startswith(b'%PDF-') or len(result.stdout)>MAX_PDF:
            raise ValueError
        return result.stdout
    except (OSError, subprocess.TimeoutExpired, ValueError):
        abort(400, description='PDF пошкоджений, надто складний або містить активні елементи. Завантажте звичайний PDF без форм, скриптів і вкладень.')


def validate(content):
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import DictionaryObject, ArrayObject, IndirectObject
    reader = PdfReader(io.BytesIO(content), strict=True)
    if reader.is_encrypted: raise ValueError('encrypted')
    forbidden={'/JS','/JavaScript','/OpenAction','/AA','/EmbeddedFiles','/EmbeddedFile','/Launch',
               '/Movie','/Sound','/Rendition','/RichMedia','/XFA','/AcroForm','/SubmitForm','/ImportData','/GoToR','/GoToE'}
    stack=[reader.trailer]; seen=set(); count=0
    while stack:
        obj=stack.pop(); count+=1
        if count>20000: raise ValueError('complexity')
        if isinstance(obj,IndirectObject):
            key=(obj.idnum,obj.generation)
            if key in seen: continue
            seen.add(key); obj=obj.get_object()
        if isinstance(obj,DictionaryObject):
            if forbidden.intersection(obj.keys()) or str(obj.get('/S','')) in forbidden or str(obj.get('/Type','')) in forbidden or str(obj.get('/Subtype','')) in forbidden:
                raise ValueError('active content')
            stack.extend(obj.values())
        elif isinstance(obj,ArrayObject): stack.extend(obj)
    pages=reader.pages
    if not 1<=len(pages)<=100: raise ValueError('pages')
    writer=PdfWriter()
    for page in pages: writer.add_page(page)
    output=io.BytesIO(); writer.write(output)
    if len(output.getvalue())>MAX_PDF: raise ValueError('size')
    return output.getvalue()


if __name__=='__main__':
    import resource
    resource.setrlimit(resource.RLIMIT_CPU,(5,5))
    if sys.platform.startswith('linux'):
        resource.setrlimit(resource.RLIMIT_AS,(128*1024*1024,128*1024*1024))
    try:
        data=sys.stdin.buffer.read(MAX_PDF+1)
        if not data or len(data)>MAX_PDF: raise ValueError('size')
        sys.stdout.buffer.write(validate(data))
    except Exception:
        # Never echo patient data, PDF objects or parser diagnostics to the response.
        sys.exit(1)
