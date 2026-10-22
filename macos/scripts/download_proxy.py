"""System proxy support for installer subprocesses only."""
from __future__ import annotations
import os
import urllib.request
from urllib.request import getproxies


def system_proxies() -> dict[str, str]:
    reader = getattr(urllib.request, 'getproxies_macosx_sysconf', None)
    if reader is None:
        reader = getattr(urllib.request, 'getproxies_registry', None)
    return reader() if reader else {}


def download_environment() -> dict[str, str]:
    env = os.environ.copy()
    # getproxies reads macOS System Configuration / Windows Internet Settings
    # when no explicit proxy environment is configured. Never log proxy URLs.
    proxies = getproxies()
    # A NO_PROXY-only environment otherwise hides OS proxy settings in urllib.
    if not any(key.lower() in ('http_proxy', 'https_proxy', 'all_proxy') for key in env):
        proxies = {**system_proxies(), **proxies}
    for scheme in ('http', 'https', 'all'):
        if proxies.get(scheme):
            env[scheme + '_proxy'] = proxies[scheme]
    bypass = proxies.get('no', '')
    # Local management and QQ connections must always bypass the proxy.
    env['no_proxy'] = ','.join(filter(None, [bypass, 'localhost', '127.0.0.1', '::1']))
    env['NO_PROXY'] = env['no_proxy']
    return env
