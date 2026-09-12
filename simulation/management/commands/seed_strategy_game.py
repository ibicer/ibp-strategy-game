from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from simulation.models import (
    DemandValue,
    FISCAL_MONTH_NUMBERS,
    ForecastSnapshot,
    Game,
    GameMembership,
    Product,
    ScenarioYear,
)


# ---------------------------------------------------------------------
# Shared decimal constants
# ---------------------------------------------------------------------

ZERO = Decimal("0")
ONE = Decimal("1")

MONEY = Decimal("0.01")


# ---------------------------------------------------------------------
# Demo accounts
# ---------------------------------------------------------------------

INSTRUCTOR_USERNAME = "instructor"
INSTRUCTOR_EMAIL = "instructor@example.com"
INSTRUCTOR_PASSWORD = "ChangeMe123!"

PLAYER_USERNAME = "player"
PLAYER_EMAIL = "player@example.com"
PLAYER_PASSWORD = "Play12345!"


# ---------------------------------------------------------------------
# Game configuration
# ---------------------------------------------------------------------

GAME_NAME = "Summit Bikes — Integrated Business Planning for a New Product Line"
GAME_CODE = "summit-bikes"


# ---------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------

PRODUCTS = {
    "ultimate-mountain": {
        "name": "Ultimate Mountain",
        "standard_price": Decimal("1000.00"),
        "variable_unit_cost": Decimal("200.00"),
    },

    "tough-track": {
        "name": "Tough Track",
        "standard_price": Decimal("500.00"),
        "variable_unit_cost": Decimal("150.00"),
    },
}


# ---------------------------------------------------------------------
# Forecast structure
#
# Fiscal operating months:
#
# September, October, November, December,
# January, February, March, April,
# May, June, July, August
#
# Forecast base months:
#
# August planning forecast
# September forecast
# October forecast
# ...
# July forecast
#
# Each row below is written from the perspective of the month being
# forecast.
#
# Example:
#
# 10: [990, 960]
#
# means:
#
# August forecast for October   = 990
# September forecast for October = 960
#
# This row-oriented format is substantially easier to maintain than
# manually building 156 individual ForecastSnapshot relationships.
# ---------------------------------------------------------------------

FORECAST_MONTHS = list(
    FISCAL_MONTH_NUMBERS
)

FORECAST_BASE_MONTHS = [
    8,
    *FISCAL_MONTH_NUMBERS[:-1],
]


# =====================================================================
# YEAR 1
# =====================================================================


# ---------------------------------------------------------------------
# Year 1 financial configuration
# ---------------------------------------------------------------------

YEAR_1_REVENUE_TARGET = Decimal(
    "19000000.00"
)

YEAR_1_OPERATING_BUDGET = Decimal(
    "10500000.00"
)

YEAR_1_MONTHLY_INTEREST_RATE = Decimal(
    "0.01000"
)

YEAR_1_INITIAL_INVENTORY_UNITS = 100

YEAR_1_LABOR_FIXED_COST_SHARE = Decimal(
    "0.60"
)

YEAR_1_MAINTENANCE_FIXED_COST_SHARE = Decimal(
    "0.40"
)


# ---------------------------------------------------------------------
# Year 1 true demand
# ---------------------------------------------------------------------

YEAR_1_TRUE_DEMAND = {

    "ultimate-mountain": {
        9: 990,
        10: 1050,
        11: 1020,
        12: 1060,
        1: 900,
        2: 970,
        3: 910,
        4: 960,
        5: 1110,
        6: 1060,
        7: 1000,
        8: 980,
    },

    "tough-track": {
        9: 1240,
        10: 1230,
        11: 1170,
        12: 1240,
        1: 1280,
        2: 1240,
        3: 1230,
        4: 1040,
        5: 1160,
        6: 1370,
        7: 1220,
        8: 1220,
    },
}


# ---------------------------------------------------------------------
# Year 1 rolling-horizon forecasts
#
# The first value in every row is the initial August forecast.
# Each subsequent value is the next monthly forecast vintage.
# ---------------------------------------------------------------------

YEAR_1_FORECAST_ROWS = {

    "ultimate-mountain": {

        9: [
            1060,
        ],

        10: [
            990,
            960,
        ],

        11: [
            1150,
            1130,
            1080,
        ],

        12: [
            870,
            930,
            1000,
            1020,
        ],

        1: [
            1060,
            1030,
            980,
            960,
            920,
        ],

        2: [
            750,
            820,
            830,
            830,
            820,
            920,
        ],

        3: [
            1150,
            1130,
            1140,
            1030,
            1020,
            980,
            950,
        ],

        4: [
            700,
            720,
            810,
            790,
            840,
            860,
            840,
            900,
        ],

        5: [
            1450,
            1440,
            1390,
            1330,
            1310,
            1280,
            1240,
            1220,
            1160,
        ],

        6: [
            750,
            740,
            760,
            800,
            820,
            870,
            930,
            940,
            960,
            1000,
        ],

        7: [
            1370,
            1350,
            1220,
            1270,
            1240,
            1160,
            1140,
            1130,
            1110,
            1140,
            1030,
        ],

        8: [
            620,
            660,
            660,
            680,
            680,
            700,
            840,
            810,
            820,
            860,
            900,
            910,
        ],
    },


    "tough-track": {

        9: [
            1320,
        ],

        10: [
            1170,
            1190,
        ],

        11: [
            1330,
            1270,
            1290,
        ],

        12: [
            1110,
            1170,
            1130,
            1210,
        ],

        1: [
            1480,
            1510,
            1370,
            1360,
            1350,
        ],

        2: [
            1010,
            1110,
            1080,
            1160,
            1140,
            1210,
        ],

        3: [
            1460,
            1410,
            1390,
            1390,
            1340,
            1300,
            1270,
        ],

        4: [
            760,
            840,
            860,
            920,
            880,
            860,
            990,
            990,
        ],

        5: [
            1510,
            1450,
            1410,
            1380,
            1370,
            1250,
            1320,
            1290,
            1250,
        ],

        6: [
            990,
            1120,
            1070,
            1070,
            1170,
            1190,
            1230,
            1280,
            1280,
            1270,
        ],

        7: [
            1610,
            1550,
            1510,
            1470,
            1440,
            1500,
            1390,
            1390,
            1350,
            1280,
            1280,
        ],

        8: [
            810,
            900,
            870,
            910,
            910,
            990,
            1060,
            970,
            1070,
            1080,
            1140,
            1170,
        ],
    },
}


# =====================================================================
# YEAR 2 FINANCIAL DESIGN
# =====================================================================


# ---------------------------------------------------------------------
# Market expansion
#
# The board expects baseline market demand / revenue opportunity to
# increase by 85% relative to Year 1.
# ---------------------------------------------------------------------

YEAR_2_MARKET_EXPANSION_MULTIPLIER = Decimal(
    "1.85"
)

YEAR_2_PLUS_1_MULTIPLIER = Decimal(
    "1.01"
)

YEAR_2_PLUS_2_MULTIPLIER = Decimal(
    "1.02"
)


# ---------------------------------------------------------------------
# Calculate the Year 1 variable component of the operating budget.
#
# We use the initial August annual-planning forecast, exactly as the
# annual cost-allocation logic does.
#
# Ultimate Mountain:
#     initial AOP forecast × $200 variable cost
#
# Tough Track:
#     initial AOP forecast × $150 variable cost
# ---------------------------------------------------------------------

def initial_forecast_total(
    forecast_rows,
    product_code,
):
    """
    Return the annual forecast visible during the August
    Annual Operating Plan for one product.

    The first number in each forecast-month row is the
    August forecast vintage.
    """
    return sum(
        monthly_vintages[0]
        for monthly_vintages
        in forecast_rows[
            product_code
        ].values()
    )


YEAR_1_ULTIMATE_INITIAL_FORECAST = (
    initial_forecast_total(
        YEAR_1_FORECAST_ROWS,
        "ultimate-mountain",
    )
)

YEAR_1_TOUGH_TRACK_INITIAL_FORECAST = (
    initial_forecast_total(
        YEAR_1_FORECAST_ROWS,
        "tough-track",
    )
)


YEAR_1_VARIABLE_BUDGET = (
    (
        Decimal(
            YEAR_1_ULTIMATE_INITIAL_FORECAST
        )
        * PRODUCTS[
            "ultimate-mountain"
        ][
            "variable_unit_cost"
        ]
    )
    +
    (
        Decimal(
            YEAR_1_TOUGH_TRACK_INITIAL_FORECAST
        )
        * PRODUCTS[
            "tough-track"
        ][
            "variable_unit_cost"
        ]
    )
).quantize(
    MONEY
)


YEAR_1_FIXED_BUDGET = (
    YEAR_1_OPERATING_BUDGET
    - YEAR_1_VARIABLE_BUDGET
).quantize(
    MONEY
)


if YEAR_1_FIXED_BUDGET < ZERO:
    raise RuntimeError(
        (
            "Year 1 operating budget is lower than "
            "its forecast-based variable-cost component."
        )
    )


# ---------------------------------------------------------------------
# Year 2 baseline revenue target
#
# $19,000,000 × 1.85
# = $35,150,000
# ---------------------------------------------------------------------

YEAR_2_REVENUE_TARGET = (
    YEAR_1_REVENUE_TARGET
    * YEAR_2_MARKET_EXPANSION_MULTIPLIER
).quantize(
    MONEY
)


# ---------------------------------------------------------------------
# Year 2 baseline variable budget
#
# Year 1 variable budget × 1.85
# ---------------------------------------------------------------------

YEAR_2_VARIABLE_BUDGET = (
    YEAR_1_VARIABLE_BUDGET
    * YEAR_2_MARKET_EXPANSION_MULTIPLIER
).quantize(
    MONEY
)


# ---------------------------------------------------------------------
# Year 2 baseline operating budget
#
# Year 1 fixed cost stays unchanged.
# Only the variable component grows with expected activity.
# ---------------------------------------------------------------------

YEAR_2_OPERATING_BUDGET = (
    YEAR_1_FIXED_BUDGET
    + YEAR_2_VARIABLE_BUDGET
).quantize(
    MONEY
)


# ---------------------------------------------------------------------
# Revenue alternatives shown during the Year 2 Annual Operating Plan.
#
# These are calculated here for validation/output only.
# ScenarioYear stores the baseline.
# AnnualPlan stores the player's selected alternative.
# ---------------------------------------------------------------------

YEAR_2_PLUS_1_REVENUE_TARGET = (
    YEAR_2_REVENUE_TARGET
    * YEAR_2_PLUS_1_MULTIPLIER
).quantize(
    MONEY
)

YEAR_2_PLUS_2_REVENUE_TARGET = (
    YEAR_2_REVENUE_TARGET
    * YEAR_2_PLUS_2_MULTIPLIER
).quantize(
    MONEY
)


YEAR_2_PLUS_1_OPERATING_BUDGET = (
    YEAR_1_FIXED_BUDGET
    + (
        YEAR_2_VARIABLE_BUDGET
        * YEAR_2_PLUS_1_MULTIPLIER
    )
).quantize(
    MONEY
)

YEAR_2_PLUS_2_OPERATING_BUDGET = (
    YEAR_1_FIXED_BUDGET
    + (
        YEAR_2_VARIABLE_BUDGET
        * YEAR_2_PLUS_2_MULTIPLIER
    )
).quantize(
    MONEY
)


# ---------------------------------------------------------------------
# Remaining Year 2 assumptions
#
# No difference from Year 1 was specified for these values.
# ---------------------------------------------------------------------

YEAR_2_MONTHLY_INTEREST_RATE = (
    YEAR_1_MONTHLY_INTEREST_RATE
)

YEAR_2_INITIAL_INVENTORY_UNITS = (
    YEAR_1_INITIAL_INVENTORY_UNITS
)

YEAR_2_LABOR_FIXED_COST_SHARE = (
    YEAR_1_LABOR_FIXED_COST_SHARE
)

YEAR_2_MAINTENANCE_FIXED_COST_SHARE = (
    YEAR_1_MAINTENANCE_FIXED_COST_SHARE
)


# =====================================================================
# YEAR 2 TRUE DEMAND
# =====================================================================

YEAR_2_TRUE_DEMAND = {

    "ultimate-mountain": {
        9: 1970,
        10: 1800,
        11: 1900,
        12: 1780,
        1: 1910,
        2: 1870,
        3: 1910,
        4: 1780,
        5: 1630,
        6: 1710,
        7: 910,
        8: 1140,
    },

    "tough-track": {
        9: 2450,
        10: 2530,
        11: 2610,
        12: 2410,
        1: 2360,
        2: 2390,
        3: 2260,
        4: 2570,
        5: 2310,
        6: 2380,
        7: 3150,
        8: 3090,
    },
}


# ---------------------------------------------------------------------
# Expected totals for the updated Year 2 external-shock scenario.
#
# Ultimate Mountain:
#     July   = 910
#     August = 1,140
#     Annual total = 20,310
#
# Tough Track:
#     July   = 3,150
#     August = 3,090
#     Annual total = 30,510
#
# Combined Year 2 true demand = 50,820 units.
# ---------------------------------------------------------------------

YEAR_2_EXPECTED_TRUE_DEMAND_TOTALS = {
    "ultimate-mountain": 20310,
    "tough-track": 30510,
}


# =====================================================================
# YEAR 2 ROLLING-HORIZON FORECASTS
# =====================================================================


# ---------------------------------------------------------------------
# Ultimate Mountain
#
# The late-year forecast vintages are intentionally kept high while
# realized demand falls sharply in July and August. This creates the
# external-shock / excess-inventory challenge used in Year 2.
#
# Latest one-month-ahead forecasts:
#     June   = 2,010
#     July   = 2,093
#     August = 2,107
# ---------------------------------------------------------------------

YEAR_2_ULTIMATE_FORECAST_ROWS = {

    9: [
        2089,
    ],

    10: [
        2180,
        1753,
    ],

    11: [
        1636,
        2181,
        1797,
    ],

    12: [
        1525,
        1361,
        2087,
        1845,
    ],

    1: [
        2086,
        1793,
        1421,
        2259,
        1678,
    ],

    2: [
        1972,
        1864,
        1539,
        2171,
        2314,
        2195,
    ],

    3: [
        1682,
        1970,
        2521,
        1912,
        1552,
        1579,
        2282,
    ],

    4: [
        2364,
        1893,
        1977,
        1693,
        1757,
        1775,
        1702,
        1993,
    ],

    5: [
        1150,
        1354,
        1958,
        1609,
        1595,
        1466,
        1171,
        1321,
        1575,
    ],

    6: [
        1775,
        1210,
        1420,
        1950,
        1890,
        1811,
        1699,
        1436,
        1701,
        2010,
    ],

    7: [
        1247,
        1791,
        2086,
        2413,
        1476,
        1645,
        1252,
        2157,
        1524,
        1875,
        2093,
    ],

    8: [
        2288,
        2351,
        1432,
        1535,
        1350,
        1455,
        1671,
        2321,
        2078,
        2500,
        1721,
        2107,
    ],
}


# ---------------------------------------------------------------------
# Tough Track
#
# The late-year market shock moves in the opposite direction:
# realized demand rises to 3,150 in July and 3,090 in August.
# The final August forecast remains 2,343, preserving the intended
# positive-demand shock.
#
# The source heading for the final vintage was labelled
# "August Forecasts". It is interpreted here as JULY Forecasts,
# because:
#
#     July is the final forecast base month,
#     and it produces the forecast for August.
#
# This preserves the same 12-vintage fiscal structure used in Year 1.
# ---------------------------------------------------------------------

YEAR_2_TOUGH_TRACK_FORECAST_ROWS = {

    9: [
        2292,
    ],

    10: [
        1916,
        2290,
    ],

    11: [
        2292,
        2329,
        2915,
    ],

    12: [
        2644,
        1974,
        2429,
        2371,
    ],

    1: [
        2677,
        2551,
        1786,
        2184,
        2002,
    ],

    2: [
        2079,
        2908,
        2649,
        3036,
        2805,
        2614,
    ],

    3: [
        2943,
        2221,
        2965,
        2695,
        2562,
        1867,
        2693,
    ],

    4: [
        1874,
        2423,
        3399,
        2135,
        1804,
        2699,
        2008,
        2311,
    ],

    5: [
        2078,
        2454,
        3011,
        1866,
        2078,
        1590,
        2912,
        2737,
        2570,
    ],

    6: [
        1903,
        1590,
        1873,
        2969,
        2764,
        3013,
        1762,
        2686,
        2858,
        2853,
    ],

    7: [
        1869,
        1739,
        2608,
        2956,
        2757,
        2268,
        3082,
        2768,
        2318,
        1891,
        2816,
    ],

    8: [
        2543,
        1657,
        2140,
        2179,
        2202,
        2759,
        1912,
        2735,
        1966,
        2384,
        2722,
        2343,
    ],
}


YEAR_2_FORECAST_ROWS = {
    "ultimate-mountain": (
        YEAR_2_ULTIMATE_FORECAST_ROWS
    ),

    "tough-track": (
        YEAR_2_TOUGH_TRACK_FORECAST_ROWS
    ),
}


# =====================================================================
# SCENARIO CONFIGURATION
# =====================================================================

SCENARIO_YEARS = {

    1: {
        "revenue_target": (
            YEAR_1_REVENUE_TARGET
        ),
        "operating_budget": (
            YEAR_1_OPERATING_BUDGET
        ),
        "monthly_interest_rate": (
            YEAR_1_MONTHLY_INTEREST_RATE
        ),
        "initial_inventory_units": (
            YEAR_1_INITIAL_INVENTORY_UNITS
        ),
        "labor_fixed_cost_share": (
            YEAR_1_LABOR_FIXED_COST_SHARE
        ),
        "maintenance_fixed_cost_share": (
            YEAR_1_MAINTENANCE_FIXED_COST_SHARE
        ),
        "true_demand": (
            YEAR_1_TRUE_DEMAND
        ),
        "forecast_rows": (
            YEAR_1_FORECAST_ROWS
        ),
    },

    2: {
        "revenue_target": (
            YEAR_2_REVENUE_TARGET
        ),
        "operating_budget": (
            YEAR_2_OPERATING_BUDGET
        ),
        "monthly_interest_rate": (
            YEAR_2_MONTHLY_INTEREST_RATE
        ),
        "initial_inventory_units": (
            YEAR_2_INITIAL_INVENTORY_UNITS
        ),
        "labor_fixed_cost_share": (
            YEAR_2_LABOR_FIXED_COST_SHARE
        ),
        "maintenance_fixed_cost_share": (
            YEAR_2_MAINTENANCE_FIXED_COST_SHARE
        ),
        "true_demand": (
            YEAR_2_TRUE_DEMAND
        ),
        "forecast_rows": (
            YEAR_2_FORECAST_ROWS
        ),
    },
}


# =====================================================================
# SEED HELPERS
# =====================================================================


def create_or_update_user(
    *,
    username,
    email,
    password,
    is_staff=False,
    is_superuser=False,
):
    """
    Create or update a development user.
    """
    user_model = get_user_model()

    user, created = (
        user_model.objects
        .get_or_create(
            username=username,
            defaults={
                "email": email,
                "is_staff": is_staff,
                "is_superuser": is_superuser,
            },
        )
    )

    user.email = email
    user.is_staff = is_staff
    user.is_superuser = is_superuser

    user.set_password(
        password
    )

    user.save(
        update_fields=[
            "email",
            "is_staff",
            "is_superuser",
            "password",
        ]
    )

    return user, created


def create_or_update_products():
    """
    Create or update the two mountain-bike products.
    """
    product_objects = {}

    for (
        product_code,
        product_data,
    ) in PRODUCTS.items():

        product, _ = (
            Product.objects
            .update_or_create(
                code=product_code,
                defaults={
                    "name": (
                        product_data[
                            "name"
                        ]
                    ),
                    "standard_price": (
                        product_data[
                            "standard_price"
                        ]
                    ),
                    "variable_unit_cost": (
                        product_data[
                            "variable_unit_cost"
                        ]
                    ),
                },
            )
        )

        product.full_clean()
        product.save()

        product_objects[
            product_code
        ] = product

    return product_objects


def validate_forecast_structure(
    *,
    forecast_rows,
):
    """
    Validate the triangular rolling-horizon forecast structure.

    September:
        1 vintage

    October:
        2 vintages

    ...

    August:
        12 vintages
    """

    for product_code in PRODUCTS:

        if product_code not in forecast_rows:
            raise RuntimeError(
                (
                    "Missing forecast data for "
                    f"{product_code}."
                )
            )

        product_forecasts = (
            forecast_rows[
                product_code
            ]
        )

        for (
            position,
            forecast_month,
        ) in enumerate(
            FORECAST_MONTHS,
            start=1,
        ):
            if (
                forecast_month
                not in product_forecasts
            ):
                raise RuntimeError(
                    (
                        f"Missing forecast month "
                        f"{forecast_month} for "
                        f"{product_code}."
                    )
                )

            values = (
                product_forecasts[
                    forecast_month
                ]
            )

            if len(values) != position:
                raise RuntimeError(
                    (
                        f"Expected {position} forecast "
                        f"vintages for product "
                        f"{product_code}, month "
                        f"{forecast_month}; found "
                        f"{len(values)}."
                    )
                )


def create_true_demand(
    *,
    scenario_year,
    products,
    true_demand,
):
    """
    Replace all true-demand rows for the selected scenario year.
    """

    DemandValue.objects.filter(
        scenario_year=scenario_year,
    ).delete()

    rows = []

    for (
        product_code,
        monthly_values,
    ) in true_demand.items():

        product = products[
            product_code
        ]

        for (
            month,
            ideal_demand,
        ) in monthly_values.items():

            rows.append(
                DemandValue(
                    scenario_year=(
                        scenario_year
                    ),
                    product=product,
                    month=month,
                    ideal_demand=(
                        ideal_demand
                    ),
                )
            )

    DemandValue.objects.bulk_create(
        rows
    )


def create_rolling_forecasts(
    *,
    scenario_year,
    products,
    forecast_rows,
):
    """
    Replace all rolling-horizon forecasts for the selected
    scenario year.

    Row-oriented source data are converted into:

        base_month
        forecast_month
        forecast_units
    """

    validate_forecast_structure(
        forecast_rows=forecast_rows,
    )

    ForecastSnapshot.objects.filter(
        scenario_year=scenario_year,
    ).delete()

    rows = []

    for (
        product_code,
        product_forecasts,
    ) in forecast_rows.items():

        product = products[
            product_code
        ]

        for forecast_month in (
            FORECAST_MONTHS
        ):
            vintage_values = (
                product_forecasts[
                    forecast_month
                ]
            )

            for (
                vintage_index,
                forecast_units,
            ) in enumerate(
                vintage_values
            ):
                base_month = (
                    FORECAST_BASE_MONTHS[
                        vintage_index
                    ]
                )

                rows.append(
                    ForecastSnapshot(
                        scenario_year=(
                            scenario_year
                        ),
                        product=product,
                        base_month=(
                            base_month
                        ),
                        forecast_month=(
                            forecast_month
                        ),
                        forecast_units=(
                            forecast_units
                        ),
                    )
                )

    ForecastSnapshot.objects.bulk_create(
        rows
    )


def validate_true_demand_totals(
    *,
    true_demand,
    expected_totals,
):
    """
    Validate supplied annual true-demand totals.
    """

    for (
        product_code,
        expected_total,
    ) in expected_totals.items():

        actual_total = sum(
            true_demand[
                product_code
            ].values()
        )

        if actual_total != expected_total:
            raise RuntimeError(
                (
                    f"True-demand total mismatch for "
                    f"{product_code}. Expected "
                    f"{expected_total:,}; found "
                    f"{actual_total:,}."
                )
            )


def validate_scenario_rows(
    *,
    scenario_year,
):
    """
    Validate the expected number of scenario rows.
    """

    expected_demand_rows = (
        len(PRODUCTS)
        * len(FISCAL_MONTH_NUMBERS)
    )

    vintages_per_product = sum(
        range(
            1,
            len(FISCAL_MONTH_NUMBERS)
            + 1,
        )
    )

    expected_forecast_rows = (
        len(PRODUCTS)
        * vintages_per_product
    )

    demand_count = (
        DemandValue.objects
        .filter(
            scenario_year=scenario_year,
        )
        .count()
    )

    forecast_count = (
        ForecastSnapshot.objects
        .filter(
            scenario_year=scenario_year,
        )
        .count()
    )

    if (
        demand_count
        != expected_demand_rows
    ):
        raise RuntimeError(
            (
                f"Year {scenario_year.year_number}: "
                f"expected "
                f"{expected_demand_rows} demand rows, "
                f"found {demand_count}."
            )
        )

    if (
        forecast_count
        != expected_forecast_rows
    ):
        raise RuntimeError(
            (
                f"Year {scenario_year.year_number}: "
                f"expected "
                f"{expected_forecast_rows} forecast "
                f"rows, found {forecast_count}."
            )
        )

    return (
        demand_count,
        forecast_count,
    )


def create_or_update_scenario_year(
    *,
    game,
    year_number,
    configuration,
    products,
):
    """
    Create or update one scenario year and all associated
    demand / forecast data.
    """

    scenario_year, created = (
        ScenarioYear.objects
        .update_or_create(
            game=game,
            year_number=year_number,
            defaults={
                "revenue_target": (
                    configuration[
                        "revenue_target"
                    ]
                ),

                "operating_budget": (
                    configuration[
                        "operating_budget"
                    ]
                ),

                "monthly_interest_rate": (
                    configuration[
                        "monthly_interest_rate"
                    ]
                ),

                "initial_inventory_units": (
                    configuration[
                        "initial_inventory_units"
                    ]
                ),

                "labor_fixed_cost_share": (
                    configuration[
                        "labor_fixed_cost_share"
                    ]
                ),

                "maintenance_fixed_cost_share": (
                    configuration[
                        "maintenance_fixed_cost_share"
                    ]
                ),
            },
        )
    )

    scenario_year.full_clean()
    scenario_year.save()

    create_true_demand(
        scenario_year=scenario_year,
        products=products,
        true_demand=(
            configuration[
                "true_demand"
            ]
        ),
    )

    create_rolling_forecasts(
        scenario_year=scenario_year,
        products=products,
        forecast_rows=(
            configuration[
                "forecast_rows"
            ]
        ),
    )

    demand_count, forecast_count = (
        validate_scenario_rows(
            scenario_year=scenario_year,
        )
    )

    return {
        "scenario_year": (
            scenario_year
        ),
        "created": (
            created
        ),
        "demand_count": (
            demand_count
        ),
        "forecast_count": (
            forecast_count
        ),
    }


# =====================================================================
# MANAGEMENT COMMAND
# =====================================================================


class Command(BaseCommand):
    help = (
        "Create or refresh the Summit Bikes "
        "Integrated Business Planning simulation."
    )

    @transaction.atomic
    def handle(
        self,
        *args,
        **options,
    ):
        # --------------------------------------------------------------
        # Users
        # --------------------------------------------------------------

        (
            instructor,
            instructor_created,
        ) = create_or_update_user(
            username=(
                INSTRUCTOR_USERNAME
            ),
            email=(
                INSTRUCTOR_EMAIL
            ),
            password=(
                INSTRUCTOR_PASSWORD
            ),
            is_staff=True,
            is_superuser=True,
        )

        (
            player,
            player_created,
        ) = create_or_update_user(
            username=(
                PLAYER_USERNAME
            ),
            email=(
                PLAYER_EMAIL
            ),
            password=(
                PLAYER_PASSWORD
            ),
        )

        # --------------------------------------------------------------
        # Game
        # --------------------------------------------------------------

        game, game_created = (
            Game.objects
            .update_or_create(
                code=GAME_CODE,
                defaults={
                    "name": GAME_NAME,
                    "description": (
                        "An executive Integrated Business Planning simulation "
                        "for a new product line, combining Annual Operating "
                        "Planning with monthly Sales & Operations Planning "
                        "across revenue, pricing, replenishment, inventory, "
                        "maintenance, and operating performance."
                    ),
                    "created_by": instructor,
                    "status": (
                        Game.Status.OPEN
                    ),

                    # The simulation still begins
                    # with Year 1.
                    "current_year": 1,
                    "current_month": 8,
                },
            )
        )

        # --------------------------------------------------------------
        # Memberships
        # --------------------------------------------------------------

        GameMembership.objects.update_or_create(
            game=game,
            user=instructor,
            defaults={
                "role": (
                    GameMembership.Role.ADMIN
                ),
                "team_name": "",
            },
        )

        GameMembership.objects.update_or_create(
            game=game,
            user=player,
            defaults={
                "role": (
                    GameMembership.Role.PLAYER
                ),
                "team_name": (
                    "Demo Executive Team"
                ),
            },
        )

        # --------------------------------------------------------------
        # Products
        # --------------------------------------------------------------

        products = (
            create_or_update_products()
        )

        # --------------------------------------------------------------
        # Validate supplied Year 2 demand totals
        # --------------------------------------------------------------

        validate_true_demand_totals(
            true_demand=(
                YEAR_2_TRUE_DEMAND
            ),
            expected_totals=(
                YEAR_2_EXPECTED_TRUE_DEMAND_TOTALS
            ),
        )

        # --------------------------------------------------------------
        # Scenario Years 1 and 2
        # --------------------------------------------------------------

        scenario_results = {}

        for (
            year_number,
            configuration,
        ) in SCENARIO_YEARS.items():

            scenario_results[
                year_number
            ] = (
                create_or_update_scenario_year(
                    game=game,
                    year_number=(
                        year_number
                    ),
                    configuration=(
                        configuration
                    ),
                    products=products,
                )
            )

        # --------------------------------------------------------------
        # Success output
        # --------------------------------------------------------------

        self.stdout.write("")

        self.stdout.write(
            self.style.SUCCESS(
                (
                    "Summit Bikes Integrated Business Planning "
                    "simulation seeded successfully."
                )
            )
        )

        self.stdout.write("")

        self.stdout.write(
            f"Game: {game.name} ({game.code})"
        )

        self.stdout.write(
            (
                "Game created: "
                f"{'Yes' if game_created else 'No — updated'}"
            )
        )

        # --------------------------------------------------------------
        # Year results
        # --------------------------------------------------------------

        for year_number in sorted(
            scenario_results
        ):
            result = (
                scenario_results[
                    year_number
                ]
            )

            self.stdout.write("")

            self.stdout.write(
                (
                    f"Year {year_number} created: "
                    f"{'Yes' if result['created'] else 'No — updated'}"
                )
            )

            self.stdout.write(
                (
                    "True-demand rows: "
                    f"{result['demand_count']}"
                )
            )

            self.stdout.write(
                (
                    "Forecast rows: "
                    f"{result['forecast_count']}"
                )
            )

        # --------------------------------------------------------------
        # Year 1 financial decomposition
        # --------------------------------------------------------------

        self.stdout.write("")

        self.stdout.write(
            self.style.SUCCESS(
                "Year 1 budget decomposition"
            )
        )

        self.stdout.write(
            (
                "Variable-cost component: "
                f"${YEAR_1_VARIABLE_BUDGET:,.2f}"
            )
        )

        self.stdout.write(
            (
                "Fixed-cost component: "
                f"${YEAR_1_FIXED_BUDGET:,.2f}"
            )
        )

        # --------------------------------------------------------------
        # Year 2 strategic alternatives
        # --------------------------------------------------------------

        self.stdout.write("")

        self.stdout.write(
            self.style.SUCCESS(
                "Year 2 AOP revenue alternatives"
            )
        )

        self.stdout.write(
            (
                "Baseline: "
                f"Revenue ${YEAR_2_REVENUE_TARGET:,.2f} | "
                f"Budget ${YEAR_2_OPERATING_BUDGET:,.2f}"
            )
        )

        self.stdout.write(
            (
                "+1%: "
                f"Revenue ${YEAR_2_PLUS_1_REVENUE_TARGET:,.2f} | "
                f"Budget ${YEAR_2_PLUS_1_OPERATING_BUDGET:,.2f}"
            )
        )

        self.stdout.write(
            (
                "+2%: "
                f"Revenue ${YEAR_2_PLUS_2_REVENUE_TARGET:,.2f} | "
                f"Budget ${YEAR_2_PLUS_2_OPERATING_BUDGET:,.2f}"
            )
        )

        # --------------------------------------------------------------
        # Year 2 demand validation
        # --------------------------------------------------------------

        self.stdout.write("")

        self.stdout.write(
            (
                "Year 2 Ultimate Mountain true demand: "
                f"{sum(YEAR_2_TRUE_DEMAND['ultimate-mountain'].values()):,}"
            )
        )

        self.stdout.write(
            (
                "Year 2 Tough Track true demand: "
                f"{sum(YEAR_2_TRUE_DEMAND['tough-track'].values()):,}"
            )
        )

        self.stdout.write(
            (
                "Year 2 total true demand: "
                f"{sum(YEAR_2_TRUE_DEMAND['ultimate-mountain'].values()) + sum(YEAR_2_TRUE_DEMAND['tough-track'].values()):,}"
            )
        )

        # --------------------------------------------------------------
        # Shared assumptions
        # --------------------------------------------------------------

        self.stdout.write("")

        self.stdout.write(
            (
                "Opening inventory per product: "
                f"{YEAR_1_INITIAL_INVENTORY_UNITS} units"
            )
        )

        self.stdout.write(
            (
                "Fixed-cost allocation: "
                f"{YEAR_1_LABOR_FIXED_COST_SHARE:.0%} labor / "
                f"{YEAR_1_MAINTENANCE_FIXED_COST_SHARE:.0%} maintenance"
            )
        )

        # --------------------------------------------------------------
        # Demo credentials
        # --------------------------------------------------------------

        self.stdout.write("")

        self.stdout.write(
            self.style.WARNING(
                "Development demo credentials:"
            )
        )

        self.stdout.write(
            (
                f"Instructor: "
                f"{INSTRUCTOR_USERNAME} / "
                f"{INSTRUCTOR_PASSWORD}"
            )
        )

        self.stdout.write(
            (
                f"Player: "
                f"{PLAYER_USERNAME} / "
                f"{PLAYER_PASSWORD}"
            )
        )

        if (
            instructor_created
            or player_created
        ):
            self.stdout.write(
                self.style.WARNING(
                    (
                        "Change or remove demo passwords "
                        "before deployment."
                    )
                )
            )