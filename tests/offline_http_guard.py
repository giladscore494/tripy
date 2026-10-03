"""Optional pytest plugin for verification without outbound HTTP: PYTHONPATH=tests pytest -p offline_http_guard."""
from urllib.parse import urlparse
import requests


def pytest_configure(config):
    original = requests.sessions.Session.send

    def offline_send(self, request, **kwargs):
        host = urlparse(request.url).hostname
        if host not in ('localhost', '127.0.0.1', '::1'):
            raise RuntimeError(f'Offline test guard blocked HTTP to {host}')
        return original(self, request, **kwargs)

    requests.sessions.Session.send = offline_send
