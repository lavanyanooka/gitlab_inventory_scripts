import os
import requests
from urllib.parse import quote
from collections import Counter

base = "https://gitlab.com/api/v4"
token = os.environ.get("GITLAB_TOKEN", "")
group = os.environ.get("GITLAB_GROUP", "")
if not token or not group:
    raise SystemExit("Missing GITLAB_TOKEN or GITLAB_GROUP")

s = requests.Session()
s.headers.update({"PRIVATE-TOKEN": token})

def get_paginated(path, params=None):
    out = []
    page = 1
    params = dict(params or {})
    while True:
        params.update({"per_page": 100, "page": page})
        r = s.get(base + path, params=params, timeout=60)
        r.raise_for_status()
        items = r.json()
        if not items:
            break
        out.extend(items)
        nxt = r.headers.get("X-Next-Page")
        if not nxt:
            break
        page = int(nxt)
    return out

projects = get_paginated(f"/groups/{quote(group, safe='')}/projects", {"include_subgroups": "true", "with_shared": "false"})
print(f"Projects: {len(projects)}")

counts = Counter()
projects_with_packages = 0
errors = 0
for p in projects:
    pid = p["id"]
    try:
        packages = get_paginated(f"/projects/{pid}/packages")
    except Exception:
        errors += 1
        continue
    if packages:
        projects_with_packages += 1
    for pkg in packages:
        counts[pkg.get("package_type", "unknown")] += 1

print(f"Projects with packages: {projects_with_packages}")
print(f"Package-list errors: {errors}")
print("Package type counts:")
for k in sorted(counts):
    print(f"  {k}: {counts[k]}")
