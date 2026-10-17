Duration strings write lengths of time in a compact form.
They often appear in configuration files and command-line flags.
parse_duration reads such a string written by a person.
Settings for many different services are written this way.
The unit table is defined once at the top of the module.
The function is pure and keeps no state between calls.
It has no side effects such as logging or printing.
Command-line tools and configuration loaders both call it.
The module uses only the Python standard library.
The parser is small enough to read in one sitting.
Tests call the function directly with literal strings.
The function takes exactly one argument, the text to parse.
Durations are a common kind of setting in service configuration.
Several services share this helper to read durations the same way.
Operators edit these settings by hand.
Short strings are easier to type than long spelled-out forms.
Many tools from different vendors use similar notations.
