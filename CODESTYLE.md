Welcome to my cool repo these are my rules:
- Indentation is always a soft 4-space tab, function call parameter lists.
- All lines under 80 columns.
- Empty line between declarations on the same indentation level (multiple
  assignments can be grouped together).
- Import system libraries first, followed by local files.
- Don't use imports that pollute the local scope (`import _ as _`,
  `from _ import _`) unless it would be obnoxious not to (e.g. `typing`).
- Classes are `CapitalCamelCase`, functions and variables are `snake_case`.
- Most classes should have `__slots__`.
- Don't name private things with `_` unless there's a namespace clash with e.g.
  `__getattr__`.
