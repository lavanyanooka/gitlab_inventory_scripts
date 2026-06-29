# GitLab → GitHub Branch Protection Rules Mapping Document

## Overview

This document explains how GitLab branch protection rules are mapped to GitHub branch protection rules during migration.

---

## Mapping Summary Table

| GitLab Setting | GitHub Equivalent | Mapping Logic |
|---|---|---|
| `push_access_levels[].access_level` | `restrictions` (push restrictions) | If max push level ≤ 40 (Maintainer) → restrict push to admins/specified users only |
| `merge_access_levels[].access_level` | Informs rule strictness | Used as context; GitHub uses PR reviews instead |
| `allow_force_push` | `allow_force_pushes` | Direct 1:1 mapping (boolean) |
| `code_owner_approval_required` | `required_pull_request_reviews.require_code_owner_reviews` | Direct 1:1 mapping (boolean) |
| `approval_rules[].approvals_required` | `required_pull_request_reviews.required_approving_review_count` | Takes max approvals required, capped at 6 (GitHub max) |
| `approvals_before_merge` (project setting) | `required_approving_review_count` | Fallback if no rule-level setting |
| *(no direct equivalent)* | `required_status_checks` | Configured via defaults in config YAML |
| *(no direct equivalent)* | `enforce_admins` | Configurable default (default: false) |
| *(no direct equivalent)* | `required_linear_history` | Configurable default (default: false) |
| *(no direct equivalent)* | `required_signatures` | Configurable default (default: false) |
| *(no direct equivalent)* | `dismiss_stale_reviews` | Configurable default (default: true) |
| *(no direct equivalent)* | `allow_deletions` | Configurable default (default: false) |

---

## Detailed Mapping Rules

### 1. Push Restrictions

**GitLab**: Uses `push_access_levels` array with numeric access levels:
- `0` = No one
- `30` = Developer
- `40` = Maintainer
- `60` = Admin

**GitHub**: Uses `restrictions` object with explicit user/team/app lists.

**Mapping Logic**:
```
IF max(push_access_levels[].access_level) <= 40 (Maintainer or less)
  → GitHub: restrictions = {users: [], teams: [], apps: []}
    (Only admins/specified people can push)
ELSE
  → GitHub: restrictions = null (no push restrictions)
```

### 2. Force Push

**GitLab**: `allow_force_push` (boolean per protected branch)  
**GitHub**: `allow_force_pushes` (boolean per branch protection rule)

**Mapping**: Direct 1:1 → `allow_force_push` = `allow_force_pushes`

### 3. Code Owner Approval

**GitLab**: `code_owner_approval_required` (Premium/Ultimate, boolean)  
**GitHub**: `required_pull_request_reviews.require_code_owner_reviews` (boolean)

**Mapping**: Direct 1:1 → `true`/`false`

### 4. Required Approvals / PR Reviews

**GitLab** has:
- Project-level `approvals_before_merge`
- Branch-level `approval_rules[].approvals_required`

**GitHub** has:
- `required_pull_request_reviews.required_approving_review_count` (max: 6)

**Mapping Logic**:
```
1. Start with config default (default: 1)
2. If project has approvals_before_merge → use max(1, that value)
3. If approval_rules exist with rule_type="regular" → use max of all
4. Cap at 6 (GitHub maximum)
```

### 5. Status Checks

**GitLab**: No direct equivalent in branch protection API (handled by CI pipelines and merge request settings)  
**GitHub**: `required_status_checks` with `strict` flag and `contexts` list

**Mapping**: Configured via defaults in the config file. No automatic extraction from GitLab CI.

### 6. Admin Enforcement

**GitLab**: Admins can always override protection  
**GitHub**: `enforce_admins` flag applies rules to admins too

**Mapping**: Configurable default → `false` (matches GitLab behavior where admins bypass rules)

---

## Access Level Mapping

| GitLab Access Level | GitLab Role | GitHub Mapping |
|---|---|---|
| 0 | No access | `restrict` (no one can push) |
| 30 | Developer | `maintain` permission |
| 40 | Maintainer | `admin` permission |
| 60 | Admin | `admin` permission |

---

## What Gets Migrated vs. What Doesn't

### ✅ Migrated (automatic)
- Branch name/pattern
- Push access restrictions
- Force push settings
- Code owner approval requirement
- Approval counts (from approval rules)

### ⚙️ Configurable (from YAML defaults)
- Required status checks (contexts list)
- Strict status checks (require branches to be up-to-date)
- Dismiss stale reviews
- Enforce admins
- Require linear history
- Require signed commits
- Allow deletions

### ❌ Not Directly Mappable
- GitLab `user_id`/`group_id` specific access → GitHub uses teams/users (manual mapping needed)
- GitLab `deploy_key_id` push access → No GitHub equivalent in branch protection
- GitLab wildcard branch patterns (`release-*`) → GitHub requires exact branch names or `fnmatch` patterns
- GitLab inherited protection from parent group → GitHub has no group inheritance

---

## Configuration Used for This Migration

```yaml
mapping:
  push_access:
    0: "restrict"       # No one can push
    30: "maintain"      # Developers -> maintain
    40: "admin"         # Maintainers -> admin
    60: "admin"         # Admins -> admin

  defaults:
    require_pull_request: true
    required_approving_reviews: 1
    dismiss_stale_reviews: true
    require_code_owner_reviews: false
    required_status_checks: []
    strict_status_checks: false
    enforce_admins: false
    require_linear_history: false
    require_signed_commits: false
    allow_force_pushes: false
    allow_deletions: false
```

---

## Migration Results Summary

**Date**: 2026-06-29  
**Source**: GitLab group `ranjiths-infomagnus-group` (14 projects)  
**Target**: GitHub org `im-sandbox-rushik`

| Status | Count | Notes |
|--------|-------|-------|
| **Success** | 9 | `main` branch protected on 9 GitHub repos |
| **Failed** | 9 | `master` branch doesn't exist on GitHub repos |
| **Skipped** | 38 | `develop`/`release` branches not present on GitHub, some repos had no matching GitHub repo |

### Successfully Protected Repos (main branch)

| GitHub Repository | Protection Applied |
|---|---|
| im-sandbox-rushik/api-service | ✅ |
| im-sandbox-rushik/web-app | ✅ |
| im-sandbox-rushik/infrastructure | ✅ |
| im-sandbox-rushik/mobile-app | ✅ |
| im-sandbox-rushik/shared-library | ✅ |
| im-sandbox-rushik/web-frontend | ✅ |
| im-sandbox-rushik/shared-lib | ✅ |
| im-sandbox-rushik/unique-nuget-package | ✅ |

### GitHub Protection Payload Applied

```json
{
  "required_status_checks": null,
  "enforce_admins": false,
  "required_pull_request_reviews": {
    "dismiss_stale_reviews": true,
    "require_code_owner_reviews": false,
    "required_approving_review_count": 1
  },
  "restrictions": {
    "users": [],
    "teams": [],
    "apps": []
  },
  "required_linear_history": false,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_signatures": false
}
```

---

## Post-Migration Validation

Validation result: **9/9 passed** — all successfully applied rules were verified to match the expected configuration on GitHub.
