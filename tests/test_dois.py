import pytest
import requests.exceptions
from requests import Session

from rfparser import (
    DOI,
    doi_exists,
    unpaywall_get_oa_status,
)


def test_DOI():
    doi_str = "10.1128/AEM.72.1.946-949.2006"
    doi = DOI(doi_str)
    assert str(doi) == doi_str
    assert doi == DOI(doi_str.lower())
    with pytest.raises(ValueError, match="no DOI"):
        DOI("")
    with pytest.raises(ValueError, match="malformed DOI"):
        DOI("foo")


def test_doi_exists():
    assert doi_exists("10.1128/AEM.72.1.946-949.2006") is True
    assert doi_exists("10.17138/tgft(11)11-21") is True
    assert doi_exists("foo/bar") is False
    assert doi_exists("0.1101/2021.08.04.455072") is False
    assert doi_exists("") is False


def test_unpaywall_get_oa_status():
    unpaywall_session = Session()
    test_email = "user@example.org"
    assert unpaywall_get_oa_status(unpaywall_session, "10.1128/AEM.72.1.946-949.2006", test_email) == "green"
    assert unpaywall_get_oa_status(unpaywall_session, "10.17138/tgft(11)11-21", test_email) == "gold"
    with pytest.raises(requests.exceptions.HTTPError):
        unpaywall_get_oa_status(unpaywall_session, "foo/bar", test_email)
