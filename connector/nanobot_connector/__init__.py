"""nanobot-connector: local file connector daemon.

Runs on a user's computer, connects outbound to a nanobot server, and serves an
allow-listed, read-only slice of the local filesystem so the agent can read the
user's local files (e.g. to build a PPT) without the files being uploaded first.
"""

__version__ = "0.1.0"
