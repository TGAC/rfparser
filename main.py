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


def RF_get_paginated(s: Session, url: str, params: Optional[Dict] = None) -> List[Dict]:
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

    base_RF_url = "https://api.researchfish.com/restapi"

    # Login
    # Use a session to store the auth cookie
    s = Session()  # use a session
    data = {
        "username": config["rf_username"],
        "password": config["rf_password"],
    }
    r = s.post(f"{base_RF_url}/user/login", data=data)
    r.raise_for_status()
    log.debug("Successfully logged in")

    # Get awards
    # awards = RF_get_paginated(s, f"{base_RF_url}/award")
    # with open("/tmp/awards.txt", "w") as f:
    #     for award in awards:
    #         print(award["fa_name"], file=f)

    # Get publication dois
    params = {
        "section": "publications",
    }
    publications = RF_get_paginated(s, f"{base_RF_url}/outcome", params=params)
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
    base_CR_url = "https://api.crossref.org"
    for doi, pub in pubs_with_doi.items():
        # Get publication metadata from CrossRef
        r = requests.get(f"{base_CR_url}/works/{doi}")
        r.raise_for_status()
        r_dict = r.json()
        assert r_dict["status"] == "ok"
        pub_metadata = r_dict["message"]
        # Join title parts while removing leading, trailing and multiple whitespaces
        title = " ".join(itertools.chain.from_iterable(title_part.split() for title_part in pub_metadata["title"]))
        pub["title"] = title


if __name__ == "__main__":
    main()
