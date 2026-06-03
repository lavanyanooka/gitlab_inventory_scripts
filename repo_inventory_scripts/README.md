# GitLab Project Inventory Script

This standalone script fetches comprehensive statistics for all projects in a GitLab group, including repositories in subgroups.

## Features

- Fetches detailed project statistics from GitLab API
- Supports filtering projects based on a CSV file
- Exports data to CSV format for analysis
- Tracks repository size, commits, branches, files, and more
- Identifies large files and repositories exceeding size limits
- Detects CI/CD pipeline configurations
- Counts exportable models for migration planning

## Requirements

- Python 3.7 or higher
- Required Python package: `requests`

## Quick Start


###  Set Environment Variables

**For PowerShell (Windows):**
```powershell
$env:GITLAB_TOKEN="your_gitlab_token_here"
$env:GITLAB_GROUP="your_group_name"
$env:GITLAB_URL="https://gitlab.com"  # Optional, defaults to gitlab.com
```

**For Git Bash / Linux / macOS:**
```bash
export GITLAB_TOKEN="your_gitlab_token_here"
export GITLAB_GROUP="your_group_name"
export GITLAB_URL="https://gitlab.com"  # Optional, defaults to gitlab.com
```

### 3. Run the Script

```bash
python gitlab.py
```

The script will automatically:
1. Read credentials from environment variables
2. Fetch all projects from the specified GitLab group (including subgroups)
3. Collect detailed statistics for each project
4. Export results to `data/gitlab-stats.csv`

## Getting a GitLab Access Token

1. Log in to your GitLab instance
2. Go to **User Settings** → **Access Tokens**
3. Create a new token with the following scopes:
   - `read_api`
   - `read_repository`
4. Copy the token and set it as the `GITLAB_TOKEN` environment variable

## Output

The script generates a CSV file (`data/gitlab-stats.csv`) with the following information for each project:

- Project ID, name, and path
- Group hierarchy (top_level, subgroup_1, subgroup_2, etc.)
- Status (active/archived)
- Stars, forks, open issues
- Contributor count
- Commit count (across all branches)
- Branch count
- File count (default branch and all branches)
- Repository size (MB and GB)
- Large file detection (>100MB)
- Size threshold flags (>2GB, >6GB)
- CI/CD pipeline detection
- Exportable model counts (for migration planning):
  - Users, protected branches, merge requests, issues, webhooks, tags, milestones, wikis

## Project Filtering

To process only specific projects:

1. Set `project_list_file` in your configuration:
```ini
project_list_file=gitlab-stats.csv
```

2. Create a CSV file in the `data/` directory with columns:
   - `Name`: Project name
   - `Migrate Repo`: Filter value (e.g., "Migrate", "Yes")

3. Optionally customize filter values:
```ini
migrate_repo_values=["Migrate", "Yes"]
```

## Troubleshooting

### "No GitLab token found"
- Ensure you've set the `GITLAB_TOKEN` environment variable
- Verify the token is set in your current terminal session:
  - PowerShell: `echo $env:GITLAB_TOKEN`
  - Bash: `echo $GITLAB_TOKEN`

### "No GitLab group defined"
- Ensure you've set the `GITLAB_GROUP` environment variable
- Verify the group name is correct
- For subgroups, use the full path (e.g., "parent-group/subgroup")

### "Group not found" error
- Verify the group name is correct
- Ensure your token has access to the group
- Check that the token has `read_api` and `read_repository` scopes

### Rate limiting
- The script respects GitLab API rate limits
- For large groups (>100 projects), the script may take several minutes
- Consider running during off-peak hours for very large groups
Keep your GitLab access token secure and never commit it to version control
- Use tokens with minimum required permissions (`read_api`, `read_repository`)
- Environment variables are cleared when you close your terminal session
- For persistent credentials, consider using your shell's profile configuration (e.g., `.bashrc`, PowerShell profile)ersion control
- Keep your GitLab access token secure
- Use tokens with minimum required permissions
- Consider using environment variables in CI/CD environments

## Output Directory

All output files are saved to the `data/` subdirectory, which is automatically created if it doesn't exist.

## License

This script is provided as-is for GitLab inventory and migration planning purposes.
