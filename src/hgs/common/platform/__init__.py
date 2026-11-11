"""Platform-specific helpers. The rest of the code must not branch on sys.platform."""

from .instance_lock import InstanceLock, InstanceLockError

__all__ = ["InstanceLock", "InstanceLockError"]
