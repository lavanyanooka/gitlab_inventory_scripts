# Install deps
pip install requests openpyxl

# Run (Git Bash / Linux / macOS)
python secrets_inventory.py --group my-org --token glpat-xxxx --gitlab-url https://gitlab.com --debug

# Or with env vars
export GITLAB_URL=https://gitlab.com
export GITLAB_TOKEN=glpat-xxxx
python secrets_inventory.py --group my-org --debug

# PowerShell
$env:GITLAB_TOKEN = "glpat-xxxx"
python secrets_inventory.py --group my-org --debug

# Multiple tokens
python secrets_inventory.py --group my-org --tokens "token1,token2,token3" --debug