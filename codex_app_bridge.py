"""SDK subprocess: relay JSONL to the existing daemon's Unix WebSocket."""

import sys
from threading import Thread

from websockets.sync.client import unix_connect


def bridge(socket_path):
    # The App daemon rejects the permessage-deflate extension offer.
    with unix_connect(str(socket_path), compression=None, max_size=64 * 1024 * 1024) as websocket:

        def forward_stdin():
            try:
                for line in sys.stdin:
                    if line.strip():
                        websocket.send(line.rstrip("\n"))
            finally:
                websocket.close()

        Thread(target=forward_stdin, daemon=True).start()
        for message in websocket:
            if not isinstance(message, str):
                raise RuntimeError("Expected a WebSocket text frame from App Server")
            print(message, flush=True)


if __name__ == "__main__":
    bridge(sys.argv[1])
