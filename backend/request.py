import http.client
import uuid
import traceback

boundary = '----WebKitFormBoundary' + uuid.uuid4().hex
parts = []

def add_field(name, value):
    parts.append(
        ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (boundary, name, value)).encode('utf-8')
    )

add_field('template_id', 'template_A_portrait_1080x1350')
add_field('payload', '[{"area_id":"main_box","text":"Avril Lavigne presentó su primer vino...","color":"#FFFFFF","font_size":56}]')

fname = 'backend_app.py'
with open(fname, 'rb') as f:
    file_bytes = f.read()

file_header = ('--%s\r\nContent-Disposition: form-data; name="image"; filename="%s"\r\nContent-Type: text/plain\r\n\r\n' % (boundary, fname)).encode('utf-8')
body = b''.join(parts) + file_header + file_bytes + b'\r\n' + ('--%s--\r\n' % boundary).encode('utf-8')

conn = http.client.HTTPConnection('127.0.0.1', 8000, timeout=30)
headers = {
    'Content-Type': 'multipart/form-data; boundary=%s' % boundary,
    'Content-Length': str(len(body))
}

try:
    conn.request('POST', '/debug-request', body, headers)
    resp = conn.getresponse()
    print('HTTP', resp.status, resp.reason)
    data = resp.read()
    try:
        print(data.decode('utf-8'))
    except:
        print(repr(data))
except Exception:
    traceback.print_exc()
finally:
    conn.close()