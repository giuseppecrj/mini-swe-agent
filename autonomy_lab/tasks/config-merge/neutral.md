Applications read their settings from more than one place.
A package ships a settings file together with its code.
Each deployment keeps a settings file of its own.
merge_config runs once when the application starts.
The combined settings are handed to the rest of the program.
Operators write and review the settings files.
Typical settings name hosts, ports, timeouts and features.
The module uses only the Python standard library.
The function does no input or output of its own.
Reading and parsing the files happens before the call.
Tests call the function with small literal dictionaries.
Several services share this helper.
It replaces merging code that was copied between projects.
Each service decides which settings files it reads.
Operators compare settings across environments during reviews.
A deployment tool calls the function before it starts a service.
The helper is part of a small internal library.
New services adopt it when they move to the shared tooling.
The settings files are kept under version control.
