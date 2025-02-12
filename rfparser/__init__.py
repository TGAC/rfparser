#!/usr/bin/env python3

import argparse
import csv
import itertools
import logging
import os
import re
import sys
import urllib.parse
from time import sleep
from typing import (
    Any,
    Optional,
    SupportsIndex,
    TYPE_CHECKING,
    Union,
)
from xml.etree import ElementTree
from xml.etree.ElementTree import indent

import requests
import yaml
from requests import (
    Response,
    Session,
)

from .util import (
    extend_list_to_size,
    is_same_person,
    str_if_not_None,
    strip_tags,
    unique,
)

if TYPE_CHECKING:
    from typing_extensions import Self

__version__ = "0.0.1"

REQUEST_TIMEOUT = 5.0
REQUEST_RETRIES = 3
REQUEST_RETRIES_BACKOFF_FACTOR = 1.0
BASE_CR_URL = "https://api.crossref.org"
BASE_DC_URL = "https://api.datacite.org"
BASE_DOI_URL = "https://doi.org"
BASE_RF_URL = "https://api.researchfish.com/restapi"
BASE_UNPAYWALL_URL = "https://api.unpaywall.org"

KNOWN_BOOK_SERIES = {
    "Advances in Experimental Medicine and Biology",
    "Advances in Microbial Physiology",
    "Compendium of Plant Genomes",
    "Genome Dynamics",
    "Lecture Notes in Computer Science",
    "Methods in Cell Biology",
    "Methods in Enzymology",
    "Methods in Molecular Biology",
}

DC_resourceTypeGeneral_TO_CR_TYPE = {
    "BookChapter": "book-chapter",
    "ConferencePaper": "proceedings-article",
    "JournalArticle": "journal-article",
    "Preprint": "preprint",
}
# Map CrossRef's "type" to the CategoryId element of the output XML file
CR_TYPE_TO_XML_CATEGORY_ID = {
    "book-chapter": "2",
    "journal-article": "1",
    "preprint": "124",
    "proceedings-article": "2",
}
XML_CATEGORY_ID_TO_CATEGORY = {
    "1": "Journal Article",
    "2": "Book chapter",
    "124": "PrePrint",
}
# https://support.unpaywall.org/support/solutions/articles/44001777288-what-do-the-types-of-oa-status-green-gold-hybrid-and-bronze-mean-
UNPAYWALL_OA_STATUS_TO_XML_OPENACCESS = {
    "bronze": "Bronze Open Access",
    "closed": "No Open Access",
    "gold": "Gold Open Access",
    "green": "Green Open Access",
    "hybrid": "Gold Open Access",
}
VALID_DOI = re.compile(r"[\d.]+/.+")
VALID_ORCID_ID = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")

log = logging.getLogger(__name__)


class Researcher:
    def __init__(
        self,
        family_names: Optional[str] = None,
        given_names: Optional[str] = None,
        name: Optional[str] = None,
        orcid_id: Optional[str] = None,
    ):
        self.family_names = family_names
        self.given_names = given_names
        if not family_names:
            assert name
            self.name = name
        self.orcid_id = sanitise_orcid_id(orcid_id)


class Author(Researcher):
    @classmethod
    def from_CR(cls, author_dict: dict[str, Any]) -> "Self":
        orcid_id = author_dict.get("ORCID")
        family_names = author_dict.get("family")
        if family_names:
            given_names = author_dict.get("given")
            return cls(family_names=family_names, given_names=given_names, orcid_id=orcid_id)
        else:
            name = author_dict.get("name")
            if not name:
                raise Exception(f"Unrecognised author_dict format: {author_dict}")
            return cls(name=name, orcid_id=orcid_id)

    @classmethod
    def from_DC(cls, creator_dict: dict[str, Any]) -> "Self":
        nameIdentifiers = creator_dict["nameIdentifiers"]
        orcid_id: Optional[str] = None
        for nameIdentifier_dict in nameIdentifiers:
            if nameIdentifier_dict["nameIdentifierScheme"] == "ORCID":
                orcid_id = nameIdentifier_dict["nameIdentifier"]
        family_names = creator_dict.get("familyName")
        if family_names:
            given_names = creator_dict.get("givenName")
            return cls(family_names=family_names, given_names=given_names, orcid_id=orcid_id)
        else:
            name = creator_dict.get("name")
            if not name:
                raise Exception(f"Unrecognised creator_dict format: {creator_dict}")
            return cls(name=name, orcid_id=orcid_id)

    def to_contributor_format(self) -> str:
        """
        Format the author's names to a str for the ContributorsList field of the
        XML output.
        """
        if self.family_names:
            if self.given_names:
                given_name_initials = "".join(name[0] for name in self.given_names.split())
                return f"{self.family_names} {given_name_initials}"
            else:
                return self.family_names
        else:
            return self.name


class User(Researcher):
    family_names: str
    given_names: str

    def __init__(self, username: str, family_names: str, given_names: str, orcid_id: Optional[str] = None):
        self.username = username
        super().__init__(family_names=family_names, given_names=given_names, orcid_id=orcid_id)


class DOI(str):
    def __new__(cls, doi: Optional[str]) -> "Self":
        """
        Sanitise a DOI, see
        https://www.doi.org/doi-handbook/DOI_Handbook_Final.pdf
        """
        doi = (doi or "").strip()
        if doi in ("", "A", "NA", "n/a"):
            raise ValueError("no DOI")
        match = VALID_DOI.search(doi)
        if not match:
            raise ValueError("malformed DOI")
        return super().__new__(cls, match.group(0))

    def lower(self) -> str:
        # Change only ASCII characters to lowercase
        return "".join(c.lower() if c.isascii() else c for c in self)

    def __eq__(self, __value: object) -> bool:
        if not isinstance(__value, DOI):
            return False
        return self.lower() == __value.lower()

    def __hash__(self) -> int:
        return hash(self.lower())

    def startswith(
        self,
        prefix: Union[str, tuple[str, ...]],
        start: Optional[SupportsIndex] = None,
        end: Optional[SupportsIndex] = None,
    ) -> bool:
        lower_prefix = prefix.lower() if isinstance(prefix, str) else tuple(_.lower() for _ in prefix)
        return self.lower().startswith(lower_prefix, start, end)


BROKEN_DOI_TO_REASON = {
    DOI("10.5281/zenodo.7333887"): "has resourceTypeGeneral 'JournalArticle' but is actually associated code and data",
    DOI("10.5281/zenodo.7333888"): "has resourceTypeGeneral 'JournalArticle' but is actually associated code and data",
}


def RF_login(username: str, password: str) -> Session:
    """
    Login to ResearchFish API and return a session storing the auth cookie.
    """
    s = Session()
    data = {
        "username": username,
        "password": password,
    }
    r = s.post(f"{BASE_RF_URL}/user/login", data=data)
    r.raise_for_status()
    return s


def get_url(
    url: str,
    params: Optional[dict] = None,
    headers: Optional[dict[str, str]] = None,
    timeout: float = REQUEST_TIMEOUT,
    retries: int = REQUEST_RETRIES,
    s: Optional[Session] = None,
) -> Response:
    for i in range(retries):
        backoff_time = 0 if i == 0 else REQUEST_RETRIES_BACKOFF_FACTOR * (2**i)
        try:
            if s:
                r = s.get(url, params=params, headers=headers, timeout=timeout + backoff_time)
            else:
                r = requests.get(url, params=params, headers=headers, timeout=timeout + backoff_time)
        except Exception:
            log.exception("Failed %d times to get URL %s", i + 1, url)
            sleep(backoff_time)
            continue
        try:
            r.raise_for_status()
            break
        except Exception:
            if 400 <= r.status_code < 500:
                # Client error
                raise
            log.exception("Failed %d times to get URL %s , status code %d", i + 1, url, r.status_code)
            sleep(backoff_time)
    else:
        raise Exception(f"Failed too many times to get URL {url}")
    return r


def RF_get_paginated(s: Session, url: str, params: Optional[dict] = None, max_pages: int = sys.maxsize) -> list[dict]:
    """
    Get paginated items from ResearchFish API.
    """
    log.info("Started RF_get_paginated")
    if params is None:
        params = {}
    else:
        params = params.copy()
    assert max_pages > 0
    next = 0
    ret: list[dict] = []
    while next is not None and next < max_pages:
        params["start"] = next
        r = get_url(url, params=params, s=s)
        r_dict = r.json()
        ret.extend(r_dict["results"])
        next = r_dict.get("next")
    return ret


def get_doi_RA(doi: str) -> dict[str, str]:
    """
    Get Registration Agency for all DOIs.

    See https://www.doi.org/doi-handbook/HTML/which-ra_-service.html
    """
    r = get_url(f"{BASE_DOI_URL}/doiRA/{urllib.parse.quote(doi, safe='')}")
    return r.json()[0]


def CR_get_pub_metadata(doi: str, headers: Optional[dict[str, str]] = None) -> dict[str, Any]:
    """
    Get metadata for a publication from CrossRef API.
    """
    # CrossRef doesn't support HTTP persistent connections, so use a new
    # connection every time instead of a Session.
    cr_url = f"{BASE_CR_URL}/works/{urllib.parse.quote(doi, safe='')}"
    r = get_url(cr_url, headers=headers)
    r_dict = r.json()
    assert r_dict["status"] == "ok"
    return r_dict["message"]


def DC_get_pub_metadata(doi: str) -> dict[str, Any]:
    r = get_url(f"{BASE_DC_URL}/dois/{doi}")
    r_dict = r.json()
    return r_dict["data"]["attributes"]


def unpaywall_get_oa_status(s: Session, doi: str, email: str) -> str:
    """
    Get the Open Access status of a publication using the Unpaywall API
    """
    r = get_url(f"{BASE_UNPAYWALL_URL}/v2/{urllib.parse.quote(doi, safe='')}?email={email}", s=s)
    r_dict = r.json()
    return r_dict["oa_status"]


def get_dois_from_old_xml(nbiros_pub_export_xml_url: Optional[str], pubs_with_doi: dict[DOI, dict[str, Any]]) -> None:
    """
    Get the DOIs from the old ei.xml file generated from NBIROS.
    """
    log.info("Started get_dois_from_old_xml")
    if not nbiros_pub_export_xml_url:
        log.warning("nbiros_pub_export_xml_url option not specified")
        return
    r = requests.get(nbiros_pub_export_xml_url)
    r.raise_for_status()
    root_el = ElementTree.fromstring(r.text)
    assert root_el.tag == "publications"
    for pub_el in root_el:
        id_el = pub_el.find("id")
        assert id_el is not None
        pub_old_id = id_el.text
        assert pub_old_id

        doi_el = pub_el.find("DOI")
        assert doi_el is not None
        try:
            doi = DOI(doi_el.text)
        except ValueError as e:
            title_el = pub_el.find("Title")
            assert title_el is not None
            category_el = pub_el.find("Category")
            assert category_el is not None
            log.warning(
                "Skipping NBIROS publication '%s' (doi: '%s'; title '%s'; type '%s'): %s",
                pub_old_id,
                doi_el.text,
                title_el.text,
                category_el.text,
                e,
            )
            continue
        pubs_with_doi.setdefault(doi, {})
        pubs_with_doi[doi].setdefault("nbiros_entries", [])
        pubs_with_doi[doi]["nbiros_entries"].append(pub_el)


def sanitise_orcid_id(orcid_id: Optional[str]) -> Optional[str]:
    if not orcid_id:
        return None
    # Remove initial part, if it's a URL
    number = orcid_id.split("/")[-1]
    number = number.replace("-", "-")
    assert len(number) == 19, f"Malformed ORCID id {orcid_id}"
    assert VALID_ORCID_ID.match(number), f"Malformed ORCID id {orcid_id}"
    return f"https://orcid.org/{number}"


def get_users(people_data_csv_url: Optional[str]) -> list[User]:
    log.info("Started get_users")
    if not people_data_csv_url:
        log.warning("people_data_csv_url option not specified")
        return []
    r = requests.get(people_data_csv_url)
    r.raise_for_status()
    reader = csv.reader(r.text.splitlines())
    users = [
        User(username=username, family_names=family_names, given_names=given_names, orcid_id=orcid_id)
        for (username, given_names, family_names, orcid_id) in reader
    ]
    duplicated_user_indexes = []
    for i, user1 in enumerate(users):
        for user2 in users[i + 1 :]:
            if user1.given_names == user2.given_names and user1.family_names == user2.family_names:
                duplicated_user_indexes.append(i)
                break
    for index in reversed(duplicated_user_indexes):
        log.warning("Duplicated user %s will be eliminated", users[index])
        del users[index]
    log.info("Total users: %s", len(users))
    return users


def write_xml_output(
    pubs_with_doi: dict[DOI, dict[str, Any]],
    outfile: str,
    people_data_csv_url: Optional[str],
) -> None:
    """
    Write the publications to an XML file for the EI website.
    """

    def author_dict_to_username(author: Author) -> Optional[str]:
        # First try to match the ORCID id
        orcid_id = author.orcid_id
        if orcid_id:
            usernames = [user.username for user in users if user.orcid_id == orcid_id]
            if usernames:
                if len(usernames) > 1:
                    log.warning("Multiple usernames for ORCID id %s", orcid_id)
                return usernames[0]
        # Try to match the family and given names
        family_names = author.family_names
        if family_names:
            given_names = author.given_names or ""
            usernames = [
                user.username
                for user in users
                if not (orcid_id and user.orcid_id)
                and is_same_person(user.family_names, user.given_names, family_names, given_names)
            ]
            if usernames:
                if len(usernames) > 1:
                    log.warning(
                        "Multiple usernames for family names '%s', given names '%s': %s",
                        family_names,
                        given_names,
                        usernames,
                    )
                return usernames[0]
        # No need to try to match "name", which is only used for consortia
        return None

    log.info("Started write_xml_output")
    users = get_users(people_data_csv_url)
    root_el = ElementTree.Element("publications")
    for doi, pub in reversed(pubs_with_doi.items()):
        if pub["metadata_ok"]:
            publication_el = ElementTree.SubElement(root_el, "publication")
            ElementTree.SubElement(publication_el, "id").text = doi
            ElementTree.SubElement(publication_el, "Organisation").text = "EI"
            category_id = CR_TYPE_TO_XML_CATEGORY_ID[pub["type"]]
            ElementTree.SubElement(publication_el, "Category").text = XML_CATEGORY_ID_TO_CATEGORY[category_id]
            ElementTree.SubElement(publication_el, "CategoryID").text = category_id
            ElementTree.SubElement(publication_el, "Title").text = pub["title"]
            ElementTree.SubElement(publication_el, "DOI").text = doi
            if category_id in {"1", "124"}:
                # a journal article or preprint
                ElementTree.SubElement(publication_el, "JournalName").text = pub["container-title"]
            else:
                # category_id == "2", i.e. a book chapter
                ElementTree.SubElement(publication_el, "JournalName").text = ""
                ElementTree.SubElement(publication_el, "BookTitle").text = pub["container-title"]
                if "series-title" in pub:
                    ElementTree.SubElement(publication_el, "SeriesTitle").text = pub["series-title"]
            ElementTree.SubElement(publication_el, "JournalVolume").text = pub["volume"]
            ElementTree.SubElement(publication_el, "JournalPages").text = pub["pages"]
            try:
                contributor_ids_list = [author_dict_to_username(author_dict) for author_dict in pub["authors"]]
                for nbiros_entry in pub.get("nbiros_entries", []):
                    ContributorIds_el = nbiros_entry.find("ContributorIds")
                    assert ContributorIds_el is not None
                    ContributorIds_text = ContributorIds_el.text or ""
                    contributor_ids_list.extend(c.strip() for c in ContributorIds_text.split(","))
                contributor_ids = unique(filter(None, contributor_ids_list))
            except Exception:
                log.error("Error while generating ContributorIds for DOI %s", doi)
                raise
            ElementTree.SubElement(publication_el, "ContributorIds").text = ", ".join(contributor_ids)
            ElementTree.SubElement(publication_el, "ContributorList").text = ", ".join(
                author.to_contributor_format() for author in pub["authors"]
            )
            ElementTree.SubElement(publication_el, "Year").text = str_if_not_None(pub["year"])
            ElementTree.SubElement(publication_el, "Month").text = str_if_not_None(pub["month"])
            ElementTree.SubElement(publication_el, "Day").text = str_if_not_None(pub["day"])
            ElementTree.SubElement(publication_el, "OpenAccess").text = UNPAYWALL_OA_STATUS_TO_XML_OPENACCESS[
                pub["oa_status"]
            ]
    xml_tree = ElementTree.ElementTree(root_el)
    indent(xml_tree, space="\t")
    xml_tree.write(outfile, encoding="utf-8", xml_declaration=True)


def main() -> None:
    # Parse command-line options
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--config", default="config.yaml", help="configuration file path")
    parser.add_argument(
        "-p",
        "--pages",
        default=sys.maxsize,
        type=int,
        help="maximum number of pages of ResearchFish publications to get",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="set log level to DEBUG")
    parser.add_argument("-x", "--xml", help="XML output path. If not set, no XML file will be produced")
    args = parser.parse_args()

    if args.verbose:
        log_level = logging.DEBUG
    else:
        log_level = logging.INFO
    logging.basicConfig(level=log_level)
    # Restore urllib3 log level
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    assert args.pages > 0

    # Read config file
    try:
        with open(args.config) as f:
            config = yaml.safe_load(f)
        log.info(f"Read configuration file {args.config}")
    except Exception:
        config = {}
        log.warning(f"Could not read configuration file {args.config}")

    for env_var in ("RF_USERNAME", "RF_PASSWORD", "RFPARSER_EMAIL", "NBIROS_PUB_EXPORT_XML_URL", "PEOPLE_DATA_CSV_URL"):
        if env_var in os.environ:
            config_key = env_var.lower()
            if config_key.startswith("rfparser_"):
                config_key = config_key[9:]
            config[config_key] = os.environ[env_var]

    assert config.get("rf_username"), "ResearchFish username not configured"
    assert config.get("rf_password"), "ResearchFish password not configured"
    assert config.get("email"), "Email not configured"

    # Create dictionary of publications indexed by DOI
    pubs_with_doi: dict[DOI, dict[str, Any]] = {}

    get_dois_from_old_xml(config.get("nbiros_pub_export_xml_url"), pubs_with_doi)
    log.info(f"Unique publication DOIs: {len(pubs_with_doi)}")

    # Login to ResearchFish API
    rf_session = RF_login(config["rf_username"], config["rf_password"])
    log.debug("Successfully logged in to ResearchFish API")

    # Get awards
    # awards = RF_get_paginated(rf_session, f"{BASE_RF_URL}/award")
    # with open("/tmp/awards.txt", "w") as f:
    #     for award in awards:
    #         print(award["fa_name"], file=f)

    # Get publication dois
    params = {
        "section": "publications",
    }
    publications = RF_get_paginated(rf_session, f"{BASE_RF_URL}/outcome", params=params, max_pages=args.pages)
    log.info(f"Total publications on ResearchFish: {len(publications)}")

    for p in publications:
        try:
            doi = DOI(p["r1_2_19"])
        except ValueError as e:
            log.warning(
                "Skipping ResearchFish publication '%s' (doi: '%s'; title '%s'; type '%s'): %s",
                p["id"],
                p["r1_2_19"],
                p["title"],
                p["r1_2"],
                e,
            )
        else:
            pubs_with_doi.setdefault(doi, {})
            pubs_with_doi[doi].setdefault("rf_entries", [])
            pubs_with_doi[doi]["rf_entries"].append(p)
    log.info(f"Unique publication DOIs: {len(pubs_with_doi)}")

    # Process publications with a DOI
    cr_headers = {
        "User-Agent": f"rfparser/{__version__} (https://github.com/TGAC/rfparser; mailto:{config['email']})",
    }
    unpaywall_session = Session()
    for doi, pub in pubs_with_doi.items():
        pub["metadata_ok"] = False
        if doi in BROKEN_DOI_TO_REASON:
            log.warning("Skipping publication '%s': %s", doi, BROKEN_DOI_TO_REASON[doi])
            continue
        try:
            # Get registration agency for the DOI
            doi_RA = get_doi_RA(doi)
            if "RA" not in doi_RA:
                raise Exception(doi_RA["status"])
            pub["RA"] = doi_RA["RA"]

            # Get metadata from the RA
            # https://www.doi.org/the-community/existing-registration-agencies/
            if pub["RA"] in ("Crossref", "OP"):
                pub_metadata = CR_get_pub_metadata(doi, headers=cr_headers)
                # Join title parts while removing leading, trailing and multiple whitespaces
                title = " ".join(
                    itertools.chain.from_iterable(title_part.split() for title_part in pub_metadata["title"])
                )
                title = strip_tags(title)
                pub["title"] = title

                pub_type = pub_metadata["type"]
                if pub_type == "posted-content":
                    assert (
                        pub_metadata["subtype"] == "preprint"
                    ), f"publication is of type '{pub_type}' with unknown subtype '{pub_metadata['pub_subtype']}'"
                    pub_type = pub_metadata["subtype"]
                assert pub_type in CR_TYPE_TO_XML_CATEGORY_ID, f"unknown publication type {pub_type}"
                pub["type"] = pub_type

                container_title_list = pub_metadata["container-title"]
                if len(container_title_list) == 0:
                    assert pub_type == "preprint", f"publication of type {pub_type} cannot have empty container-title"
                    institution = pub_metadata.get("institution")
                    if institution:
                        assert (
                            len(pub_metadata["institution"]) == 1
                        ), f"institution with multiple or no elements: {pub_metadata['institution']}"
                        container_title = pub_metadata["institution"][0]["name"]
                    elif "Research Square" in pub_metadata["publisher"]:
                        container_title = "Research Square"
                    elif "PeerJ" in pub_metadata["publisher"] or doi.startswith("10.37044/osf.io/"):
                        # PeerJ Preprints or BioHackrXiv
                        container_title = pub_metadata["group-title"]
                    elif doi.startswith("10.17504/protocols.io."):
                        container_title = "protocols.io"
                    else:
                        raise Exception("cannot determine preprint journal")
                elif len(container_title_list) == 1:
                    container_title = container_title_list[0]
                else:
                    assert (
                        pub_type == "book-chapter"
                    ), f"publication of type {pub_type} cannot have container-title with multiple elements: {container_title_list}"
                    assert (
                        len(container_title_list) == 2
                    ), f"publication of type {pub_type} cannot have container-title with more than 2 elements: {container_title_list}"
                    # the book title and book series are not in a fixed order
                    if container_title_list[0] in KNOWN_BOOK_SERIES:
                        container_title, pub["series-title"] = reversed(container_title_list)
                    elif container_title_list[1] in KNOWN_BOOK_SERIES:
                        container_title, pub["series-title"] = container_title_list
                    else:
                        raise Exception(
                            f"publication of type {pub_type} has container-title with unknown book series: {container_title_list}"
                        )
                pub["container-title"] = container_title

                pub["volume"] = pub_metadata.get("volume")
                pub["pages"] = pub_metadata.get("page")

                authors = pub_metadata.get("author")
                # Missing authors (or any other incorrect publication metadata) need
                # to be fixed by the publisher, see
                # https://community.crossref.org/t/where-to-report-incorrect-metadata/3321
                assert authors, "missing authors"
                pub["authors"] = [Author.from_CR(author_dict) for author_dict in authors]

                # The "issued" field contains the earliest known publication date
                # (see https://github.com/CrossRef/rest-api-doc#sorting )
                issued_date_parts = pub_metadata["issued"]["date-parts"][0]
                pub["year"], pub["month"], pub["day"] = extend_list_to_size(issued_date_parts, 3)

                pub["oa_status"] = unpaywall_get_oa_status(unpaywall_session, doi, config["email"])
            elif pub["RA"] == "DataCite":
                pub_metadata = DC_get_pub_metadata(doi)
                pub["title"] = pub_metadata["titles"][0]["title"]

                resourceTypeGeneral = pub_metadata["types"]["resourceTypeGeneral"]
                pub_type = DC_resourceTypeGeneral_TO_CR_TYPE.get(resourceTypeGeneral)
                assert pub_type, f"unknown publication type {resourceTypeGeneral}"
                pub["type"] = pub_type

                if pub_type == "preprint":
                    publisher = pub_metadata["publisher"]
                    assert publisher, f"publication of type {pub_type} cannot have empty publisher"
                    assert isinstance(publisher, str)
                    container_title = publisher
                else:
                    container = pub_metadata["container"]
                    if not container and doi.startswith("10.17863/cam."):
                        log.warning("Skipping publication '%s': likely a copy of the publisher version", doi)
                        continue
                    raise Exception("Unhandled situation!")
                pub["container-title"] = container_title

                pub["volume"] = pub_metadata.get("volume")
                pages = pub_metadata.get("firstPage")
                if pages and (lastPage := pub_metadata.get("lastPage")):
                    pages = f"{pages}-{lastPage}"
                pub["pages"] = pages

                creators = pub_metadata["creators"]
                assert creators, "missing authors"
                pub["authors"] = [Author.from_DC(creator_dict) for creator_dict in creators]

                dates = pub_metadata["dates"]
                issued_date = None
                for date_dict in dates:
                    if date_dict["dateType"] == "Issued":
                        issued_date = date_dict["date"]
                        break
                assert issued_date, "missing issued date"
                issued_date_parts = issued_date[:10].split("-")
                pub["year"], pub["month"], pub["day"] = extend_list_to_size(issued_date_parts, 3)

                # https://support.unpaywall.org/support/solutions/articles/44001900286
                pub["oa_status"] = "green"
            else:
                raise Exception(f"Registration agency is {pub['RA']}, which is not currently handled")

            pub["metadata_ok"] = True
        except Exception as e:
            if str(e).startswith("Failed too many times to get URL"):
                raise
            log.error("Skipping publication '%s': %s", doi, e)

    if args.xml:
        write_xml_output(pubs_with_doi, args.xml, config.get("people_data_csv_url"))


if __name__ == "__main__":
    main()
