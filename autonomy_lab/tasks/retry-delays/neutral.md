Network clients wait between attempts when a request fails.
retry_delays works out those waits before the first attempt.
A job runner reads the four arguments from its settings.
The caller sleeps for each delay in turn.
Delays can be in seconds or in milliseconds.
The function does not sleep or read a clock itself.
The function is pure and has no side effects.
The module uses only the Python standard library.
Unit tests call the function with literal arguments.
Each retry repeats the request that failed.
A queue consumer uses it after a message fails to process.
Several background workers share this helper.
Operators tune the arguments when a service misbehaves.
The helper replaces delay tables that were written by hand.
An HTTP client library calls it for each failed request.
The list is computed once and then used for the whole job.
