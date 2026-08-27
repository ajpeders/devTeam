"""HTTP client for communicating with the MyDevTeam daemon over Unix socket or TCP."""

import http.client
import json
import socket


class UnixHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection subclass that connects via a Unix domain socket."""

    def __init__(self, socket_path: str, timeout: float = 10):
        self.socket_path = socket_path
        # Use "localhost" as the host — it's ignored for the actual connection
        super().__init__("localhost", timeout=timeout)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


class TCPHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection subclass that connects via TCP."""

    def __init__(self, host: str, port: int, timeout: float = 10):
        self.host = host
        self.port = port
        super().__init__(host, port=port, timeout=timeout)

    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect((self.host, self.port))


class DaemonClient:
    """HTTP client that talks to a daemon over Unix socket or TCP."""

    def __init__(self, address: str, agent_secret: str = ""):
        """
        Args:
            address: Unix socket path (e.g., "/tmp/devteam.sock") or
                     TCP address (e.g., "localhost:4223")
            agent_secret: Shared secret for authenticating with internal endpoints.
        """
        self.address = address
        self.agent_secret = agent_secret
        self._conn: http.client.HTTPConnection | None = None

    def _create_connection(self) -> http.client.HTTPConnection:
        """Create appropriate connection based on address format."""
        if self.address.startswith("/") or self.address.endswith(".sock"):
            return UnixHTTPConnection(self.address)
        elif ":" in self.address:
            host, port_str = self.address.rsplit(":", 1)
            return TCPHTTPConnection(host, int(port_str))
        else:
            # Assume Unix socket if no port specified
            return UnixHTTPConnection(self.address)

    def post(self, path: str, data: dict) -> dict:
        """HTTP POST to daemon, returns parsed JSON response."""
        conn = self._create_connection()
        try:
            body = json.dumps(data).encode("utf-8")
            headers = {"Content-Type": "application/json"}
            if self.agent_secret:
                headers["X-Agent-Key"] = self.agent_secret
            conn.request("POST", path, body=body, headers=headers)
            response = conn.getresponse()
            raw = response.read().decode("utf-8")
            if response.status < 200 or response.status >= 300:
                raise RuntimeError(f"POST {path} failed ({response.status}): {raw}")
            return json.loads(raw) if raw else {}
        finally:
            conn.close()


# Backwards compatibility alias
UnixSocketClient = DaemonClient
