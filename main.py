#!/usr/bin/env python3

import argparse
import itertools
import logging
import os
from typing import (
    Any,
    Dict,
    List,
    Optional,
)

import requests
import yaml
from requests import Session

from util import strip_tags

BASE_CR_URL = "https://api.crossref.org"
BASE_RF_URL = "https://api.researchfish.com/restapi"


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


def RF_get_paginated(s: Session, url: str, params: Optional[Dict] = None) -> List[Dict]:
    """
    Get paginated items from ResearchFish API.
    """
    next = 0
    if params is None:
        params = {}
    else:
        params = params.copy()
    ret: List[Dict] = []
    while next is not None:
        params["start"] = next
        r = s.get(url, params=params)
        r.raise_for_status()
        r_dict = r.json()
        ret.extend(r_dict["results"])
        next = r_dict.get("next")
    return ret


def CR_get_pub_metadata(doi: str) -> Dict[str, Any]:
    """
    Get metadata for a publication from CrossRef API.
    """
    r = requests.get(f"{BASE_CR_URL}/works/{doi}")
    r.raise_for_status()
    r_dict = r.json()
    assert r_dict["status"] == "ok"
    return r_dict["message"]


def main() -> None:
    # Parse command-line options
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--config", default="config.yaml", help="configuration file path")
    parser.add_argument("-v", "--verbose", action="store_true", help="set log level to DEBUG")
    args = parser.parse_args()

    if args.verbose:
        log_level = logging.DEBUG
    else:
        log_level = logging.INFO
    logging.basicConfig(level=log_level)

    log = logging.getLogger(__name__)

    # Read config file
    try:
        with open(args.config) as f:
            config = yaml.safe_load(f)
        log.info(f"Read configuration file {args.config}")
    except Exception:
        config = {}
        log.warning(f"Could not read configuration file {args.config}")

    if "RF_USERNAME" in os.environ:
        config["rf_username"] = os.environ["RF_USERNAME"]
    if "RF_PASSWORD" in os.environ:
        config["rf_password"] = os.environ["RF_PASSWORD"]

    # Login to ResearchFish API
    s = RF_login(config["rf_username"], config["rf_password"])
    log.debug("Successfully logged in to ResearchFish API")

    # Get awards
    # awards = RF_get_paginated(s, f"{BASE_RF_URL}/award")
    # with open("/tmp/awards.txt", "w") as f:
    #     for award in awards:
    #         print(award["fa_name"], file=f)

    # Get publication dois
    params = {
        "section": "publications",
    }
    publications = RF_get_paginated(s, f"{BASE_RF_URL}/outcome", params=params)
    log.info(f"Total publications: {len(publications)}")
    pubs_without_doi = [p for p in publications if p["r1_2_19"] is None]
    log.info(f"Publications without a DOI: {len(pubs_without_doi)}")

    # Create dictionary of publications indexed by DOI
    pubs_with_doi: Dict[str, Dict[str, Any]] = {}
    for p in publications:
        doi = p["r1_2_19"]
        if doi is not None:
            pubs_with_doi.setdefault(doi, {})
            pubs_with_doi[doi].setdefault("rf_entries", [])
            pubs_with_doi[doi]["rf_entries"].append(p)
    log.info(f"Unique publication DOIs: {len(pubs_with_doi)}")

    # Process publications with a DOI
    for doi, pub in pubs_with_doi.items():
        pub["metadata_ok"] = False
        try:
            pub_metadata = CR_get_pub_metadata(doi)
            # Join title parts while removing leading, trailing and multiple whitespaces
            title = " ".join(itertools.chain.from_iterable(title_part.split() for title_part in pub_metadata["title"]))
            title = strip_tags(title)
            pub["title"] = title
            pub["metadata_ok"] = True
        except Exception as e:
            log.error("Skipping publication '%s': %s", doi, e)


if __name__ == "__main__":
    main()
