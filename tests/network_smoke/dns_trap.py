"""Bounded synthetic non-forwarding resolver. Records counts, never query data."""
import socketserver
import struct
import threading


class Trap:
    def __init__(self, address, port=53):
        self.count = 0
        self.lock = threading.Lock()
        owner = self

        def answer(data):
            if len(data) < 12 or len(data) > 4096:
                raise ValueError("invalid synthetic DNS packet")
            with owner.lock:
                owner.count += 1
            return data[:2] + struct.pack("!5H", 0x8183, 1, 0, 0, 0) + data[12:]

        class UDP(socketserver.BaseRequestHandler):
            def handle(self):
                data, stream = self.request
                stream.sendto(answer(data), self.client_address)

        class TCP(socketserver.BaseRequestHandler):
            def handle(self):
                from dns_boundary_probe import receive_exact
                self.request.settimeout(3)
                size = struct.unpack("!H", receive_exact(self.request, 2))[0]
                if size > 4096:
                    return
                data = answer(receive_exact(self.request, size))
                self.request.sendall(struct.pack("!H", len(data)) + data)

        self.servers = [socketserver.UDPServer((address, port), UDP)]
        try:
            self.servers.append(socketserver.TCPServer((address, port), TCP))
        except OSError:
            self.servers[0].server_close()
            raise
        for server in self.servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()

    def close(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()
