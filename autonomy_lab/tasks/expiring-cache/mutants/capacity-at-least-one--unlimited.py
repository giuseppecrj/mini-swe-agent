class ExpiringCache:
    """A small key-value cache with a capacity and a lifetime for its entries.

    Callers pass the current time explicitly as `now`, so the cache never reads a clock.
    """

    def __init__(self, capacity, lifetime):
        if lifetime <= 0:
            raise ValueError("lifetime must be positive")
        self.capacity = capacity
        self.lifetime = lifetime
        self._entries = {}  # key -> (value, time of the latest put), oldest put first

    def put(self, key, value, now):
        if key in self._entries:
            del self._entries[key]
        elif self.capacity > 0 and len(self._entries) >= self.capacity:
            oldest = next(iter(self._entries))
            del self._entries[oldest]
        self._entries[key] = (value, now)

    def get(self, key, now, default=None):
        """Return the value stored for `key`, or `default` if it is missing or has expired."""
        if key not in self._entries:
            return default
        value, time = self._entries[key]
        if now - time >= self.lifetime:
            del self._entries[key]
            return default
        return value
