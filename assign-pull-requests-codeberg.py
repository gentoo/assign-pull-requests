#!/usr/bin/env python
# Assign pull requests - codeberg version, adapted from
# assign-pull-requests.py by Michał Górny.

import bugzilla
import socket
import email.utils
import json
import os
import os.path
import sys
import re
import lxml.etree
import urllib.request as urllib
import xmlrpc.client as xmlrpcclient

from codebergapi import CodebergAPI


BUG_LONG_URL_RE = re.compile(
    r"https?://bugs\.gentoo\.org/show_bug\.cgi\?id=(\d+)(?:[&#].*)?$"
)
BUG_SHORT_URL_RE = re.compile(r"https?://bugs\.gentoo\.org/(\d+)(?:[?#].*)?$")


def map_dev(dev, dev_mapping):
    if d := dev_mapping.get(dev.lower()):
        return f"@{d}"
    if dev.endswith("@gentoo.org"):
        dev = dev[: -len("@gentoo.org")]
    else:
        dev = dev.replace("@", "[at]")
    return f"~~{dev}~~"


def map_proj(proj, proj_mapping):
    if proj.lower() in proj_mapping:
        return "@" + proj_mapping[proj.lower()].lower()
    if proj.endswith("@gentoo.org"):
        proj = proj[: -len("@gentoo.org")]
    else:
        proj = proj.replace("@", "[at]")
    return "~~[%s (project)]~~" % proj


def bugz_user_query(mails, bz):
    return bz.getusers(mails)


def verify_email(mail, bz):
    if not mail:  # early check ;-)
        return False

    try:
        resp = bugz_user_query([mail], bz)
    except xmlrpcclient.Fault as e:
        if e.faultCode == 51:  # account does not exist
            return False
        raise
    else:
        assert len(resp) == 1
        return True


def verify_emails(mails, bz):
    """Verify if emails have Bugzilla accounts. Returns iterator over
    mails that do not have accounts."""
    # To avoid querying bugzilla a lot, start with one big query for
    # all users. If they are all fine, we will get no error here.
    # If at least one fails, we need to get user-by-user to get all
    # failing.
    try:
        short_circ = bugz_user_query(mails, bz)
    except:
        pass
    else:
        assert len(short_circ) == len(mails)
        return

    for m in mails:
        if not verify_email(m, bz):
            yield m


def commit_contains_correct_signoff(commit):
    """Verify the commit contains a correct sign-off.

    Given a commit, it is considered to contain a correct sign-off IFF:
    It has a footer line starting with 'Signed-off-by: '
    AND that line contains the email address of the committer.

    Other sign-off lines MAY also be present, but do not affect the outcome of
    this check. It's entirely valid for one person to have multiple sign-off
    lines, using different email addresses (signing off on something for both
    work and Gentoo development).
    """
    committer_email = commit["commit"]["committer"]["email"].lower()  # case insensitive
    for line in commit["commit"]["message"].splitlines():
        lower_line = line.lower()
        if lower_line.startswith("signed-off-by:"):
            _, signed_email = email.utils.parseaddr(lower_line)
            if signed_email == committer_email:
                return True
    return False


def scanfiles(filelist, categories):
    """
    Scan files in a PR to determine areas, packages and metadata.xml files
    """
    areas = set()
    packages = set()
    metadata_xml_files = set()

    for f in filelist:
        path = f["filename"].split("/")
        if path[0] in categories:
            areas.add("ebuilds")
            if path[1] == "metadata.xml":
                areas.add("category-metadata")
            elif len(path) <= 2:
                areas.add("other files")
            else:
                if path[2] == "metadata.xml":
                    metadata_xml_files.add(f["raw_url"])
                packages.add("/".join(path[0:2]))
        elif path[0] == "eclass":
            areas.add("eclasses")
        elif path[0] == "profiles":
            if path[1] != "use.local.desc":
                areas.add("profiles")
        elif path[0] == "metadata":
            if path[1] not in ("md5-cache", "pkg_desc_index"):
                areas.add("other files")
        else:
            areas.add("other files")

    return areas, packages, metadata_xml_files


def delete_old_review(repo, pr_id, codeberg_username):
    reviews = repo.get_reviews(pr_id)

    for rv in reviews:
        if rv["user"]["login"] == codeberg_username:
            if "Pull Request assignment" in rv["body"]:
                repo.delete_review(pr_id, rv["id"])


def assign_one(
    repo,
    pr,
    categories,
    dev_mapping,
    proj_mapping,
    codeberg_username,
    ref_repo_path,
    label_mapping,
    bz,
    bugzilla_url,
):
    assignee_limit = 5
    bug_limit = 5
    body = pr["body"]
    pr_id = pr["number"]

    # Check if we are to reassign
    if "[please reassign]" in pr["title"].lower():
        print(f"PR#{pr_id}: [please reassign] found")
        # Edit title
        newtitle = re.sub(
            r"\s*\[please reassign\]\s*", "", pr["title"], flags=re.IGNORECASE
        )
        repo.set_pr_title(pr_id, newtitle)
    else:
        if pr["assignee"]:
            print(f"PR#{pr_id}: already assigned")
            return
        for l in pr["labels"]:
            if l["name"] in ("assigned", "need assignment", "do not merge"):
                print(f"PR#{pr_id}: {l['name']} label found")
                return

    if any(l["name"] == "no assignee limit" for l in pr["labels"]):
        assignee_limit = 9999
        bug_limit = 9999

    delete_old_review(repo, pr_id, codeberg_username)

    commits = repo.commits(pr_id)
    files = repo.files(pr_id)

    # look through files in the PR to determine the areas affected
    areas, packages, metadata_xml_files = scanfiles(files, categories)

    # Begin building our comment...

    body = f"""## Pull Request assignment

*Submitter*: @{pr["user"]["login"]}
*Areas affected*: {", ".join(sorted(areas)) or "(none, wtf?)"}
*Packages affected*: {", ".join(sorted(packages)[:5]) or "(none)"}{", ..." if len(packages) > 5 else ""}
"""

    # At least one of the listed packages is maintained entirely by
    # non-Codeberg developers
    cant_assign = False
    # if for at least one package, the user is not in maintainers, we
    # do not consider it self-maintained
    self_maintained = True
    invalid_email = False
    existing_package = False
    maint_needed = False
    new_package = False
    invalid_bug_linked = False
    unique_maints = set()
    totally_all_maints = set()

    # TODO Try to determine unique set of maintainers
    if packages:
        pkg_maints = {}
        for p in packages:
            ppath = os.path.join(ref_repo_path, p, "metadata.xml")
            try:
                metadata_xml = lxml.etree.parse(ppath)
            except (OSError, IOError):
                pkg_maints[p] = ["@gentoo/proxy-maint (new package)"]
                new_package = True
            else:
                existing_package = True
                all_ms = []
                for m in metadata_xml.getroot():
                    if m.tag != "maintainer":
                        continue
                    memail = m.findtext("email").strip()
                    totally_all_maints.add(memail)
                    # map the maintainer to their codeberg handle
                    # mapping is email -> codeberg handle
                    if m.get("type") == "project":
                        ms = map_proj(memail, proj_mapping)
                    else:
                        ms = map_dev(memail, dev_mapping)

                    for subm in m:
                        if m.tag == "description" and m.get("lang", "en") == "en":
                            ms += f"({m.text})"
                    all_ms.append(ms)

                if all_ms:
                    # no codebergers? no good
                    cant_assign = not (any("@" in m for m in all_ms))

                    pkg_maints[p] = all_ms

                    if "@" + pr["user"]["login"] not in all_ms:
                        self_maintained = False
                    unique_maints.add(tuple(sorted(all_ms)))
                    if len(unique_maints) > assignee_limit:
                        break
                else:
                    # maintainer-needed!
                    pkg_maints[p] = ["@gentoo/proxy-maint (maintainer needed)"]
                    maint_needed = True

        if len(unique_maints) > assignee_limit:
            cant_assign = True
            body += "\n@gentoo/codeberg: Too many disjoint maintainers, disabling auto-assignment."
        else:
            for p in sorted(packages):
                body += "\n**%s**: %s" % (p, ", ".join(pkg_maints[p]))
            if cant_assign:
                body += "\n\nAt least one of the listed packages is maintained entirely by non-Codeberg developers!"
    else:
        cant_assign = True
        body += "\n@gentoo/codeberg"

    if len(unique_maints) > assignee_limit:
        totally_all_maints = set()

    # if any metadata.xml files were changed, we want to check the new
    # maintainers for invalid addresses too
    # TODO: report maintainer change diffs
    for mxml in metadata_xml_files:
        with urllib.urlopen(mxml) as f:
            try:
                metadata_xml = lxml.etree.parse(f)
            except lxml.etree.XMLSyntaxError:
                continue
        for m in metadata_xml.getroot():
            if m.tag == "maintainer":
                totally_all_maints.add(m.findtext("email").strip())

    # Scan for bugs (Bug: or Closes: commit trailers)
    bugs = set()
    for c in commits:
        for l in c["commit"]["message"].splitlines():
            if l.startswith("Bug:") or l.startswith("Closes:"):
                tag, url = l.split(":", 1)
                url = url.strip()
                m = BUG_LONG_URL_RE.match(url)
                if m is None:
                    m = BUG_SHORT_URL_RE.match(url)
                if m is not None:
                    bugs.add(int(m.group(1)))

    body += "\n\n## Linked bugs"
    if bugs:
        if len(bugs) > bug_limit:
            buglinks = ", ".join(f"[{x}]({bugzilla_url}/{x})" for x in bugs)
            body += f"\nBugs linked: {buglinks}"
            body += "\nCross-linking bugs disabled due to large number of bugs linked."
        else:
            real_bugs = bz.getbugs(list(bugs), include_fields=["assigned_to"])
            real_bugs_ids = [bug.id for bug in real_bugs]
            invalid_bugs = bugs.difference(set(real_bugs_ids))

            if real_bugs_ids:
                buglinks = ", ".join(
                    f"[{x}]({bugzilla_url}/{x})" for x in real_bugs_ids
                )
                body += f"\nBugs linked: {buglinks}"
            if invalid_bugs:
                invalid_bug_linked = True
                body += f"\n\n**The following linked bugs do not exist!** {', '.join(str(b) for b in invalid_bugs)}"

            # FIXME: This is disabled until bugs.gentoo.org supports
            # codeberg.org URLs in the "See Also" field (or any URL).
            # bug 964700

            # updq = bz.build_update(
            #     keywords_add=['PullRequest'],
            #     see_also_add=[pr['url']]
            # )
            # try:
            #     bz.update_bugs(real_bugs_ids, updq)
            # except xmlrpcclient.Fault as e:
            #     if e.faultCode != 101:
            #         raise
            #     # non-existing bugs that were linked should already have been dealt with

        # match security@, security-audit@, and security-kernel@
        security = any(
            bug.assigned_to_detail["id"] in [2546, 23358, 25934] for bug in real_bugs
        )
    else:
        body += "\n\nNo bugs to link found. If your pull request references any of the Gentoo bug reports, please add appropriate [GLEP 66](https://www.gentoo.org/glep/glep-0066.html#commit-messages) tags to the commit message and request reassignment."

    if existing_package and not self_maintained and not bugs:
        body += "\n\n**If you do not receive any reply to this pull request, please open or link a bug to attract the attention of maintainers**"

    if not existing_package:
        body += "\n\n## New packages\nThis Pull Request appears to be introducing new packages only. Due to limited manpower, adding new packages is considered low priority. This does not mean that your pull request will not receive any attention, however, it might take quite some time for it to be reviewed. In the meantime, your new ebuild might find a home in the [GURU project repository](https://wiki.gentoo.org/wiki/Project:GURU): the ebuild repository maintained collaboratively by Gentoo users. GURU offers your ebuild a place to be reviewed and improved by other Gentoo users, while making it easy for Gentoo users to install it and enjoy the software it adds."

    # Verify maintainers for invalid addresses
    if totally_all_maints:
        invalid_mails = sorted(verify_emails(totally_all_maints, bz))
        if invalid_mails:
            invalid_email = True
            body += "\n\n## Missing Bugzilla accounts\n\n**WARNING**: The following maintainers do not match any Bugzilla accounts:"
            for m in invalid_mails:
                body += f"\n- {m}"
            body += "\n\nPlease either fix the e-mail addresses in metadata.xml or create a Bugzilla account, and request reassignment afterwards."

    # Check for missing signoff
    missing_signoff = not all(commit_contains_correct_signoff(c) for c in commits)
    if missing_signoff:
        body += "\n\n## Missing GCO sign-off\n\nPlease read the terms of [Gentoo Certificate of Origin](https://www.gentoo.org/glep/glep-0076.html#certificate-of-origin) and acknowledge them by adding a sign-off to *all* your commits. The sign-off MUST include the email address of the git committer."

    body += "\n\n---\nIn order to force reassignment and/or bug reference scan, please append `[please reassign]` to the pull request title.\n\n*Docs*: [Code of Conduct](https://wiki.gentoo.org/wiki/Project:Council/Code_of_conduct) ● [Copyright policy](https://www.gentoo.org/glep/glep-0076.html) ([expl.](https://dev.gentoo.org/~mgorny/articles/new-gentoo-copyright-policy-explained.html)) ● [Devmanual](https://devmanual.gentoo.org/) ● [Codeberg PRs](https://wiki.gentoo.org/wiki/Project:Codeberg/Pull_requests) ● [Proxy-maint guide](https://wiki.gentoo.org/wiki/Project:Proxy_Maintainers/User_Guide)"

    # finally! post comment...
    repo.create_review(pr_id, body)

    updated_labels = []
    for l in pr["labels"]:
        if l["name"] in (
            "assigned",
            "need assignment",
            "self-maintained",
            "maintainer-needed",
            "new package",
            "no signoff",
            "bug linked",
            "no bug found",
            "invalid email",
            "invalid bug linked",
        ):
            continue
        # retain label if not in the list above
        updated_labels.append(l["id"])

    if maint_needed:
        updated_labels.append(label_mapping["maintainer-needed"])
        self_maintained = False
    if new_package:
        updated_labels.append(label_mapping["new package"])
    if cant_assign:
        updated_labels.append(label_mapping["need assignment"])
    else:
        if self_maintained:
            updated_labels.append(label_mapping["self-maintained"])
        updated_labels.append(label_mapping["assigned"])
    if bugs:
        updated_labels.append(label_mapping["bug linked"])
        if security:
            updated_labels.append(label_mapping["security"])
    elif not self_maintained:
        updated_labels.append(label_mapping["no bug found"])
    if invalid_bug_linked:
        updated_labels.append(label_mapping["invalid bug linked"])
    if invalid_email:
        updated_labels.append(label_mapping["invalid email"])
    if missing_signoff:
        updated_labels.append(label_mapping["no signoff"])
    if "[noci]" in pr["title"].lower():
        updated_labels.append(label_mapping["noci"])

    repo.add_pr_labels(pr_id, updated_labels)

    print(f"PR#{pr_id}: assigned")


def main(repo_path):
    CODEBERG_DEV_MAPPING = os.environ["CODEBERG_DEV_MAPPING"]
    CODEBERG_PROXIED_MAINT_MAPPING = os.environ["CODEBERG_PROXIED_MAINT_MAPPING"]
    CODEBERG_PROJ_MAPPING = os.environ["CODEBERG_PROJ_MAPPING"]
    CODEBERG_USERNAME = os.environ["CODEBERG_USERNAME"]
    CODEBERG_TOKEN_FILE = os.environ["CODEBERG_TOKEN_FILE"]
    (owner, repo) = os.environ["CODEBERG_REPO"].split("/")

    with open(CODEBERG_TOKEN_FILE) as f:
        token = f.read().strip()

    BUGZILLA_URL = os.environ.get("BUGZILLA_URL", "https://bugs.gentoo.org")
    BUGZILLA_APIKEY_FILE = os.environ["BUGZILLA_APIKEY_FILE"]

    with open(BUGZILLA_APIKEY_FILE) as f:
        bugz_apikey = f.read().strip()

    bz = bugzilla.Bugzilla(BUGZILLA_URL, api_key=bugz_apikey)

    with open(CODEBERG_PROXIED_MAINT_MAPPING) as f:
        dev_mapping = json.load(f)
    with open(CODEBERG_DEV_MAPPING) as f:
        dev_mapping.update(json.load(f))
    with open(CODEBERG_PROJ_MAPPING) as f:
        proj_mapping = json.load(f)
    with open(os.path.join(repo_path, "profiles/categories")) as f:
        categories = [l.strip() for l in f.read().splitlines()]

    with CodebergAPI(owner, repo, token) as repo:
        pulls = repo.pulls()
        labels = repo.labels()

        label_mapping = {l["name"]: l["id"] for l in labels}

        for pr in pulls:
            assign_one(
                repo,
                pr,
                categories,
                dev_mapping,
                proj_mapping,
                CODEBERG_USERNAME,
                repo_path,
                label_mapping,
                bz,
                BUGZILLA_URL,
            )


if __name__ == "__main__":
    try:
        sys.exit(main(*sys.argv[1:]))
    except socket.timeout:
        print("-- Exiting due to socket timeout --")
        sys.exit(0)
