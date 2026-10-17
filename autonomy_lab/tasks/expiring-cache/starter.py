class ExpiringCache:
    """A small key-value cache whose entries expire `ttl` time units after they are stored.

    Callers pass the current time explicitly as `now`, so the cache never reads a clock.
    """

    def __init__(self, capacity, ttl):
        self.capacity = capacity
        self.ttl = ttl
        self._entries = {}  # key -> (value, stored_at)

    def put(self, key, value, now):
        if len(self._entries) >= self.capacity:
            oldest = next(iter(self._entries))
            del self._entries[oldest]
        self._entries[key] = (value, now)

    def get(self, key, now, default=None):
        if key not in self._entries:
            return default
        value, stored_at = self._entries[key]
        if now - stored_at > self.ttl:
            del self._entries[key]
            return default
        self._entries[key] = (value, now)
        return value
