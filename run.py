#!/usr/bin/env python3

import os

os.environ.setdefault("FOCUS_ENV", "development")

from app import create_app  # noqa: E402  (must follow the line above)

app = create_app()


def _is_on(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    debug = _is_on("FLASK_DEBUG")

    default_host = "127.0.0.1" if debug else "0.0.0.0"  # nosec B104
    host = os.environ.get("HOST", default_host)

    if debug and host not in ("127.0.0.1", "localhost", "::1"):
        print(
            "\n  REFUSING TO START.\n"
            "  FLASK_DEBUG is on and HOST is set to "
            f"'{host}', which would expose a remote code execution\n"
            "  console to everyone on this network. Use HOST=127.0.0.1, or "
            "turn debug off.\n"
        )
        raise SystemExit(1)

    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"  Reachable from other devices on this network on port {port}.")

    app.run(host=host, port=port, debug=debug)
