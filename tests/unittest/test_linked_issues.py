"""GitHub resolves closing links itself; we must read that answer correctly.

The failure that matters is not an exception — it is returning [] when GitHub
actually knows about a link. /describe rewrites the PR body, and GitHub derives
keyword links from that body, so a wrong empty answer can drop the link and stop
the issue auto-closing on merge.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pr_agent.git_providers.github_provider import GithubProvider


def _provider(response=None, raises=None, base_url="https://api.github.com"):
    """A GithubProvider with just enough wired up to call get_linked_issues."""
    provider = GithubProvider.__new__(GithubProvider)
    provider.repo = "acme/widgets"
    provider.pr_num = 42
    provider.base_url = base_url

    requester = MagicMock()
    if raises is not None:
        requester.requestJsonAndCheck.side_effect = raises
    else:
        requester.requestJsonAndCheck.return_value = ({}, response)
    provider.pr = SimpleNamespace(_requester=requester)
    return provider, requester


def _payload(nodes):
    return {"data": {"repository": {"pullRequest": {"closingIssuesReferences": {"nodes": nodes}}}}}


def test_returns_linked_issues():
    provider, _ = _provider(_payload([
        {"number": 128, "title": "Duplicate pushes produce a single comment",
         "url": "https://github.com/acme/widgets/issues/128", "state": "OPEN"},
    ]))

    assert provider.get_linked_issues() == [{
        "number": 128,
        "title": "Duplicate pushes produce a single comment",
        "url": "https://github.com/acme/widgets/issues/128",
        "state": "open",
    }]


def test_posts_the_pr_number_and_repo_it_was_given():
    provider, requester = _provider(_payload([]))
    provider.get_linked_issues()

    method, url = requester.requestJsonAndCheck.call_args[0]
    variables = requester.requestJsonAndCheck.call_args[1]["input"]["variables"]
    assert method == "POST"
    assert url == "https://api.github.com/graphql"
    assert variables == {"owner": "acme", "name": "widgets", "number": 42}


def test_graphql_errors_are_not_read_as_no_links():
    """GraphQL answers HTTP 200 on failure, so requestJsonAndCheck raises nothing."""
    provider, _ = _provider({"data": None, "errors": [{"message": "Resource not accessible"}]})
    assert provider.get_linked_issues() == []


def test_transport_failure_is_swallowed():
    provider, _ = _provider(raises=RuntimeError("boom"))
    assert provider.get_linked_issues() == []


def test_no_linked_issues_returns_empty():
    provider, _ = _provider(_payload([]))
    assert provider.get_linked_issues() == []


def test_tolerates_partial_and_malformed_nodes():
    provider, _ = _provider(_payload([
        {"number": 7, "title": None, "url": None, "state": None},
        {"title": "no number at all"},
        "not-a-dict",
    ]))

    assert provider.get_linked_issues() == [
        {"number": 7, "title": "", "url": "", "state": ""}
    ]


def test_tolerates_a_truncated_response_shape():
    provider, _ = _provider({"data": {"repository": None}})
    assert provider.get_linked_issues() == []


def test_unexpected_repo_path_does_not_call_the_api():
    provider, requester = _provider(_payload([]))
    provider.repo = "no-slash-here"
    assert provider.get_linked_issues() == []
    requester.requestJsonAndCheck.assert_not_called()


@pytest.mark.parametrize("base_url,expected", [
    ("https://api.github.com", "https://api.github.com/graphql"),
    ("https://api.github.com/", "https://api.github.com/graphql"),
    # GitHub Enterprise serves REST under /api/v3 and GraphQL under /api/graphql
    ("https://ghe.acme.com/api/v3", "https://ghe.acme.com/api/graphql"),
])
def test_graphql_endpoint(base_url, expected):
    provider, requester = _provider(_payload([]), base_url=base_url)
    provider.get_linked_issues()
    assert requester.requestJsonAndCheck.call_args[0][1] == expected


def test_other_providers_report_unknown_rather_than_none_linked():
    """The base implementation means 'this provider cannot answer'."""
    from pr_agent.git_providers.git_provider import GitProvider
    assert GitProvider.get_linked_issues(object()) == []
