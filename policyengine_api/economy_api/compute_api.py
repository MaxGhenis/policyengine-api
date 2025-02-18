"""
The Compute API is a separate Flask app that runs on a separate port. 
It is designed to be run on a separate server from the main PolicyEngine web app, so that it can
be used for compute-intensive tasks without affecting the main server.
"""

import flask
from flask_cors import CORS
import json
import threading
from policyengine_api.constants import GET, POST, VERSION
from policyengine_api.country import PolicyEngineCountry
from policyengine_api.endpoints.policy import (
    create_policy_reform,
    get_current_law_policy_id,
)
from policyengine_api.data import PolicyEngineDatabase
from policyengine_api.economy_api.compare import compare_economic_outputs
from policyengine_api.economy_api.economy import compute_economy
from multiprocessing import Process

app = flask.Flask(__name__)

CORS(app)

uk = PolicyEngineCountry("policyengine_uk")
us = PolicyEngineCountry("policyengine_us")
countries = dict(uk=uk, us=us)

database = PolicyEngineDatabase(local=True)


@app.route("/", methods=[GET])
def home():
    return f"<h1>PolicyEngine compute API v{VERSION}</h1><p>Use this API to compute the impact of public policy on economies.</p>"


def set_economy_data(
    database: PolicyEngineDatabase, policy_id: int, country_id: str
) -> None:
    """
    Synchronously computes the economy data for a given policy and country.

    Args:
        database (PolicyEngineDatabase): The database.
        policy_id (int): The policy ID.
        country_id (str): The country ID. Currently supported countries are the UK and the US.
    """

    policy = database.get_policy(policy_id, country_id)["policy"]
    if policy is None:
        return flask.Response(f"Policy {policy_id} not found.", status=404)

    country = countries.get(country_id)
    if country is None:
        return flask.Response(f"Country {country_id} not found.", status=404)

    impact = compute_economy(country_id, policy_id)

    database.set_economy(impact, country_id, policy_id, True)


@app.route("/<country_id>/compare/<policy_id>", methods=[GET])
@app.route(
    "/<country_id>/compare/<policy_id>/<baseline_policy_id>", methods=[GET]
)
def score_policy_reform_against_baseline(
    country_id: str, policy_id: str, baseline_policy_id: str = None
) -> dict:
    """
    The /compare endpoint is designed for the PolicyEngine web app. It computes the impact of a policy reform
    on an economy, relative to a baseline policy.

    Args:
        country_id (str): The country ID. Currently supported countries are the UK and the US.
        policy_id (str): The policy ID.
        baseline_policy_id (str, optional): The baseline policy ID. Defaults to None (in which case the baseline is current law).

    Returns:
        dict: The results of the computation.
    """
    print("Computing impact of policy reform...")
    options = dict(flask.request.args)
    region = options.pop("region", None)
    time_period = options.pop("time_period", None)
    if region is None:
        return flask.Response("Region not specified.", status=400)
    if time_period is None:
        return flask.Response("Time period not specified.", status=400)

    country = countries.get(country_id)
    if country is None:
        return flask.Response(f"Country {country_id} not found.", status=404)

    if baseline_policy_id is None:
        baseline_policy_id = get_current_law_policy_id(country_id)

    reform_impact = database.get_in_table(
        "reform_impact",
        country_id=country_id,
        reform_policy_id=policy_id,
        baseline_policy_id=baseline_policy_id,
        region=region,
        time_period=time_period,
        options_json=json.dumps(options),
        api_version=VERSION,
    )

    if reform_impact is None:
        database.set_in_table(
            "reform_impact",
            dict(
                country_id=country_id,
                reform_policy_id=policy_id,
                baseline_policy_id=baseline_policy_id,
                region=region,
                time_period=time_period,
                options_json=json.dumps(options),
                api_version=VERSION,
            ),
            dict(
                reform_impact_json=json.dumps({}),
                status="computing",
            ),
        )

        # Start a new thread to compute the reform impact
        print("Starting new thread to compute reform impact...")
        thread = threading.Thread(
            target=set_reform_impact_data,
            args=(
                database,
                baseline_policy_id,
                policy_id,
                country_id,
                region,
                time_period,
                options,
            ),
        )
        thread.start()

    return {"status": "computing"}


def ensure_economy_computed(
    country_id: str,
    policy_id: str,
    region: str,
    time_period: str,
    options: dict,
):
    economy = database.get_in_table(
        "economy",
        country_id=country_id,
        policy_id=policy_id,
        region=region,
        time_period=time_period,
        options_json=json.dumps(options),
        api_version=VERSION,
    )
    if economy is None:
        try:
            economy_result = compute_economy(
                country_id,
                policy_id,
                region=region,
                time_period=time_period,
                options=options,
            )
            database.set_in_table(
                "economy",
                dict(
                    country_id=country_id,
                    policy_id=policy_id,
                    region=region,
                    time_period=time_period,
                    options_json=json.dumps(options),
                    api_version=VERSION,
                ),
                dict(
                    economy_json=json.dumps(economy_result),
                    status="ok",
                ),
            )
        except Exception as e:
            print(e)
            database.set_in_table(
                "economy",
                dict(
                    country_id=country_id,
                    policy_id=policy_id,
                    region=region,
                    time_period=time_period,
                    options_json=json.dumps(options),
                    api_version=VERSION,
                ),
                dict(
                    economy_json=json.dumps({}),
                    status="error",
                    message=str(e),
                ),
            )


def set_reform_impact_data(
    database: PolicyEngineDatabase,
    baseline_policy_id: int,
    policy_id: int,
    country_id: str,
    region: str,
    time_period: str,
    options: dict,
) -> None:
    """
    Syncronously computes the reform impact for a given policy and country.

    Args:
        database (PolicyEngineDatabase): The database.
        baseline_policy_id (int): The baseline policy ID.
        policy_id (int): The policy ID.
        country_id (str): The country ID. Currently supported countries are the UK and the US.
        region (str): The region to filter on.
        time_period (str): The time period, e.g. 2024.
        options (dict): Any additional options.
    """
    print("Computing reform impact...")
    economy_arguments = region, time_period, options

    for required_policy_id in [baseline_policy_id, policy_id]:
        ensure_economy_computed(
            country_id,
            required_policy_id,
            *economy_arguments,
        )

    economy_kwargs = dict(
        country_id=country_id,
        region=region,
        time_period=time_period,
        options_json=json.dumps(options),
        api_version=VERSION,
    )

    baseline_economy = database.get_in_table(
        "economy",
        policy_id=baseline_policy_id,
        **economy_kwargs,
    )
    reform_economy = database.get_in_table(
        "economy",
        policy_id=policy_id,
        **economy_kwargs,
    )
    if baseline_economy["status"] != "ok" or reform_economy["status"] != "ok":
        database.set_in_table(
            "reform_impact",
            dict(
                **economy_kwargs,
                baseline_policy_id=baseline_policy_id,
                reform_policy_id=policy_id,
            ),
            dict(
                reform_impact_json=json.dumps({}),
                status="error",
                message="Baseline or reform economy computation failed.",
            ),
        )
    else:
        baseline_economy = json.loads(baseline_economy["economy_json"])
        reform_economy = json.loads(reform_economy["economy_json"])
        print("Comparing economies...")
        impact = compare_economic_outputs(baseline_economy, reform_economy)

        database.set_in_table(
            "reform_impact",
            dict(
                **economy_kwargs,
                baseline_policy_id=baseline_policy_id,
                reform_policy_id=policy_id,
            ),
            dict(
                reform_impact_json=json.dumps(impact),
                status="ok",
            ),
        )
