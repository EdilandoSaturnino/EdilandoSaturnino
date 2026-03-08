"""GitHub API client for fetching user stats and language data."""



import logging

import os

import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse



import requests



logger = logging.getLogger(__name__)





class GitHubAPI:

    """Fetches GitHub stats via GraphQL (with token) or REST (fallback)."""



    GRAPHQL_URL = "https://api.github.com/graphql"

    REST_URL = "https://api.github.com"



    def __init__(self, username: str, token: str = None, commits_mode: str = "contributions"):

        self.username = username

        self.token = token or os.environ.get("GITHUB_TOKEN", "")
        self.commits_mode = commits_mode

        self.headers = {"Accept": "application/vnd.github.v3+json"}
        self._token_is_target_user = None

        if self.token:

            self.headers["Authorization"] = f"Bearer {self.token}"



    def _request(self, method: str, url: str, **kwargs) -> requests.Response:

        """Make an HTTP request with rate-limit awareness and retry.

        Checks X-RateLimit-Remaining after each response.
        On 403 rate-limit, waits until reset and retries once.
        """

        kwargs.setdefault("headers", self.headers)

        kwargs.setdefault("timeout", 15)



        resp = requests.request(method, url, **kwargs)




        remaining = resp.headers.get("X-RateLimit-Remaining")

        if remaining is not None and int(remaining) < 10:

            reset_ts = int(resp.headers.get("X-RateLimit-Reset", 0))

            logger.warning(

                "GitHub API rate limit low: %s remaining (resets at %s)",

                remaining,

                time.strftime("%H:%M:%S", time.localtime(reset_ts)),

            )




        if resp.status_code == 403 and "rate limit" in resp.text.lower():

            reset_ts = int(resp.headers.get("X-RateLimit-Reset", 0))

            wait = max(reset_ts - int(time.time()), 1)

            logger.warning("Rate limited. Waiting %ds for reset...", wait)

            time.sleep(wait)

            resp = requests.request(method, url, **kwargs)



        return resp



    def fetch_stats(self) -> dict:

        """Fetch user statistics. Uses GraphQL if token available, REST otherwise."""

        if self.token:
            stats = self._fetch_stats_graphql()
            if self.commits_mode in {"raw", "raw_all"}:
                try:
                    stats["commits"] = self._fetch_commit_count_raw()
                except requests.exceptions.RequestException as e:
                    logger.warning("Raw commit count failed (%s). Keeping contributions count.", e)
            return stats

        return self._fetch_stats_rest()



    def _fetch_stats_graphql(self) -> dict:

        """Fetch stats via GraphQL and sum all-time commits in 1-year chunks."""

        base_query = """
        query($username: String!) {
          user(login: $username) {
            createdAt
            pullRequests {
              totalCount
            }
            issues {
              totalCount
            }
            repositories(ownerAffiliations: OWNER, first: 100) {
              totalCount
              nodes {
                stargazerCount
              }
            }
          }
        }
        """

        commits_query = """
        query($username: String!, $from: DateTime!, $to: DateTime!) {
          user(login: $username) {
            contributionsCollection(from: $from, to: $to) {
              totalCommitContributions
            }
          }
        }
        """

        try:
            base_resp = self._request(
                "POST",
                self.GRAPHQL_URL,
                json={"query": base_query, "variables": {"username": self.username}},
            )
            base_resp.raise_for_status()
        except requests.exceptions.Timeout:
            logger.warning("GraphQL request timed out, falling back to REST.")
            return self._fetch_stats_rest()
        except requests.exceptions.HTTPError as e:
            logger.warning("GraphQL HTTP error (%s), falling back to REST.", e)
            return self._fetch_stats_rest()

        base_data = base_resp.json()
        if "errors" in base_data:
            logger.warning("GraphQL errors: %s", base_data["errors"])
            return self._fetch_stats_rest()

        user = base_data["data"]["user"]
        repos = user["repositories"]
        total_stars = sum(n["stargazerCount"] for n in repos["nodes"])

        start = datetime.fromisoformat(user["createdAt"].replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        one_year = timedelta(days=365)
        one_second = timedelta(seconds=1)
        total_commits = 0

        while start < now:
            end = min(start + one_year - one_second, now)
            try:
                commits_resp = self._request(
                    "POST",
                    self.GRAPHQL_URL,
                    json={
                        "query": commits_query,
                        "variables": {
                            "username": self.username,
                            "from": start.isoformat(),
                            "to": end.isoformat(),
                        },
                    },
                )
                commits_resp.raise_for_status()
            except requests.exceptions.Timeout:
                logger.warning("GraphQL request timed out, falling back to REST.")
                return self._fetch_stats_rest()
            except requests.exceptions.HTTPError as e:
                logger.warning("GraphQL HTTP error (%s), falling back to REST.", e)
                return self._fetch_stats_rest()

            commits_data = commits_resp.json()
            if "errors" in commits_data:
                logger.warning("GraphQL errors: %s", commits_data["errors"])
                return self._fetch_stats_rest()

            total_commits += commits_data["data"]["user"]["contributionsCollection"][
                "totalCommitContributions"
            ]
            start = end + one_second

        return {
            "commits": total_commits,
            "stars": total_stars,
            "prs": user["pullRequests"]["totalCount"],
            "issues": user["issues"]["totalCount"],
            "repos": repos["totalCount"],
        }



    def _fetch_stats_rest(self) -> dict:

        """Fallback: fetch stats via REST API (public data only)."""

        user_resp = self._request(

            "GET", f"{self.REST_URL}/users/{self.username}"

        )

        user_resp.raise_for_status()

        user_data = user_resp.json()




        total_stars = 0

        for repos in self._paginate_repos():

            total_stars += sum(r.get("stargazers_count", 0) for r in repos)




        if self.commits_mode in {"raw", "raw_all"}:
            commit_count = self._fetch_commit_count_raw()
        else:
            events_resp = self._request(
                "GET",
                f"{self.REST_URL}/users/{self.username}/events/public",
                params={"per_page": 100},
            )
            events_resp.raise_for_status()
            events = events_resp.json()
            commit_count = sum(
                len(e.get("payload", {}).get("commits", []))
                for e in events
                if e.get("type") == "PushEvent"
            )




        pr_count = self._search_count(f"author:{self.username} type:pr")




        issue_count = self._search_count(f"author:{self.username} type:issue")



        return {

            "commits": commit_count,

            "stars": total_stars,

            "prs": pr_count,

            "issues": issue_count,

            "repos": user_data.get("public_repos", 0),

        }



    def _paginate_repos(self, include_non_owner: bool = False):

        """Yield pages of owned repos from the REST API."""

        page = 1

        use_authenticated_repos = self._is_token_for_target_user()
        while True:
            if use_authenticated_repos:
                affiliation = "owner"
                if include_non_owner:
                    affiliation = "owner,collaborator,organization_member"
                repos_resp = self._request(
                    "GET",
                    f"{self.REST_URL}/user/repos",
                    params={
                        "per_page": 100,
                        "page": page,
                        "visibility": "all",
                        "affiliation": affiliation,
                    },
                )
            else:
                repo_type = "owner"
                if include_non_owner:
                    repo_type = "all"
                repos_resp = self._request(
                    "GET",
                    f"{self.REST_URL}/users/{self.username}/repos",
                    params={"per_page": 100, "page": page, "type": repo_type},
                )

            repos_resp.raise_for_status()

            repos = repos_resp.json()

            if not repos:

                break

            yield repos

            if len(repos) < 100:

                break

            page += 1

    def _is_token_for_target_user(self) -> bool:

        """True when token exists and belongs to the configured username."""

        if not self.token:
            return False

        if self._token_is_target_user is not None:
            return self._token_is_target_user

        try:
            resp = self._request("GET", f"{self.REST_URL}/user")
            if resp.status_code != 200:
                self._token_is_target_user = False
                return False
            login = resp.json().get("login", "")
            self._token_is_target_user = login.lower() == self.username.lower()
        except requests.exceptions.RequestException:
            self._token_is_target_user = False

        return self._token_is_target_user

    def _fetch_commit_count_raw(self) -> int:

        """Count authored commits across repos using REST commits endpoint."""

        total_commits = 0
        seen_repos = set()
        include_non_owner = self.commits_mode == "raw_all"
        for repos in self._paginate_repos(include_non_owner=include_non_owner):
            for repo in repos:
                full_name = repo.get("full_name")
                if not full_name or full_name in seen_repos:
                    continue
                seen_repos.add(full_name)
                total_commits += self._count_repo_commits(full_name)
        return total_commits

    def _count_repo_commits(self, full_name: str) -> int:

        """Count commits authored by the configured username in one repository."""

        resp = self._request(
            "GET",
            f"{self.REST_URL}/repos/{full_name}/commits",
            params={"author": self.username, "per_page": 1},
        )

        if resp.status_code in {409, 422}:
            return 0

        resp.raise_for_status()
        last_page = self._extract_last_page_number(resp.headers.get("Link", ""))
        if last_page is not None:
            return last_page

        data = resp.json()
        if isinstance(data, list):
            return len(data)
        return 0

    @staticmethod
    def _extract_last_page_number(link_header: str):

        """Extract last page number from an RFC5988 Link header."""

        if not link_header:
            return None

        for part in link_header.split(","):
            if 'rel="last"' not in part:
                continue
            start = part.find("<")
            end = part.find(">")
            if start == -1 or end == -1 or end <= start + 1:
                continue
            url = part[start + 1 : end]
            page = parse_qs(urlparse(url).query).get("page", [None])[0]
            if page and str(page).isdigit():
                return int(page)
        return None



    def _search_count(self, query: str) -> int:

        """Use the GitHub Search API to get a total_count for a query."""

        try:

            resp = self._request(

                "GET",

                f"{self.REST_URL}/search/issues",

                params={"q": query, "per_page": 1},

            )

            if resp.status_code == 200:

                return resp.json().get("total_count", 0)

            logger.warning("Search API returned %d for query '%s'", resp.status_code, query)

        except requests.exceptions.RequestException as e:

            logger.warning("Search API failed for '%s': %s", query, e)

        return 0



    def fetch_languages(self) -> dict:

        """Fetch language byte counts aggregated across all owned non-fork repos."""

        languages = {}

        for repos in self._paginate_repos():

            for repo in repos:

                if repo.get("fork"):

                    continue

                try:

                    lang_resp = self._request("GET", repo["languages_url"])

                    if lang_resp.status_code == 200:

                        for lang, bytes_count in lang_resp.json().items():

                            languages[lang] = languages.get(lang, 0) + bytes_count

                    else:

                        logger.warning(

                            "Could not fetch languages for %s (HTTP %d)",

                            repo.get("full_name", "unknown"),

                            lang_resp.status_code,

                        )

                except requests.exceptions.RequestException as e:

                    logger.warning(

                        "Error fetching languages for %s: %s",

                        repo.get("full_name", "unknown"),

                        e,

                    )

        return languages

