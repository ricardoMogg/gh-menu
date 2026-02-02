#!/bin/bash
set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Get the absolute path to the repo directory
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_NAME="com.gh-menu.plist"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$LAUNCH_AGENTS_DIR/$PLIST_NAME"

echo "================================================"
echo "gh-menu Installation Script"
echo "================================================"
echo ""

# Check if uv is installed, install if needed
if ! command -v uv &> /dev/null; then
    echo "uv is not installed. Installing..."
    curl -LsSf https://astral.sh/uv/install.sh | sh

    # Add uv to PATH for this session
    export PATH="$HOME/.local/bin:$PATH"

    # Verify installation
    if ! command -v uv &> /dev/null; then
        echo -e "${RED}✗ Failed to install uv${NC}"
        exit 1
    fi
    echo -e "${GREEN}✓ uv installed${NC}"
else
    echo -e "${GREEN}✓ uv is installed${NC}"
fi

# Install dependencies
echo ""
echo "Installing dependencies..."
cd "$REPO_DIR"
uv sync
echo -e "${GREEN}✓ Dependencies installed${NC}"

# Prompt for GitHub API key
echo ""
echo "================================================"
echo "GitHub API Key Setup"
echo "================================================"
echo ""
echo "You need a GitHub Personal Access Token with the following scopes:"
echo "  - repo (full control of private repositories)"
echo ""
echo "Create one at: https://github.com/settings/tokens/new"
echo ""
read -p "Enter your GitHub API key: " GH_API_KEY

if [ -z "$GH_API_KEY" ]; then
    echo -e "${RED}✗ No API key provided${NC}"
    exit 1
fi

# Validate the API key by testing it
echo ""
echo "Validating API key..."
VALIDATION_RESPONSE=$(curl -s -w "\n%{http_code}" -H "Authorization: token $GH_API_KEY" -H "Accept: application/vnd.github.v3+json" "https://api.github.com/search/issues?q=is:open+is:pr+user-review-requested:@me+org:withbridge&per_page=1")
HTTP_CODE=$(echo "$VALIDATION_RESPONSE" | tail -n1)
RESPONSE_BODY=$(echo "$VALIDATION_RESPONSE" | sed '$d')

if [ "$HTTP_CODE" != "200" ]; then
    echo -e "${RED}✗ API key validation failed (HTTP $HTTP_CODE)${NC}"
    echo ""
    echo "Response:"
    echo "$RESPONSE_BODY" | head -5
    echo ""
    echo "Please check that your API key:"
    echo "  - Is correctly copied (no extra spaces)"
    echo "  - Has the 'repo' scope enabled"
    echo "  - Is not expired"
    exit 1
fi

# Check if the response is valid JSON with the expected structure
if ! echo "$RESPONSE_BODY" | grep -q '"total_count"'; then
    echo -e "${RED}✗ Unexpected API response${NC}"
    exit 1
fi

echo -e "${GREEN}✓ API key is valid${NC}"

# Create .env file
echo ""
echo "Creating .env file..."
cat > "$REPO_DIR/.env" <<EOF
GH_API_KEY=$GH_API_KEY
EOF
echo -e "${GREEN}✓ Created .env file${NC}"

# Generate Launch Agent plist
echo ""
echo "Generating Launch Agent plist..."
UV_PATH="$HOME/.local/bin/uv"

cat > "$REPO_DIR/$PLIST_NAME" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.gh-menu</string>
    <key>ProgramArguments</key>
    <array>
        <string>$UV_PATH</string>
        <string>run</string>
        <string>main.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>EnvironmentVariables</key>
    <dict>
        <key>GH_API_KEY</key>
        <string>$GH_API_KEY</string>
    </dict>
    <key>WorkingDirectory</key>
    <string>$REPO_DIR</string>
    <key>StandardOutPath</key>
    <string>$HOME/Library/Logs/gh-menu/stdout.log</string>
    <key>StandardErrorPath</key>
    <string>$HOME/Library/Logs/gh-menu/stderr.log</string>
</dict>
</plist>
EOF
echo -e "${GREEN}✓ Generated plist file${NC}"

# Create LaunchAgents directory if it doesn't exist
mkdir -p "$LAUNCH_AGENTS_DIR"

# Copy plist to LaunchAgents
echo ""
echo "Installing Launch Agent..."
cp "$REPO_DIR/$PLIST_NAME" "$PLIST_PATH"
echo -e "${GREEN}✓ Copied to $PLIST_PATH${NC}"

# Unload existing agent if it's running
if launchctl list | grep -q "com.gh-menu"; then
    echo ""
    echo "Unloading existing Launch Agent..."
    launchctl unload "$PLIST_PATH" 2>/dev/null || true
    echo -e "${GREEN}✓ Unloaded existing agent${NC}"
fi

# Load the Launch Agent
echo ""
echo "Loading Launch Agent..."
launchctl load "$PLIST_PATH"
echo -e "${GREEN}✓ Launch Agent loaded${NC}"

# Success message
echo ""
echo "================================================"
echo -e "${GREEN}Installation Complete!${NC}"
echo "================================================"
echo ""
echo "The gh-menu app is now running and will start automatically on login."
echo "Check the menu bar for the PR indicator."
echo ""
echo "Logs are available at:"
echo "  ~/Library/Logs/gh-menu/gh-menu.log"
echo "  ~/Library/Logs/gh-menu/stdout.log"
echo "  ~/Library/Logs/gh-menu/stderr.log"
echo ""
echo "To uninstall:"
echo "  ./uninstall.sh"
echo ""
