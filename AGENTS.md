# Repository guidance

This is a macOS SwiftUI application with Python USB workers, an iOS XCTest
helper, and an Android Java mock-location helper.

- Preserve explicit stop / restoration behavior and report restoration failures.
- Never run device-affecting tests without the user's authorization.
- Do not embed personal routes, device identifiers, signing teams, or credentials.
- Generated applications, firmware, signing keys, and private logs are not source.
- Run `python -m unittest discover -v` in the documented Python environment.
- Build Android before packaging the Mac application; see README.md.
- Do not imply that transport success means a third-party app accepted a record.
