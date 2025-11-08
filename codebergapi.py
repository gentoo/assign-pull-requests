import requests


class CodebergAPI:
    def __init__(self, owner: str, repo: str, token: str):
        self.owner = owner
        self.repo = repo
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"token {token}",
                "Content-Type": "application/json",
            }
        )
        self.session.hooks = {
            "response": lambda r, *args, **kwargs: r.raise_for_status()
        }

    def close(self):
        self.session.close()

    @property
    def repos_baseurl(self):
        return f"https://codeberg.org/api/v1/repos/{self.owner}/{self.repo}"

    def pulls(self):
        next_url = f"{self.repos_baseurl}/pulls?state=open"
        while next_url:
            r = self.session.get(next_url)
            yield from r.json()
            next_url = (x := r.links.get("next")) and x["url"]

    def set_pr_title(self, pr_id, title):
        self.session.patch(
            f"{self.repos_baseurl}/pulls/{pr_id}", json={"title": title}
        )

    def add_pr_labels(self, pr_id, labels):
        self.session.patch(
            f"{self.repos_baseurl}/pulls/{pr_id}", json=({"labels": labels})
        )

    def labels(self):
        return self.session.get(f"{self.repos_baseurl}/labels").json()

    def commits(self, pr_id):
        return self.session.get(f"{self.repos_baseurl}/pulls/{pr_id}/commits").json()

    def files(self, pr_id):
        return self.session.get(f"{self.repos_baseurl}/pulls/{pr_id}/files").json()

    def get_reviews(self, pr_id):
        return self.session.get(f"{self.repos_baseurl}/pulls/{pr_id}/reviews").json()

    def create_review(self, pr_id, comment):
        # Does not appear to be possible to simply post comments
        # https://codeberg.org/api/swagger#/repository/repoCreatePullReview
        self.session.post(
            f"{self.repos_baseurl}/pulls/{pr_id}/reviews",
            json={
                "body": comment,
            },
        )

    def delete_review(self, pr_id, review_id):
        self.session.delete(f"{self.repos_baseurl}/pulls/{pr_id}/reviews/{review_id}")
