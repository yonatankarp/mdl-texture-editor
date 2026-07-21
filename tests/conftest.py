import os
import shutil
import threading

import pytest
from werkzeug.serving import make_server

from server import create_app

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def live_server_factory(tmp_path):
    # A real HTTP server (Playwright needs one; the other suites use Flask's
    # in-process test_client). Rooted at a throwaway dir holding a copy of a
    # sample model, so extract/save write _edit/ and _backup_mdl/ under tmp, not
    # the repo. Static assets are served from the repo's static/ regardless of
    # root, so the real index.html + app.js load unchanged.
    #
    # Function-scoped so every test starts from a pristine skin: extract reuses
    # any existing working PNG, so a shared root would leak one test's strokes
    # into the next test's "original" pixels.
    def start(*models):
        root = tmp_path
        samples = root / "samples"
        samples.mkdir(exist_ok=True)
        for model in models or ("Paper2.MDL",):
            shutil.copy(os.path.join(ROOT, "samples", model), samples / model)

        app = create_app(str(root))
        app.config.update(TESTING=True)
        server = make_server("127.0.0.1", 0, app, threaded=True)
        # The /api/watch SSE handler loops forever; daemon threads let the process
        # exit even if one is mid-sleep when we shut down.
        server.daemon_threads = True
        port = server.server_port

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread, f"http://127.0.0.1:{port}"

    servers = []
    try:
        def factory(*models):
            server, thread, url = start(*models)
            servers.append((server, thread))
            return url
        yield factory
    finally:
        for server, thread in servers:
            server.shutdown()
            thread.join(timeout=5)


@pytest.fixture
def live_server(live_server_factory):
    # Paper2.MDL is the model index.html loads by default, so the page boots
    # into a ready editing state with no extra driving.
    return live_server_factory("Paper2.MDL")
