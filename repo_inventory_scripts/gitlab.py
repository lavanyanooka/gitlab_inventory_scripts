import requests
import csv
import sys
import os
import json
from datetime import datetime, timedelta
from pathlib import Path

def log(message):
    """Print timestamped log message"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] {message}")

# Initialize script
script_start_time = datetime.now()
log("Starting GitLab Project Details script")

# Initialize directories
script_dir = Path(__file__).parent
data_dir = script_dir / 'data'
data_dir.mkdir(parents=True, exist_ok=True)

# =======================
# Configuration Section
# =======================
# Try to load GitLab token from environment variable first
log("Checking for GITLAB_TOKEN in environment variables...")
GITLAB_TOKEN = os.environ.get('GITLAB_TOKEN')
GROUP_NAME = os.environ.get('GITLAB_GROUP')
GITLAB_URL = os.environ.get('GITLAB_URL', 'https://gitlab.com')

# GitHub token for potential future integration
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN')

# Project filter configuration
PROJECT_LIST_FILE = None
MIGRATE_REPO_VALUES = []

# If no environment variable, try to load from gl-migrate.conf or .token file
if not GITLAB_TOKEN:
    log("Environment variable not found. Checking for configuration files...")
    
    # Try gl-migrate.conf first
    gl_migrate_config_file = script_dir / 'gl-migrate.conf'
    token_file = script_dir / '.token'
    
    config_data = {}
    
    # Try loading from gl-migrate.conf
    if gl_migrate_config_file.exists():
        try:
            log(f"Configuration file found at: {gl_migrate_config_file}")
            log("Reading credentials and settings...")
            
            with open(gl_migrate_config_file, 'r', encoding='utf-8') as f:
                content = f.read()
                
                # Parse configuration file (supports both JSON and key=value format)
                if content.strip().startswith('{'):
                    # JSON format
                    config_data = json.loads(content)
                else:
                    # Key=value format
                    for line in content.splitlines():
                        line = line.strip()
                        if line and not line.startswith('#') and '=' in line:
                            key, value = line.split('=', 1)
                            config_data[key.strip()] = value.strip().strip('"').strip("'")
            
            GITLAB_TOKEN = config_data.get('GITLAB_TOKEN') or config_data.get('GITLAB_API_PRIVATE_TOKEN') or config_data.get('token')
            GROUP_NAME = config_data.get('GITLAB_GROUP') or config_data.get('group')  # Try uppercase first, then lowercase
            
            # Set environment variables for reuse by other scripts
            if GITLAB_TOKEN:
                os.environ['GITLAB_TOKEN'] = GITLAB_TOKEN
                log("Set GITLAB_TOKEN environment variable")
            
            if GROUP_NAME:
                os.environ['GITLAB_GROUP'] = GROUP_NAME
                log("Set GITLAB_GROUP environment variable")
            
            # Check for GitHub token in the same file
            GITHUB_TOKEN = config_data.get('GITHUB_TOKEN') or config_data.get('github_token')
            if GITHUB_TOKEN:
                os.environ['GITHUB_TOKEN'] = GITHUB_TOKEN
                log("Set GITHUB_TOKEN environment variable")
            
            # Check for custom GitLab URL
            GITLAB_URL = config_data.get('GITLAB_URL') or config_data.get('GITLAB_HOSTNAME') or config_data.get('gitlab_url') or GITLAB_URL
            if GITLAB_URL:
                os.environ['GITLAB_URL'] = GITLAB_URL
                log(f"Set GITLAB_URL environment variable to: {GITLAB_URL}")
            
            # Check for project list file configuration
            if config_data.get('project_list_file'):
                PROJECT_LIST_FILE = config_data['project_list_file']
                log(f"Project list file configured: {PROJECT_LIST_FILE}")
            
            # Check for migrate repo values to filter on
            if config_data.get('migrate_repo_values'):
                if isinstance(config_data['migrate_repo_values'], list):
                    MIGRATE_REPO_VALUES = config_data['migrate_repo_values']
                elif isinstance(config_data['migrate_repo_values'], str):
                    MIGRATE_REPO_VALUES = [config_data['migrate_repo_values']]
                log(f"Migrate repo values to filter: {MIGRATE_REPO_VALUES}")
            else:
                # Default to 'Migrate' if not specified
                MIGRATE_REPO_VALUES = ['Migrate']
                log("Using default migrate repo value: ['Migrate']")
            
            log(f"Configuration successfully loaded from file")
            if GROUP_NAME:
                log(f"Group name: {GROUP_NAME}")
            else:
                log("WARNING: No group name found in configuration file")
                    
        except (ValueError, KeyError) as e:
            log(f"ERROR: Failed to parse configuration file: {e}")
            sys.exit(1)
        except Exception as e:
            log(f"ERROR: Unexpected error reading configuration file: {e}")
            sys.exit(1)
    
    # Fall back to .token file if gl-migrate.conf doesn't exist
    elif token_file.exists():
        try:
            log(f"Trying .token file at: {token_file}")
            with open(token_file, 'r', encoding='utf-8') as f:
                config_data = json.load(f)
                
            GITLAB_TOKEN = config_data.get('GITLAB_TOKEN') or config_data.get('GITLAB_API_PRIVATE_TOKEN') or config_data.get('token')
            GROUP_NAME = config_data.get('GITLAB_GROUP') or config_data.get('group')
            
            if GITLAB_TOKEN:
                os.environ['GITLAB_TOKEN'] = GITLAB_TOKEN
                log("Set GITLAB_TOKEN environment variable")
            
            if GROUP_NAME:
                os.environ['GITLAB_GROUP'] = GROUP_NAME
                log("Set GITLAB_GROUP environment variable")
            
            # Check for GitHub token
            GITHUB_TOKEN = config_data.get('GITHUB_TOKEN') or config_data.get('github_token')
            if GITHUB_TOKEN:
                os.environ['GITHUB_TOKEN'] = GITHUB_TOKEN
                log("Set GITHUB_TOKEN environment variable")
            
            # Check for custom GitLab URL
            if 'GITLAB_URL' in config_data or 'gitlab_url' in config_data:
                GITLAB_URL = config_data.get('GITLAB_URL') or config_data.get('gitlab_url') or GITLAB_URL
                os.environ['GITLAB_URL'] = GITLAB_URL
                log(f"Set GITLAB_URL environment variable to: {GITLAB_URL}")
            
            # Check for project list file configuration
            if 'project_list_file' in config_data:
                PROJECT_LIST_FILE = config_data['project_list_file']
                log(f"Project list file configured: {PROJECT_LIST_FILE}")
            
            # Check for migrate repo values
            if 'migrate_repo_values' in config_data:
                if isinstance(config_data['migrate_repo_values'], list):
                    MIGRATE_REPO_VALUES = config_data['migrate_repo_values']
                elif isinstance(config_data['migrate_repo_values'], str):
                    MIGRATE_REPO_VALUES = [config_data['migrate_repo_values']]
            else:
                MIGRATE_REPO_VALUES = ['Migrate']
            
            log(f"Configuration successfully loaded from .token file")
                    
        except Exception as e:
            log(f"ERROR: Failed to parse .token file: {e}")
            sys.exit(1)
    
    else:
        log("ERROR: No configuration file found")
        print("Please set GITLAB_TOKEN environment variable or create gl-migrate.conf or .token file")
        sys.exit(1)
else:
    # If token came from environment, still try to read other values from .token file
    token_file = script_dir / '.token'
    if token_file.exists():
        try:
            with open(token_file, 'r') as f:
                token_data = json.load(f)
                
                # Get group name if not already set
                if not GROUP_NAME:
                    GROUP_NAME = token_data.get('group')
                    if GROUP_NAME:
                        os.environ['GITLAB_GROUP'] = GROUP_NAME
                        log(f"Set GITLAB_GROUP environment variable to: {GROUP_NAME}")
                
                # Check for GitHub token if not already set
                if not GITHUB_TOKEN and 'github_token' in token_data:
                    GITHUB_TOKEN = token_data['github_token']
                    os.environ['GITHUB_TOKEN'] = GITHUB_TOKEN
                    log("Set GITHUB_TOKEN environment variable")
                
                # Check for custom GitLab URL if not already set
                if GITLAB_URL == 'https://gitlab.com' and 'gitlab_url' in token_data:
                    GITLAB_URL = token_data['gitlab_url']
                    os.environ['GITLAB_URL'] = GITLAB_URL
                    log(f"Set GITLAB_URL environment variable to: {GITLAB_URL}")
                
                # Check for project list file configuration
                if 'project_list_file' in token_data:
                    PROJECT_LIST_FILE = token_data['project_list_file']
                    log(f"Project list file configured: {PROJECT_LIST_FILE}")
                
                # Check for migrate repo values
                if 'migrate_repo_values' in token_data:
                    if isinstance(token_data['migrate_repo_values'], list):
                        MIGRATE_REPO_VALUES = token_data['migrate_repo_values']
                    elif isinstance(token_data['migrate_repo_values'], str):
                        MIGRATE_REPO_VALUES = [token_data['migrate_repo_values']]
                else:
                    MIGRATE_REPO_VALUES = ['Migrate']
                
                log(f"Additional settings loaded from .token file")
        except:
            # If we can't read the file, use defaults
            if not MIGRATE_REPO_VALUES:
                MIGRATE_REPO_VALUES = ['Migrate']
            log("Could not read additional settings from .token file")

# Check if GROUP_NAME is defined - if not, stop the script
if not GROUP_NAME:
    log("ERROR: No GitLab group defined. Please set GITLAB_GROUP environment variable or add 'group' to configuration file")
    sys.exit(1)

# Set output file path
OUTPUT_FILE = data_dir / 'gitlab-stats.csv'
log(f"Output file will be saved to: {OUTPUT_FILE}")

# Validate token exists
if not GITLAB_TOKEN:
    log("ERROR: No GitLab token found")
    sys.exit(1)

# Mask tokens for security in logs (show only first 8 and last 4 characters)
masked_token = f"{GITLAB_TOKEN[:8]}...{GITLAB_TOKEN[-4:]}" if len(GITLAB_TOKEN) > 12 else "***"
log(f"GitLab token found: {masked_token}")

# Display GitHub token status if available
if GITHUB_TOKEN:
    masked_gh_token = f"{GITHUB_TOKEN[:8]}...{GITHUB_TOKEN[-4:]}" if len(GITHUB_TOKEN) > 12 else "***"
    log(f"GitHub token also available: {masked_gh_token}")
else:
    log("No GitHub token found (optional)")

# GitLab API configuration
base_url = f"{GITLAB_URL}/api/v4"
headers = {'PRIVATE-TOKEN': GITLAB_TOKEN}

log(f"Using GitLab instance: {GITLAB_URL}")

# =======================
# Project Filtering
# =======================
def load_project_filter():
    """
    Load project filter from CSV file if configured.
    Returns a set of project names to process, or None to process all projects.
    """
    if not PROJECT_LIST_FILE:
        log("No project list file configured. Will process all projects in group.")
        return None
    
    # Build path to project list file (always in data subfolder)
    project_list_path = data_dir / PROJECT_LIST_FILE
    
    if not os.path.exists(project_list_path):
        log(f"WARNING: Project list file not found at: {project_list_path}")
        log("Falling back to processing all projects in group.")
        return None
    
    try:
        log(f"Loading project list from: {project_list_path}")
        projects_to_migrate = set()
        
        with open(project_list_path, 'r', encoding='utf-8') as csvfile:
            reader = csv.DictReader(csvfile)
            
            # Check if required columns exist
            if 'Migrate Repo' not in reader.fieldnames or 'Name' not in reader.fieldnames:
                log("ERROR: Required columns 'Name' or 'Migrate Repo' not found in CSV")
                log("Falling back to processing all projects in group.")
                return None
            
            # Filter projects based on Migrate Repo column
            for row in reader:
                migrate_value = row.get('Migrate Repo', '').strip()
                project_name = row.get('Name', '').strip()
                
                # Check if migrate value matches any of our filter values
                if migrate_value in MIGRATE_REPO_VALUES and project_name:
                    projects_to_migrate.add(project_name)
                    log(f"  Added '{project_name}' to filter (Migrate Repo: {migrate_value})")
        
        log(f"Loaded {len(projects_to_migrate)} projects to process from filter file")
        
        if len(projects_to_migrate) == 0:
            log(f"WARNING: No projects matched the filter values: {MIGRATE_REPO_VALUES}")
            log("Falling back to processing all projects in group.")
            return None
        
        return projects_to_migrate
        
    except Exception as e:
        log(f"ERROR: Failed to load project filter file: {e}")
        log("Falling back to processing all projects in group.")
        return None

# Load project filter
project_filter = load_project_filter()

# =======================
# Utility Functions
# =======================
def bytes_to_mb(bytes_value):
    """Convert bytes to megabytes"""
    if bytes_value is None:
        return 0
    return round(bytes_value / (1024 * 1024), 2)

def should_process_project(project, project_filter):
    """
    Determine if a project should be processed based on the filter.
    If filter is None, all projects are processed.
    """
    if project_filter is None:
        return True
    
    # Check various name fields that might match
    project_name = project.get('name', '')
    project_path = project.get('path', '')
    
    # Check if project name or path matches any in our filter
    return project_name in project_filter or project_path in project_filter

def add_subgroup_hierarchy(stats):
    """
    Add simplified subgroup hierarchy columns to the stats.
    Parses the 'path' field (path_with_namespace) and creates:
    - parent_group: The top-level/parent group
    - subgroups: Comma-separated list of all subgroups (excluding parent)
    - subgroup_count: Number of subgroups (excluding parent)
    
    Example:
        path='engineering/platform/devops/repo1' becomes:
        parent_group='engineering'
        subgroups='platform,devops'
        subgroup_count=2
    """
    if not stats:
        return stats
    
    # Add group hierarchy columns to each project
    enhanced_stats = []
    for idx, project_stat in enumerate(stats):
        # Create a copy to avoid modifying original
        enhanced_stat = {}
        
        # Add 'id' and 'name' first
        enhanced_stat['id'] = project_stat['id']
        enhanced_stat['name'] = project_stat['name']
        
        # Parse and add group hierarchy
        path = project_stat.get('path', '')
        path_parts = path.split('/')
        groups = path_parts[:-1]  # All parts except the last (project name)
        
        # Debug: Log first project's group structure
        if idx == 0:
            log(f"First project path: {path}")
            log(f"  Path parts: {path_parts}")
            log(f"  Groups extracted: {groups}")
            log(f"  Number of groups: {len(groups)}")
        
        # Add simplified group columns
        if len(groups) >= 1:
            enhanced_stat['parent_group'] = groups[0]
        else:
            enhanced_stat['parent_group'] = ''
        
        # Get subgroups (all except parent)
        if len(groups) > 1:
            subgroup_list = groups[1:]
            enhanced_stat['subgroups'] = ','.join(subgroup_list)
            enhanced_stat['subgroup_count'] = len(subgroup_list)
        else:
            enhanced_stat['subgroups'] = ''
            enhanced_stat['subgroup_count'] = 0
        
        # Add 'path' column after group columns
        enhanced_stat['path'] = project_stat['path']
        
        # Add all remaining fields in original order
        for key, value in project_stat.items():
            if key not in ['id', 'name', 'path']:  # Skip already added fields
                enhanced_stat[key] = value
        
        enhanced_stats.append(enhanced_stat)
    
    # Log summary of columns created
    if enhanced_stats:
        log(f"Created simplified group hierarchy columns: parent_group, subgroups, subgroup_count")
        # Show example with subgroups
        example_with_subgroups = next((s for s in enhanced_stats if s.get('subgroup_count', 0) > 0), None)
        if example_with_subgroups:
            log(f"  Example: {example_with_subgroups.get('name')} -> "
                f"parent_group='{example_with_subgroups.get('parent_group')}', "
                f"subgroups='{example_with_subgroups.get('subgroups')}', "
                f"count={example_with_subgroups.get('subgroup_count')}")
    
    return enhanced_stats

def fetch_all_projects(group_name, headers):
    """
    Fetch all projects from a GitLab group, handling pagination.
    Returns a list of all projects in the group and subgroups.
    """
    all_projects = []
    page = 1
    per_page = 100
    
    # Continue fetching pages until no more projects are found
    while True:
        log(f"Fetching projects page {page} (up to {per_page} per page)...")
        # Build API URL with parameters for subgroups and statistics
        group_projects_url = f"{base_url}/groups/{group_name}/projects?per_page={per_page}&page={page}&include_subgroups=true&statistics=true"
        
        try:
            # Make API request with timeout
            response = requests.get(group_projects_url, headers=headers, timeout=30)
            
            # Handle non-200 responses
            if response.status_code != 200:
                log(f"ERROR: Failed to fetch projects. Status code: {response.status_code}")
                log(f"Response: {response.text[:500]}...")
                
                # Provide specific error messages based on status code
                if response.status_code == 401:
                    log("ERROR: Unauthorized - Check your token validity")
                elif response.status_code == 403:
                    log("ERROR: Forbidden - Check token permissions")
                elif response.status_code == 404:
                    log(f"ERROR: Group '{group_name}' not found")
                
                return None
            
            # Parse JSON response
            projects = response.json()
            
            # Check if we've reached the end of pagination
            if not projects:
                log(f"No more projects found. Total projects: {len(all_projects)}")
                break
                
            # Add projects to our collection
            all_projects.extend(projects)
            log(f"Added {len(projects)} projects. Total so far: {len(all_projects)}")
            
            # Check if there are more pages using response headers
            if 'X-Next-Page' in response.headers and response.headers['X-Next-Page']:
                page += 1
            else:
                log(f"Reached last page. Total projects: {len(all_projects)}")
                break
                
        except requests.exceptions.Timeout:
            log(f"ERROR: Request timed out on page {page}")
            return None
        except requests.exceptions.ConnectionError as e:
            log(f"ERROR: Connection error: {e}")
            return None
        except Exception as e:
            log(f"ERROR: Unexpected error fetching projects: {e}")
            return None
    
    return all_projects

def get_branch_count(project_id, headers):
    """
    Get the number of branches in a project.
    Uses HEAD request first for efficiency, falls back to GET if needed.
    """
    try:
        # Try HEAD request first (more efficient)
        branches_url = f"{base_url}/projects/{project_id}/repository/branches?per_page=1"
        response = requests.head(branches_url, headers=headers, timeout=10)
        
        # If HEAD request works and has X-Total header, use it
        if response.status_code == 200 and 'X-Total' in response.headers:
            return int(response.headers['X-Total'])
        
        # Fall back to GET request if HEAD didn't work
        response = requests.get(branches_url, headers=headers, timeout=10)
        
        if response.status_code == 200:
            # Prefer X-Total header if available
            if 'X-Total' in response.headers:
                return int(response.headers['X-Total'])
            # Fallback: count branches in response (only first page, may be inaccurate for repos with >100 branches)
            return len(response.json())
        else:
            return 0
    except Exception as e:
        log(f"    WARNING: Could not fetch branch count: {e}")
        return 0

def get_exportable_model_counts(project_id, headers):
    """
    Get counts for all models that can be exported via gl-exporter.
    Returns a dictionary with counts for various exportable resources.
    Based on gl-exporter capabilities table.
    """
    model_counts = {
        'users_count': 0,           # Project members
        'protected_branches': 0,     # Protected branch count
        'merge_requests': 0,         # Pull requests
        'merge_request_notes': 0,    # MR comments
        'issues': 0,                 # Issues
        'issue_notes': 0,            # Issue comments
        'webhooks': 0,               # Webhooks
        'tags': 0,                   # Releases/Tags
        'commit_comments': 0,        # Commit comments
        'has_wiki': False,           # Wiki enabled
        'milestones': 0              # Milestones
    }
    
    try:
        # Get project members (users)
        log(f"    Fetching project members...")
        members_url = f"{base_url}/projects/{project_id}/members/all?per_page=1"
        response = requests.head(members_url, headers=headers, timeout=10)
        if response.status_code == 200 and 'X-Total' in response.headers:
            model_counts['users_count'] = int(response.headers['X-Total'])
            log(f"    Found {model_counts['users_count']} project members")
        
        # Get protected branches
        log(f"    Fetching protected branches...")
        protected_url = f"{base_url}/projects/{project_id}/protected_branches?per_page=1"
        response = requests.head(protected_url, headers=headers, timeout=10)
        if response.status_code == 200 and 'X-Total' in response.headers:
            model_counts['protected_branches'] = int(response.headers['X-Total'])
            log(f"    Found {model_counts['protected_branches']} protected branches")
        
        # Get merge requests (all states)
        log(f"    Fetching merge requests...")
        mr_url = f"{base_url}/projects/{project_id}/merge_requests?state=all&per_page=1"
        response = requests.head(mr_url, headers=headers, timeout=10)
        if response.status_code == 200 and 'X-Total' in response.headers:
            model_counts['merge_requests'] = int(response.headers['X-Total'])
            log(f"    Found {model_counts['merge_requests']} merge requests")
        
        # Get merge request notes (sample first 10 MRs to estimate)
        if model_counts['merge_requests'] > 0:
            log(f"    Estimating merge request notes...")
            mr_list_url = f"{base_url}/projects/{project_id}/merge_requests?state=all&per_page=10"
            response = requests.get(mr_list_url, headers=headers, timeout=15)
            if response.status_code == 200:
                mrs = response.json()
                total_notes = 0
                for mr in mrs[:5]:  # Sample first 5 to avoid timeout
                    notes_url = f"{base_url}/projects/{project_id}/merge_requests/{mr['iid']}/notes?per_page=1"
                    notes_response = requests.head(notes_url, headers=headers, timeout=5)
                    if notes_response.status_code == 200 and 'X-Total' in notes_response.headers:
                        total_notes += int(notes_response.headers['X-Total'])
                
                # Estimate total based on sample
                if len(mrs) > 0:
                    avg_notes = total_notes / min(len(mrs), 5)
                    model_counts['merge_request_notes'] = int(avg_notes * model_counts['merge_requests'])
                    log(f"    Estimated {model_counts['merge_request_notes']} merge request notes")
        
        # Get issues
        log(f"    Fetching issues...")
        issues_url = f"{base_url}/projects/{project_id}/issues?per_page=1"
        response = requests.head(issues_url, headers=headers, timeout=10)
        if response.status_code == 200 and 'X-Total' in response.headers:
            model_counts['issues'] = int(response.headers['X-Total'])
            log(f"    Found {model_counts['issues']} issues")
        
        # Get issue notes (sample first 10 issues to estimate)
        if model_counts['issues'] > 0:
            log(f"    Estimating issue notes...")
            issues_list_url = f"{base_url}/projects/{project_id}/issues?per_page=10"
            response = requests.get(issues_list_url, headers=headers, timeout=15)
            if response.status_code == 200:
                issues = response.json()
                total_notes = 0
                for issue in issues[:5]:  # Sample first 5 to avoid timeout
                    notes_url = f"{base_url}/projects/{project_id}/issues/{issue['iid']}/notes?per_page=1"
                    notes_response = requests.head(notes_url, headers=headers, timeout=5)
                    if notes_response.status_code == 200 and 'X-Total' in notes_response.headers:
                        total_notes += int(notes_response.headers['X-Total'])
                
                # Estimate total based on sample
                if len(issues) > 0:
                    avg_notes = total_notes / min(len(issues), 5)
                    model_counts['issue_notes'] = int(avg_notes * model_counts['issues'])
                    log(f"    Estimated {model_counts['issue_notes']} issue notes")
        
        # Get webhooks (project hooks)
        log(f"    Fetching webhooks...")
        hooks_url = f"{base_url}/projects/{project_id}/hooks?per_page=1"
        response = requests.head(hooks_url, headers=headers, timeout=10)
        if response.status_code == 200 and 'X-Total' in response.headers:
            model_counts['webhooks'] = int(response.headers['X-Total'])
            log(f"    Found {model_counts['webhooks']} webhooks")
        
        # Get tags (releases)
        log(f"    Fetching tags/releases...")
        tags_url = f"{base_url}/projects/{project_id}/repository/tags?per_page=1"
        response = requests.head(tags_url, headers=headers, timeout=10)
        if response.status_code == 200 and 'X-Total' in response.headers:
            model_counts['tags'] = int(response.headers['X-Total'])
            log(f"    Found {model_counts['tags']} tags/releases")
        
        # Get commit comments (sample recent commits to estimate)
        log(f"    Estimating commit comments...")
        commits_url = f"{base_url}/projects/{project_id}/repository/commits?per_page=10"
        response = requests.get(commits_url, headers=headers, timeout=15)
        if response.status_code == 200:
            commits = response.json()
            total_comments = 0
            for commit in commits[:5]:  # Sample first 5 commits
                comments_url = f"{base_url}/projects/{project_id}/repository/commits/{commit['id']}/comments?per_page=1"
                comments_response = requests.head(comments_url, headers=headers, timeout=5)
                if comments_response.status_code == 200 and 'X-Total' in comments_response.headers:
                    total_comments += int(comments_response.headers['X-Total'])
            
            model_counts['commit_comments'] = total_comments
            log(f"    Found {model_counts['commit_comments']} commit comments (sample)")
        
        # Check for wiki
        log(f"    Checking wiki status...")
        # Get project details to check wiki enabled status
        project_url = f"{base_url}/projects/{project_id}"
        response = requests.get(project_url, headers=headers, timeout=10)
        if response.status_code == 200:
            project_data = response.json()
            wiki_enabled = project_data.get('wiki_enabled', False)
            
            # If wiki is enabled, check if it has any pages
            if wiki_enabled:
                wikis_url = f"{base_url}/projects/{project_id}/wikis?per_page=1"
                wikis_response = requests.head(wikis_url, headers=headers, timeout=5)
                if wikis_response.status_code == 200:
                    # Check if there are any wiki pages
                    if 'X-Total' in wikis_response.headers:
                        wiki_pages = int(wikis_response.headers['X-Total'])
                        model_counts['has_wiki'] = wiki_pages > 0
                    else:
                        # Try GET to check for pages
                        wikis_response = requests.get(wikis_url, headers=headers, timeout=5)
                        if wikis_response.status_code == 200:
                            wiki_pages = len(wikis_response.json())
                            model_counts['has_wiki'] = wiki_pages > 0
            
            log(f"    Wiki enabled: {model_counts['has_wiki']}")
        
        # Get milestones
        log(f"    Fetching milestones...")
        milestones_url = f"{base_url}/projects/{project_id}/milestones?per_page=1"
        response = requests.head(milestones_url, headers=headers, timeout=10)
        if response.status_code == 200 and 'X-Total' in response.headers:
            model_counts['milestones'] = int(response.headers['X-Total'])
            log(f"    Found {model_counts['milestones']} milestones")
        
        return model_counts
        
    except Exception as e:
        log(f"    ERROR: Failed to get exportable model counts: {e}")
        return model_counts

def get_repository_file_count(project_id, headers):
    """
    Get the actual total number of files in the repository.
    Handles large repositories with special logic and pagination.
    """
    try:
        # First, get project info to determine repository size and default branch
        project_url = f"{base_url}/projects/{project_id}"
        project_response = requests.get(project_url, headers=headers, timeout=10)
        
        if project_response.status_code != 200:
            log(f"    WARNING: Could not fetch project info (status: {project_response.status_code})")
            return 0
        
        project_info = project_response.json()
        default_branch = project_info.get('default_branch')
        
        # Check repository size to determine counting strategy
        repo_size = project_info.get('statistics', {}).get('repository_size', 0)
        repo_size_mb = repo_size / (1024 * 1024)
        
        # Log if this is a large repository
        if repo_size_mb > 10000:  # If repo is larger than 10GB
            log(f"    Large repository detected ({repo_size_mb:.2f} MB). Using optimized counting method...")
        
        # Handle empty repositories
        if not default_branch:
            log(f"    WARNING: No default branch found - repository might be empty")
            return 0
            
        log(f"    Using default branch: {default_branch}")
        
        # Special handling for very large repositories (>10GB)
        if repo_size_mb > 10000:
            try:
                # Verify repository is accessible
                commits_url = f"{base_url}/projects/{project_id}/repository/commits?ref={default_branch}&per_page=1"
                commits_response = requests.head(commits_url, headers=headers, timeout=10)
                
                if commits_response.status_code == 200:
                    # Try to use search API for file counting (more efficient for large repos)
                    search_url = f"{base_url}/projects/{project_id}/search?scope=blobs&search=*"
                    search_response = requests.head(search_url, headers=headers, timeout=10)
                    
                    if search_response.status_code == 200 and 'X-Total' in search_response.headers:
                        file_count = int(search_response.headers.get('X-Total', 0))
                        log(f"    File count from search API: {file_count}")
                        if file_count > 0:
                            return file_count
                    
                    # If search doesn't work, log and return 0
                    log(f"    Very large repository - exact file count unavailable")
                    return 0
                    
            except Exception as e:
                log(f"    Error with alternative counting method: {e}")
        
        # Standard counting method for normal-sized repositories
        tree_url = f"{base_url}/projects/{project_id}/repository/tree"
        
        all_files = []
        page = 1
        per_page = 100
        max_pages = 50  # Limit to prevent timeout on large repos
        
        # Paginate through repository tree
        while page <= max_pages:
            params = {
                'recursive': 'true',  # Get all files recursively
                'per_page': per_page,
                'page': page,
                'ref': default_branch
            }
            
            try:
                response = requests.get(tree_url, headers=headers, params=params, timeout=30)
                
                # Handle different response codes
                if response.status_code == 404:
                    log(f"    WARNING: Repository tree not found - repository might be empty")
                    return 0
                elif response.status_code != 200:
                    log(f"    WARNING: Could not fetch repository tree (status: {response.status_code})")
                    break
                
                items = response.json()
                
                # Check if we've reached the end of results
                if not items:
                    break
                
                # Count only files (blobs), not directories
                files_on_page = [item for item in items if item.get('type') == 'blob']
                all_files.extend(files_on_page)
                
                log(f"    Page {page}: Found {len(files_on_page)} files (total so far: {len(all_files)})")
                
                # Check if there are more pages than we can process
                total_pages_header = response.headers.get('X-Total-Pages')
                if total_pages_header:
                    total_pages = int(total_pages_header)
                    if page == 1:
                        log(f"    Total pages available: {total_pages}")
                    if total_pages > max_pages:
                        log(f"    WARNING: Repository has {total_pages} pages of files. Will process first {max_pages} pages.")
                        # Estimate total files based on current sample
                        if len(all_files) > 0:
                            estimated_total = int((len(all_files) / page) * total_pages)
                            log(f"    Estimated total files: ~{estimated_total}")
                
                # Check if there's a next page
                next_page = response.headers.get('X-Next-Page')
                if not next_page:
                    break
                    
                page += 1
                
            except requests.exceptions.Timeout:
                log(f"    WARNING: Timeout on page {page}")
                break
            except Exception as e:
                log(f"    WARNING: Error on page {page}: {e}")
                break
        
        file_count = len(all_files)
        
        # Log final count with context
        if page > max_pages:
            log(f"    Large repository detected. Counted {file_count} files in first {max_pages} pages (minimum count).")
        else:
            log(f"    Total files counted: {file_count}")
        
        return file_count
        
    except Exception as e:
        log(f"    ERROR: Failed to count repository files: {type(e).__name__}: {e}")
        return 0

def get_repository_stats_via_api(project_id, headers):
    """
    Get comprehensive repository statistics using multiple API endpoints.
    Returns a dictionary with various repository metrics.
    """
    # Initialize statistics dictionary with default values
    stats = {
        'file_count': 0,
        'repository_size': 0,
        'storage_size': 0,
        'commit_count': 0,
        'branch_count': 0,
        'object_count': 0,
        'all_branches_file_count': 0,
        'has_large_file': False,
        'exceeds_6gb': False,
        'exceeds_2gb': False,
        'has_pipeline': False  # Add pipeline detection
    }
    
    try:
        # Get project details with full statistics
        project_url = f"{base_url}/projects/{project_id}?statistics=true&license=true"
        response = requests.get(project_url, headers=headers, timeout=20)
        
        if response.status_code == 200:
            project_data = response.json()
            
            # Extract statistics from API response
            statistics = project_data.get('statistics', {})
            if statistics:
                stats['repository_size'] = statistics.get('repository_size', 0)
                stats['storage_size'] = statistics.get('storage_size', 0)
                stats['commit_count'] = statistics.get('commit_count', 0)
                
                # Check size thresholds using total storage size (not just repository)
                storage_size_bytes = stats['storage_size']
                stats['exceeds_2gb'] = storage_size_bytes > (2 * 1024 * 1024 * 1024)  # 2GB in bytes
                stats['exceeds_6gb'] = storage_size_bytes > (6 * 1024 * 1024 * 1024)  # 6GB in bytes
                
                log(f"    API Statistics - Repository size: {stats['repository_size']} bytes, "
                    f"Storage size: {stats['storage_size']} bytes, "
                    f"Commit count: {stats['commit_count']}")
        
        # Get detailed file count for default branch
        stats['file_count'] = get_repository_file_count(project_id, headers)

        # Get branch count
        stats['branch_count'] = get_branch_count(project_id, headers)

        # Get unique commit count across all branches for accurate validation
        all_branch_commits = get_all_branches_commit_count(project_id, headers)
        if all_branch_commits > 0:
            stats['commit_count'] = all_branch_commits
        
        # Check for pipeline configuration
        stats['has_pipeline'] = check_for_pipeline_config(project_id, headers)
        
        # Get file count across all branches and check for large files
        all_branches_stats = get_all_branches_file_stats(project_id, headers)
        stats['all_branches_file_count'] = all_branches_stats['total_files']
        stats['has_large_file'] = all_branches_stats['has_large_file']

        # If API did not return sizes, fall back to summed blob sizes across branches
        if stats['repository_size'] == 0 and all_branches_stats.get('total_bytes', 0) > 0:
            stats['repository_size'] = all_branches_stats['total_bytes']
            stats['storage_size'] = all_branches_stats['total_bytes']
            log(f"    Repository size fallback from branch scan: {bytes_to_mb(stats['repository_size'])} MB")
        
        # Calculate total object count (commits, tags, branches, files, etc.)
        stats['object_count'] = get_repository_object_count(project_id, headers, stats)
        
        return stats
        
    except Exception as e:
        log(f"    WARNING: Error fetching repository stats: {e}")
        return stats
def check_for_pipeline_config(project_id, headers):
    """
    Check if the project has a GitLab CI/CD pipeline configuration file.
    Looks for .gitlab-ci.yml, .gitlab-ci.yaml, or gitlab-ci.yml in the root directory.
    Returns True if found, False otherwise.
    """
    try:
        # Get project info to determine default branch
        project_url = f"{base_url}/projects/{project_id}"
        project_response = requests.get(project_url, headers=headers, timeout=10)
        
        if project_response.status_code != 200:
            log(f"    WARNING: Could not fetch project info for pipeline check")
            return False
        
        project_info = project_response.json()
        default_branch = project_info.get('default_branch')
        
        if not default_branch:
            log(f"    WARNING: No default branch found - cannot check for pipeline")
            return False
        
        # List of possible GitLab CI configuration filenames
        pipeline_files = [
            '.gitlab-ci.yml',
            '.gitlab-ci.yaml',
            'gitlab-ci.yml',
            'gitlab-ci.yaml',
            '.gitlab/ci.yml',
            '.gitlab/ci.yaml'
        ]
        
        log(f"    Checking for pipeline configuration files...")
        
        # Check each possible pipeline file
        for pipeline_file in pipeline_files:
            # Encode the file path for URL
            encoded_file_path = requests.utils.quote(pipeline_file, safe='')
            file_url = f"{base_url}/projects/{project_id}/repository/files/{encoded_file_path}"
            
            # Add the branch reference
            params = {'ref': default_branch}
            
            try:
                # Use HEAD request for efficiency (we just need to know if file exists)
                response = requests.head(file_url, headers=headers, params=params, timeout=5)
                
                if response.status_code == 200:
                    log(f"    Found pipeline configuration: {pipeline_file}")
                    return True
                
            except Exception:
                # Continue checking other files if this one fails
                continue
        
        log(f"    No pipeline configuration found")
        return False
        
    except Exception as e:
        log(f"    ERROR: Failed to check for pipeline configuration: {e}")
        return False

def check_file_exists(project_id, file_path, ref_branch, headers):
    """
    Check if a file exists in the repository using HEAD request (efficient).
    Returns True if file exists, False otherwise.
    """
    try:
        encoded_path = requests.utils.quote(file_path, safe='')
        file_url = f"{base_url}/projects/{project_id}/repository/files/{encoded_path}"
        params = {'ref': ref_branch}
        response = requests.head(file_url, headers=headers, params=params, timeout=8)
        return response.status_code == 200
    except Exception:
        return False

def get_releases_count(project_id, headers):
    """
    Get the number of releases in the project using HEAD request.
    Returns the count of releases.
    """
    try:
        releases_url = f"{base_url}/projects/{project_id}/releases?per_page=1"
        response = requests.head(releases_url, headers=headers, timeout=10)
        if response.status_code == 200 and 'X-Total' in response.headers:
            return int(response.headers.get('X-Total', 0))
        # Fallback to GET if HEAD doesn't return X-Total
        response = requests.get(releases_url, headers=headers, timeout=10)
        if response.status_code == 200:
            return len(response.json())
        return 0
    except Exception as e:
        log(f"    WARNING: Could not fetch releases count: {e}")
        return 0

def check_lfs_enabled(project_id, headers):
    """
    Check if Git LFS is enabled and get LFS statistics.
    Returns dictionary with has_lfs, lfs_file_count, lfs_total_size_bytes, and lfs_total_size_mb.
    
    Uses a three-step detection strategy:
    1. Check project statistics API for lfs_objects_size (most reliable)
    2. Check .gitattributes file for LFS filter patterns
    3. Query GitLab LFS API for object count and sizes
    """
    lfs_info = {
        'has_lfs': False,
        'lfs_file_count': 0,
        'lfs_total_size_bytes': 0,
        'lfs_total_size_mb': 0
    }
    
    try:
        default_branch = None
        
        # STEP 0: Get LFS size from project statistics (most reliable method)
        log(f"    Checking project statistics for LFS data...")
        try:
            project_url = f"{base_url}/projects/{project_id}?statistics=true"
            project_response = requests.get(project_url, headers=headers, timeout=10)
            
            if project_response.status_code == 200:
                project_info = project_response.json()
                default_branch = project_info.get('default_branch')
                
                # Get LFS size from statistics
                statistics = project_info.get('statistics', {})
                lfs_objects_size = statistics.get('lfs_objects_size', 0)
                
                if lfs_objects_size > 0:
                    lfs_info['has_lfs'] = True
                    lfs_info['lfs_total_size_bytes'] = int(lfs_objects_size)
                    lfs_info['lfs_total_size_mb'] = round(lfs_objects_size / (1024 * 1024), 2)
                    log(f"    ✓ LFS detected from statistics: {lfs_info['lfs_total_size_mb']} MB")
                
        except Exception as e:
            log(f"    Could not fetch LFS size from project statistics: {e}")
        
        # STEP 1: Check for .gitattributes file with LFS filter
        log(f"    Checking for LFS configuration...")
        lfs_patterns = []
        
        try:
            # Get project info if not already fetched
            if not default_branch:
                project_url = f"{base_url}/projects/{project_id}"
                project_response = requests.get(project_url, headers=headers, timeout=10)
                
                if project_response.status_code == 200:
                    project_info = project_response.json()
                    default_branch = project_info.get('default_branch')
                
            if default_branch:
                # Fetch .gitattributes file
                gitattributes_path = requests.utils.quote('.gitattributes', safe='')
                gitattributes_url = f"{base_url}/projects/{project_id}/repository/files/{gitattributes_path}"
                params = {'ref': default_branch}
                
                gitattributes_response = requests.get(gitattributes_url, headers=headers, params=params, timeout=10)
                
                if gitattributes_response.status_code == 200:
                    gitattributes_data = gitattributes_response.json()
                    content = gitattributes_data.get('content', '')
                    
                    # Decode base64 content
                    import base64
                    decoded_content = base64.b64decode(content).decode('utf-8')
                    
                    # Parse .gitattributes for LFS patterns
                    for line in decoded_content.splitlines():
                        line = line.strip()
                        if line and 'filter=lfs' in line and not line.startswith('#'):
                            lfs_info['has_lfs'] = True
                            # Extract the pattern (e.g., "*.psd")
                            parts = line.split()
                            if parts:
                                pattern = parts[0]
                                lfs_patterns.append(pattern)
                    
                    if lfs_info['has_lfs'] and lfs_patterns:
                        log(f"    ✓ Found LFS configuration in .gitattributes with patterns: {lfs_patterns}")
                
        except Exception as e:
            log(f"    No .gitattributes file or error reading it: {e}")
        
        # STEP 2: Try GitLab API to get LFS objects count (if not already detected)
        # Only query the LFS objects API if we need the file count
        try:
            # GitLab LFS objects API endpoint
            # Note: This may require specific permissions or GitLab version
            lfs_url = f"{base_url}/projects/{project_id}/lfs_objects"
            lfs_response = requests.get(lfs_url, headers=headers, timeout=15)
            
            if lfs_response.status_code == 200:
                lfs_objects = lfs_response.json()
                
                if isinstance(lfs_objects, list) and len(lfs_objects) > 0:
                    lfs_info['has_lfs'] = True
                    lfs_info['lfs_file_count'] = len(lfs_objects)
                    
                    # Only calculate size from API if we didn't get it from statistics
                    if lfs_info['lfs_total_size_bytes'] == 0:
                        total_size_bytes = 0
                        for obj in lfs_objects:
                            size = obj.get('size', 0)
                            total_size_bytes += size
                        
                        # Store both bytes and MB
                        lfs_info['lfs_total_size_bytes'] = int(total_size_bytes)
                        lfs_info['lfs_total_size_mb'] = round(total_size_bytes / (1024 * 1024), 2)
                    
                    log(f"    ✓ LFS objects API: {lfs_info['lfs_file_count']} files")
            
            elif lfs_response.status_code == 404:
                log(f"    LFS objects API endpoint not available (404)")
                
        except Exception as e:
            log(f"    Could not query LFS objects API: {e}")
        
        # STEP 3: Fallback - scan repository tree for LFS pointer files if .gitattributes indicates LFS
        # but API didn't return objects (common in some GitLab configurations)
        if lfs_info['has_lfs'] and lfs_info['lfs_file_count'] == 0 and lfs_patterns:
            try:
                log(f"    Scanning repository for LFS pointer files...")
                # Get repository tree recursively
                tree_url = f"{base_url}/projects/{project_id}/repository/tree"
                params = {
                    'recursive': 'true',
                    'per_page': 100,
                    'ref': default_branch
                }
                
                tree_response = requests.get(tree_url, headers=headers, params=params, timeout=20)
                
                if tree_response.status_code == 200:
                    items = tree_response.json()
                    lfs_pointer_count = 0
                    
                    # Check files matching LFS patterns
                    for item in items[:500]:  # Limit scan to first 500 items for performance
                        if item.get('type') == 'blob':
                            file_path = item.get('path', '')
                            
                            # Check if file matches any LFS pattern
                            import fnmatch
                            for pattern in lfs_patterns:
                                if fnmatch.fnmatch(file_path, pattern):
                                    lfs_pointer_count += 1
                                    break
                    
                    if lfs_pointer_count > 0:
                        lfs_info['lfs_file_count'] = lfs_pointer_count
                        log(f"    ✓ Repository scan: {lfs_pointer_count} files matching LFS patterns (estimated)")
                        
            except Exception as e:
                log(f"    Error scanning for LFS pointer files: {e}")
        
        # Final summary
        if lfs_info['has_lfs']:
            if lfs_info['lfs_total_size_mb'] > 0:
                log(f"    📊 LFS Summary: {lfs_info['lfs_file_count']} files, {lfs_info['lfs_total_size_mb']} MB total")
            else:
                log(f"    📊 LFS Summary: LFS enabled but size data not available")
        
        return lfs_info
        
    except Exception as e:
        log(f"    ERROR: Failed to check LFS: {e}")
        return lfs_info
    
    
def get_repository_object_count(project_id, headers, existing_stats):
    """
    Calculate total number of objects in repository.
    Includes commits, branches, tags, files, and merge requests.
    """
    try:
        object_count = 0
        
        # Start with existing commit count
        object_count += existing_stats.get('commit_count', 0)
        
        # Add existing branch count
        object_count += existing_stats.get('branch_count', 0)
        
        # Get and add tag count
        tags_url = f"{base_url}/projects/{project_id}/repository/tags"
        tags_response = requests.head(tags_url, headers=headers, timeout=10)
        if tags_response.status_code == 200 and 'X-Total' in tags_response.headers:
            tag_count = int(tags_response.headers.get('X-Total', 0))
            object_count += tag_count
            log(f"    Found {tag_count} tags")
        
        # Add file and directory objects
        # Each file is a blob, estimate directory tree objects as files/10
        file_count = existing_stats.get('all_branches_file_count', existing_stats.get('file_count', 0))
        estimated_tree_objects = max(file_count // 10, 1)  # Rough estimate of directory objects
        object_count += file_count + estimated_tree_objects
        
        # Get and add merge request count (each MR creates additional commits)
        mr_url = f"{base_url}/projects/{project_id}/merge_requests?state=all&per_page=1"
        mr_response = requests.head(mr_url, headers=headers, timeout=10)
        if mr_response.status_code == 200 and 'X-Total' in mr_response.headers:
            mr_count = int(mr_response.headers.get('X-Total', 0))
            object_count += mr_count  # Each MR has at least one commit
            log(f"    Found {mr_count} merge requests")
        
        log(f"    Total estimated objects: {object_count}")
        return object_count
        
    except Exception as e:
        log(f"    WARNING: Error calculating object count: {e}")
        # Return minimum estimate based on commits and files
        return existing_stats.get('commit_count', 0) + existing_stats.get('file_count', 0)

def get_all_branches_commit_count(project_id, headers):
    """
    Count unique commits across all branches for accurate migration validation.
    """
    try:
        commit_shas = set()
        branch_page = 1
        branches_url = f"{base_url}/projects/{project_id}/repository/branches"

        while True:
            branch_params = {'per_page': 100, 'page': branch_page}
            branches_response = requests.get(branches_url, headers=headers, params=branch_params, timeout=15)
            if branches_response.status_code != 200:
                log(f"    WARNING: Could not fetch branches for commit counting (page {branch_page})")
                break

            branches = branches_response.json()
            if not branches:
                break

            for idx, branch in enumerate(branches):
                branch_name = branch.get('name')
                if not branch_name:
                    continue

                try:
                    commit_page = 1
                    commits_url = f"{base_url}/projects/{project_id}/repository/commits"
                    while True:
                        commit_params = {
                            'ref_name': branch_name,
                            'per_page': 100,
                            'page': commit_page
                        }

                        commits_response = requests.get(commits_url, headers=headers, params=commit_params, timeout=15)
                        if commits_response.status_code != 200:
                            break

                        commits = commits_response.json()
                        if not commits:
                            break

                        for commit in commits:
                            sha = commit.get('id')
                            if sha:
                                commit_shas.add(sha)

                        next_commit_page = commits_response.headers.get('X-Next-Page')
                        if not next_commit_page:
                            break
                        commit_page = int(next_commit_page)

                    if branch_page == 1 and idx < 3:
                        log(f"    Branch '{branch_name}': accumulated {len(commit_shas)} unique commits so far")

                except Exception as e:
                    log(f"    WARNING: Error counting commits for branch {branch_name}: {e}")
                    continue

            next_branch_page = branches_response.headers.get('X-Next-Page')
            if not next_branch_page:
                break
            branch_page = int(next_branch_page)

        total_commits = len(commit_shas)
        log(f"    Total unique commits across all branches: {total_commits}")
        return total_commits

    except Exception as e:
        log(f"    ERROR: Failed to count commits across branches: {e}")
        return 0

def get_all_branches_file_stats(project_id, headers):
    """
    Get file count across all branches and check for large files using full pagination.
    """
    stats = {
        'total_files': 0,
        'has_large_file': False,
        'total_bytes': 0  # Initialize total_bytes to prevent KeyError
    }
    
    try:
        # Get all branches in the repository (paginate to cover all)
        # Track unique blob occurrences by (path, sha) so branched versions count distinctly
        unique_blobs = set()  # (path, sha) pairs
        large_file_found = False
        branch_page = 1
        total_branches_processed = 0
        branches_url = f"{base_url}/projects/{project_id}/repository/branches"
        while True:
            branch_params = {'per_page': 100, 'page': branch_page}
            branches_response = requests.get(branches_url, headers=headers, params=branch_params, timeout=15)
            if branches_response.status_code != 200:
                log(f"    WARNING: Could not fetch branches for file stats (page {branch_page})")
                break

            branches = branches_response.json()
            if not branches:
                break

            log(f"    Checking files across page {branch_page} branches ({len(branches)} branches on this page)...")

            # Process each branch
            for idx, branch in enumerate(branches):
                branch_name = branch.get('name')
                if not branch_name:
                    continue

                total_branches_processed += 1
                try:
                    tree_url = f"{base_url}/projects/{project_id}/repository/tree"
                    page = 1
                    branch_files = 0

                    # Paginate through branch files (full pagination for accuracy)
                    while True:
                        params = {
                            'recursive': 'true',
                            'per_page': 100,
                            'page': page,
                            'ref': branch_name
                        }

                        tree_response = requests.get(tree_url, headers=headers, params=params, timeout=15)

                        if tree_response.status_code != 200:
                            break

                        items = tree_response.json()
                        if not items:
                            break

                        # Process each file in the response
                        for item in items:
                            if item.get('type') == 'blob':
                                file_path = item.get('path', '')
                                blob_sha = item.get('id')
                                if file_path:
                                    key = (file_path, blob_sha)
                                    if key not in unique_blobs:
                                        unique_blobs.add(key)
                                        size = item.get('size', 0) or 0
                                        stats['total_bytes'] += size
                                    branch_files += 1

                                # Check for potentially large files based on extension
                                if not large_file_found and file_path:
                                    large_file_patterns = ['.zip', '.tar', '.gz', '.iso', '.dmg', '.exe', '.deb', '.rpm',
                                                         '.pkg', '.msi', '.war', '.ear', '.jar', '.pdf', '.mp4',
                                                         '.mov', '.avi', '.mkv', '.mp3', '.wav', '.flac']
                                    if any(file_path.lower().endswith(ext) for ext in large_file_patterns):
                                        file_url = f"{base_url}/projects/{project_id}/repository/files/{requests.utils.quote(file_path, safe='')}"
                                        file_params = {'ref': branch_name}
                                        try:
                                            file_response = requests.head(file_url, headers=headers, params=file_params, timeout=5)
                                            if file_response.status_code == 200:
                                                file_size_str = file_response.headers.get('X-Gitlab-Size', '0')
                                                try:
                                                    file_size = int(file_size_str)
                                                    if file_size > 100 * 1024 * 1024:
                                                        large_file_found = True
                                                        log(f"    Found large file (>100MB): {file_path} ({file_size / (1024*1024):.1f} MB)")
                                                except ValueError:
                                                    pass
                                        except Exception:
                                            pass

                        next_tree_page = tree_response.headers.get('X-Next-Page')
                        if not next_tree_page:
                            break
                        page = int(next_tree_page)

                    if idx < 3 and branch_page == 1:
                        log(f"    Branch '{branch_name}': found {branch_files} files in this branch")

                except Exception as e:
                    log(f"    WARNING: Error checking branch {branch_name}: {e}")
                    continue

            next_branch_page = branches_response.headers.get('X-Next-Page')
            if not next_branch_page:
                break
            branch_page = int(next_branch_page)

        stats['total_files'] = len(unique_blobs)  # Count of unique files across branches
        stats['has_large_file'] = large_file_found

        log(f"    Processed {total_branches_processed} branches; total unique files across branches: {stats['total_files']}")

        return stats

    except Exception as e:
        log(f"    ERROR: Failed to get all branches file stats: {e}")
        return stats

# =======================
# Main Execution
# =======================
if __name__ == "__main__":
    # Fetch all projects from the specified GitLab group
    log(f"Fetching all projects from group: {GROUP_NAME}")
    projects = fetch_all_projects(GROUP_NAME, headers)

    # Handle fetch failures
    if projects is None:
        log("ERROR: Failed to fetch projects")
        sys.exit(1)

    log(f"Successfully fetched {len(projects)} projects from GitLab")

    # Apply project filter if configured
    if project_filter is not None:
        # Filter projects based on the loaded filter
        filtered_projects = [p for p in projects if should_process_project(p, project_filter)]
        log(f"After filtering, {len(filtered_projects)} projects will be processed")
        projects = filtered_projects
    
    # Handle empty results
    if not projects:
        log("WARNING: No projects to process after filtering")
        sys.exit(0)

    # Initialize data collection
    stats = []
    failed_projects = []

    # Process each project to collect detailed statistics
    log("Collecting detailed statistics for each project...")
    for idx, project in enumerate(projects):
        project_id = project['id']
        project_name = project['name']
        
        # Show progress every 10 projects
        if (idx + 1) % 10 == 0:
            log(f"Processing project {idx + 1}/{len(projects)}...")
        
        try:
            log(f"Processing: {project_name}")
            
            # Check project archive status
            is_archived = project.get('archived', False)
            if is_archived:
                log(f"  Note: This project is ARCHIVED")
            
            # Get default branch name
            default_branch = project.get('default_branch', 'main')
            
            # Fetch contributor statistics
            log(f"  Fetching contributors...")
            commit_stats_url = f"{base_url}/projects/{project_id}/repository/contributors"
            
            commit_response = requests.get(commit_stats_url, headers=headers, timeout=15)
            
            if commit_response.status_code == 200:
                contributors = commit_response.json()
                # Contributors API is default-branch only; keep for reference
                total_commits_default_branch = sum(c.get('commits', 0) for c in contributors)
                log(f"  Found {len(contributors)} contributors (default-branch commits: {total_commits_default_branch})")
            else:
                # Handle empty or inaccessible repositories
                log(f"  WARNING: Could not fetch contributors (status: {commit_response.status_code})")
                contributors = []
                total_commits_default_branch = 0
            
            # Get comprehensive repository statistics
            log(f"  Fetching comprehensive repository statistics...")
            repo_stats = get_repository_stats_via_api(project_id, headers)
            
            # Extract values from statistics
            repository_size = repo_stats['repository_size']
            storage_size = repo_stats['storage_size']
            file_count = repo_stats['file_count']
            branch_count = repo_stats['branch_count']
            
            # Fallback to original project data if statistics are missing
            if repository_size == 0 and storage_size == 0:
                log(f"    Checking original project data for statistics...")
                repository_size = project.get('statistics', {}).get('repository_size', 0)
                storage_size = project.get('statistics', {}).get('storage_size', 0)
                
                if repository_size > 0 or storage_size > 0:
                    log(f"    Found statistics in original data: repository_size={repository_size}, storage_size={storage_size}")
            
            # Recalculate size thresholds with final values
            exceeds_2gb = storage_size > (2 * 1024 * 1024 * 1024)  # 2GB in bytes
            exceeds_6gb = storage_size > (6 * 1024 * 1024 * 1024)  # 6GB in bytes
            
            if storage_size > 0:
                log(f"    Size check: Total storage: {bytes_to_mb(storage_size)} MB - Exceeds 2GB: {exceeds_2gb}, Exceeds 6GB: {exceeds_6gb}")
            
            log(f"  Repository stats - Files: {file_count}, Size: {bytes_to_mb(repository_size)} MB")
            
            # Get exportable model counts (for gl-exporter migration)
            log(f"  Fetching exportable model counts for migration planning...")
            model_counts = get_exportable_model_counts(project_id, headers)
            
            # Check for Git LFS usage
            log(f"  Checking for Git LFS usage...")
            lfs_info = check_lfs_enabled(project_id, headers)
            
            # Check for additional repository files and configurations
            log(f"  Checking for repository configuration files...")
            has_gitmodules = check_file_exists(project_id, '.gitmodules', default_branch, headers) if default_branch else False
            has_codeowners = (check_file_exists(project_id, 'CODEOWNERS', default_branch, headers) or 
                             check_file_exists(project_id, '.gitlab/CODEOWNERS', default_branch, headers) or
                             check_file_exists(project_id, 'docs/CODEOWNERS', default_branch, headers)) if default_branch else False
            
            # Check for PR/MR template files
            pr_template_paths = [
                '.gitlab/merge_request_templates/default.md',
                '.gitlab/merge_request_templates/Default.md',
                '.gitlab/merge_request_templates/merge_request_template.md',
                'PULL_REQUEST_TEMPLATE.md',
                '.github/PULL_REQUEST_TEMPLATE.md',
                'docs/pull_request_template.md'
            ]
            has_pr_template = any(check_file_exists(project_id, path, default_branch, headers) for path in pr_template_paths) if default_branch else False
            
            # Get releases count
            log(f"  Fetching releases count...")
            releases_count = get_releases_count(project_id, headers)
            
            if has_gitmodules:
                log(f"    Found .gitmodules file (submodules present)")
            if has_codeowners:
                log(f"    Found CODEOWNERS file")
            if has_pr_template:
                log(f"    Found merge request template")
            if releases_count > 0:
                log(f"    Found {releases_count} releases")
            
            # Build complete project statistics dictionary
            project_stats = {
                'id': project['id'],
                'name': project['name'],
                'path': project['path_with_namespace'],
                'status': 'archived' if is_archived else 'active',
                'archived': is_archived,
                'stars': project.get('star_count', 0),
                'forks': project.get('forks_count', 0),
                'open_issues': project.get('open_issues_count', 0),
                'last_activity': project.get('last_activity_at', 'N/A'),
                'contributors': len(contributors),
                'pr_count': model_counts['merge_requests'],  # PR Count before Commit Count
                'total_commits': repo_stats.get('commit_count', 0),
                'branch_count': branch_count,
                'file_count': file_count,
                'all_branches_file_count': repo_stats.get('all_branches_file_count', file_count),
                'total_objects': repo_stats.get('object_count', 0),
                'repository_size_mb': bytes_to_mb(repository_size),
                'repository_size_gb': round(bytes_to_mb(repository_size) / 1024, 2),  # GB calculation
                'total_size_mb': bytes_to_mb(storage_size),
                'total_size_gb': round(bytes_to_mb(storage_size) / 1024, 2),  # GB calculation
                'has_large_file_100mb': repo_stats.get('has_large_file', False),
                'exceeds_2gb': exceeds_2gb,
                'exceeds_6gb': exceeds_6gb,
                'pipeline': repo_stats.get('has_pipeline', False),
                'has_lfs': lfs_info['has_lfs'],
                'lfs_file_count': lfs_info['lfs_file_count'],
                'lfs_total_size_bytes': lfs_info.get('lfs_total_size_bytes', 0),
                'lfs_total_size_mb': lfs_info['lfs_total_size_mb'],
                'has_gitmodules': has_gitmodules,
                'has_codeowners': has_codeowners,
                'has_pr_template': has_pr_template,
                'releases_count': releases_count,
                'branch_protections': model_counts['protected_branches'],
                'has_rulesets': False,  # GitHub-specific feature, not available in GitLab
                'ruleset_count': 0,  # GitHub-specific feature, not available in GitLab
                'visibility': project.get('visibility', 'N/A'),
                'created_at': project.get('created_at', 'N/A'),
                'default_branch': default_branch,
                'web_url': project.get('web_url', 'N/A'),
                # Exportable model counts (gl-exporter capabilities)
                'exportable_users': model_counts['users_count'],
                'exportable_protected_branches': model_counts['protected_branches'],
                'exportable_merge_requests': model_counts['merge_requests'],
                'exportable_mr_notes': model_counts['merge_request_notes'],
                'exportable_issues': model_counts['issues'],
                'exportable_issue_notes': model_counts['issue_notes'],
                'exportable_webhooks': model_counts['webhooks'],
                'exportable_tags': model_counts['tags'],
                'exportable_commit_comments': model_counts['commit_comments'],
                'exportable_has_wiki': model_counts['has_wiki'],
                'exportable_milestones': model_counts['milestones']
            }
            
            stats.append(project_stats)
            
        except requests.exceptions.Timeout:
            log(f"  ERROR: Timeout while processing project: {project_name}")
            failed_projects.append(project_name)
            continue
        except Exception as e:
            log(f"  ERROR: Failed to process project {project_name}: {e}")
            failed_projects.append(project_name)
            continue

    # Summary of processing results
    log(f"\nCompleted processing {len(stats)} projects successfully")
    if failed_projects:
        log(f"Failed to process {len(failed_projects)} projects: {failed_projects}")
    
    if project_filter is not None:
        log(f"Note: Processing was filtered based on project list file: {PROJECT_LIST_FILE}")
        log(f"Filter values used: {MIGRATE_REPO_VALUES}")

    # Write results to CSV file
    log(f"Writing results to {OUTPUT_FILE}...")
    if stats:
        # Add simplified subgroup hierarchy columns (parent_group, subgroups, subgroup_count)
        log("Adding simplified subgroup hierarchy columns...")
        stats_with_groups = add_subgroup_hierarchy(stats)
        
        # Get field names from first record (now includes group level columns)
        fieldnames = stats_with_groups[0].keys()
        
        # Try to write CSV with backup mechanism for locked files
        csv_written = False
        output_path = OUTPUT_FILE
        
        try:
            # Write CSV with headers
            with open(output_path, 'w', newline='', encoding='utf-8') as csvfile:
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(stats_with_groups)
            csv_written = True
            log(f"Successfully wrote {len(stats_with_groups)} project records to {output_path}")
            
        except (PermissionError, IOError) as e:
            # File is locked or permission denied - create backup file
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            backup_filename = f"{OUTPUT_FILE.stem}_backup_{timestamp}{OUTPUT_FILE.suffix}"
            output_path = OUTPUT_FILE.parent / backup_filename
            
            log(f"WARNING: Could not write to {OUTPUT_FILE}: {e}")
            log(f"Creating backup file instead: {output_path}")
            
            try:
                with open(output_path, 'w', newline='', encoding='utf-8') as csvfile:
                    writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(stats_with_groups)
                csv_written = True
                log(f"Successfully wrote {len(stats_with_groups)} project records to backup file: {output_path}")
            except Exception as backup_error:
                log(f"ERROR: Failed to write backup file: {backup_error}")
        
        if not csv_written:
            log("ERROR: Could not write CSV file to primary or backup location")
    else:
        log("No data to write to CSV file")

    # Display environment variables that were set
    log("\nEnvironment variables set for use by other scripts:")
    log(f"GITLAB_TOKEN: {'Set' if os.environ.get('GITLAB_TOKEN') else 'Not set'}")
    log(f"GITLAB_GROUP: {os.environ.get('GITLAB_GROUP', 'Not set')}")
    log(f"GITLAB_URL: {os.environ.get('GITLAB_URL', 'Not set')}")
    log(f"GITHUB_TOKEN: {'Set' if os.environ.get('GITHUB_TOKEN') else 'Not set'}")
    
# Calculate and log execution time
script_end_time = datetime.now()
execution_time = script_end_time - script_start_time

# Convert to hours and minutes
total_seconds = int(execution_time.total_seconds())
hours = total_seconds // 3600
minutes = (total_seconds % 3600) // 60
seconds = total_seconds % 60

log("\n" + "="*50)
if hours > 0:
    log(f"Script execution time: {hours} hours, {minutes} minutes, {seconds} seconds")
elif minutes > 0:
    log(f"Script execution time: {minutes} minutes, {seconds} seconds")
else:
    log(f"Script execution time: {seconds} seconds")
log("="*50)

log("Script completed")