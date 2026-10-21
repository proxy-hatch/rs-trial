# Patterns

A Pattern is the one supported way to do a recurring task. Each Pattern has one file here, such as `settings.md`. Agents may add a Pattern; changing an existing one needs the owner. `./devctl check` validates every file.

Every Pattern file starts with YAML front matter:

```yaml
---
reference: src/app/settings.py
enforced_by:
  - banned:os.environ
  - test:tests/test_settings.py::test_settings_are_the_only_reader
---
```

- `reference` is the path of the reference implementation. It must exist.
- `enforced_by` lists at least one reference to the check that enforces the Pattern. Each must resolve:

| Reference | Resolves when |
| --- | --- |
| `check:<step>` | The devctl check step exists |
| `test:<path>::<name>` | The test exists |
| `ruff:<code>` | The rule is selected in the Ruff settings (Python) |
| `banned:<module.member>` | The member is in Ruff's `banned-api` table (Python) |
| `contract:<name>` | An import-linter contract has that name (Python) |
| `type:<path>` | The file exists and mypy checks it (Python) |

The text after the front matter states the rule, when it applies and the bypass it bans.
