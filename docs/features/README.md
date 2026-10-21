# Feature map

Each feature has one file here, named after the feature, such as `sign-up.md`. Agents keep these files current with the code, and `./devctl check` validates them.

Every feature file starts with YAML front matter:

```yaml
---
purpose: Lets a visitor create an account with an email address.
entry_points:
  - src/app/web/sign_up.py
data:
  - src/app/accounts/store.py
observe: The log line "account created" with the new account ID.
reproduce: ./devctl seed-account demo@example.com
---
```

| Key | Value | `./devctl check` requires |
| --- | --- | --- |
| `purpose` | One sentence on what the feature does for its user | Present and not empty |
| `entry_points` | Paths where the feature is entered | A non-empty list; each path exists |
| `data` | Paths of the data it reads or writes, or `[]` | A list, possibly empty; each path exists |
| `observe` | How to see the feature working | Present and not empty |
| `reproduce` | A `./devctl` command that exercises the feature | The word after `./devctl` is a registered devctl verb |

The text after the front matter describes the feature for a reader who has not seen it.
