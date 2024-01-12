#!/usr/bin/env python3

import argparse
import itertools
import logging
import os
import sys
from time import sleep
from typing import (
    Any,
    Dict,
    List,
    Optional,
)
from xml.etree import ElementTree

import requests
import yaml
from requests import Session

from .util import (
    str_if_not_None,
    strip_tags,
)

if sys.version_info >= (3, 9):
    from xml.etree.ElementTree import indent
else:
    from .ElementTree39 import indent

__version__ = "0.0.1"

BASE_CR_URL = "https://api.crossref.org"
BASE_RF_URL = "https://api.researchfish.com/restapi"
BASE_UNPAYWALL_URL = "https://api.unpaywall.org"

KNOWN_BOOK_SERIES = {
    "Advances in Experimental Medicine and Biology",
    "Advances in Microbial Physiology",
    "Genome Dynamics",
    "Lecture Notes in Computer Science",
    "Methods in Cell Biology",
    "Methods in Enzymology",
    "Methods in Molecular Biology",
}

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

log = logging.getLogger(__name__)


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


def RF_get_paginated(s: Session, url: str, params: Optional[Dict] = None, max_pages: int = sys.maxsize) -> List[Dict]:
    """
    Get paginated items from ResearchFish API.
    """
    if params is None:
        params = {}
    else:
        params = params.copy()
    assert max_pages > 0
    next = 0
    ret: List[Dict] = []
    while next is not None and next < max_pages:
        params["start"] = next
        for i in range(3):
            try:
                r = s.get(url, params=params)
                r.raise_for_status()
                break
            except Exception:
                log.exception("Failed %d times to get URL", i + 1)
                sleep(1)
        else:
            raise Exception("Failed too many times to get URL")
        r_dict = r.json()
        ret.extend(r_dict["results"])
        next = r_dict.get("next")
    return ret


def CR_get_pub_metadata(doi: str, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """
    Get metadata for a publication from CrossRef API.
    """
    # CrossRef doesn't support HTTP persistent connections, so use a new
    # connection every time instead of a Session.
    r = requests.get(f"{BASE_CR_URL}/works/{doi}", headers=headers)
    r.raise_for_status()
    r_dict = r.json()
    assert r_dict["status"] == "ok"
    return r_dict["message"]


def unpaywall_get_oa_status(s: Session, doi: str, email: str) -> str:
    """
    Get the Open Access status of a publication using the Unpaywall API
    """
    r = s.get(f"{BASE_UNPAYWALL_URL}/v2/{doi}?email={email}")
    r.raise_for_status()
    r_dict = r.json()
    return r_dict["oa_status"]


def sanitise_doi(doi: Optional[str]) -> Optional[str]:
    """
    Sanitise a doi, which should always start with "10.", see
    https://www.doi.org/the-identifier/resources/handbook/2_numbering
    """
    doi = (doi or "").strip()
    if doi in ("", "A", "NA", "n/a"):
        return None
    doi_start = doi.find("10.")
    if doi_start > 0:
        return doi[doi_start:]
    elif doi_start == -1:
        raise ValueError("Malformed DOI")
    return doi


def get_dois_from_old_xml(nbiros_pub_export_xml_url: Optional[str], pubs_with_doi: Dict[str, Dict[str, Any]]) -> None:
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
            doi = sanitise_doi(doi_el.text)
        except ValueError:
            log.warning("Publication %s has malformed DOI: %s", pub_old_id, doi_el.text)
            continue
        if not doi:
            category_el = pub_el.find("Category")
            assert category_el is not None
            pub_type = category_el.text
            log.warning("Publication %s of type %s has no DOI", pub_old_id, pub_type)
            continue
        pubs_with_doi.setdefault(doi, {})
        pubs_with_doi[doi].setdefault("nbiros_entries", [])
        pubs_with_doi[doi]["nbiros_entries"].append(pub_el)


def write_xml_output(pubs_with_doi: Dict[str, Dict[str, Any]], outfile: str) -> None:
    """
    Write the publications to an XML file for the EI website.
    """

    def author_dict_to_contributor(author_dict: Dict[str, Any]) -> str:
        """
        Transform an author dict from CrossRef to a str for the ContributorsList
        field of the XML output.
        """
        family_names = author_dict.get("family")
        if family_names:
            given_names = author_dict.get("given")
            if given_names:
                given_name_initials = "".join(name[0] for name in given_names.split())
                return f"{family_names} {given_name_initials}"
            else:
                return family_names
        else:
            name = author_dict.get("name")
            if not name:
                raise Exception(f"Unrecognised author_dict format: {author_dict}")
            return name

    log.info("Started write_xml_output")
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
                ElementTree.SubElement(publication_el, "BookTitle").text = pub["container-title"]
                if "series-title" in pub:
                    ElementTree.SubElement(publication_el, "SeriesTitle").text = pub["series-title"]
            ElementTree.SubElement(publication_el, "JournalVolume").text = pub["volume"]
            ElementTree.SubElement(publication_el, "JournalPages").text = pub["pages"]
            ElementTree.SubElement(publication_el, "ContributorList").text = ", ".join(
                author_dict_to_contributor(author_dict) for author_dict in pub["authors"]
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

    assert args.pages > 0

    # Read config file
    try:
        with open(args.config) as f:
            config = yaml.safe_load(f)
        log.info(f"Read configuration file {args.config}")
    except Exception:
        config = {}
        log.warning(f"Could not read configuration file {args.config}")

    for env_var in ("RF_USERNAME", "RF_PASSWORD", "RFPARSER_EMAIL", "NBIROS_PUB_EXPORT_XML_URL"):
        if env_var in os.environ:
            config_key = env_var.lower()
            if config_key.startswith("rfparser_"):
                config_key = config_key[9:]
            config[config_key] = os.environ[env_var]

    assert config.get("rf_username"), "ResearchFish username not configured"
    assert config.get("rf_password"), "ResearchFish password not configured"
    assert config.get("email"), "Email not configured"

    # Create dictionary of publications indexed by DOI
    pubs_with_doi: Dict[str, Dict[str, Any]] = {}

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
        doi = p["r1_2_19"]
        if doi is None:
            log.warning(
                "Skipping ResearchFish publication '%s': publication '%s' of type %s has no DOI",
                p["id"],
                p["title"],
                p["r1_2"],
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
        try:
            pub_metadata = CR_get_pub_metadata(doi, headers=cr_headers)
            # Join title parts while removing leading, trailing and multiple whitespaces
            title = " ".join(itertools.chain.from_iterable(title_part.split() for title_part in pub_metadata["title"]))
            title = strip_tags(title)
            pub["title"] = title

            pub_type = pub_metadata["type"]
            if pub_type == "posted-content":
                assert (
                    pub_metadata["subtype"] == "preprint"
                ), f"publication is of type '{pub_type}' with unknown subtype '{pub_metadata['pub_subtype']}'"
                pub_type = pub_metadata["subtype"]
            pub["type"] = pub_type
            assert pub_type in CR_TYPE_TO_XML_CATEGORY_ID, f"unknown publication type {pub_type}"

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
                elif "PeerJ" in pub_metadata["publisher"]:
                    container_title = pub_metadata["group-title"]
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
                else:
                    if container_title_list[1] not in KNOWN_BOOK_SERIES:
                        log.warning(
                            "publication with doi %s of type %s has container-title with unknown book series: %s",
                            doi,
                            pub_type,
                            container_title_list,
                        )
                    container_title, pub["series-title"] = container_title_list
            pub["container-title"] = container_title

            pub["volume"] = pub_metadata.get("volume")
            pub["pages"] = pub_metadata.get("page")

            pub["authors"] = pub_metadata.get("author")
            # Missing authors (or any other incorrect publication metadata) need
            # to be fixed by the publisher, see
            # https://community.crossref.org/t/where-to-report-incorrect-metadata/3321
            assert pub["authors"], f"publication of type {pub_type} cannot have empty authors"

            # The "issued" field contains the earliest known publication date
            # (see https://github.com/CrossRef/rest-api-doc#sorting )
            issued_date = pub_metadata["issued"]["date-parts"][0]
            # issued_date may not contain the day, i.e. be a list of length 2
            issued_year, issued_month, issued_day = issued_date + [None] * (3 - len(issued_date))
            pub["year"] = issued_year
            pub["month"] = issued_month
            pub["day"] = issued_day

            pub["oa_status"] = unpaywall_get_oa_status(unpaywall_session, doi, config["email"])

            pub["metadata_ok"] = True
        except Exception as e:
            log.error("Skipping publication '%s': %s", doi, e)

    if args.xml:
        write_xml_output(pubs_with_doi, args.xml)


if __name__ == "__main__":
    main()
