"""nanobot Connector: server-side support for local file connectors.

The connector lets a lightweight daemon running on a user's own computer expose
a read-only, allow-listed slice of its filesystem to the agent over an outbound
WebSocket connection (reverse-connect, CMDB-agent style). This package holds the
server-side pieces:

- ``protocol``: wire frame models shared by gateway and client.
- ``devices``: pairing codes + device tokens (issue / verify / revoke).
- ``hub``: node registry + RPC routing over the WS data channel.
- ``transfer``: chunked file receive with sha256 verification and atomic landing.
"""

from nanobot.connector.protocol import PROTOCOL_VERSION

__all__ = ["PROTOCOL_VERSION"]
