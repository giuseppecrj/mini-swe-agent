ExpiringCache keeps recently computed values in memory.
It helps a service avoid repeating slow lookups.
Callers pass the current time to every call.
Passing the time explicitly keeps tests deterministic.
Time can be in any unit if lifetime and now use the same one.
The cache is not shared between processes.
Keys can be any hashable value.
Values can be any Python object.
A typical key is the text of a remote request.
The class has no background thread.
All of its work happens inside put and get.
The module uses only the Python standard library.
The class is not thread-safe.
Callers that share it across threads add their own lock.
Unit tests call put and get with chosen time values.
A web service creates one cache when it starts.
Typical values are parsed responses from other services.
