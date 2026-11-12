"""Platform-specific helpers. The rest of the code must not branch on sys.platform."""

from .instance_lock import InstanceLock, InstanceLockError, stop_stale_core

__all__ = ["InstanceLock", "InstanceLockError", "stop_stale_core"]
