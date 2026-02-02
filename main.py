import os
import sys
import webbrowser
import logging
import atexit
from datetime import datetime, timezone
from pathlib import Path
import rumps
import requests
from typing import Optional

# Setup logging
log_dir = Path.home() / "Library" / "Logs" / "gh-menu"
log_dir.mkdir(parents=True, exist_ok=True)
log_file = log_dir / "gh-menu.log"
pid_file = log_dir / "gh-menu.pid"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"
GITHUB_ORG = "withbridge"

def check_single_instance():
    """Ensure only one instance of gh-menu is running."""
    if pid_file.exists():
        try:
            with open(pid_file, 'r') as f:
                old_pid = int(f.read().strip())

            # Check if process is still running
            try:
                os.kill(old_pid, 0)  # Doesn't actually kill, just checks if process exists
                logger.info(f"Another instance is already running (PID: {old_pid}). Exiting.")
                sys.exit(0)
            except OSError:
                # Process doesn't exist, stale PID file
                logger.info(f"Removing stale PID file (PID: {old_pid})")
        except (ValueError, IOError):
            # Invalid PID file, ignore
            pass

    # Write current PID
    with open(pid_file, 'w') as f:
        f.write(str(os.getpid()))
    logger.info(f"Started with PID: {os.getpid()}")

    # Clean up PID file on exit
    atexit.register(lambda: pid_file.unlink(missing_ok=True))

def get_relative_time(created_at_str):
    """Calculate relative time from ISO timestamp."""
    created_at = datetime.fromisoformat(created_at_str.replace('Z', '+00:00'))
    now = datetime.now(timezone.utc)
    delta = now - created_at

    seconds = delta.total_seconds()
    if seconds < 3600:
        minutes = int(seconds / 60)
        return f"{minutes}m ago"
    elif seconds < 86400:
        hours = int(seconds / 3600)
        return f"{hours}h ago"
    elif seconds < 604800:
        days = int(seconds / 86400)
        return f"{days}d ago"
    else:
        weeks = int(seconds / 604800)
        return f"{weeks}w ago"

def github_rest_headers(api_key: str) -> dict:
    return {
        "Authorization": f"token {api_key}",
        "Accept": "application/vnd.github.v3+json",
    }

def github_graphql_headers(api_key: str) -> dict:
    # GraphQL recommends "bearer" auth; "token" sometimes works but is less standard.
    return {
        "Authorization": f"bearer {api_key}",
        "Accept": "application/vnd.github+json",
    }

def github_graphql(api_key: str, query: str, variables: dict | None = None, timeout: int = 10) -> dict:
    payload = {"query": query, "variables": variables or {}}
    resp = requests.post(
        GITHUB_GRAPHQL_URL,
        headers=github_graphql_headers(api_key),
        json=payload,
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data and data["errors"]:
        messages = "; ".join(e.get("message", "GraphQL error") for e in data["errors"])
        raise RuntimeError(messages)
    return data.get("data", {})

def fetch_my_open_prs_with_unresolved(api_key: str, first: int = 25) -> list[dict]:
    """
    Returns list of PRs authored by the authenticated user, with unresolved review thread counts.

    Unresolved count is the number of unresolved review threads (not total comments).
    """
    query = """
    query($query: String!, $first: Int!) {
      search(query: $query, type: ISSUE, first: $first) {
        nodes {
          ... on PullRequest {
            title
            url
            createdAt
            number
            author { login }
            repository { nameWithOwner }
            isDraft
            mergeable
            mergeStateStatus
            reviewDecision
            # NOTE: Some GitHub GraphQL schemas do not support filtering reviewThreads by state.
            # We fetch a limited number and count unresolved threads client-side via isResolved.
            reviewThreads(first: 100) {
              totalCount
              nodes { isResolved }
            }
            commits(last: 1) {
              nodes {
                commit {
                  statusCheckRollup {
                    state
                  }
                }
              }
            }
          }
        }
      }
    }
    """
    variables = {"query": f"is:open is:pr author:@me org:{GITHUB_ORG}", "first": first}
    data = github_graphql(api_key, query, variables=variables)
    nodes = (((data or {}).get("search") or {}).get("nodes")) or []
    prs: list[dict] = []
    for node in nodes:
        if not node:
            continue
        review_threads = node.get("reviewThreads") or {}
        thread_nodes = review_threads.get("nodes") or []
        unresolved_threads = sum(1 for t in thread_nodes if t and not t.get("isResolved", False))

        commits = node.get("commits") or {}
        commit_nodes = commits.get("nodes") or []
        status_rollup = None
        if commit_nodes and commit_nodes[-1]:
            status_rollup = (((commit_nodes[-1].get("commit") or {}).get("statusCheckRollup")) or None)
        checks_state = (status_rollup or {}).get("state") if status_rollup else None

        is_draft = bool(node.get("isDraft", False))
        review_decision = node.get("reviewDecision")
        mergeable = node.get("mergeable")
        merge_state_status = node.get("mergeStateStatus")

        # Heuristic "ready for merge":
        # - Not draft
        # - No unresolved review threads
        # - Approved (reviewDecision == APPROVED)
        # - Checks passing (SUCCESS/NEUTRAL) if GitHub reports a rollup state
        # - Mergeable (MERGEABLE) and in a clean-ish merge state
        checks_ok = (checks_state is None) or (checks_state in ("SUCCESS", "NEUTRAL"))
        merge_ok = (mergeable in (None, "MERGEABLE")) and (merge_state_status in (None, "CLEAN", "UNSTABLE", "HAS_HOOKS"))
        ready_for_merge = (not is_draft) and (unresolved_threads == 0) and (review_decision == "APPROVED") and checks_ok and merge_ok

        prs.append(
            {
                "title": node.get("title", ""),
                "url": node.get("url", ""),
                "created_at": node.get("createdAt", ""),
                "number": node.get("number"),
                "author": ((node.get("author") or {}).get("login")) or "unknown",
                "repo": ((node.get("repository") or {}).get("nameWithOwner")) or "unknown/unknown",
                "unresolved": int(unresolved_threads),
                "is_draft": is_draft,
                "review_decision": review_decision,
                "checks_state": checks_state,
                "mergeable": mergeable,
                "merge_state_status": merge_state_status,
                "ready_for_merge": bool(ready_for_merge),
            }
        )
    return prs

class GitHubPRMenuApp(rumps.App):
    def __init__(self):
        super(GitHubPRMenuApp, self).__init__("PRs: -", quit_button="Quit")
        self.api_key = os.environ.get("GH_API_KEY")
        self.dynamic_menu_keys: list[str] = []

        logger.info("Starting gh-menu app")
        logger.info(f"Log file location: {log_file}")

        if not self.api_key:
            self.title = "⚠️ Set GH_API_KEY env var"
            logger.warning("GH_API_KEY environment variable not set")
            self.menu = ["Set GH_API_KEY environment variable", "See README for instructions"]
        else:
            self.check_prs()
            self.timer = rumps.Timer(self.check_prs, 5)
            self.timer.start()

    def _clear_dynamic_menu(self) -> None:
        for key in self.dynamic_menu_keys:
            if key in self.menu:
                del self.menu[key]
        self.dynamic_menu_keys = []

    def _add_menu_item(self, title: str, url: Optional[str] = None) -> None:
        if url:
            item = rumps.MenuItem(title, callback=lambda _, u=url: webbrowser.open(u))
        else:
            item = rumps.MenuItem(title)
        self.menu.add(item)
        self.dynamic_menu_keys.append(title)

    def check_prs(self, _=None):
        if not self.api_key:
            return

        try:
            rest_headers = github_rest_headers(self.api_key)

            # PRs where you are requested as a reviewer, sorted by oldest first
            review_url = "https://api.github.com/search/issues"
            review_params = {
                "q": f"is:open is:pr user-review-requested:@me org:{GITHUB_ORG}",
                "sort": "created",
                "order": "asc",
            }
            review_resp = requests.get(review_url, headers=rest_headers, params=review_params, timeout=10)
            review_resp.raise_for_status()
            review_data = review_resp.json()
            review_count = review_data.get("total_count", 0)
            review_items = review_data.get("items", [])

            # Your open PRs with unresolved review-thread counts
            my_prs = fetch_my_open_prs_with_unresolved(self.api_key, first=25)
            my_open_count = len(my_prs)
            my_unresolved_total = sum(pr.get("unresolved", 0) for pr in my_prs)

            # Update menu bar title (compact but informative)
            if review_count == 0 and my_open_count == 0:
                self.title = "🟢 R:0 M:0"
            elif review_count == 0 and my_unresolved_total == 0:
                self.title = f"🟢 R:0 M:{my_open_count}"
            else:
                # Red if you have review requests or any unresolved threads on your PRs
                if my_unresolved_total > 0:
                    self.title = f"🔴 R:{review_count} M:{my_open_count}/{my_unresolved_total}"
                else:
                    self.title = f"🔴 R:{review_count} M:{my_open_count}"

            # Rebuild dynamic menu section(s)
            self._clear_dynamic_menu()

            # Section: PRs awaiting your review
            self._add_menu_item("— Awaiting your review —")
            if review_count == 0:
                self._add_menu_item("🟢 None")
            else:
                # Add newest first (oldest at bottom)
                for pr in reversed(review_items):
                    author = pr.get("user", {}).get("login", "unknown")
                    pr_title = pr.get("title", "")
                    pr_url = pr.get("html_url", "")
                    created_at = pr.get("created_at", "")
                    pr_number = pr.get("number", "?")
                    repo = (pr.get("repository_url", "").split("repos/")[-1]) or "unknown/unknown"

                    # Truncate title to keep menu readable
                    max_title_length = 45
                    if len(pr_title) > max_title_length:
                        pr_title = pr_title[:max_title_length] + "..."

                    age = get_relative_time(created_at)
                    menu_text = f"🔀 {repo}#{pr_number} [{author}] {pr_title} ({age})"
                    self._add_menu_item(menu_text, url=pr_url)

            # Section: Your open PRs (unresolved threads)
            self._add_menu_item("— Your open PRs —")
            if my_open_count == 0:
                self._add_menu_item("🟢 None")
            else:
                # Show oldest at bottom (so newest first)
                # Sort by createdAt ascending then reverse for menu
                def created_key(p: dict) -> str:
                    return (p.get("created_at") or "")

                for pr in reversed(sorted(my_prs, key=created_key)):
                    pr_title = pr.get("title", "")
                    pr_url = pr.get("url", "")
                    created_at = pr.get("created_at", "")
                    pr_number = pr.get("number", "?")
                    repo = pr.get("repo", "unknown/unknown")
                    unresolved = pr.get("unresolved", 0)

                    max_title_length = 45
                    if len(pr_title) > max_title_length:
                        pr_title = pr_title[:max_title_length] + "..."

                    # GraphQL returns ISO8601 like "2026-02-02T12:34:56Z"
                    age = get_relative_time(created_at) if created_at else "?"
                    menu_text = f"🧑 {repo}#{pr_number} {pr_title} ({age}) • unresolved: {unresolved}"
                    self._add_menu_item(menu_text, url=pr_url)

            # Section: Ready for merge (subset of your open PRs)
            ready_prs = [p for p in my_prs if p.get("ready_for_merge")]
            self._add_menu_item("— Ready for merge —")
            if not ready_prs:
                self._add_menu_item("🟢 None")
            else:
                for pr in reversed(sorted(ready_prs, key=created_key)):
                    pr_title = pr.get("title", "")
                    pr_url = pr.get("url", "")
                    created_at = pr.get("created_at", "")
                    pr_number = pr.get("number", "?")
                    repo = pr.get("repo", "unknown/unknown")
                    checks_state = pr.get("checks_state")

                    max_title_length = 45
                    if len(pr_title) > max_title_length:
                        pr_title = pr_title[:max_title_length] + "..."

                    age = get_relative_time(created_at) if created_at else "?"
                    checks_suffix = f" • checks: {checks_state}" if checks_state else ""
                    menu_text = f"✅ {repo}#{pr_number} {pr_title} ({age}){checks_suffix}"
                    self._add_menu_item(menu_text, url=pr_url)

            logger.info(
                f"Review requests: {review_count}; My open PRs: {my_open_count}; My unresolved threads total: {my_unresolved_total}"
            )

        except requests.exceptions.RequestException as e:
            error_msg = str(e)[:50]  # Truncate to keep menu bar readable
            self.title = f"❌ {error_msg}"
            logger.error(f"Error checking GitHub: {e}")
        except Exception as e:
            error_msg = str(e)[:50]  # Truncate to keep menu bar readable
            self.title = f"❌ {error_msg}"
            logger.error(f"Unexpected error: {e}")

def main():
    check_single_instance()
    GitHubPRMenuApp().run()

if __name__ == "__main__":
    main()
