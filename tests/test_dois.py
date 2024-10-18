import pytest
import requests.exceptions
from requests import Session

from rfparser import (
    DOI,
    get_doi_RA,
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


def test_get_dois_RA():
    doi_to_expected_RA = {
        "10.1128/AEM.72.1.946-949.2006": "Crossref",
        "10.17138/tgft(11)11-21": "Crossref",
        "10.48550/arXiv.2410.03490": "DataCite",
        "foo/bar": None,
        "0.1101/2021.08.04.455072": None,
        "": None,
    }
    for doi, expected_RA in doi_to_expected_RA.items():
        doi_RA = get_doi_RA(doi)
        RA = doi_RA.get("RA")
        assert RA == expected_RA
        if RA is None:
            assert doi_RA.get("status")


def test_unpaywall_get_oa_status():
    unpaywall_session = Session()
    test_email = "user@example.org"
    assert unpaywall_get_oa_status(unpaywall_session, "10.1128/AEM.72.1.946-949.2006", test_email) == "green"
    assert unpaywall_get_oa_status(unpaywall_session, "10.17138/tgft(11)11-21", test_email) == "gold"
    with pytest.raises(requests.exceptions.HTTPError):
        unpaywall_get_oa_status(unpaywall_session, "foo/bar", test_email)
