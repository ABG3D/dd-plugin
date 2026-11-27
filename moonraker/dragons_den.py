# Dragons Den Moonraker component
#
# Serves the usb_camera nozzle alignment page at /server/dragons_den/camera.
# The page streams the webcam, draws the crosshair / focus circle on an HTML5
# canvas and drives Klipper through Moonraker's G-code API.
#
# Copyright (C) 2026  ABG3D
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging
import os

VERSION = "0.0.1"
PAGE_NAME = "dragons_den_camera.html"
CAMERA_ENDPOINT = "/server/dragons_den/camera"
INFO_ENDPOINT = "/server/dragons_den/info"

try:
    from ..common import RequestType, TransportType
    _GET = RequestType.GET
    _HTTP = TransportType.HTTP
except ImportError:  # older Moonraker releases
    _GET = ['GET']
    _HTTP = ['http']


class DragonsDen:
    def __init__(self, config):
        self.server = config.get_server()
        default_page = os.path.join(
            os.path.dirname(os.path.realpath(__file__)), PAGE_NAME)
        self.page_path = os.path.expanduser(
            config.get('camera_page', default_page))
        self._register(CAMERA_ENDPOINT, self._handle_camera, html=True)
        self._register(INFO_ENDPOINT, self._handle_info, html=False)

    def _register(self, path, callback, html):
        attempts = []
        if html:
            attempts.append(dict(transports=_HTTP, wrap_result=False,
                                 content_type="text/html; charset=utf-8",
                                 auth_required=False))
            attempts.append(dict(transports=_HTTP, wrap_result=False,
                                 content_type="text/html; charset=utf-8"))
        attempts.append(dict(transports=_HTTP))
        attempts.append({})
        for kwargs in attempts:
            try:
                self.server.register_endpoint(path, _GET, callback, **kwargs)
                return
            except TypeError:
                continue
        logging.error("dragons_den: unable to register %s", path)

    async def _handle_camera(self, web_request):
        with open(self.page_path, 'r', encoding='utf-8') as f:
            return f.read()

    async def _handle_info(self, web_request):
        return {'version': VERSION, 'camera_page': CAMERA_ENDPOINT}


def load_component(config):
    return DragonsDen(config)
