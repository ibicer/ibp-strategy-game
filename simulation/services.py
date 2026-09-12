from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .models import (
    AnnualPlan,
    DemandValue,
    FISCAL_MONTH_NUMBERS,
    FISCAL_MONTH_POSITION,
    ForecastSnapshot,
    Game,
    MonthlyDecision,
    PlayerYearScore,
    Product,
    ProductDecision,
    ScenarioYear,
    YearAttempt,
)


# ---------------------------------------------------------------------
# Shared decimal constants
# ---------------------------------------------------------------------

MONEY = Decimal("0.01")
WHOLE_UNIT = Decimal("1")

ZERO = Decimal("0")
ONE = Decimal("1")
TWO = Decimal("2")

YEAR_END_REVENUE_TOLERANCE = Decimal("0.002")       # 0.2%
YEAR_END_BUDGET_TOLERANCE = Decimal("0.05")           # 5%
YEAR_END_SALES_CREDIT_TOLERANCE = Decimal("0.02")     # 2%

# Monthly S&OP hard guardrail:
# cumulative variable production cost + proposed variable production cost
# may not exceed 180% of the initial annual variable-cost budget.
MONTHLY_ORDER_BUDGET_OVERAGE_LIMIT = Decimal("0.80")
MONTHLY_ORDER_SPEND_CEILING_MULTIPLIER = (
    ONE
    + MONTHLY_ORDER_BUDGET_OVERAGE_LIMIT
)

# ---------------------------------------------------------------------
# Master simulation scenario cloning and integrity validation
# ---------------------------------------------------------------------

MASTER_SIMULATION_CODE = "summit-bikes"


def validate_game_scenario(
    *,
    game,
    source_game=None,
):
    """
    Validate that a game contains a complete playable scenario.

    When source_game is supplied, the target is compared with the master
    scenario exactly for:
        - scenario-year numbers,
        - DemandValue keys and counts,
        - ForecastSnapshot keys and counts.

    The function also verifies that every copied value matches the source.
    A ValidationError is raised when any inconsistency is found.
    """
    if game is None:
        raise ValidationError(
            "A game is required for scenario validation."
        )

    target_years = list(
        ScenarioYear.objects
        .filter(game=game)
        .order_by("year_number")
    )

    if not target_years:
        raise ValidationError(
            "The simulation does not contain any configured scenario years."
        )

    target_year_numbers = [
        year.year_number
        for year in target_years
    ]

    if len(target_year_numbers) != len(set(target_year_numbers)):
        raise ValidationError(
            "The simulation contains duplicate scenario-year numbers."
        )

    if source_game is None:
        for scenario_year in target_years:
            if not scenario_year.demand_values.exists():
                raise ValidationError(
                    (
                        f"Year {scenario_year.year_number} has no "
                        "configured demand values."
                    )
                )

            if not scenario_year.forecast_snapshots.exists():
                raise ValidationError(
                    (
                        f"Year {scenario_year.year_number} has no "
                        "configured forecast snapshots."
                    )
                )

        return {
            "ready": True,
            "scenario_years": len(target_years),
            "demand_values": DemandValue.objects.filter(
                scenario_year__game=game
            ).count(),
            "forecast_snapshots": ForecastSnapshot.objects.filter(
                scenario_year__game=game
            ).count(),
        }

    source_years = list(
        ScenarioYear.objects
        .filter(game=source_game)
        .order_by("year_number")
    )

    if not source_years:
        raise ValidationError(
            (
                f"The master simulation '{source_game.code}' has no "
                "configured scenario years."
            )
        )

    source_year_numbers = [
        year.year_number
        for year in source_years
    ]

    if target_year_numbers != source_year_numbers:
        raise ValidationError(
            (
                "Scenario-year validation failed. "
                f"Expected {source_year_numbers}; "
                f"found {target_year_numbers}."
            )
        )

    target_year_by_number = {
        year.year_number: year
        for year in target_years
    }

    for source_year in source_years:
        target_year = target_year_by_number[
            source_year.year_number
        ]

        year_fields = (
            "revenue_target",
            "operating_budget",
            "monthly_interest_rate",
            "initial_inventory_units",
            "labor_fixed_cost_share",
            "maintenance_fixed_cost_share",
        )

        for field_name in year_fields:
            if (
                getattr(target_year, field_name)
                != getattr(source_year, field_name)
            ):
                raise ValidationError(
                    (
                        f"Year {source_year.year_number} scenario "
                        f"validation failed for '{field_name}'."
                    )
                )

        source_demands = {
            (row.product_id, row.month): row.ideal_demand
            for row in DemandValue.objects.filter(
                scenario_year=source_year
            )
        }

        target_demands = {
            (row.product_id, row.month): row.ideal_demand
            for row in DemandValue.objects.filter(
                scenario_year=target_year
            )
        }

        if target_demands != source_demands:
            raise ValidationError(
                (
                    f"Year {source_year.year_number} demand-data "
                    "validation failed."
                )
            )

        source_forecasts = {
            (
                row.product_id,
                row.base_month,
                row.forecast_month,
            ): row.forecast_units
            for row in ForecastSnapshot.objects.filter(
                scenario_year=source_year
            )
        }

        target_forecasts = {
            (
                row.product_id,
                row.base_month,
                row.forecast_month,
            ): row.forecast_units
            for row in ForecastSnapshot.objects.filter(
                scenario_year=target_year
            )
        }

        if target_forecasts != source_forecasts:
            raise ValidationError(
                (
                    f"Year {source_year.year_number} forecast-data "
                    "validation failed."
                )
            )

    return {
        "ready": True,
        "scenario_years": len(target_years),
        "demand_values": DemandValue.objects.filter(
            scenario_year__game=game
        ).count(),
        "forecast_snapshots": ForecastSnapshot.objects.filter(
            scenario_year__game=game
        ).count(),
    }


@transaction.atomic
def clone_game_scenario(
    *,
    target_game,
    source_game=None,
    source_code=MASTER_SIMULATION_CODE,
):
    """
    Initialize an instructor-created game from the reusable master scenario.

    This service is intentionally backend-only. Instructors do not need to
    know about or manage scenario cloning.

    Copied:
        - ScenarioYear
        - DemandValue
        - ForecastSnapshot

    Shared rather than copied:
        - Product

    Never copied:
        - memberships,
        - attempts,
        - annual plans,
        - monthly decisions,
        - product decisions,
        - player scores.

    The complete clone is validated against the master before the surrounding
    transaction is allowed to commit.
    """
    if target_game is None:
        raise ValidationError(
            "A target game is required when initializing the simulation."
        )

    if source_game is None:
        source_game = (
            Game.objects
            .filter(code=source_code)
            .first()
        )

    if source_game is None:
        raise ValidationError(
            (
                "The simulation template is unavailable. "
                "Please contact the system administrator."
            )
        )

    if source_game.pk == target_game.pk:
        raise ValidationError(
            "The simulation template cannot initialize itself."
        )

    if ScenarioYear.objects.filter(game=target_game).exists():
        raise ValidationError(
            (
                f"{target_game.name} is already initialized. "
                "Automatic setup was cancelled to prevent duplication."
            )
        )

    # Validate the master itself before copying anything.
    source_status = validate_game_scenario(
        game=source_game,
    )

    source_years = list(
        ScenarioYear.objects
        .filter(game=source_game)
        .order_by("year_number")
    )

    year_map = {}

    for source_year in source_years:
        cloned_year = ScenarioYear(
            game=target_game,
            year_number=source_year.year_number,
            revenue_target=source_year.revenue_target,
            operating_budget=source_year.operating_budget,
            monthly_interest_rate=source_year.monthly_interest_rate,
            initial_inventory_units=source_year.initial_inventory_units,
            labor_fixed_cost_share=source_year.labor_fixed_cost_share,
            maintenance_fixed_cost_share=(
                source_year.maintenance_fixed_cost_share
            ),
        )

        cloned_year.full_clean()
        cloned_year.save()

        year_map[source_year.pk] = cloned_year

    source_demands = list(
        DemandValue.objects
        .filter(scenario_year__game=source_game)
        .select_related("scenario_year", "product")
        .order_by(
            "scenario_year__year_number",
            "product_id",
            "month",
        )
    )

    demand_rows = [
        DemandValue(
            scenario_year=year_map[row.scenario_year_id],
            product=row.product,
            month=row.month,
            ideal_demand=row.ideal_demand,
        )
        for row in source_demands
    ]

    if demand_rows:
        DemandValue.objects.bulk_create(demand_rows)

    source_forecasts = list(
        ForecastSnapshot.objects
        .filter(scenario_year__game=source_game)
        .select_related("scenario_year", "product")
        .order_by(
            "scenario_year__year_number",
            "product_id",
            "base_month",
            "forecast_month",
        )
    )

    forecast_rows = [
        ForecastSnapshot(
            scenario_year=year_map[row.scenario_year_id],
            product=row.product,
            base_month=row.base_month,
            forecast_month=row.forecast_month,
            forecast_units=row.forecast_units,
        )
        for row in source_forecasts
    ]

    if forecast_rows:
        ForecastSnapshot.objects.bulk_create(forecast_rows)

    # Exact post-clone comparison with the master. Any mismatch raises
    # ValidationError and rolls the entire atomic operation back.
    target_status = validate_game_scenario(
        game=target_game,
        source_game=source_game,
    )

    if (
        target_status["scenario_years"]
        != source_status["scenario_years"]
        or target_status["demand_values"]
        != source_status["demand_values"]
        or target_status["forecast_snapshots"]
        != source_status["forecast_snapshots"]
    ):
        raise ValidationError(
            (
                "Automatic simulation setup did not pass its final "
                "integrity check."
            )
        )

    return {
        "ready": True,
        "source_game": source_game,
        "target_game": target_game,
        "scenario_years_created": (
            target_status["scenario_years"]
        ),
        "demand_values_created": (
            target_status["demand_values"]
        ),
        "forecast_snapshots_created": (
            target_status["forecast_snapshots"]
        ),
    }


# ---------------------------------------------------------------------
# Attempt-resolution helpers
# ---------------------------------------------------------------------

def resolve_year_attempt(
    *,
    membership,
    scenario_year,
    attempt=None,
):
    """
    Resolve the year attempt governing a calculation.

    New records use YearAttempt. Legacy records may still have a null
    attempt. If no explicit attempt is supplied, the current attempt is
    used when one exists; otherwise the legacy path is used.
    """
    if attempt is not None:
        if attempt.membership_id != membership.id:
            raise ValidationError(
                "The selected attempt does not belong to this player."
            )

        if attempt.scenario_year_id != scenario_year.id:
            raise ValidationError(
                "The selected attempt does not belong to this simulation year."
            )

        return attempt

    return (
        YearAttempt.objects
        .filter(
            membership=membership,
            scenario_year=scenario_year,
            is_current=True,
        )
        .order_by("-attempt_number")
        .first()
    )


def attempt_filter(
    attempt,
    *,
    prefix="",
):
    """
    Build an ORM filter that isolates one attempt.

    A null attempt selects legacy rows only; it never means all attempts.
    """
    field_name = (
        f"{prefix}attempt"
        if prefix
        else "attempt"
    )

    if attempt is None:
        return {
            f"{field_name}__isnull": True,
        }

    return {
        field_name: attempt,
    }


# ---------------------------------------------------------------------
# Decimal and pricing helpers
# ---------------------------------------------------------------------

def to_decimal(value):
    """
    Convert a value safely to Decimal.
    """
    if value is None:
        return ZERO

    if isinstance(value, Decimal):
        return value

    return Decimal(str(value))


def qmoney(value):
    """
    Round a monetary value to two decimal places.
    """
    return to_decimal(value).quantize(
        MONEY,
        rounding=ROUND_HALF_UP,
    )


def demand_multiplier(
    price,
    standard_price,
):
    """
    Calculate the symmetric price-elasticity response.

    Rule:
        A 1% price change causes a 2% demand change
        in the opposite direction.

    Examples:
        5% discount  -> 10% demand increase
        5% premium   -> 10% demand decrease

    Demand cannot fall below zero.
    """
    price = to_decimal(
        price
    )

    standard_price = to_decimal(
        standard_price
    )

    if standard_price <= ZERO:
        raise ValidationError(
            "Standard price must be greater than zero."
        )

    price_change_rate = (
        price
        - standard_price
    ) / standard_price

    return max(
        ZERO,
        (
            ONE
            - TWO
            * price_change_rate
        ),
    )


def sales_credit_per_unit(
    price,
    standard_price,
    sales_credit_rate,
):
    """
    Sales credit earned per unit.

    Example

    Standard price = 1000

    20% policy

    Maximum credit = 200

    Sell 1000 → 200

    Sell 900 →100

    Sell 800 →0
    """

    price = to_decimal(price)
    standard_price = to_decimal(
        standard_price
    )

    sales_credit_rate = to_decimal(
        sales_credit_rate
    )

    maximum_credit = (
        standard_price
        * sales_credit_rate
    )

    minimum_price = (
        standard_price
        - maximum_credit
    )

    # Sales credit increases linearly with selling price.
    #
    # Rule:
    #     Credit / Unit = max(0, Selling Price - Minimum Price)
    #
    # Therefore credit continues to increase when price rises above
    # the standard price. It is not capped at the standard-price level.
    return qmoney(
        max(
            ZERO,
            (
                price
                - minimum_price
            ),
        )
    )


def demand_uncertainty_rate_for_year(
    scenario_year,
):
    """
    Return the one-month-ahead demand uncertainty used for planning.

    Year 1:
        ±10% around the adjusted forecast mean.

    Year 2:
        ±25% around the adjusted forecast mean.

    Year 3:
        Falls back to Year 2 uncertainty until its scenario is defined.
    """
    uncertainty_by_year = {
        1: Decimal("0.10"),
        2: Decimal("0.25"),
    }

    return uncertainty_by_year.get(
        scenario_year.year_number,
        Decimal("0.25"),
    )


def forecast_demand_band(
    forecast_units,
    *,
    operating_multiplier=ONE,
    price_multiplier=ONE,
    uncertainty_rate=Decimal("0.10"),
):
    """
    Return the integer demand outlook used for monthly planning.

    The complete demand distribution is adjusted for:
        - maintenance / capability effects,
        - price elasticity,
        - year-specific forecast uncertainty.

    Uniform planning ranges:
        Year 1 -> ±10%
        Year 2 -> ±25%

    The caller supplies the applicable uncertainty_rate.
    """
    uncertainty_rate = to_decimal(
        uncertainty_rate
    )

    if (
        uncertainty_rate < ZERO
        or uncertainty_rate >= ONE
    ):
        raise ValidationError(
            "Demand uncertainty must be between 0% and 100%."
        )

    mean_demand = (
        Decimal(forecast_units)
        * to_decimal(
            operating_multiplier
        )
        * to_decimal(
            price_multiplier
        )
    )

    low_demand = (
        mean_demand
        * (
            ONE
            - uncertainty_rate
        )
    )

    high_demand = (
        mean_demand
        * (
            ONE
            + uncertainty_rate
        )
    )

    return {
        "mean": max(
            0,
            int(
                mean_demand.quantize(
                    WHOLE_UNIT,
                    rounding=ROUND_HALF_UP,
                )
            ),
        ),
        "low": max(
            0,
            int(
                low_demand.quantize(
                    WHOLE_UNIT,
                    rounding=ROUND_HALF_UP,
                )
            ),
        ),
        "high": max(
            0,
            int(
                high_demand.quantize(
                    WHOLE_UNIT,
                    rounding=ROUND_HALF_UP,
                )
            ),
        ),
        "uncertainty_rate": (
            uncertainty_rate
        ),
        "uncertainty_percent": (
            uncertainty_rate
            * Decimal("100")
        ),
    }

def newsvendor_inventory_target(
    *,
    low_demand,
    high_demand,
    selling_price,
    variable_unit_cost,
    monthly_interest_rate,
    is_terminal_month=False,
):
    """
    Calculate the monthly newsvendor inventory target.

    Fixed production-cost allocations are excluded from the
    inventory decision. The calculation uses variable economics.

    September through July
    ----------------------
    Underage cost:
        Selling Price - Variable Unit Cost

    Overage cost:
        Variable Unit Cost × Monthly Interest Rate

    Critical ratio:
        Cu / (Cu + Co)

    August / terminal month
    -----------------------
    Unsold inventory has zero salvage value.

    Therefore:
        Overage cost = Variable Unit Cost

    and:
        Critical ratio
        = (Selling Price - Variable Unit Cost) / Selling Price

    Uniform-demand base-stock target:
        Q* = Low + CR × (High - Low)
    """
    selling_price = to_decimal(
        selling_price
    )

    variable_unit_cost = to_decimal(
        variable_unit_cost
    )

    monthly_interest_rate = to_decimal(
        monthly_interest_rate
    )

    low_demand = Decimal(
        low_demand
    )

    high_demand = Decimal(
        high_demand
    )

    underage_cost = max(
        ZERO,
        (
            selling_price
            - variable_unit_cost
        ),
    )

    if is_terminal_month:
        overage_cost = max(
            ZERO,
            variable_unit_cost,
        )
    else:
        overage_cost = max(
            ZERO,
            (
                variable_unit_cost
                * monthly_interest_rate
            ),
        )

    denominator = (
        underage_cost
        + overage_cost
    )

    if denominator <= ZERO:
        critical_ratio = Decimal(
            "0.50"
        )
    else:
        critical_ratio = (
            underage_cost
            / denominator
        )

    critical_ratio = min(
        ONE,
        max(
            ZERO,
            critical_ratio,
        ),
    )

    demand_range = (
        high_demand
        - low_demand
    )

    target_inventory = (
        low_demand
        + critical_ratio
        * demand_range
    )

    target_inventory_units = max(
        0,
        int(
            target_inventory.quantize(
                WHOLE_UNIT,
                rounding=ROUND_HALF_UP,
            )
        ),
    )

    return {
        "critical_ratio": (
            critical_ratio
        ),
        "underage_cost": qmoney(
            underage_cost
        ),
        "overage_cost": qmoney(
            overage_cost
        ),
        "target_inventory_units": (
            target_inventory_units
        ),
        "is_terminal_month": (
            bool(
                is_terminal_month
            )
        ),
    }


def fixed_cost_absorption_status(
    *,
    annual_plan,
    scenario_year,
    attempt=None,
):
    """
    Return the current annual fixed-cost absorption position.

    The AOP establishes an annual fixed manufacturing-cost budget.

    Finalized monthly COGS absorbs that budget through:
        - labor fixed cost,
        - maintenance fixed cost, when maintenance is not deferred.

    The system checks the cumulative DOLLAR amount absorbed after every
    finalized month.

    Once cumulative absorbed fixed cost reaches the effective annual
    fixed-cost budget:
        Labor Cost Absorbed       = 0 for subsequent units
        Maintenance Cost Absorbed = 0 for subsequent units

    Any fixed-cost budget left unabsorbed at the end of August is
    recognized as manufacturing overhead.
    """

    attempt = resolve_year_attempt(
        membership=annual_plan.membership,
        scenario_year=scenario_year,
        attempt=(
            attempt
            or annual_plan.attempt
        ),
    )

    cost_parameters = (
        annual_cost_parameters(
            scenario_year=scenario_year,
        )
    )

    annual_forecast_volume = int(
        cost_parameters[
            "total_forecast_units"
        ]
    )

    labor_fixed_cost_per_unit = (
        cost_parameters[
            "labor_fixed_cost_per_unit"
        ]
    )

    maintenance_fixed_cost_per_unit = (
        cost_parameters[
            "maintenance_fixed_cost_per_unit"
        ]
    )

    effective_maintenance_cost_per_unit = (
        ZERO
        if annual_plan.skip_maintenance
        else maintenance_fixed_cost_per_unit
    )

    effective_fixed_cost_per_unit = (
        labor_fixed_cost_per_unit
        + effective_maintenance_cost_per_unit
    )

    # Effective annual fixed-cost budget that must be absorbed.
    effective_fixed_cost_budget = (
        Decimal(
            annual_forecast_volume
        )
        * effective_fixed_cost_per_unit
    )

    absorbed_totals = (
        ProductDecision.objects
        .filter(
            monthly_decision__membership=(
                annual_plan.membership
            ),
            monthly_decision__scenario_year=(
                scenario_year
            ),
            monthly_decision__is_finalized=True,
            **attempt_filter(
                attempt,
                prefix="monthly_decision__",
            ),
        )
        .aggregate(
            labor=Sum(
                "labor_fixed_cost"
            ),
            maintenance=Sum(
                "maintenance_fixed_cost"
            ),
            units=Sum(
                "units_sold"
            ),
        )
    )

    labor_fixed_cost_absorbed = to_decimal(
        absorbed_totals[
            "labor"
        ]
    )

    maintenance_fixed_cost_absorbed = to_decimal(
        absorbed_totals[
            "maintenance"
        ]
    )

    total_fixed_cost_absorbed = (
        labor_fixed_cost_absorbed
        + maintenance_fixed_cost_absorbed
    )

    actual_units_sold = int(
        absorbed_totals[
            "units"
        ]
        or 0
    )

    remaining_fixed_cost_budget = max(
        ZERO,
        (
            effective_fixed_cost_budget
            - total_fixed_cost_absorbed
        ),
    )

    fixed_cost_fully_absorbed = (
        remaining_fixed_cost_budget
        <= MONEY
    )

    if fixed_cost_fully_absorbed:
        remaining_fixed_cost_budget = ZERO

    if effective_fixed_cost_budget > ZERO:
        absorption_percent = min(
            Decimal("100"),
            (
                total_fixed_cost_absorbed
                / effective_fixed_cost_budget
                * Decimal("100")
            ),
        )
    else:
        absorption_percent = Decimal("100")

    # Unit-equivalent figures are retained for display/reference only.
    if effective_fixed_cost_per_unit > ZERO:
        remaining_absorption_units = max(
            0,
            int(
                (
                    remaining_fixed_cost_budget
                    / effective_fixed_cost_per_unit
                ).to_integral_value(
                    rounding=ROUND_HALF_UP
                )
            ),
        )
    else:
        remaining_absorption_units = 0

    absorbed_units = min(
        actual_units_sold,
        annual_forecast_volume,
    )

    return {
        "cost_parameters": (
            cost_parameters
        ),
        "annual_forecast_volume": (
            annual_forecast_volume
        ),
        "actual_units_sold": (
            actual_units_sold
        ),
        "absorbed_units": (
            absorbed_units
        ),
        "remaining_absorption_units": (
            remaining_absorption_units
        ),

        "effective_fixed_cost_budget": (
            effective_fixed_cost_budget
        ),
        "labor_fixed_cost_absorbed": (
            labor_fixed_cost_absorbed
        ),
        "maintenance_fixed_cost_absorbed": (
            maintenance_fixed_cost_absorbed
        ),
        "total_fixed_cost_absorbed": (
            total_fixed_cost_absorbed
        ),
        "remaining_fixed_cost_budget": (
            remaining_fixed_cost_budget
        ),

        "effective_fixed_cost_per_unit": (
            effective_fixed_cost_per_unit
        ),
        "fixed_cost_fully_absorbed": (
            fixed_cost_fully_absorbed
        ),
        "absorption_percent": (
            absorption_percent
        ),
    }



# ---------------------------------------------------------------------
# Year 2 late-year market intelligence
# ---------------------------------------------------------------------

YEAR_2_LATE_MARKET_SIGNALS = {
    "ultimate-mountain": {
        "direction": "LOWER",
        "median_shift_rate": Decimal("-0.20"),
        "median_shift_percent": Decimal("20"),
        "headline": "Downside demand signal",
        "message": (
            "Recent market intelligence suggests that the median demand "
            "realization may be approximately 20% below the current "
            "forecast expectation."
        ),
    },
    "tough-track": {
        "direction": "HIGHER",
        "median_shift_rate": Decimal("0.18"),
        "median_shift_percent": Decimal("18"),
        "headline": "Upside demand signal",
        "message": (
            "Recent market intelligence suggests that the median demand "
            "realization may be approximately 18% above the current "
            "forecast expectation."
        ),
    },
}


def _product_market_key(product):
    """
    Return a stable lowercase/hyphenated key from Product.name.

    Example:
        "Ultimate Mountain" -> "ultimate-mountain"
        "Tough Track"       -> "tough-track"
    """
    return "-".join(
        str(product.name)
        .strip()
        .lower()
        .replace("_", " ")
        .split()
    )


def year_2_late_market_signal(
    *,
    scenario_year,
    month,
    product,
):
    """
    Return the July/August Year 2 market-intelligence signal.

    IMPORTANT:
        This is an INFORMATIONAL signal only.

        It does NOT:
            - change ForecastSnapshot values,
            - change forecast_map,
            - change the forecast mean,
            - change the ±25% forecast band,
            - change the newsvendor distribution,
            - change the critical fractile,
            - change realized demand.

        Students must decide how, if at all, to respond through their
        pricing and replenishment decisions.

    Signal window:
        Year 2 July and August only.

    Ultimate Mountain:
        median realization signal ≈ 20% below forecast expectation.

    Tough Track:
        median realization signal ≈ 18% above forecast expectation.
    """

    if scenario_year.year_number != 2:
        return None

    if month not in (7, 8):
        return None

    signal = YEAR_2_LATE_MARKET_SIGNALS.get(
        _product_market_key(product)
    )

    if signal is None:
        return None

    return {
        **signal,
        "active": True,
        "month": month,
        "informational_only": True,
    }



def monthly_planning_analysis(
    *,
    scenario_year,
    annual_plan,
    products,
    forecast_map,
    beginning_inventory_map,
    month,
):
    """
    Build the complete monthly S&OP decision-support dataset.

    Year 1
    ------
    The player receives guided inventory support:
        - ±10% one-month demand range,
        - critical ratio,
        - base-stock target,
        - recommended replenishment.

    Year 2
    ------
    The player receives less inventory guidance:
        - ±25% one-month demand range,
        - critical ratio,
        - NO displayed base-stock target,
        - NO recommended replenishment.

    Fixed-cost absorption
    ---------------------
    The AOP establishes the annual fixed-cost absorption budget.

    Finalized monthly labor and maintenance COGS consume that budget.

    While remaining fixed-cost budget is positive:
        Cost Analysis shows the normal AOP fixed-cost allocation.

    Once cumulative absorbed fixed cost reaches the annual budget:
        Labor Cost Absorbed       = 0
        Maintenance Cost Absorbed = 0
        Fixed Unit Cost           = 0
        Relevant Total Unit Cost  = Variable Unit Cost

    The returned dataset also exposes the absorption position so the
    monthly template can explain why fixed cost has become zero.

    Year 2 inventory-liquidation awareness
    --------------------------------------
    When beginning inventory is high relative to the current demand
    outlook, the service flags price reduction as a possible strategic
    use of pricing flexibility. It does not choose the price for the
    player.

    August terminal-month economics
    --------------------------------
    August has zero salvage value for unsold inventory.

    Therefore the newsvendor overage cost is the full variable unit
    cost, not the monthly holding cost:

        Co = Variable Unit Cost

        CR = (Selling Price - Variable Unit Cost) / Selling Price

    This applies in BOTH Year 1 and Year 2.

    Year 2 July/August market intelligence
    --------------------------------------
    July and August include an informational median-demand signal:
        Ultimate Mountain -> approximately 20% below expectation
        Tough Track       -> approximately 18% above expectation

    This signal NEVER modifies the rolling forecast or its planning
    distribution. It is shown separately so the player can decide
    whether to react through price and/or replenishment.
    """

    absorption_status = (
        fixed_cost_absorption_status(
            annual_plan=annual_plan,
            scenario_year=scenario_year,
            attempt=annual_plan.attempt,
        )
    )

    ordering_guardrail = (
        monthly_ordering_guardrail_status(
            annual_plan=annual_plan,
            scenario_year=scenario_year,
        )
    )

    cost_parameters = (
        absorption_status[
            "cost_parameters"
        ]
    )

    fixed_cost_fully_absorbed = (
        absorption_status[
            "fixed_cost_fully_absorbed"
        ]
    )

    if fixed_cost_fully_absorbed:
        labor_unit_cost = ZERO
        maintenance_unit_cost = ZERO
    else:
        labor_unit_cost = (
            cost_parameters[
                "labor_fixed_cost_per_unit"
            ]
        )

        if annual_plan.skip_maintenance:
            maintenance_unit_cost = ZERO
        else:
            maintenance_unit_cost = (
                cost_parameters[
                    "maintenance_fixed_cost_per_unit"
                ]
            )

    uncertainty_rate = (
        demand_uncertainty_rate_for_year(
            scenario_year
        )
    )

    uncertainty_percent = (
        uncertainty_rate
        * Decimal("100")
    )

    show_replenishment_guidance = (
        scenario_year.year_number == 1
    )

    is_terminal_month = (
        month == 8
    )

    # Defensive consistency check: the configured fiscal sequence
    # must also end in August.
    if FISCAL_MONTH_NUMBERS[-1] != 8:
        raise ValidationError(
            "The fiscal-month sequence must end in August."
        )

    allow_inventory_liquidation_pricing = (
        scenario_year.year_number >= 2
    )

    rows = []

    for product in products:
        forecast_units = int(
            forecast_map.get(
                product.id,
                0,
            )
        )

        beginning_inventory = int(
            beginning_inventory_map.get(
                product.id,
                0,
            )
        )

        market_signal = (
            year_2_late_market_signal(
                scenario_year=scenario_year,
                month=month,
                product=product,
            )
        )

        variable_unit_cost = (
            product.variable_unit_cost
        )

        fixed_unit_cost = (
            labor_unit_cost
            + maintenance_unit_cost
        )

        total_unit_cost = (
            variable_unit_cost
            + fixed_unit_cost
        )

        holding_cost_per_unit = (
            variable_unit_cost
            * scenario_year.monthly_interest_rate
        )

        standard_price = (
            product.standard_price
        )

        minimum_price = (
            annual_plan
            .minimum_price_for_product(
                product
            )
        )

        initial_price = (
            standard_price
        )

        initial_price_multiplier = (
            demand_multiplier(
                initial_price,
                standard_price,
            )
        )

        demand_band = (
            forecast_demand_band(
                forecast_units,
                operating_multiplier=(
                    annual_plan
                    .quality_capacity_multiplier
                ),
                price_multiplier=(
                    initial_price_multiplier
                ),
                uncertainty_rate=(
                    uncertainty_rate
                ),
            )
        )

        newsvendor = (
            newsvendor_inventory_target(
                low_demand=(
                    demand_band["low"]
                ),
                high_demand=(
                    demand_band["high"]
                ),
                selling_price=(
                    initial_price
                ),
                variable_unit_cost=(
                    variable_unit_cost
                ),
                monthly_interest_rate=(
                    scenario_year
                    .monthly_interest_rate
                ),
                is_terminal_month=(
                    is_terminal_month
                ),
            )
        )

        calculated_replenishment = max(
            0,
            (
                newsvendor[
                    "target_inventory_units"
                ]
                - beginning_inventory
            ),
        )

        if show_replenishment_guidance:
            displayed_target_inventory = (
                newsvendor[
                    "target_inventory_units"
                ]
            )
            displayed_replenishment = (
                calculated_replenishment
            )
        else:
            displayed_target_inventory = None
            displayed_replenishment = None

        excess_inventory_units = max(
            0,
            (
                beginning_inventory
                - demand_band["mean"]
            ),
        )

        liquidation_candidate = (
            allow_inventory_liquidation_pricing
            and beginning_inventory
            > demand_band["mean"]
        )

        strong_liquidation_candidate = (
            allow_inventory_liquidation_pricing
            and beginning_inventory
            > demand_band["high"]
        )

        if strong_liquidation_candidate:
            inventory_pricing_signal = (
                "STRONG_LIQUIDATION"
            )
        elif liquidation_candidate:
            inventory_pricing_signal = (
                "CONSIDER_LIQUIDATION"
            )
        else:
            inventory_pricing_signal = (
                "NORMAL"
            )

        unit_credit = (
            sales_credit_per_unit(
                price=initial_price,
                standard_price=(
                    standard_price
                ),
                sales_credit_rate=(
                    annual_plan
                    .sales_credit_rate
                ),
            )
        )

        expected_sales_credit = (
            Decimal(
                demand_band["mean"]
            )
            * unit_credit
        )

        expected_sales_credit_low = (
            Decimal(
                demand_band["low"]
            )
            * unit_credit
        )

        expected_sales_credit_high = (
            Decimal(
                demand_band["high"]
            )
            * unit_credit
        )

        rows.append({
            "product": product,

            "forecast_units": (
                forecast_units
            ),
            "forecast_low": (
                demand_band["low"]
            ),
            "forecast_mean": (
                demand_band["mean"]
            ),
            "forecast_high": (
                demand_band["high"]
            ),
            "uncertainty_rate": (
                uncertainty_rate
            ),
            "uncertainty_percent": (
                uncertainty_percent
            ),

            # Late-year market intelligence is separate from the
            # forecast and does not alter any forecast values.
            "market_signal": (
                market_signal
            ),
            "market_signal_active": (
                market_signal is not None
            ),
            "market_signal_direction": (
                market_signal["direction"]
                if market_signal
                else None
            ),
            "market_signal_median_shift_rate": (
                market_signal["median_shift_rate"]
                if market_signal
                else ZERO
            ),
            "market_signal_median_shift_percent": (
                market_signal["median_shift_percent"]
                if market_signal
                else ZERO
            ),
            "market_signal_headline": (
                market_signal["headline"]
                if market_signal
                else ""
            ),
            "market_signal_message": (
                market_signal["message"]
                if market_signal
                else ""
            ),
            "market_signal_informational_only": (
                bool(market_signal)
            ),

            "beginning_inventory_units": (
                beginning_inventory
            ),
            "excess_inventory_units": (
                excess_inventory_units
            ),
            "liquidation_candidate": (
                liquidation_candidate
            ),
            "strong_liquidation_candidate": (
                strong_liquidation_candidate
            ),
            "inventory_pricing_signal": (
                inventory_pricing_signal
            ),

            # Current marginal accounting cost.
            # These fixed-cost components automatically become zero
            # after the annual AOP forecast-volume ceiling is consumed.
            "variable_unit_cost": qmoney(
                variable_unit_cost
            ),
            "labor_unit_cost": qmoney(
                labor_unit_cost
            ),
            "maintenance_unit_cost": qmoney(
                maintenance_unit_cost
            ),
            "fixed_unit_cost": qmoney(
                fixed_unit_cost
            ),
            "total_unit_cost": qmoney(
                total_unit_cost
            ),
            "holding_cost_per_unit": qmoney(
                holding_cost_per_unit
            ),

            # Annual fixed-cost absorption position.
            "annual_forecast_volume": (
                absorption_status[
                    "annual_forecast_volume"
                ]
            ),
            "actual_units_sold_to_date": (
                absorption_status[
                    "actual_units_sold"
                ]
            ),
            "absorbed_units_to_date": (
                absorption_status[
                    "absorbed_units"
                ]
            ),
            "remaining_absorption_units": (
                absorption_status[
                    "remaining_absorption_units"
                ]
            ),
            "fixed_cost_fully_absorbed": (
                fixed_cost_fully_absorbed
            ),
            "fixed_cost_absorption_percent": (
                absorption_status[
                    "absorption_percent"
                ]
            ),
            "effective_fixed_cost_budget": qmoney(
                absorption_status[
                    "effective_fixed_cost_budget"
                ]
            ),
            "total_fixed_cost_absorbed": qmoney(
                absorption_status[
                    "total_fixed_cost_absorbed"
                ]
            ),
            "remaining_fixed_cost_budget": qmoney(
                absorption_status[
                    "remaining_fixed_cost_budget"
                ]
            ),

            "standard_price": (
                standard_price
            ),
            "minimum_price": qmoney(
                minimum_price
            ),
            "sales_credit_rate": (
                annual_plan
                .sales_credit_rate
            ),
            "sales_credit_rate_percent": (
                annual_plan
                .sales_credit_rate
                * Decimal("100")
            ),

            "initial_sales_credit_per_unit": (
                qmoney(
                    unit_credit
                )
            ),
            "initial_expected_sales_credit": (
                qmoney(
                    expected_sales_credit
                )
            ),
            "initial_expected_sales_credit_low": (
                qmoney(
                    expected_sales_credit_low
                )
            ),
            "initial_expected_sales_credit_high": (
                qmoney(
                    expected_sales_credit_high
                )
            ),

            # Newsvendor / critical-fractile metadata.
            "is_terminal_month": (
                is_terminal_month
            ),
            "terminal_month_zero_salvage": (
                is_terminal_month
            ),
            "salvage_value_per_unit": (
                ZERO
                if is_terminal_month
                else None
            ),
            "critical_ratio_mode": (
                "ZERO_SALVAGE"
                if is_terminal_month
                else "CARRYING_COST"
            ),
            "critical_ratio": (
                newsvendor[
                    "critical_ratio"
                ]
            ),
            "critical_ratio_percent": (
                newsvendor[
                    "critical_ratio"
                ]
                * Decimal("100")
            ),
            "underage_cost": (
                newsvendor[
                    "underage_cost"
                ]
            ),
            "overage_cost": (
                newsvendor[
                    "overage_cost"
                ]
            ),

            "newsvendor_target_units": (
                displayed_target_inventory
            ),
            "recommended_replenishment_units": (
                displayed_replenishment
            ),
        })

    return {
        "rows": rows,
        "cost_parameters": (
            cost_parameters
        ),
        "year_number": (
            scenario_year.year_number
        ),
        "uncertainty_rate": (
            uncertainty_rate
        ),
        "uncertainty_percent": (
            uncertainty_percent
        ),
        "show_replenishment_guidance": (
            show_replenishment_guidance
        ),
        "allow_inventory_liquidation_pricing": (
            allow_inventory_liquidation_pricing
        ),
        "is_terminal_month": (
            is_terminal_month
        ),
        "terminal_month_zero_salvage": (
            is_terminal_month
        ),
        "late_market_signal_active": (
            scenario_year.year_number == 2
            and month in (7, 8)
        ),

        # Monthly hard replenishment-spend guardrail.
        "ordering_guardrail": (
            ordering_guardrail
        ),
        "annual_variable_cost_budget": (
            ordering_guardrail[
                "annual_variable_cost_budget"
            ]
        ),
        "variable_cost_spend_ceiling": (
            ordering_guardrail[
                "variable_cost_ceiling"
            ]
        ),
        "cumulative_variable_production_cost": (
            ordering_guardrail[
                "cumulative_variable_production_cost"
            ]
        ),
        "remaining_allowed_replenishment_spend": (
            ordering_guardrail[
                "remaining_allowed_spend"
            ]
        ),
        "ordering_blocked": (
            ordering_guardrail[
                "ordering_blocked"
            ]
        ),

        # Company-level AOP fixed-cost absorption status.
        "annual_forecast_volume": (
            absorption_status[
                "annual_forecast_volume"
            ]
        ),
        "actual_units_sold_to_date": (
            absorption_status[
                "actual_units_sold"
            ]
        ),
        "absorbed_units_to_date": (
            absorption_status[
                "absorbed_units"
            ]
        ),
        "remaining_absorption_units": (
            absorption_status[
                "remaining_absorption_units"
            ]
        ),
        "fixed_cost_fully_absorbed": (
            fixed_cost_fully_absorbed
        ),
        "fixed_cost_absorption_percent": (
            absorption_status[
                "absorption_percent"
            ]
        ),
        "effective_fixed_cost_budget": qmoney(
            absorption_status[
                "effective_fixed_cost_budget"
            ]
        ),
        "total_fixed_cost_absorbed": qmoney(
            absorption_status[
                "total_fixed_cost_absorbed"
            ]
        ),
        "remaining_fixed_cost_budget": qmoney(
            absorption_status[
                "remaining_fixed_cost_budget"
            ]
        ),
    }


# ---------------------------------------------------------------------
# Annual cost-allocation logic
# ---------------------------------------------------------------------

def annual_cost_parameters(scenario_year):
    """
    Calculate the annual unit-cost structure from the August forecast.

    The August forecast contains the full September-August planning
    horizon.

    Annual variable-cost estimate:
        Sum of forecast units × product variable unit cost

    Fixed-cost pool:
        Operating budget − annual variable-cost estimate

    Fixed cost per forecast unit:
        Fixed-cost pool ÷ total forecast units

    The resulting fixed cost per unit is allocated:
        60% labor
        40% maintenance

    This function defines the AOP allocation rate only.

    Whether that fixed-cost allocation is still applicable in a later
    monthly S&OP cycle depends on cumulative ACTUAL units sold. Use
    fixed_cost_absorption_status() for that dynamic determination.
    """
    products = list(
        Product.objects.all().order_by("name")
    )

    august_forecast_rows = (
        ForecastSnapshot.objects.filter(
            scenario_year=scenario_year,
            base_month=8,
        )
        .select_related("product")
    )

    # Sum the complete September-August horizon for each product.
    forecast_units_by_product = {}

    for forecast_row in august_forecast_rows:
        forecast_units_by_product[
            forecast_row.product_id
        ] = (
            forecast_units_by_product.get(
                forecast_row.product_id,
                0,
            )
            + forecast_row.forecast_units
        )

    product_rows = []
    total_forecast_units = 0
    total_variable_cost_estimate = ZERO

    for product in products:
        forecast_units = (
            forecast_units_by_product.get(
                product.id,
                0,
            )
        )

        variable_cost_estimate = (
            Decimal(forecast_units)
            * product.variable_unit_cost
        )

        total_forecast_units += forecast_units
        total_variable_cost_estimate += (
            variable_cost_estimate
        )

        product_rows.append({
            "product": product,
            "forecast_units": forecast_units,
            "variable_unit_cost": (
                product.variable_unit_cost
            ),
            "variable_cost_estimate": qmoney(
                variable_cost_estimate
            ),
        })

    if total_forecast_units <= 0:
        raise ValidationError(
            "The August annual forecast does not contain any units."
        )

    if scenario_year.year_number == 2:
        year_1_scenario = (
            scenario_year.game
            .scenario_years
            .get(
                year_number=1
            )
        )

        year_1_parameters = (
            annual_cost_parameters(
                scenario_year=year_1_scenario
            )
        )

        fixed_cost_pool = to_decimal(
            year_1_parameters[
                "fixed_cost_pool"
            ]
        )
    else:
        fixed_cost_pool = (
            scenario_year.operating_budget
            - total_variable_cost_estimate
        )

    if fixed_cost_pool < ZERO:
        raise ValidationError(
            "The annual fixed-cost pool cannot be negative."
        )

    fixed_cost_per_unit = (
        fixed_cost_pool
        / Decimal(total_forecast_units)
    )

    labor_fixed_cost_per_unit = (
        fixed_cost_per_unit
        * scenario_year.labor_fixed_cost_share
    )

    maintenance_fixed_cost_per_unit = (
        fixed_cost_per_unit
        * scenario_year.maintenance_fixed_cost_share
    )

    for row in product_rows:
        product = row["product"]

        row["labor_fixed_cost_per_unit"] = qmoney(
            labor_fixed_cost_per_unit
        )

        row[
            "maintenance_fixed_cost_per_unit"
        ] = qmoney(
            maintenance_fixed_cost_per_unit
        )

        row["full_unit_cost"] = qmoney(
            product.variable_unit_cost
            + fixed_cost_per_unit
        )

        row[
            "unit_cost_without_maintenance"
        ] = qmoney(
            product.variable_unit_cost
            + labor_fixed_cost_per_unit
        )

    return {
        "product_rows": product_rows,
        "total_forecast_units": (
            total_forecast_units
        ),
        "total_variable_cost_estimate": qmoney(
            total_variable_cost_estimate
        ),
        "fixed_cost_pool": qmoney(
            fixed_cost_pool
        ),

        # Full precision for simulation calculations.
        "fixed_cost_per_unit": (
            fixed_cost_per_unit
        ),
        "labor_fixed_cost_per_unit": (
            labor_fixed_cost_per_unit
        ),
        "maintenance_fixed_cost_per_unit": (
            maintenance_fixed_cost_per_unit
        ),

        # Rounded values for display.
        "fixed_cost_per_unit_display": qmoney(
            fixed_cost_per_unit
        ),
        "labor_fixed_cost_per_unit_display": qmoney(
            labor_fixed_cost_per_unit
        ),
        "maintenance_fixed_cost_per_unit_display": qmoney(
            maintenance_fixed_cost_per_unit
        ),
    }



def annual_revenue_budget_options(
    *,
    scenario_year,
):
    """
    Return annual revenue and budget alternatives.

    Year 1:
        Fixed board target and budget.

    Year 2:
        Baseline = Year 1 × 1.85
        +1%      = baseline × 1.01
        +2%      = baseline × 1.02

    The Year 1 fixed-cost pool remains unchanged.
    Only the variable-cost component grows with the selected ambition.
    """
    if scenario_year.year_number == 1:
        return {
            "baseline_revenue_target": qmoney(
                scenario_year.revenue_target
            ),
            "baseline_operating_budget": qmoney(
                scenario_year.operating_budget
            ),
            "options": [
                {
                    "code": "BASELINE",
                    "label": "Board Target",
                    "growth_percent": Decimal("0"),
                    "revenue_target": qmoney(
                        scenario_year.revenue_target
                    ),
                    "operating_budget": qmoney(
                        scenario_year.operating_budget
                    ),
                    "board_preferred": True,
                },
            ],
        }

    if scenario_year.year_number != 2:
        raise ValidationError(
            (
                "Revenue and budget alternatives have not "
                f"yet been configured for Year "
                f"{scenario_year.year_number}."
            )
        )

    year_1_scenario = (
        scenario_year.game
        .scenario_years
        .get(
            year_number=1
        )
    )

    year_1_costs = (
        annual_cost_parameters(
            scenario_year=year_1_scenario
        )
    )

    year_1_fixed_cost_component = to_decimal(
        year_1_costs[
            "fixed_cost_pool"
        ]
    )

    year_1_variable_cost_component = to_decimal(
        year_1_costs[
            "total_variable_cost_estimate"
        ]
    )

    market_expansion_multiplier = Decimal(
        "1.85"
    )

    option_definitions = [
        {
            "code": "BASELINE",
            "label": "Baseline",
            "increment": Decimal("0.00"),
            "multiplier": Decimal("1.00"),
            "board_preferred": False,
        },
        {
            "code": "PLUS_1",
            "label": "Baseline +1%",
            "increment": Decimal("0.01"),
            "multiplier": Decimal("1.01"),
            "board_preferred": False,
        },
        {
            "code": "PLUS_2",
            "label": "Baseline +2%",
            "increment": Decimal("0.02"),
            "multiplier": Decimal("1.02"),
            "board_preferred": True,
        },
    ]

    options = []

    for definition in option_definitions:
        option_multiplier = (
            definition[
                "multiplier"
            ]
        )

        revenue_target = (
            to_decimal(
                year_1_scenario.revenue_target
            )
            * market_expansion_multiplier
            * option_multiplier
        )

        variable_budget = (
            year_1_variable_cost_component
            * market_expansion_multiplier
            * option_multiplier
        )

        operating_budget = (
            year_1_fixed_cost_component
            + variable_budget
        )

        options.append({
            "code": (
                definition[
                    "code"
                ]
            ),
            "label": (
                definition[
                    "label"
                ]
            ),
            "growth_percent": (
                definition[
                    "increment"
                ]
                * Decimal("100")
            ),
            "market_expansion_percent": (
                Decimal("85")
            ),
            "revenue_target": qmoney(
                revenue_target
            ),
            "operating_budget": qmoney(
                operating_budget
            ),
            "fixed_cost_component": qmoney(
                year_1_fixed_cost_component
            ),
            "variable_cost_component": qmoney(
                variable_budget
            ),
            "board_preferred": (
                definition[
                    "board_preferred"
                ]
            ),
        })

    baseline = options[0]

    return {
        "market_expansion_multiplier": (
            market_expansion_multiplier
        ),
        "market_expansion_percent": (
            Decimal("85")
        ),
        "baseline_revenue_target": (
            baseline[
                "revenue_target"
            ]
        ),
        "baseline_operating_budget": (
            baseline[
                "operating_budget"
            ]
        ),
        "fixed_cost_component": qmoney(
            year_1_fixed_cost_component
        ),
        "year_1_variable_cost_component": qmoney(
            year_1_variable_cost_component
        ),
        "options": options,
    }

# ---------------------------------------------------------------------
# Monthly S&OP hard guardrails
# ---------------------------------------------------------------------

def maximum_price_before_zero_demand(
    product,
):
    """
    Return the highest selling price permitted by the demand model.

    Demand multiplier:
        1 - 2 × ((Price - Standard Price) / Standard Price)

    Demand becomes zero when:
        Price = 1.50 × Standard Price

    The player may use the zero-demand threshold itself, but may not
    enter a price above it.
    """
    standard_price = to_decimal(
        product.standard_price
    )

    if standard_price <= ZERO:
        raise ValidationError(
            "Standard price must be greater than zero."
        )

    return qmoney(
        standard_price
        * Decimal("1.50")
    )


def annual_variable_cost_budget(
    *,
    scenario_year,
):
    """
    Return the annual variable-cost budget implied by the INITIAL
    September-August AOP forecast.

    Annual Variable-Cost Budget
        = Sum(
            Initial Annual Forecast Units
            × Product Variable Unit Cost
        )

    This is the variable-cost component already calculated by
    annual_cost_parameters().
    """
    cost_parameters = (
        annual_cost_parameters(
            scenario_year=scenario_year
        )
    )

    return qmoney(
        cost_parameters[
            "total_variable_cost_estimate"
        ]
    )


def cumulative_variable_production_cost(
    *,
    membership,
    scenario_year,
    attempt=None,
):
    """
    Return cumulative variable cost of PRODUCTION through finalized months.

    Production is represented by replenishment quantities:

        Variable Production Cost
        = Replenishment Units × Product Variable Unit Cost

    This deliberately does NOT use units sold or accounting COGS.
    The purpose of this guardrail is to prevent production/replenishment
    from exceeding the variable-cost capacity established by the AOP.
    """
    attempt = resolve_year_attempt(
        membership=membership,
        scenario_year=scenario_year,
        attempt=attempt,
    )

    finalized_lines = (
        ProductDecision.objects
        .filter(
            monthly_decision__membership=membership,
            monthly_decision__scenario_year=scenario_year,
            monthly_decision__is_finalized=True,
            **attempt_filter(
                attempt,
                prefix="monthly_decision__",
            ),
        )
        .select_related(
            "product"
        )
    )

    total_variable_production_cost = ZERO

    for line in finalized_lines:
        total_variable_production_cost += (
            Decimal(
                line.replenishment_units
            )
            * to_decimal(
                line.product.variable_unit_cost
            )
        )

    return qmoney(
        total_variable_production_cost
    )


def monthly_ordering_guardrail_status(
    *,
    annual_plan,
    scenario_year,
):
    """
    Return the monthly variable-cost production guardrail.

    Hard rule
    ---------
        Cumulative Variable Production Cost
        + Proposed Variable Production Cost
        <= 180% of Initial Annual Variable-Cost Budget

    where:

        Proposed Variable Production Cost
        = Sum(
            Proposed Replenishment Units
            × Product Variable Unit Cost
        )

    Fixed-cost absorption, manufacturing overhead, inventory write-off,
    holding cost, and other accounting consequences do not consume this
    production allowance.

    If cumulative variable production cost has already reached or
    exceeded the ceiling, remaining order capacity is zero.
    """
    variable_cost_budget = (
        annual_variable_cost_budget(
            scenario_year=scenario_year
        )
    )

    if variable_cost_budget <= ZERO:
        raise ValidationError(
            "The annual variable-cost budget must be greater than zero."
        )

    variable_cost_ceiling = (
        variable_cost_budget
        * Decimal("1.80")
    )

    cumulative_production_cost = (
        cumulative_variable_production_cost(
            membership=annual_plan.membership,
            scenario_year=scenario_year,
            attempt=annual_plan.attempt,
        )
    )

    remaining_allowed_spend = max(
        ZERO,
        (
            variable_cost_ceiling
            - cumulative_production_cost
        ),
    )

    ceiling_reached_or_exceeded = (
        cumulative_production_cost
        >= variable_cost_ceiling
    )

    return {
        "annual_variable_cost_budget": qmoney(
            variable_cost_budget
        ),
        "variable_cost_ceiling_percent": (
            Decimal("180")
        ),
        "variable_cost_ceiling": qmoney(
            variable_cost_ceiling
        ),
        "cumulative_variable_production_cost": qmoney(
            cumulative_production_cost
        ),
        "remaining_allowed_spend": qmoney(
            remaining_allowed_spend
        ),
        "ceiling_reached_or_exceeded": (
            ceiling_reached_or_exceeded
        ),
        "ordering_blocked": (
            ceiling_reached_or_exceeded
        ),
    }


def proposed_replenishment_spend(
    *,
    annual_plan,
    scenario_year,
    replenishment_by_product,
):
    """
    Return the variable cost of the proposed production/replenishment.

        Proposed Variable Production Cost
        = Sum(
            Replenishment Quantity
            × Product Variable Unit Cost
        )

    Fixed manufacturing allocations are intentionally excluded.
    """
    product_lookup = {
        product.id: product
        for product in Product.objects.all()
    }

    total_units = 0
    variable_spend = ZERO

    for product_key, quantity in (
        replenishment_by_product.items()
    ):
        if isinstance(
            product_key,
            Product,
        ):
            product = product_key
        else:
            product = product_lookup.get(
                int(product_key)
            )

        if product is None:
            raise ValidationError(
                "A proposed replenishment references an unknown product."
            )

        quantity_decimal = to_decimal(
            quantity
        )

        if quantity_decimal < ZERO:
            raise ValidationError(
                (
                    f"Replenishment quantity for {product.name} "
                    "cannot be negative."
                )
            )

        quantity_units = int(
            quantity_decimal
        )

        total_units += quantity_units

        variable_spend += (
            Decimal(
                quantity_units
            )
            * to_decimal(
                product.variable_unit_cost
            )
        )

    return {
        "total_units": total_units,
        "variable_spend": qmoney(
            variable_spend
        ),
        "total_spend": qmoney(
            variable_spend
        ),
    }


def validate_monthly_decision_guardrails(
    *,
    monthly_decision,
    annual_plan=None,
):
    """
    Apply defensive hard validation before a month is finalized.

    Price guardrail
    ---------------
    The feasible upper price is:

        Maximum Price = 1.50 × Standard Price

    At that price, modeled demand reaches zero. Prices above this
    threshold are not permitted.

    Variable-cost production guardrail
    ----------------------------------
    Cumulative variable cost of production plus the proposed variable
    cost of production may not exceed 180% of the initial annual
    variable-cost budget.

    If the ceiling has already been reached or exceeded, all proposed
    replenishment quantities must be zero.
    """
    if annual_plan is None:
        attempt = resolve_year_attempt(
            membership=monthly_decision.membership,
            scenario_year=monthly_decision.scenario_year,
            attempt=monthly_decision.attempt,
        )

        annual_plan = (
            AnnualPlan.objects
            .select_related(
                "membership",
                "scenario_year",
                "attempt",
            )
            .get(
                membership=(
                    monthly_decision.membership
                ),
                scenario_year=(
                    monthly_decision.scenario_year
                ),
                **attempt_filter(
                    attempt,
                ),
            )
        )

    product_lines = list(
        monthly_decision
        .product_decisions
        .select_related(
            "product"
        )
        .all()
    )

    if not product_lines:
        raise ValidationError(
            "No product decisions were submitted for this month."
        )

    price_errors = []

    for line in product_lines:
        selling_price = to_decimal(
            line.selling_price
        )

        if selling_price < ZERO:
            price_errors.append(
                (
                    f"{line.product.name}: selling price cannot be negative."
                )
            )
            continue

        maximum_price = (
            maximum_price_before_zero_demand(
                line.product
            )
        )

        if selling_price > maximum_price:
            price_errors.append(
                (
                    f"{line.product.name}: the proposed selling price "
                    f"of ${selling_price:,.2f} exceeds the feasible "
                    f"maximum of ${maximum_price:,.2f}. At the maximum "
                    "price, modeled demand reaches zero. Reduce the price "
                    "before finalizing the monthly S&OP plan."
                )
            )

    if price_errors:
        raise ValidationError(
            price_errors
        )

    replenishment_by_product = {
        line.product_id: (
            line.replenishment_units
        )
        for line in product_lines
    }

    guardrail = (
        monthly_ordering_guardrail_status(
            annual_plan=annual_plan,
            scenario_year=(
                monthly_decision.scenario_year
            ),
        )
    )

    proposed_spend = (
        proposed_replenishment_spend(
            annual_plan=annual_plan,
            scenario_year=(
                monthly_decision.scenario_year
            ),
            replenishment_by_product=(
                replenishment_by_product
            ),
        )
    )

    proposed_total_spend = to_decimal(
        proposed_spend[
            "total_spend"
        ]
    )

    remaining_allowed_spend = to_decimal(
        guardrail[
            "remaining_allowed_spend"
        ]
    )

    if (
        guardrail[
            "ceiling_reached_or_exceeded"
        ]
        and proposed_spend[
            "total_units"
        ] > 0
    ):
        raise ValidationError(
            (
                "The variable-cost production ceiling has already been "
                "reached or exceeded. No additional production can be "
                "ordered this month. Set all replenishment quantities "
                "to zero before continuing."
            )
        )

    if (
        proposed_total_spend
        > remaining_allowed_spend
    ):
        raise ValidationError(
            (
                "The proposed replenishment exceeds the remaining "
                "variable-cost production allowance. Only "
                f"${remaining_allowed_spend:,.2f} of additional variable "
                "production cost remains before the 180% ceiling is "
                "reached. Reduce replenishment quantities and submit "
                "the plan again."
            )
        )

    return {
        "guardrail": guardrail,
        "proposed_spend": (
            proposed_spend
        ),
        "is_valid": True,
    }


# ---------------------------------------------------------------------
# Fiscal-month helpers
# ---------------------------------------------------------------------

def previous_fiscal_month(month):
    """
    Return the preceding simulation month.

    September has no preceding sales month because August is the annual
    planning month.
    """
    position = FISCAL_MONTH_POSITION.get(
        month
    )

    if position is None:
        raise ValueError(
            f"Invalid fiscal month: {month}"
        )

    if position == 0:
        return None

    return FISCAL_MONTH_NUMBERS[
        position - 1
    ]


def next_fiscal_month(month):
    """
    Return the following month in the September-August sequence.

    August returns None because it is the final month of the year.
    """
    position = FISCAL_MONTH_POSITION.get(
        month
    )

    if position is None:
        raise ValueError(
            f"Invalid fiscal month: {month}"
        )

    next_position = position + 1

    if next_position >= len(
        FISCAL_MONTH_NUMBERS
    ):
        return None

    return FISCAL_MONTH_NUMBERS[
        next_position
    ]


# ---------------------------------------------------------------------
# Inventory carryover
# ---------------------------------------------------------------------

def beginning_inventory_for_product(
    *,
    membership,
    scenario_year,
    month,
    product,
    attempt=None,
):
    """
    Return the beginning inventory for one product.

    September:
        Uses the initial scenario inventory, normally 100 units.

    October-August:
        Uses the previous month's ending inventory.
    """
    attempt = resolve_year_attempt(
        membership=membership,
        scenario_year=scenario_year,
        attempt=attempt,
    )

    previous_month = previous_fiscal_month(
        month
    )

    if previous_month is None:
        return (
            scenario_year.initial_inventory_units
        )

    previous_line = (
        ProductDecision.objects.filter(
            monthly_decision__membership=(
                membership
            ),
            monthly_decision__scenario_year=(
                scenario_year
            ),
            monthly_decision__month=(
                previous_month
            ),
            monthly_decision__is_finalized=True,
            product=product,
            **attempt_filter(
                attempt,
                prefix="monthly_decision__",
            ),
        )
        .only("ending_inventory_units")
        .first()
    )

    if previous_line is None:
        raise ValidationError(
            (
                f"A finalized prior-month result is required "
                f"before planning {product.name} for the next "
                f"month."
            )
        )

    return (
        previous_line.ending_inventory_units
    )


def beginning_inventory_by_product(
    *,
    membership,
    scenario_year,
    month,
    products,
    attempt=None,
):
    """
    Return beginning inventory indexed by product ID.
    """
    return {
        product.id: (
            beginning_inventory_for_product(
                membership=membership,
                scenario_year=scenario_year,
                month=month,
                product=product,
                attempt=attempt,
            )
        )
        for product in products
    }


# ---------------------------------------------------------------------
# Monthly simulation
# ---------------------------------------------------------------------

@transaction.atomic
def run_month(
    monthly_decision,
):
    """
    Finalize one simulation month.

    Demand realization
    ------------------
    True demand
    × maintenance / quality multiplier
    × symmetric price-elasticity multiplier
    = Realized demand

    Price elasticity
    ----------------
    A 1% price change causes a 2% demand change
    in the opposite direction.

    Fixed-cost absorption
    ---------------------
    The Annual Operating Plan establishes:

        - Annual forecast volume
        - Annual fixed-cost pool
        - Fixed cost per forecast unit

    The fixed manufacturing cost is absorbed through COGS only
    until cumulative labor and maintenance absorption reaches the
    annual AOP fixed-cost budget.

    For units sold within the forecast-volume ceiling:

        COGS
        = Variable Unit Cost
        + Labor Fixed Cost per Unit
        + Maintenance Fixed Cost per Unit

    Once the annual fixed-cost budget is fully absorbed:

        COGS
        = Variable Unit Cost only

    August manufacturing overhead
    -----------------------------
    If annual sales finish below the AOP forecast volume:

        Manufacturing Overhead
        = Remaining Unabsorbed Annual Fixed-Cost Budget

    This amount is recognized once, in August.

    Inventory write-off
    -------------------
    Ending inventory in August has zero salvage value:

        Inventory Write-Off
        = Ending Inventory × Total Production Cost per Unit

    Holding cost
    ------------
    Holding cost remains an operational inventory metric.
    It is not included in accounting profit.

    Accounting operating expense
    ----------------------------
    Cost of Goods Sold
    + Manufacturing Overhead
    + Inventory Write-Off
    """

    if monthly_decision.is_finalized:
        raise ValidationError(
            "This month has already been finalized."
        )

    attempt = resolve_year_attempt(
        membership=monthly_decision.membership,
        scenario_year=monthly_decision.scenario_year,
        attempt=monthly_decision.attempt,
    )

    annual_plan = (
        AnnualPlan.objects
        .select_related(
            "scenario_year",
            "membership",
            "attempt",
        )
        .get(
            membership=(
                monthly_decision.membership
            ),
            scenario_year=(
                monthly_decision.scenario_year
            ),
            **attempt_filter(
                attempt,
            ),
        )
    )

    scenario_year = (
        monthly_decision.scenario_year
    )

    # Defensive hard guardrails. Forms should validate these rules
    # before creating/finalizing the decision, but run_month() also
    # enforces them so the simulation cannot be finalized by bypassing
    # the user-interface validation.
    validate_monthly_decision_guardrails(
        monthly_decision=monthly_decision,
        annual_plan=annual_plan,
    )

    interest_rate = (
        scenario_year.monthly_interest_rate
    )

    is_terminal_month = (
        monthly_decision.month == 8
    )

    if FISCAL_MONTH_NUMBERS[-1] != 8:
        raise ValidationError(
            "The fiscal-month sequence must end in August."
        )

    absorption_status = (
        fixed_cost_absorption_status(
            annual_plan=annual_plan,
            scenario_year=scenario_year,
            attempt=attempt,
        )
    )

    cost_parameters = (
        absorption_status[
            "cost_parameters"
        ]
    )

    annual_forecast_volume = (
        absorption_status[
            "annual_forecast_volume"
        ]
    )

    labor_fixed_cost_per_unit = (
        cost_parameters[
            "labor_fixed_cost_per_unit"
        ]
    )

    maintenance_fixed_cost_per_unit = (
        cost_parameters[
            "maintenance_fixed_cost_per_unit"
        ]
    )

    effective_maintenance_cost_per_unit = (
        ZERO
        if annual_plan.skip_maintenance
        else maintenance_fixed_cost_per_unit
    )

    effective_fixed_cost_per_unit = (
        labor_fixed_cost_per_unit
        + effective_maintenance_cost_per_unit
    )

    # ----------------------------------------------------------
    # Units sold before this month.
    #
    # Fixed-cost absorption is controlled at the annual company
    # level because annual_cost_parameters() calculates one common
    # fixed-cost-per-unit rate across the annual forecast volume.
    # ----------------------------------------------------------

    remaining_fixed_cost_budget = (
        absorption_status[
            "remaining_fixed_cost_budget"
        ]
    )

    product_lines = list(
        monthly_decision
        .product_decisions
        .select_related(
            "product"
        )
        .select_for_update()
        .order_by(
            "product_id"
        )
    )

    if not product_lines:
        raise ValidationError(
            "No product decisions were submitted "
            "for this month."
        )

    for line in product_lines:
        product = (
            line.product
        )

        demand_row = (
            DemandValue.objects.get(
                scenario_year=scenario_year,
                product=product,
                month=(
                    monthly_decision.month
                ),
            )
        )

        beginning_inventory_units = (
            beginning_inventory_for_product(
                membership=(
                    monthly_decision.membership
                ),
                scenario_year=(
                    scenario_year
                ),
                month=(
                    monthly_decision.month
                ),
                product=product,
                attempt=attempt,
            )
        )

        replenishment_units = (
            line.replenishment_units
        )

        available_inventory_units = (
            beginning_inventory_units
            + replenishment_units
        )

        adjusted_demand = Decimal(
            demand_row.ideal_demand
        )

        adjusted_demand *= (
            annual_plan
            .quality_capacity_multiplier
        )

        adjusted_demand *= (
            demand_multiplier(
                price=(
                    line.selling_price
                ),
                standard_price=(
                    product.standard_price
                ),
            )
        )

        adjusted_demand_units = max(
            0,
            int(
                adjusted_demand.quantize(
                    WHOLE_UNIT,
                    rounding=ROUND_HALF_UP,
                )
            ),
        )

        units_sold = min(
            available_inventory_units,
            adjusted_demand_units,
        )

        ending_inventory_units = max(
            0,
            (
                available_inventory_units
                - units_sold
            ),
        )

        lost_sales_units = max(
            0,
            (
                adjusted_demand_units
                - available_inventory_units
            ),
        )

        # ------------------------------------------------------
        # Revenue
        # ------------------------------------------------------

        revenue = (
            Decimal(
                units_sold
            )
            * line.selling_price
        )

        # ------------------------------------------------------
        # Fixed-cost absorption
        #
        # Fixed cost is absorbed against the remaining annual DOLLAR
        # budget, not against a calendar month.
        # ------------------------------------------------------

        variable_cogs = (
            Decimal(
                units_sold
            )
            * product.variable_unit_cost
        )

        potential_fixed_cost = (
            Decimal(
                units_sold
            )
            * effective_fixed_cost_per_unit
        )

        fixed_cost_to_absorb = min(
            remaining_fixed_cost_budget,
            potential_fixed_cost,
        )

        if (
            effective_fixed_cost_per_unit > ZERO
            and fixed_cost_to_absorb > ZERO
        ):
            labor_share_of_effective_fixed_cost = (
                labor_fixed_cost_per_unit
                / effective_fixed_cost_per_unit
            )

            maintenance_share_of_effective_fixed_cost = (
                effective_maintenance_cost_per_unit
                / effective_fixed_cost_per_unit
            )

            labor_cogs = (
                fixed_cost_to_absorb
                * labor_share_of_effective_fixed_cost
            )

            maintenance_cogs = (
                fixed_cost_to_absorb
                * maintenance_share_of_effective_fixed_cost
            )
        else:
            labor_cogs = ZERO
            maintenance_cogs = ZERO

        remaining_fixed_cost_budget = max(
            ZERO,
            (
                remaining_fixed_cost_budget
                - fixed_cost_to_absorb
            ),
        )

        total_cogs = (
            variable_cogs
            + labor_cogs
            + maintenance_cogs
        )

        # ------------------------------------------------------
        # Holding cost
        #
        # August inventory is written off rather than carried
        # into another month, so no August holding charge is
        # applied.
        # ------------------------------------------------------

        if is_terminal_month:
            holding_cost = ZERO
        else:
            holding_cost = (
                Decimal(
                    ending_inventory_units
                )
                * product.variable_unit_cost
                * interest_rate
            )

        # ------------------------------------------------------
        # August inventory write-off
        # ------------------------------------------------------

        total_unit_production_cost = (
            product.variable_unit_cost
            + effective_fixed_cost_per_unit
        )

        if is_terminal_month:
            inventory_write_off = (
                Decimal(
                    ending_inventory_units
                )
                * total_unit_production_cost
            )
        else:
            inventory_write_off = ZERO

        # ------------------------------------------------------
        # Sales credit
        # ------------------------------------------------------

        unit_sales_credit_collected = (
            sales_credit_per_unit(
                price=(
                    line.selling_price
                ),
                standard_price=(
                    product.standard_price
                ),
                sales_credit_rate=(
                    annual_plan
                    .sales_credit_rate
                ),
            )
        )

        sales_credit_collected = (
            Decimal(
                units_sold
            )
            * unit_sales_credit_collected
        )

        # ------------------------------------------------------
        # Persist product result
        # ------------------------------------------------------

        line.beginning_inventory_units = (
            beginning_inventory_units
        )

        line.available_inventory_units = (
            available_inventory_units
        )

        line.true_demand_units = (
            demand_row.ideal_demand
        )

        line.adjusted_demand_units = (
            adjusted_demand_units
        )

        line.units_sold = (
            units_sold
        )

        line.ending_inventory_units = (
            ending_inventory_units
        )

        line.lost_sales_units = (
            lost_sales_units
        )

        line.revenue = qmoney(
            revenue
        )

        # Existing field names are retained for compatibility.
        # These fields now represent the accounting COGS components.
        line.variable_production_cost = qmoney(
            variable_cogs
        )

        line.labor_fixed_cost = qmoney(
            labor_cogs
        )

        line.maintenance_fixed_cost = qmoney(
            maintenance_cogs
        )

        line.total_production_cost = qmoney(
            total_cogs
        )

        line.holding_cost = qmoney(
            holding_cost
        )

        line.inventory_write_off = qmoney(
            inventory_write_off
        )

        line.sales_credit_collected = qmoney(
            sales_credit_collected
        )

        line.save(
            update_fields=[
                "beginning_inventory_units",
                "replenishment_units",
                "available_inventory_units",
                "true_demand_units",
                "adjusted_demand_units",
                "units_sold",
                "ending_inventory_units",
                "lost_sales_units",
                "revenue",
                "variable_production_cost",
                "labor_fixed_cost",
                "maintenance_fixed_cost",
                "total_production_cost",
                "holding_cost",
                "inventory_write_off",
                "sales_credit_collected",
            ]
        )

    # ----------------------------------------------------------
    # Manufacturing overhead
    #
    # This is a monthly-decision-level amount because it is the
    # remaining unabsorbed annual fixed manufacturing cost, not a
    # product-specific expense.
    # ----------------------------------------------------------

    if is_terminal_month:
        manufacturing_overhead = (
            remaining_fixed_cost_budget
        )
    else:
        manufacturing_overhead = ZERO

    monthly_decision.manufacturing_overhead = (
        qmoney(
            manufacturing_overhead
        )
    )

    monthly_decision.is_finalized = True

    monthly_decision.save(
        update_fields=[
            "manufacturing_overhead",
            "is_finalized",
        ]
    )

    return refresh_year_score(
        membership=(
            monthly_decision.membership
        ),
        scenario_year=(
            scenario_year
        ),
        attempt=attempt,
    )


# ---------------------------------------------------------------------
# Annual score and financial performance
# ---------------------------------------------------------------------

def complete_year_attempt_if_ready(
    *,
    membership,
    scenario_year,
    attempt=None,
):
    """
    Mark an attempt complete once all twelve fiscal months are finalized.

    Legacy records have no YearAttempt and are left unchanged.
    """
    attempt = resolve_year_attempt(
        membership=membership,
        scenario_year=scenario_year,
        attempt=attempt,
    )

    if attempt is None:
        return None

    finalized_months = set(
        MonthlyDecision.objects
        .filter(
            membership=membership,
            scenario_year=scenario_year,
            is_finalized=True,
            **attempt_filter(
                attempt,
            ),
        )
        .values_list(
            "month",
            flat=True,
        )
    )

    if finalized_months != set(
        FISCAL_MONTH_NUMBERS
    ):
        return attempt

    update_fields = []

    if (
        attempt.status
        != YearAttempt.Status.COMPLETE
    ):
        attempt.status = (
            YearAttempt.Status.COMPLETE
        )
        update_fields.append("status")

    if attempt.completed_at is None:
        attempt.completed_at = timezone.now()
        update_fields.append("completed_at")

    if update_fields:
        attempt.save(
            update_fields=update_fields
        )

    return attempt


def refresh_year_score(
    membership,
    scenario_year,
    attempt=None,
):
    """
    Recalculate cumulative financial performance.

    Accounting operating expense:

        Cost of Goods Sold
        + Manufacturing Overhead
        + Inventory Write-Off

    Holding cost remains a separate operational inventory metric
    and is not deducted again from accounting profit.
    """
    attempt = resolve_year_attempt(
        membership=membership,
        scenario_year=scenario_year,
        attempt=attempt,
    )

    finalized_lines = (
        ProductDecision.objects.filter(
            monthly_decision__membership=(
                membership
            ),
            monthly_decision__scenario_year=(
                scenario_year
            ),
            monthly_decision__is_finalized=True,
            **attempt_filter(
                attempt,
                prefix="monthly_decision__",
            ),
        )
    )

    totals = finalized_lines.aggregate(
        revenue=Sum(
            "revenue"
        ),
        cogs=Sum(
            "total_production_cost"
        ),
        inventory_write_off=Sum(
            "inventory_write_off"
        ),
        holding_cost=Sum(
            "holding_cost"
        ),
        sales_credit=Sum(
            "sales_credit_collected"
        ),
        lost_sales=Sum(
            "lost_sales_units"
        ),
    )

    manufacturing_overhead = (
        MonthlyDecision.objects
        .filter(
            membership=membership,
            scenario_year=scenario_year,
            is_finalized=True,
            **attempt_filter(
                attempt,
            ),
        )
        .aggregate(
            total=Sum(
                "manufacturing_overhead"
            )
        )[
            "total"
        ]
        or ZERO
    )

    revenue = (
        totals["revenue"]
        or ZERO
    )

    cogs = (
        totals["cogs"]
        or ZERO
    )

    inventory_write_off = (
        totals[
            "inventory_write_off"
        ]
        or ZERO
    )

    holding_cost = (
        totals["holding_cost"]
        or ZERO
    )

    sales_credit_collected = (
        totals["sales_credit"]
        or ZERO
    )

    lost_sales_units = (
        totals["lost_sales"]
        or 0
    )

    operating_expense = (
        cogs
        + manufacturing_overhead
        + inventory_write_off
    )

    profit = (
        revenue
        - operating_expense
    )

    ending_inventory_value = (
        get_latest_inventory_value(
            membership=membership,
            scenario_year=scenario_year,
            attempt=attempt,
        )
    )

    annual_plan = (
        AnnualPlan.objects.get(
            membership=membership,
            scenario_year=scenario_year,
            **attempt_filter(
                attempt,
            ),
        )
    )

    revenue_attainment = (
        calculate_revenue_attainment(
            revenue=revenue,
            revenue_target=(
                annual_plan
                .effective_revenue_target
            ),
        )
    )

    budget_attainment = (
        calculate_budget_attainment(
            operating_expense=(
                operating_expense
            ),
            operating_budget=(
                annual_plan
                .effective_operating_budget
            ),
        )
    )

    service_penalty = (
        Decimal(
            lost_sales_units
        )
        * Decimal("0.02")
    )

    inventory_penalty = (
        ending_inventory_value
        / Decimal("100000")
    )

    score = max(
        ZERO,
        (
            revenue_attainment
            * Decimal("70")
        )
        + (
            budget_attainment
            * Decimal("30")
        )
        - service_penalty
        - inventory_penalty,
    )

    score_lookup = {
        "membership": membership,
        "scenario_year": scenario_year,
    }

    if attempt is None:
        score_lookup["attempt__isnull"] = True
    else:
        score_lookup["attempt"] = attempt

    player_score, _ = (
        PlayerYearScore.objects
        .update_or_create(
            **score_lookup,
            defaults={
                "membership": membership,
                "scenario_year": scenario_year,
                "attempt": attempt,
                "revenue": qmoney(
                    revenue
                ),
                "operating_expense": qmoney(
                    operating_expense
                ),
                "profit": qmoney(
                    profit
                ),
                "sales_credit_collected": qmoney(
                    sales_credit_collected
                ),
                "lost_sales_units": (
                    lost_sales_units
                ),
                "ending_inventory_value": qmoney(
                    ending_inventory_value
                ),
                "score": qmoney(
                    score
                ),
            },
        )
    )

    complete_year_attempt_if_ready(
        membership=membership,
        scenario_year=scenario_year,
        attempt=attempt,
    )

    return player_score


def get_latest_inventory_value(
    membership,
    scenario_year,
    attempt=None,
):
    """
    Return the latest ending inventory valued at variable cost.
    """
    attempt = resolve_year_attempt(
        membership=membership,
        scenario_year=scenario_year,
        attempt=attempt,
    )

    finalized_decisions = (
        MonthlyDecision.objects.filter(
            membership=membership,
            scenario_year=scenario_year,
            is_finalized=True,
            **attempt_filter(
                attempt,
            ),
        )
        .prefetch_related(
            "product_decisions__product"
        )
    )

    latest_decision = max(
        finalized_decisions,
        key=lambda decision: (
            FISCAL_MONTH_POSITION[
                decision.month
            ]
        ),
        default=None,
    )

    if latest_decision is None:
        return ZERO

    inventory_value = ZERO

    for line in (
        latest_decision
        .product_decisions
        .all()
    ):
        inventory_value += (
            Decimal(
                line.ending_inventory_units
            )
            * line.product.variable_unit_cost
        )

    return inventory_value


def calculate_revenue_attainment(
    revenue,
    revenue_target,
):
    """
    Calculate revenue-target attainment, capped at 120%.
    """
    revenue = to_decimal(
        revenue
    )

    revenue_target = to_decimal(
        revenue_target
    )

    if revenue_target <= ZERO:
        return ZERO

    return min(
        Decimal("1.20"),
        revenue / revenue_target,
    )


def calculate_budget_attainment(
    operating_expense,
    operating_budget,
):
    """
    Calculate budget performance.

    Full attainment is earned while cumulative operating expense remains
    at or below the annual budget.
    """
    operating_expense = to_decimal(
        operating_expense
    )

    operating_budget = to_decimal(
        operating_budget
    )

    if operating_budget <= ZERO:
        return ZERO

    budget_overrun = max(
        ZERO,
        (
            operating_expense
            - operating_budget
        ),
    )

    return max(
        ZERO,
        (
            ONE
            - (
                budget_overrun
                / operating_budget
            )
        ),
    )


def year_end_review_analysis(
    *,
    membership,
    scenario_year,
    attempt=None,
):
    """
    Build the complete year-end board-review analysis.

    The review is divided among three board members.

    Mrs Clarke
    -----------
    Reviews:
        - Total revenue achieved
        - Revenue target
        - Total expense
          (COGS + manufacturing overhead + inventory write-off)
        - Total budget

    Reward rule:
        1. Revenue must be within 0.2% of the annual revenue target:
               Revenue >= 99.8% of Revenue Target
        2. Total expense must not exceed 105% of the annual budget.

        The official AOP revenue target and operating budget remain
        unchanged. These tolerances are used only for the year-end
        Board Reward evaluation.

    Mr Daniels
    -----------
    Reviews:
        - Selected sales-credit policy
        - Sales credit collected
        - Sales credit target

    Reward rule:
        1. The maximum-flexibility 20% sales-credit policy must
           have been selected.
        2. Sales credit collected must be within 2% of the annual
           sales-credit target:
               Sales Credit >= 98% of Sales Credit Target

        The official sales-credit target remains unchanged.
        The tolerance is used only for the year-end Board Reward
        evaluation.

    Ms Nauta
    --------
    Reviews four operating inefficiencies:
        - Cost of carrying inventory
        - Cost of lost sales
        - Inventory write-off
        - Manufacturing overhead

    Cost of carrying inventory:
        Total inventory holding cost incurred during the year.

    Cost of lost sales:
        Lost units × (Selling price - Variable unit cost)

    Inventory write-off:
        August ending inventory written off because the product line
        is upgraded for the next fiscal year and remaining bikes have
        almost no salvage value.

    Manufacturing overhead:
        Remaining unabsorbed annual fixed-cost budget recognized at
        the August close.

    Reward rule:
        Total operating inefficiency must be strictly less than
        10% of the committed AOP operating budget.

    Additional conditional commentary:
        - If the annual fixed-cost budget becomes fully absorbed during
          the fiscal year, report the exact month in which that happened.
        - If the annual fixed-cost budget is not fully absorbed by year end,
          explain the resulting manufacturing overhead.
        - If a year-end inventory write-off occurs, explain the
          near-zero salvage-value consequence.
    """

    attempt = resolve_year_attempt(
        membership=membership,
        scenario_year=scenario_year,
        attempt=attempt,
    )

    # --------------------------------------------------------------
    # Annual policy
    # --------------------------------------------------------------

    annual_plan = (
        AnnualPlan.objects
        .select_related(
            "membership",
            "scenario_year",
            "attempt",
        )
        .get(
            membership=membership,
            scenario_year=scenario_year,
            **attempt_filter(
                attempt,
            ),
        )
    )

    # --------------------------------------------------------------
    # Require a complete fiscal year
    # --------------------------------------------------------------

    finalized_decisions = list(
        MonthlyDecision.objects
        .filter(
            membership=membership,
            scenario_year=scenario_year,
            is_finalized=True,
            **attempt_filter(
                attempt,
            ),
        )
        .prefetch_related(
            "product_decisions__product"
        )
    )

    finalized_decisions.sort(
        key=lambda decision: (
            FISCAL_MONTH_POSITION[
                decision.month
            ]
        )
    )

    finalized_months = {
        decision.month
        for decision in finalized_decisions
    }

    missing_months = [
        month
        for month in FISCAL_MONTH_NUMBERS
        if month not in finalized_months
    ]

    if missing_months:
        raise ValidationError(
            (
                "The year-end board review is available only "
                "after all monthly decisions have been finalized."
            )
        )

    # --------------------------------------------------------------
    # Accumulators
    # --------------------------------------------------------------

    total_revenue = ZERO

    total_production_cost = ZERO

    total_holding_cost = ZERO

    total_inventory_write_off = ZERO

    total_manufacturing_overhead = ZERO

    total_sales_credit_collected = ZERO

    cost_of_lost_sales = ZERO

    cost_parameters = (
        annual_cost_parameters(
            scenario_year=scenario_year
        )
    )

    annual_forecast_volume = int(
        cost_parameters[
            "total_forecast_units"
        ]
    )

    labor_fixed_cost_per_unit = (
        cost_parameters[
            "labor_fixed_cost_per_unit"
        ]
    )

    maintenance_fixed_cost_per_unit = (
        cost_parameters[
            "maintenance_fixed_cost_per_unit"
        ]
    )

    effective_maintenance_cost_per_unit = (
        ZERO
        if annual_plan.skip_maintenance
        else maintenance_fixed_cost_per_unit
    )

    effective_fixed_cost_per_unit = (
        labor_fixed_cost_per_unit
        + effective_maintenance_cost_per_unit
    )

    effective_fixed_cost_budget = (
        Decimal(
            annual_forecast_volume
        )
        * effective_fixed_cost_per_unit
    )

    cumulative_units_sold = 0
    cumulative_fixed_cost_absorbed = ZERO
    fixed_cost_fully_absorbed_month = None

    # --------------------------------------------------------------
    # Process all finalized monthly product results
    # --------------------------------------------------------------

    for decision in finalized_decisions:

        total_manufacturing_overhead += (
            to_decimal(
                decision.manufacturing_overhead
            )
        )

        month_units_sold = 0
        month_fixed_cost_absorbed = ZERO

        for line in (
            decision
            .product_decisions
            .all()
        ):
            total_revenue += to_decimal(
                line.revenue
            )

            month_units_sold += int(
                line.units_sold
            )

            total_production_cost += to_decimal(
                line.total_production_cost
            )

            month_fixed_cost_absorbed += (
                to_decimal(
                    line.labor_fixed_cost
                )
                + to_decimal(
                    line.maintenance_fixed_cost
                )
            )

            total_holding_cost += to_decimal(
                line.holding_cost
            )

            total_inventory_write_off += to_decimal(
                line.inventory_write_off
            )

            total_sales_credit_collected += (
                to_decimal(
                    line.sales_credit_collected
                )
            )

            contribution_per_lost_unit = max(
                ZERO,
                (
                    to_decimal(
                        line.selling_price
                    )
                    - to_decimal(
                        line.product.variable_unit_cost
                    )
                ),
            )

            cost_of_lost_sales += (
                Decimal(
                    line.lost_sales_units
                )
                * contribution_per_lost_unit
            )

        cumulative_units_sold += (
            month_units_sold
        )

        cumulative_fixed_cost_absorbed += (
            month_fixed_cost_absorbed
        )

        if (
            fixed_cost_fully_absorbed_month is None
            and cumulative_fixed_cost_absorbed
            >= (
                effective_fixed_cost_budget
                - MONEY
            )
        ):
            fixed_cost_fully_absorbed_month = (
                decision.month
            )

    annual_forecast_volume_reached = (
        cumulative_units_sold
        >= annual_forecast_volume
    )

    fixed_cost_fully_absorbed_during_year = (
        fixed_cost_fully_absorbed_month
        is not None
    )

    fiscal_month_names = {
        9: "September",
        10: "October",
        11: "November",
        12: "December",
        1: "January",
        2: "February",
        3: "March",
        4: "April",
        5: "May",
        6: "June",
        7: "July",
        8: "August",
    }

    fixed_cost_fully_absorbed_month_name = (
        fiscal_month_names.get(
            fixed_cost_fully_absorbed_month
        )
        if fixed_cost_fully_absorbed_month
        is not None
        else None
    )

    manufacturing_overhead_incurred = (
        total_manufacturing_overhead
        > ZERO
    )

    inventory_write_off_incurred = (
        total_inventory_write_off
        > ZERO
    )

    # --------------------------------------------------------------
    # Financial results
    # --------------------------------------------------------------

    total_expense = (
        total_production_cost
        + total_manufacturing_overhead
        + total_inventory_write_off
    )

    revenue_target = to_decimal(
        annual_plan.effective_revenue_target
    )

    total_budget = to_decimal(
        annual_plan
        .effective_operating_budget
    )

    # --------------------------------------------------------------
    # Mrs Clarke year-end evaluation thresholds
    #
    # Official AOP targets remain unchanged.
    # These thresholds are used only to determine the Board Reward.
    # --------------------------------------------------------------

    revenue_reward_floor = (
        revenue_target
        * (
            ONE
            - YEAR_END_REVENUE_TOLERANCE
        )
    )

    budget_ceiling = (
        total_budget
        * (
            ONE
            + YEAR_END_BUDGET_TOLERANCE
        )
    )

    # --------------------------------------------------------------
    # Sales-credit policy and target
    #
    # Example:
    #
    # Revenue target = $1,000,000
    #
    # 5% policy:
    #     sales-credit target = $50,000
    #
    # 10% policy:
    #     sales-credit target = $100,000
    #
    # 20% policy:
    #     sales-credit target = $200,000
    #
    # Mr Daniels' Board Reward additionally requires the
    # maximum-flexibility 20% policy.
    # --------------------------------------------------------------

    sales_credit_rate = to_decimal(
        annual_plan.sales_credit_rate
    )

    sales_credit_target = (
        revenue_target
        * sales_credit_rate
    )

    # --------------------------------------------------------------
    # Mr Daniels year-end evaluation threshold
    #
    # Official sales-credit target remains unchanged.
    # The 2% tolerance applies only to the Board Reward.
    # --------------------------------------------------------------

    sales_credit_reward_floor = (
        sales_credit_target
        * (
            ONE
            - YEAR_END_SALES_CREDIT_TOLERANCE
        )
    )

    maximum_flexibility_rate = (
        Decimal("0.20")
    )

    maximum_flexibility_selected = (
        sales_credit_rate
        == maximum_flexibility_rate
    )

    # --------------------------------------------------------------
    # Ms Nauta:
    # operating inefficiency
    #
    # Four dimensions:
    #   1. Cost of carrying inventory
    #   2. Cost of lost sales
    #   3. Inventory write-off
    #   4. Manufacturing overhead
    # --------------------------------------------------------------

    cost_of_carrying_inventory = (
        total_holding_cost
    )

    inventory_write_off_cost = (
        total_inventory_write_off
    )

    manufacturing_overhead_cost = (
        total_manufacturing_overhead
    )

    operating_inefficiency_limit = (
        total_budget
        * Decimal("0.10")
    )

    total_operating_inefficiency = (
        cost_of_carrying_inventory
        + cost_of_lost_sales
        + inventory_write_off_cost
        + manufacturing_overhead_cost
    )

    # --------------------------------------------------------------
    # Mrs Clarke:
    # financial reward decision
    # --------------------------------------------------------------

    revenue_target_achieved = (
        total_revenue
        >= revenue_target
    )

    revenue_within_tolerance = (
        total_revenue
        >= revenue_reward_floor
    )

    budget_within_tolerance = (
        total_expense
        <= budget_ceiling
    )

    clarke_reward = (
        revenue_within_tolerance
        and budget_within_tolerance
    )

    # --------------------------------------------------------------
    # Mr Daniels:
    # commercial reward decision
    #
    # BOTH conditions are required:
    #
    # 1. Maximum-flexibility 20% policy selected.
    # 2. Sales-credit target achieved.
    # --------------------------------------------------------------

    sales_credit_target_achieved = (
        total_sales_credit_collected
        >= sales_credit_target
    )

    sales_credit_within_tolerance = (
        total_sales_credit_collected
        >= sales_credit_reward_floor
    )

    daniels_reward = (
        maximum_flexibility_selected
        and sales_credit_within_tolerance
    )

    # --------------------------------------------------------------
    # Ms Nauta:
    # operations reward decision — 10% of AOP budget
    # --------------------------------------------------------------

    operating_inefficiency_within_limit = (
        total_operating_inefficiency
        < operating_inefficiency_limit
    )

    nauta_reward = (
        operating_inefficiency_within_limit
    )

    # --------------------------------------------------------------
    # Variances for boardroom display
    # --------------------------------------------------------------

    revenue_variance = (
        total_revenue
        - revenue_target
    )

    budget_variance = (
        total_budget
        - total_expense
    )

    sales_credit_variance = (
        total_sales_credit_collected
        - sales_credit_target
    )

    revenue_reward_margin = (
        total_revenue
        - revenue_reward_floor
    )

    sales_credit_reward_margin = (
        total_sales_credit_collected
        - sales_credit_reward_floor
    )

    operating_inefficiency_variance = (
        operating_inefficiency_limit
        - total_operating_inefficiency
    )

    # --------------------------------------------------------------
    # Number of board rewards earned
    # --------------------------------------------------------------

    rewards_earned = sum([
        int(clarke_reward),
        int(daniels_reward),
        int(nauta_reward),
    ])

    # --------------------------------------------------------------
    # Template-ready result
    # --------------------------------------------------------------

    return {

        # ==========================================================
        # Mrs Clarke — Financial Results
        # ==========================================================

        "clarke": {

            "total_revenue": qmoney(
                total_revenue
            ),

            "revenue_target": qmoney(
                revenue_target
            ),

            "revenue_variance": qmoney(
                revenue_variance
            ),

            "total_expense": qmoney(
                total_expense
            ),

            "manufacturing_overhead": qmoney(
                total_manufacturing_overhead
            ),

            "inventory_write_off": qmoney(
                total_inventory_write_off
            ),

            "total_budget": qmoney(
                total_budget
            ),

            "budget_ceiling": qmoney(
                budget_ceiling
            ),

            "budget_variance": qmoney(
                budget_variance
            ),

            "revenue_tolerance_rate": (
                YEAR_END_REVENUE_TOLERANCE
            ),

            "revenue_tolerance_percent": (
                YEAR_END_REVENUE_TOLERANCE
                * Decimal("100")
            ),

            "revenue_reward_floor": qmoney(
                revenue_reward_floor
            ),

            "revenue_reward_margin": qmoney(
                revenue_reward_margin
            ),

            "budget_tolerance_rate": (
                YEAR_END_BUDGET_TOLERANCE
            ),

            "budget_tolerance_percent": (
                YEAR_END_BUDGET_TOLERANCE
                * Decimal("100")
            ),

            "revenue_target_achieved": (
                revenue_target_achieved
            ),

            "revenue_within_tolerance": (
                revenue_within_tolerance
            ),

            "budget_within_tolerance": (
                budget_within_tolerance
            ),

            "reward": (
                clarke_reward
            ),
        },

        # ==========================================================
        # Mr Daniels — Commercial Results
        # ==========================================================

        "daniels": {

            "sales_credit_rate": (
                sales_credit_rate
            ),

            "sales_credit_rate_percent": (
                sales_credit_rate
                * Decimal("100")
            ),

            "maximum_flexibility_rate": (
                maximum_flexibility_rate
            ),

            "maximum_flexibility_rate_percent": (
                maximum_flexibility_rate
                * Decimal("100")
            ),

            "maximum_flexibility_selected": (
                maximum_flexibility_selected
            ),

            "sales_credit_collected": qmoney(
                total_sales_credit_collected
            ),

            "sales_credit_target": qmoney(
                sales_credit_target
            ),

            "sales_credit_variance": qmoney(
                sales_credit_variance
            ),

            "sales_credit_tolerance_rate": (
                YEAR_END_SALES_CREDIT_TOLERANCE
            ),

            "sales_credit_tolerance_percent": (
                YEAR_END_SALES_CREDIT_TOLERANCE
                * Decimal("100")
            ),

            "sales_credit_reward_floor": qmoney(
                sales_credit_reward_floor
            ),

            "sales_credit_reward_margin": qmoney(
                sales_credit_reward_margin
            ),

            "target_achieved": (
                sales_credit_target_achieved
            ),

            "sales_credit_within_tolerance": (
                sales_credit_within_tolerance
            ),

            "reward": (
                daniels_reward
            ),
        },

        # ==========================================================
        # Ms Nauta — Operating Results
        # ==========================================================

        "nauta": {

            "annual_forecast_volume": (
                annual_forecast_volume
            ),

            "annual_units_sold": (
                cumulative_units_sold
            ),

            "annual_forecast_volume_reached": (
                annual_forecast_volume_reached
            ),

            "effective_fixed_cost_budget": qmoney(
                effective_fixed_cost_budget
            ),

            "cumulative_fixed_cost_absorbed": qmoney(
                cumulative_fixed_cost_absorbed
            ),

            "fixed_cost_fully_absorbed_during_year": (
                fixed_cost_fully_absorbed_during_year
            ),

            "fixed_cost_fully_absorbed_month": (
                fixed_cost_fully_absorbed_month
            ),

            "fixed_cost_fully_absorbed_month_name": (
                fixed_cost_fully_absorbed_month_name
            ),

            "manufacturing_overhead_incurred": (
                manufacturing_overhead_incurred
            ),

            "inventory_write_off_incurred": (
                inventory_write_off_incurred
            ),

            "cost_of_carrying_inventory": qmoney(
                cost_of_carrying_inventory
            ),

            "cost_of_lost_sales": qmoney(
                cost_of_lost_sales
            ),

            "inventory_write_off": qmoney(
                inventory_write_off_cost
            ),

            "manufacturing_overhead": qmoney(
                manufacturing_overhead_cost
            ),

            "total_operating_inefficiency": qmoney(
                total_operating_inefficiency
            ),

            "operating_inefficiency_limit": qmoney(
                operating_inefficiency_limit
            ),

            "operating_inefficiency_limit_percent": (
                Decimal("10")
            ),

            "operating_inefficiency_variance": qmoney(
                operating_inefficiency_variance
            ),

            "within_limit": (
                operating_inefficiency_within_limit
            ),

            "reward": (
                nauta_reward
            ),
        },

        # ==========================================================
        # Board-level summary
        # ==========================================================

        "rewards": {

            "clarke": (
                clarke_reward
            ),

            "daniels": (
                daniels_reward
            ),

            "nauta": (
                nauta_reward
            ),

            "earned": (
                rewards_earned
            ),

            "available": 3,
        },
    }


# ---------------------------------------------------------------------
# Rolling-horizon forecasts
# ---------------------------------------------------------------------

def current_forecasts(
    scenario_year,
    base_month,
):
    """
    Return rolling forecasts grouped by product.

    Forecasts are sorted in fiscal order from September through August.
    """
    rows = (
        ForecastSnapshot.objects.filter(
            scenario_year=scenario_year,
            base_month=base_month,
        )
        .select_related("product")
    )

    forecasts_by_product = {}

    for row in rows:
        forecasts_by_product.setdefault(
            row.product,
            [],
        ).append(row)

    for product_rows in (
        forecasts_by_product.values()
    ):
        product_rows.sort(
            key=lambda row: (
                FISCAL_MONTH_POSITION[
                    row.forecast_month
                ]
            )
        )

    return forecasts_by_product