#!/usr/bin/env python
# vim:fileencoding=utf-8
# Assign pull requests
# (c) 2017-2018 Michał Górny, 2-clause BSD licensed

import bugzilla
import json
import os
import os.path
import re
import socket
import sys
try:
    import urllib.request as urllib
except ImportError:
    import urllib
try:
    import xmlrpc.client as xmlrpcclient
except ImportError:
    import xmlrpclib as xmlrpcclient

import github
import lxml.etree


BUG_LONG_URL_RE = re.compile(r'https?://bugs\.gentoo\.org/show_bug\.cgi\?id=(\d+)(?:[&#].*)?$')
BUG_SHORT_URL_RE = re.compile(r'https?://bugs\.gentoo\.org/(\d+)(?:[?#].*)?$')


def map_dev(dev, dev_mapping):
    if dev_mapping.get(dev.lower()):
        return '@' + dev_mapping[dev.lower()]
    if dev.endswith('@gentoo.org'):
        dev = dev[:-len('@gentoo.org')]
    else:
        dev = dev.replace('@', '[at]')
    return '~~%s~~' % dev


def map_proj(proj, proj_mapping):
    if proj.lower() in proj_mapping:
        return '@' + proj_mapping[proj.lower()].lower()
    if proj.endswith('@gentoo.org'):
        proj = proj[:-len('@gentoo.org')]
    else:
        proj = proj.replace('@', '[at]')
    return '~~[%s (project)]~~' % proj


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
    """ Verify if emails have Bugzilla accounts. Returns iterator over
    mails that do not have accounts. """
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


def main(ref_repo_path):
    GITHUB_DEV_MAPPING = os.environ['GITHUB_DEV_MAPPING']
    GITHUB_PROXIED_MAINT_MAPPING = os.environ['GITHUB_PROXIED_MAINT_MAPPING']
    GITHUB_PROJ_MAPPING = os.environ['GITHUB_PROJ_MAPPING']
    GITHUB_USERNAME = os.environ['GITHUB_USERNAME']
    GITHUB_TOKEN_FILE = os.environ['GITHUB_TOKEN_FILE']
    GITHUB_REPO = os.environ['GITHUB_REPO']

    with open(GITHUB_TOKEN_FILE) as f:
        token = f.read().strip()

    BUGZILLA_URL = os.environ['BUGZILLA_URL']
    BUGZILLA_APIKEY_FILE = os.environ['BUGZILLA_APIKEY_FILE']

    with open(BUGZILLA_APIKEY_FILE) as f:
        bugz_apikey = f.read().strip()

    g = github.Github(GITHUB_USERNAME, token, per_page=50)
    r = g.get_repo(GITHUB_REPO)
    bz = bugzilla.Bugzilla(BUGZILLA_URL,
                           api_key=bugz_apikey)

    with open(GITHUB_PROXIED_MAINT_MAPPING) as f:
        dev_mapping = json.load(f)
    with open(GITHUB_DEV_MAPPING) as f:
        dev_mapping.update(json.load(f))
    with open(GITHUB_PROJ_MAPPING) as f:
        proj_mapping = json.load(f)
    with open(os.path.join(ref_repo_path, 'profiles/categories')) as f:
        categories = [l.strip() for l in f.read().splitlines()]

    for issue in r.get_issues(state='open'):
        # note: we need github.Issue due to labels missing in PR
        pr_getter = lambda: r.get_pull(issue.number)
        assign_one(pr_getter, issue, dev_mapping, proj_mapping, categories,
                GITHUB_USERNAME, ref_repo_path, bz, BUGZILLA_URL)

    return 0


def assign_one(pr_getter, issue, dev_mapping, proj_mapping, categories,
        GITHUB_USERNAME, ref_repo_path, bz, BUGZILLA_URL):
    assignee_limit = 5
    bug_limit = 5

    # check if we are to reassign
    if '[please reassign]' in issue.title.lower():
        print('PR#%d: [please reassign] found' % issue.number)
        issue.edit(title=re.sub(r'\s*\[please reassign\]\s*', '', issue.title,
                                flags=re.IGNORECASE))
    else:
        # check if assigned already
        if issue.assignee:
            print('PR#%d: assignee found' % issue.number)
            return
        for l in issue.labels:
            if l.name in ('assigned', 'need assignment', 'do not merge'):
                print('PR#%d: %s label found' % (issue.number, l.name))
                return

    for l in issue.labels:
        if l.name == 'no assignee limit':
            assignee_limit = 9999
            bug_limit = 9999

    pr = pr_getter()

    # delete old results
    for co in issue.get_comments():
        if co.user.login == GITHUB_USERNAME:
            if 'Pull Request assignment' not in co.body:
                continue
            co.delete()

    # scan file list
    areas = set()
    packages = set()
    metadata_xml_files = set()
    for f in pr.get_files():
        path = f.filename.split('/')
        if path[0] in categories:
            areas.add('ebuilds')
            if path[1] == 'metadata.xml':
                areas.add('category-metadata')
            elif len(path) <= 2:
                areas.add('other files')
            else:
                if path[2] == 'metadata.xml':
                    # package metadata, need to verify it
                    metadata_xml_files.add(f.raw_url)
                packages.add('/'.join(path[0:2]))
        elif path[0] == 'eclass':
            areas.add('eclasses')
        elif path[0] == 'profiles':
            if path[1] != 'use.local.desc':
                areas.add('profiles')
        elif path[0] == 'metadata':
            if path[1] not in ('md5-cache', 'pkg_desc_index'):
                areas.add('other files')
        else:
            areas.add('other files')

    body = '''## Pull Request assignment

*Submitter*: %s
*Areas affected*: %s
*Packages affected*: %s%s
''' % ('@' + pr.user.login,
        ', '.join(sorted(areas)) or '(none, wtf?!)',
        ', '.join(sorted(packages)[0:5]) or '(none)',
        '...' if len(packages) > 5 else '')

    # at least one ...
    new_package = False
    existing_package = False
    maint_needed = False
    cant_assign = False
    not_self_maintained = False
    invalid_email = False
    invalid_bug_linked = False
    unique_maints = set()
    totally_all_maints = set()

    if packages:
        # now try to determine unique sets of maintainers
        # if we get too many unique sets, i.e. we would end up highlighting
        # everyone, do not auto-assign
        pkg_maints = {}
        for p in packages:
            ppath = os.path.join(ref_repo_path, p, 'metadata.xml')
            try:
                metadata_xml = lxml.etree.parse(ppath)
            except (OSError, IOError):
                # no metadata.xml? most likely a new package!
                pkg_maints[p] = ['@gentoo/proxy-maint (new package)']
                new_package = True
            else:
                # metadata.xml found, this PR touches existing packages
                existing_package = True
                all_ms = []
                for m in metadata_xml.getroot():
                    if m.tag != 'maintainer':
                        continue
                    totally_all_maints.add(m.findtext('email').strip())
                    if m.get('type') == 'project':
                        ms = map_proj(m.findtext('email'), proj_mapping)
                    else:
                        ms = map_dev(m.findtext('email'), dev_mapping)

                    for subm in m:
                        if m.tag == 'description' and m.get('lang', 'en') == 'en':
                            ms += ' (%s)' % m.text
                    all_ms.append(ms)

                if all_ms:
                    # not a single GitHubber? not good.
                    if not [x for x in all_ms if '@' in x]:
                        cant_assign = True
                    pkg_maints[p] = all_ms
                    # if for at least one package, the user is not
                    # in maintainers, we do not consider it self-maintained
                    # TODO: handle team memberships
                    if '@' + pr.user.login not in all_ms:
                        not_self_maintained = True
                    unique_maints.add(tuple(sorted(all_ms)))
                    if len(unique_maints) > assignee_limit:
                        break
                else:
                    # maintainer-needed!
                    pkg_maints[p] = ['@gentoo/proxy-maint (maintainer needed)']
                    maint_needed = True

        if len(unique_maints) > assignee_limit:
            cant_assign = True
            body += '\n@gentoo/github: Too many disjoint maintainers, disabling auto-assignment.'
        else:
            for p in sorted(packages):
                body += '\n**%s**: %s' % (p, ', '.join(pkg_maints[p]))
            if cant_assign:
                body += '\n\nAt least one of the listed packages is maintained entirely by non-GitHub developers!'
    else:
        cant_assign = True
        body += '\n@gentoo/github'

    if len(unique_maints) > assignee_limit:
        totally_all_maints = set()

    # if any metadata.xml files were changed, we want to check the new
    # maintainers for invalid addresses too
    # TODO: report maintainer change diffs
    for mxml in metadata_xml_files:
        f = urllib.urlopen(mxml)
        try:
            try:
                metadata_xml = lxml.etree.parse(f)
            except lxml.etree.XMLSyntaxError:
                # TODO: report this? pkgcheck should complain anyway
                pass
            else:
                for m in metadata_xml.getroot():
                    if m.tag == 'maintainer':
                        totally_all_maints.add(m.findtext('email').strip())
        finally:
            f.close()

    # scan for bugs now
    bugs = set()
    for c in pr.get_commits():
        for l in c.commit.message.splitlines():
            if l.startswith('Bug:') or l.startswith('Closes:'):
                tag, url = l.split(':', 1)
                url = url.strip()
                m = BUG_LONG_URL_RE.match(url)
                if m is None:
                    m = BUG_SHORT_URL_RE.match(url)
                if m is not None:
                    bugs.add(int(m.group(1)))

    body += '\n\n## Linked bugs'
    if bugs:
        body += '\nBugs linked: %s' % ', '.join([
                '[%d](%s/%d)' % (x, BUGZILLA_URL, x) for x in bugs])
        if len(bugs) > bug_limit:
            body += '\nCross-linking bugs disabled due to large number of bugs linked.'
        else:
            updq = bz.build_update(
                    keywords_add=['PullRequest'],
                    see_also_add=[pr.html_url])
            try:
                bz.update_bugs(list(bugs), updq)
            except xmlrpcclient.Fault as e:
                if e.faultCode != 101:
                    raise
                invalid_bug_linked = True
                body += '\n\n**One of the linked bugs does not exist!**'

        # match security@, security-audit@, and security-kernel@
        security = any(bug.assigned_to_detail['id'] in [2546, 23358, 25934]
                       for bug in bz.getbugs(list(bugs),
                                             include_fields=['assigned_to']))
    else:
        body += '\n\nNo bugs to link found. If your pull request references any of the Gentoo bug reports, please add appropriate [GLEP 66](https://www.gentoo.org/glep/glep-0066.html#commit-messages) tags to the commit message and request reassignment.'

    if existing_package and not_self_maintained and not bugs:
        body += '\n\n**If you do not receive any reply to this pull request, please open or link a bug to attract the attention of maintainers.**'

    if not existing_package:
        body += '\n\n## New packages\nThis Pull Request appears to be introducing new packages only. Due to limited manpower, adding new packages is considered low priority. This does not mean that your Pull Request will not receive any attention, however, it might take quite some time for it to be reviewed. In the meantime, your new ebuild might find a home in the [GURU project repository](https://wiki.gentoo.org/wiki/Project:GURU): the ebuild repository maintained collaboratively by Gentoo users. GURU offers your ebuild a place to be reviewed and improved by other Gentoo users, while making it easy for Gentoo users to install it and enjoy the software it adds.'

    # now verify maintainers for invalid addresses
    if totally_all_maints:
        invalid_mails = sorted(verify_emails(totally_all_maints, bz))
        if invalid_mails:
            invalid_email = True
            body += '\n\n## Missing Bugzilla accounts\n\n**WARNING**: The following maintainers do not match any Bugzilla accounts:'
            for m in invalid_mails:
                body += '\n- %s' % m
            body += '\n\nPlease either fix the e-mail addresses in metadata.xml or create a Bugzilla account, and request reassignment afterwards.'

    # check for GCO sign-off
    missing_signoff = any(
        not any(x.startswith('Signed-off-by:') for x in c.commit.message.splitlines())
        for c in pr.get_commits())
    if missing_signoff:
        body += '\n\n## Missing GCO sign-off\n\nPlease read the terms of [Gentoo Certificate of Origin](https://www.gentoo.org/glep/glep-0076.html#certificate-of-origin) and acknowledge them by adding a sign-off to *all* your commits.'

    body += '\n\n---\nIn order to force reassignment and/or bug reference scan, please append `[please reassign]` to the pull request title.\n\n*Docs*: [Code of Conduct](https://wiki.gentoo.org/wiki/Project:Council/Code_of_conduct) ● [Copyright policy](https://www.gentoo.org/glep/glep-0076.html) ([expl.](https://dev.gentoo.org/~mgorny/articles/new-gentoo-copyright-policy-explained.html)) ● [Devmanual](https://devmanual.gentoo.org/) ● [GitHub PRs](https://wiki.gentoo.org/wiki/Project:GitHub/Pull_requests) ● [Proxy-maint guide](https://wiki.gentoo.org/wiki/Project:Proxy_Maintainers/User_Guide)'

    issue.create_comment(body)

    # check for old labels to remove
    for l in issue.labels:
        if l.name in ('assigned', 'need assignment', 'self-maintained',
                      'maintainer-needed', 'new package', 'no signoff',
                      'bug linked', 'no bug found', 'invalid email',
                      'invalid bug linked'):
            issue.remove_from_labels(l.name)

    if maint_needed:
        issue.add_to_labels('maintainer-needed')
        # packages with m-needed are not self-maintained unless the user
        # makes himself the maintainer
        not_self_maintained = True
    if new_package:
        issue.add_to_labels('new package')
    if cant_assign:
        issue.add_to_labels('need assignment')
    else:
        if not not_self_maintained:
            issue.add_to_labels('self-maintained')
        issue.add_to_labels('assigned')
    if bugs:
        issue.add_to_labels('bug linked')
        if security:
            issue.add_to_labels('security')
    elif not_self_maintained:
        issue.add_to_labels('no bug found')
    if invalid_bug_linked:
        issue.add_to_labels('invalid bug linked')
    if invalid_email:
        issue.add_to_labels('invalid email')
    if missing_signoff:
        issue.add_to_labels('no signoff')
    if '[noci]' in issue.title.lower():
        issue.add_to_labels('noci')
    print('PR#%d: assigned' % pr.number)


if __name__ == '__main__':
    try:
        sys.exit(main(*sys.argv[1:]))
    except socket.timeout:
        print('-- Exiting due to socket timeout --')
        sys.exit(0)
