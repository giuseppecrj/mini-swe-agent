parse_size reads a size that a person typed.
Sizes arrive as command-line arguments and in settings files.
The function takes one argument, the size string.
A storage tool reads sizes from its own options.
A file transfer script accepts sizes on its command line.
Some values come from environment variables.
A reporting script reads sizes from a settings file.
The module uses only the Python standard library.
The function does no input or output of its own.
Tests call the function with short literal strings.
The same helper is shared by several small tools.
It replaces several slightly different copies of similar code.
Most inputs come from configuration that a colleague reviewed.
Several teams maintain tools that call this helper.
The helper lives in a small shared utility package.
Code review asked for one shared parser for these strings.
The callers are written in Python as well.
