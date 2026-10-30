"""One verified, persistent client connection; no automatic reconnects."""

import http.client
import json
import sys


def main():
    connection = http.client.HTTPConnection(sys.argv[1], 8000, timeout=300)
    connection.connect()
    local = connection.sock.getsockname()
    print(json.dumps({"server": connection.sock.getpeername(), "client": local}), flush=True)
    requests = json.loads(sys.stdin.readline())
    for path, body in requests:
        assert connection.sock is not None and connection.sock.getsockname() == local
        connection.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
        response = connection.getresponse()
        raw = response.read(1_048_577)
        assert response.status == 200 and len(raw) <= 1_048_576
        message = json.loads(raw)["choices"][0]["message"]
        assert isinstance(message, dict) and isinstance(message.get("content"), str)
        print(json.dumps({"output": json.dumps(message)}), flush=True)
    print(json.dumps({"state": "done"}), flush=True)
    sys.stdin.readline()
    connection.close()


if __name__ == "__main__":
    main()
