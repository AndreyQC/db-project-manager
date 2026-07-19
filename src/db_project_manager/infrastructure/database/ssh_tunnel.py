"""SSH Tunnel Manager — establishes and manages SSH tunnels via paramiko.

Uses paramiko.SSHClient with TCP transport forwarding.
paramiko 5.x compatible.

Lifecycle:
    1. Create SSHTunnelManager with configuration.
    2. Call start() to establish the tunnel — returns local bind port.
    3. Use the local port to connect to the database.
    4. Call stop() to tear down the tunnel.
"""

from __future__ import annotations

import socket
import threading
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    import paramiko

from db_project_manager.domain.connection import SSH_TunnelConfig


class SSH_TunnelError(Exception):
    """Raised on SSH tunnel failures."""


class SSHTunnelManager:
    """Manages an SSH tunnel lifecycle using paramiko.

    Args:
        ssh_host: Jump host IP or hostname.
        ssh_port: SSH port (default 22).
        ssh_user: SSH username.
        ssh_password: Decrypted SSH password.
        remote_bind_host: Database host as seen from the jump host.
        remote_bind_port: Database port as seen from the jump host.
        local_bind_port: Local port for tunnel (0 = auto-select).
    """

    def __init__(
        self,
        ssh_host: str,
        ssh_port: int,
        ssh_user: str,
        ssh_password: str,
        remote_bind_host: str = "127.0.0.1",
        remote_bind_port: int = 5432,
        local_bind_port: int = 0,
    ) -> None:
        self.ssh_host = ssh_host
        self.ssh_port = ssh_port
        self.ssh_user = ssh_user
        self.ssh_password = ssh_password
        self.remote_bind_host = remote_bind_host
        self.remote_bind_port = remote_bind_port
        self.local_bind_port = local_bind_port
        self._ssh_client: "paramiko.SSHClient | None" = None
        self._transport: "paramiko.Transport | None" = None
        self._channel: "paramiko.Channel | None" = None
        self._local_port: int = 0
        self._server_socket: socket.socket | None = None
        self._forward_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    @classmethod
    def from_config(cls, config: SSH_TunnelConfig, decrypted_password: str) -> "SSHTunnelManager":
        """Construct manager from SSH_TunnelConfig and a pre-decrypted password."""
        return cls(
            ssh_host=config.ssh_host,
            ssh_port=config.ssh_port,
            ssh_user=config.ssh_user,
            ssh_password=decrypted_password,
            remote_bind_host=config.remote_bind_host,
            remote_bind_port=config.remote_bind_port,
            local_bind_port=config.local_bind_port,
        )

    def start(self) -> int:
        """Start the SSH tunnel and return the local bind port.

        Returns:
            The local port number to use for connecting to the database.

        Raises:
            SSH_TunnelError: If the tunnel fails to start.
        """
        import paramiko

        if self._transport is not None:
            raise SSH_TunnelError("Tunnel is already started")

        try:
            logger.info(f"SSH connecting to {self.ssh_host}:{self.ssh_port} as {self.ssh_user}")

            # Create SSH client
            self._ssh_client = paramiko.SSHClient()
            self._ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            logger.info("Calling SSH client connect...")
            # Connect with timeout
            self._ssh_client.connect(
                hostname=self.ssh_host,
                port=self.ssh_port,
                username=self.ssh_user,
                password=self.ssh_password,
                timeout=10,  # TCP connection timeout
                banner_timeout=10,  # SSH banner timeout
                auth_timeout=10,  # SSH auth timeout
                look_for_keys=False,
                allow_agent=False,
            )

            # Get transport for port forwarding
            self._transport = self._ssh_client.get_transport()
            if self._transport is None:
                raise SSH_TunnelError("Failed to get SSH transport")

            logger.info("SSH transport acquired")

            # Create local server socket
            self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._server_socket.bind(("127.0.0.1", self.local_bind_port))
            self._server_socket.listen(5)
            self._local_port = self._server_socket.getsockname()[1]

            # Start forwarding thread
            self._stop_event.clear()
            self._forward_thread = threading.Thread(
                target=self._accept_loop,
                daemon=True,
            )
            self._forward_thread.start()

            logger.info(f"SSH tunnel ready on port {self._local_port}")
            return self._local_port

        except SSH_TunnelError:
            logger.error("SSH tunnel failed: SSH_TunnelError")
            raise
        except Exception as e:
            logger.error(f"SSH tunnel failed: {type(e).__name__}: {e}")
            self._cleanup()
            raise SSH_TunnelError(f"Failed to start SSH tunnel: {e}") from e

    def _accept_loop(self) -> None:
        """Accept local connections and forward through SSH channel."""
        logger.info("SSH tunnel accept loop started")
        while not self._stop_event.is_set():
            if self._server_socket is None:
                break
            self._server_socket.settimeout(0.5)
            try:
                client_sock, addr = self._server_socket.accept()
                logger.info(f"Local connection accepted from {addr}")
            except socket.timeout:
                continue
            except OSError:
                break

            if self._transport is None or not self._transport.is_active():
                logger.warning("Transport not active, closing client connection")
                client_sock.close()
                break

            try:
                logger.info(f"Opening channel to {self.remote_bind_host}:{self.remote_bind_port}")
                # Open channel to remote
                channel = self._transport.open_channel(
                    "direct-tcpip",
                    (self.remote_bind_host, self.remote_bind_port),
                    addr,
                )
                logger.info("Channel opened successfully")
                thread = threading.Thread(
                    target=self._pipe,
                    args=(client_sock, channel),
                    daemon=True,
                )
                thread.start()
            except Exception as e:
                logger.error(f"Failed to open channel: {type(e).__name__}: {e}")
                try:
                    client_sock.close()
                except Exception:
                    pass

    @staticmethod
    def _pipe(client: socket.socket, channel) -> None:
        """Forward data between client and SSH channel in both directions.

        Uses two threads for full-duplex bidirectional forwarding:
            - _pipe_client_to_channel: reads from PG client, sends to SSH channel
            - _pipe_channel_to_client: reads from SSH channel, sends to PG client
        """
        logger.info("Pipe started (full-duplex)")

        def _pipe_client_to_channel():
            try:
                while True:
                    data = client.recv(4096)
                    if not data:
                        logger.info("Client closed connection")
                        break
                    logger.info(f"Received {len(data)} bytes from client, sending to channel")
                    channel.sendall(data)
                    logger.info("Data sent to channel")
            except Exception as e:
                logger.info(f"Client→Channel pipe error: {type(e).__name__}: {e}")
            finally:
                try:
                    channel.close()
                except Exception:
                    pass

        def _pipe_channel_to_client():
            try:
                while True:
                    data = channel.recv(4096)
                    if not data:
                        logger.info("Channel closed connection")
                        break
                    logger.info(f"Received {len(data)} bytes from channel, sending to client")
                    client.sendall(data)
                    logger.info("Data sent to client")
            except Exception as e:
                logger.info(f"Channel→Client pipe error: {type(e).__name__}: {e}")
            finally:
                try:
                    client.close()
                except Exception:
                    pass

        t1 = threading.Thread(target=_pipe_client_to_channel, daemon=True)
        t2 = threading.Thread(target=_pipe_channel_to_client, daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        logger.info("Pipe closed")

    def stop(self) -> None:
        """Stop and clean up the SSH tunnel."""
        self._cleanup()

    def _cleanup(self) -> None:
        """Internal cleanup."""
        self._stop_event.set()

        if self._server_socket is not None:
            try:
                self._server_socket.close()
            except Exception:
                pass
            self._server_socket = None

        if self._channel is not None:
            try:
                self._channel.close()
            except Exception:
                pass
            self._channel = None

        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:
                pass
            self._transport = None

        if self._ssh_client is not None:
            try:
                self._ssh_client.close()
            except Exception:
                pass
            self._ssh_client = None

        self._local_port = 0
        self._forward_thread = None

    def is_active(self) -> bool:
        """Check whether the tunnel is currently active."""
        return self._transport is not None and self._transport.is_active()

    def get_local_port(self) -> int:
        """Get the actual local bound port.

        Returns:
            The local port, or 0 if the tunnel is not started.

        Raises:
            SSH_TunnelError: If the tunnel is not started.
        """
        if self._transport is None:
            raise SSH_TunnelError("Tunnel is not started")
        return self._local_port
