ExpiringCache keeps recently computed values in memory.
It helps a service avoid repeating slow lookups.
Each entry pairs a value with the time it was stored.
Callers pass the current time to every call.
Passing the time explicitly keeps tests deterministic.
Time can be in any unit if ttl and now use the same one.
The cache is not shared between processes.
Keys can be any hashable value.
Values can be any Python object.
A typical key is the text of a remote request.
The class has no background thread.
All of its work happens inside put and get.
The capacity argument limits how many entries are held.
The ttl argument is measured in the caller's time unit.
The module uses only the Python standard library.
A plain dictionary holds the entries.
The class is not thread-safe.
Callers that share it across threads add their own lock.
Unit tests call put and get with chosen time values.
