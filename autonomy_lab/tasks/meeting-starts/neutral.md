Scheduling tools use this helper to suggest meeting times.
Times are whole minutes after midnight, so 9:00 is 540.
On the same scale, 17:30 is 1050.
Each busy interval is a pair of minute values from a calendar.
day_start and day_end describe the working hours for one day.
The step argument has a default value of 15 minutes.
The function only reads its arguments.
It never changes the busy list it is given.
A calendar front end shows the results as buttons.
The helper handles one day and ignores time zones.
Integer minutes avoid floating-point surprises.
The module uses only the Python standard library.
Busy intervals come from invitations and blocked focus time.
A typical meeting length is 30 or 60 minutes.
Callers convert dates and time zones to minutes first.
The function is deterministic and has no side effects.
