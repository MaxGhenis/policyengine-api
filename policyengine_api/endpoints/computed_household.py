from typing import List, Tuple
from policyengine_api.data import database
from policyengine_api.constants import VERSION
from policyengine_core.reforms import Reform
from policyengine_core.parameters import get_parameter
from policyengine_core.periods import instant
from policyengine_core.enums import Enum
from policyengine_api.country import PolicyEngineCountry, COUNTRIES
import json
import dpath
import math

def get_household_under_policy(country_id: str, household_id: int, policy_id: int) -> dict:
    pre_computed_household = database.get_in_table(
        "computed_household",
        country_id=country_id,
        household_id=household_id,
        policy_id=policy_id,
        api_version=VERSION,
    )
    if pre_computed_household is not None:
        return dict(
            status="ok",
            result=json.loads(pre_computed_household["computed_household_json"]),
        )
    household_data = database.get_in_table("household", country_id=country_id, id=household_id)
    if household_data is None:
        return dict(
            status="error",
            message=f"Household {household_id} not found in {country_id}",
        )
    policy = database.get_in_table("policy", country_id=country_id, id=policy_id)
    if policy is None:
        return dict(
            status="error",
            message=f"Policy {policy_id} not found in {country_id}",
        )
    reform = json.loads(policy["policy_json"])
    country = COUNTRIES[country_id]
    household = json.loads(household_data["household_json"])
    computed_household = calculate(country, household, reform)
    database.set_in_table(
        "computed_household",
        dict(
            country_id=country_id,
            household_id=household_id,
            policy_id=policy_id,
            api_version=VERSION,
        ),
        dict(
            computed_household_json=json.dumps(computed_household),
        )
    )
    return dict(
        status="ok",
        message=None,
        result=computed_household,
    )


def calculate(
        country: PolicyEngineCountry, household: dict, reform: Reform
    ) -> dict:
        system = country.tax_benefit_system
        if len(reform) > 0:
            system = system.clone()
            for parameter_name in reform:
                for time_period, value in reform[parameter_name].items():
                    start_instant, end_instant = time_period.split(".")
                    parameter = get_parameter(system.parameters, parameter_name)
                    parameter.update(start=instant(start_instant), stop=instant(end_instant), value=value)

        simulation = country.country_package.Simulation(
            tax_benefit_system=system,
            situation=household,
        )

        household = json.loads(json.dumps(household))

        requested_computations = get_requested_computations(household)

        for (
            entity_plural,
            entity_id,
            variable_name,
            period,
        ) in requested_computations:
            try:
                variable = system.get_variable(variable_name)
                result = simulation.calculate(variable_name, period)
                population = simulation.get_population(entity_plural)
                if "axes" in household:
                    count_entities = len(household[entity_plural])
                    entity_index = 0
                    for _entity_id in household[entity_plural].keys():
                        if _entity_id == entity_id:
                            break
                        entity_index += 1
                    result = (
                        result.astype(float)
                        .reshape((-1, count_entities))
                        .T[entity_index]
                        .tolist()
                    )
                    # If the result contains infinities, throw an error
                    if any([math.isinf(value) for value in result]):
                        raise ValueError("Infinite value")
                    else:
                        household[entity_plural][entity_id][variable_name][
                            period
                        ] = result
                else:
                    entity_index = population.get_index(entity_id)
                    if variable.value_type == Enum:
                        entity_result = result.decode()[entity_index].name
                    elif variable.value_type == float:
                        entity_result = float(str(result[entity_index]))
                        # Convert infinities to JSON infinities
                        if entity_result == float("inf"):
                            entity_result = "Infinity"
                        elif entity_result == float("-inf"):
                            entity_result = "-Infinity"
                    elif variable.value_type == str:
                        entity_result = str(result[entity_index])
                    else:
                        entity_result = result.tolist()[entity_index]

                    household[entity_plural][entity_id][variable_name][
                        period
                    ] = entity_result
            except:
                pass
        return household

def get_requested_computations(household: dict):
    requested_computations = dpath.util.search(
        household,
        "*/*/*/*",
        afilter=lambda t: t is None,
        yielded=True,
    )
    requested_computation_data = []

    for computation in requested_computations:
        path = computation[0]
        entity_plural, entity_id, variable_name, period = path.split("/")
        requested_computation_data.append(
            (entity_plural, entity_id, variable_name, period)
        )

    return requested_computation_data