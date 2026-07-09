from __future__ import annotations

import subprocess
import sys

from staragent.dashboard.app import create_app


def test_node_api_does_not_import_dashboard_application() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import staragent.node.app; "
                "raise SystemExit('staragent.dashboard.app' in sys.modules)"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_compatibility_http_routes_are_marked_deprecated() -> None:
    schema = create_app().openapi()

    deprecated_routes = (
        ("/api/lark", "get"),
        ("/api/sessions/{name}", "delete"),
        ("/api/sessions/{name}/output", "get"),
        ("/api/sessions/{name}/send", "post"),
        ("/api/sessions/{name}/input", "post"),
        ("/api/sessions/{name}/transcript-state", "get"),
    )
    for path, method in deprecated_routes:
        assert schema["paths"][path][method]["deprecated"] is True
