"""Bound request bodies before multipart parsing spools them to disk."""
from starlette.responses import JSONResponse


class UploadLimit:
    def __init__(self, app, max_bytes):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['method'] not in ('POST', 'PUT'):
            return await self.app(scope, receive, send)
        upload = scope['path'].endswith(('/content', '/resources/current'))
        limit = self.max_bytes if upload else 65536
        headers = dict(scope['headers'])
        try:
            length = int(headers.get(b'content-length', b'0'))
        except ValueError:
            return await JSONResponse({'error': 'Invalid content length'}, 400)(scope, receive, send)
        if length < 0 or length > limit:
            return await JSONResponse({'error': 'Upload exceeds limit'}, 413)(scope, receive, send)
        total = 0
        exceeded = False
        async def limited_receive():
            nonlocal total, exceeded
            message = await receive()
            if message['type'] == 'http.request':
                total += len(message.get('body', b''))
                if total > limit:
                    exceeded = True
                    return {'type': 'http.disconnect'}
            return message
        async def limited_send(message):
            if exceeded:
                return
            await send(message)
        try:
            await self.app(scope, limited_receive, limited_send)
        except Exception:
            if not exceeded:
                raise
        if exceeded:
            await JSONResponse({'error': 'Upload exceeds limit'}, 413)(scope, receive, send)
