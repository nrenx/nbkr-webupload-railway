#!/bin/bash

# Colors for terminal output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${YELLOW}=== NBKRIST Student Portal Deployment Script ===${NC}"
echo

# Check if git is installed
if ! command -v git &> /dev/null; then
    echo -e "${RED}Error: git is not installed. Please install git and try again.${NC}"
    exit 1
fi

# Check if we're in a git repository
if ! git rev-parse --is-inside-work-tree &> /dev/null; then
    echo -e "${YELLOW}Initializing git repository...${NC}"
    git init
    echo -e "${GREEN}Git repository initialized.${NC}"
fi

# Check if remote origin exists
if ! git remote | grep -q "origin"; then
    echo -e "${YELLOW}No remote repository found.${NC}"
    echo -e "${YELLOW}Please enter your GitHub repository URL (e.g., https://github.com/username/repo.git):${NC}"
    read -r repo_url
    
    if [ -z "$repo_url" ]; then
        echo -e "${RED}No repository URL provided. Exiting.${NC}"
        exit 1
    fi
    
    git remote add origin "$repo_url"
    echo -e "${GREEN}Remote repository added.${NC}"
fi

# Stage all files
echo -e "${YELLOW}Staging files...${NC}"
git add .
echo -e "${GREEN}Files staged.${NC}"

# Commit changes
echo -e "${YELLOW}Enter commit message (default: 'Update application'):${NC}"
read -r commit_message
commit_message=${commit_message:-"Update application"}

git commit -m "$commit_message"
echo -e "${GREEN}Changes committed.${NC}"

# Push to GitHub
echo -e "${YELLOW}Pushing to GitHub...${NC}"
git push -u origin main || git push -u origin master

if [ $? -eq 0 ]; then
    echo -e "${GREEN}Successfully pushed to GitHub.${NC}"
    echo
    echo -e "${YELLOW}=== Deployment Instructions ===${NC}"
    echo -e "1. Go to your Render dashboard: ${GREEN}https://dashboard.render.com/${NC}"
    echo -e "2. Connect your GitHub repository if you haven't already"
    echo -e "3. Create a new Web Service with the following settings:"
    echo -e "   - Environment: ${GREEN}Python${NC}"
    echo -e "   - Build Command: ${GREEN}pip install -r requirements.txt${NC}"
    echo -e "   - Start Command: ${GREEN}gunicorn app:app${NC}"
    echo -e "4. Add the following environment variables:"
    echo -e "   - ${GREEN}SUPABASE_URL${NC}: Your Supabase URL"
    echo -e "   - ${GREEN}SUPABASE_KEY${NC}: Your Supabase API key"
    echo -e "   - ${GREEN}SUPABASE_BUCKET${NC}: student-details"
    echo
    echo -e "${GREEN}Your application is now ready to be deployed on Render!${NC}"
else
    echo -e "${RED}Failed to push to GitHub. Please check your repository URL and credentials.${NC}"
    exit 1
fi
