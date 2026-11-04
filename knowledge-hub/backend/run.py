import os
import uvicorn

if __name__ == '__main__':
    uvicorn.run('hub.main:app', host=os.environ.get('HUB_HOST', '0.0.0.0'),
                port=int(os.environ.get('HUB_PORT', '8200')))
