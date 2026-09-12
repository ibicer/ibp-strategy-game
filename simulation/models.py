import uuid
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


# ---------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------

FISCAL_MONTHS = [
    (9, "September"),
    (10, "October"),
    (11, "November"),
    (12, "December"),
    (1, "January"),
    (2, "February"),
    (3, "March"),
    (4, "April"),
    (5, "May"),
    (6, "June"),
    (7, "July"),
    (8, "August"),
]

FISCAL_MONTH_NUMBERS = [
    month
    for month, _ in FISCAL_MONTHS
]

FISCAL_MONTH_POSITION = {
    month: position
    for position, month in enumerate(
        FISCAL_MONTH_NUMBERS
    )
}


# ---------------------------------------------------------------------
# Game and membership
# ---------------------------------------------------------------------

INSTRUCTOR_TITLE_CHOICES = [
    ("PRACTITIONER", "Practitioner"),
    ("EXECUTIVE_DIRECTOR", "Executive Director"),
    ("LECTURER", "Lecturer"),
    ("ASSISTANT_PROFESSOR", "Assistant Professor"),
    ("ASSOCIATE_PROFESSOR", "Associate Professor"),
    ("PROFESSOR", "Professor"),
    ("ADJUNCT_PROFESSOR", "Adjunct Professor"),
]


class InstructorProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="instructor_profile",
    )

    institution = models.CharField(
        max_length=180,
    )

    title = models.CharField(
        max_length=32,
        choices=INSTRUCTOR_TITLE_CHOICES,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        ordering = [
            "institution",
            "user__last_name",
            "user__first_name",
        ]

    @property
    def display_name(self):
        full_name = self.user.get_full_name().strip()
        return full_name or self.user.get_username()

    def __str__(self):
        return (
            f"{self.display_name} — "
            f"{self.institution}"
        )


class Game(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        OPEN = "OPEN", "Open for registration"
        ACTIVE = "ACTIVE", "Active"
        COMPLETE = "COMPLETE", "Complete"

    name = models.CharField(
        max_length=180,
    )

    code = models.SlugField(
        max_length=40,
        unique=True,
    )

    invite_token = models.UUIDField(
        null=True,
        blank=True,
        unique=True,
        editable=False,
        help_text=(
            "Private invitation token used to register players "
            "for this game."
        ),
    )

    description = models.TextField(
        blank=True,
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="games_created",
    )

    status = models.CharField(
        max_length=12,
        choices=Status.choices,
        default=Status.DRAFT,
    )

    current_year = models.PositiveSmallIntegerField(
        default=1,
        validators=[
            MinValueValidator(1),
            MaxValueValidator(3),
        ],
    )

    current_month = models.PositiveSmallIntegerField(
        choices=FISCAL_MONTHS,
        default=8,
        help_text=(
            "August represents the annual planning stage before "
            "September operations begin."
        ),
    )

    registration_deadline = models.DateTimeField(
        null=True,
        blank=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    users = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        through="GameMembership",
        related_name="strategy_games",
    )

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if self.invite_token is None:
            self.invite_token = uuid.uuid4()

        super().save(*args, **kwargs)

    def ensure_invite_token(self):
        if self.invite_token is None:
            self.invite_token = uuid.uuid4()
            self.save(
                update_fields=[
                    "invite_token"
                ]
            )

        return self.invite_token

    def __str__(self):
        return self.name


class GameSettings(models.Model):
    game = models.OneToOneField(
        Game,
        on_delete=models.CASCADE,
        related_name="design_settings",
    )

    allow_redo_year_1 = models.BooleanField(
        default=True,
        help_text=(
            "Allow players to restart Year 1 after completing it."
        ),
    )

    allow_redo_year_2 = models.BooleanField(
        default=True,
        help_text=(
            "Allow players to restart Year 2 after completing it."
        ),
    )

    allow_full_restart = models.BooleanField(
        default=True,
        help_text=(
            "Allow players to restart the complete simulation "
            "from the beginning of Year 1."
        ),
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    def __str__(self):
        return f"Settings — {self.game}"


class GameMembership(models.Model):
    class Role(models.TextChoices):
        ADMIN = "ADMIN", "Instructor"
        PLAYER = "PLAYER", "VP Sales & Operations"

    game = models.ForeignKey(
        Game,
        on_delete=models.CASCADE,
        related_name="memberships",
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="game_memberships",
    )

    role = models.CharField(
        max_length=10,
        choices=Role.choices,
        default=Role.PLAYER,
    )

    team_name = models.CharField(
        max_length=120,
        blank=True,
    )

    results_summary_reached = models.BooleanField(
        default=False,
        help_text=(
            "True after the player completes the final-year review "
            "and opens the combined simulation results summary."
        ),
    )

    last_reviewed_year = models.PositiveSmallIntegerField(
        default=0,
        validators=[
            MaxValueValidator(3),
        ],
        help_text=(
            "Highest simulation year whose year-end Board review "
            "the player has completed. Zero means no annual review "
            "has yet been completed."
        ),
    )

    joined_at = models.DateTimeField(
        auto_now_add=True,
    )

    class Meta:
        ordering = [
            "game",
            "role",
            "user__username",
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["game", "user"],
                name="unique_game_user",
            ),
        ]

    def latest_completed_attempt_for_year(
        self,
        year_number,
    ):
        return (
            self.year_attempts
            .filter(
                scenario_year__year_number=year_number,
                status=YearAttempt.Status.COMPLETE,
            )
            .order_by(
                "-attempt_number",
                "-completed_at",
            )
            .first()
        )

    def current_attempt_for_year(
        self,
        year_number,
    ):
        return (
            self.year_attempts
            .filter(
                scenario_year__year_number=year_number,
                is_current=True,
            )
            .order_by(
                "-attempt_number"
            )
            .first()
        )

    def __str__(self):
        return (
            f"{self.user} — {self.game} "
            f"({self.get_role_display()})"
        )


# ---------------------------------------------------------------------
# Products and scenario data
# ---------------------------------------------------------------------

class Product(models.Model):
    code = models.SlugField(
        max_length=40,
        unique=True,
    )

    name = models.CharField(
        max_length=120,
        unique=True,
    )

    standard_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )

    variable_unit_cost = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text=(
            "Direct variable production cost per newly "
            "replenished unit."
        ),
    )

    class Meta:
        ordering = ["name"]

    def clean(self):
        super().clean()

        errors = {}

        if (
            self.standard_price is not None
            and self.standard_price <= Decimal("0")
        ):
            errors["standard_price"] = (
                "Standard price must be greater than zero."
            )

        if (
            self.variable_unit_cost is not None
            and self.variable_unit_cost < Decimal("0")
        ):
            errors["variable_unit_cost"] = (
                "Variable unit cost cannot be negative."
            )

        if (
            self.standard_price is not None
            and self.variable_unit_cost is not None
            and self.variable_unit_cost
            >= self.standard_price
        ):
            errors["variable_unit_cost"] = (
                "Variable unit cost must be lower than "
                "the standard selling price."
            )

        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return self.name


class ScenarioYear(models.Model):
    game = models.ForeignKey(
        Game,
        on_delete=models.CASCADE,
        related_name="scenario_years",
    )

    year_number = models.PositiveSmallIntegerField(
        validators=[
            MinValueValidator(1),
            MaxValueValidator(3),
        ],
    )

    # ----------------------------------------------------------
    # Baseline annual financial scenario
    #
    # Year 1:
    #     These values are fixed by the board and used directly.
    #
    # Years 2 and 3:
    #     These values represent the baseline board scenario.
    #     The player may select a more ambitious revenue target
    #     and the associated operating budget through AnnualPlan.
    # ----------------------------------------------------------

    revenue_target = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        help_text=(
            "Baseline board-established revenue target. "
            "Year 1 uses this value directly. Years 2 and 3 "
            "may allow the player to select a higher target."
        ),
    )

    operating_budget = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        help_text=(
            "Baseline annual operating budget. "
            "Year 1 uses this value directly. Years 2 and 3 "
            "may use a player-selected budget associated with "
            "the selected revenue commitment."
        ),
    )

    # ----------------------------------------------------------
    # Monthly financing assumption
    # ----------------------------------------------------------

    monthly_interest_rate = models.DecimalField(
        max_digits=7,
        decimal_places=5,
        default=Decimal("0.01000"),
        validators=[
            MinValueValidator(
                Decimal("0")
            ),
            MaxValueValidator(
                Decimal("1")
            ),
        ],
    )

    # ----------------------------------------------------------
    # Opening inventory
    # ----------------------------------------------------------

    initial_inventory_units = (
        models.PositiveIntegerField(
            default=100,
            help_text=(
                "Beginning September inventory "
                "supplied for each product."
            ),
        )
    )

    # ----------------------------------------------------------
    # Fixed-cost allocation
    # ----------------------------------------------------------

    labor_fixed_cost_share = (
        models.DecimalField(
            max_digits=5,
            decimal_places=4,
            default=Decimal("0.60"),
            validators=[
                MinValueValidator(
                    Decimal("0")
                ),
                MaxValueValidator(
                    Decimal("1")
                ),
            ],
        )
    )

    maintenance_fixed_cost_share = (
        models.DecimalField(
            max_digits=5,
            decimal_places=4,
            default=Decimal("0.40"),
            validators=[
                MinValueValidator(
                    Decimal("0")
                ),
                MaxValueValidator(
                    Decimal("1")
                ),
            ],
        )
    )

    class Meta:
        ordering = [
            "year_number"
        ]

        constraints = [
            models.UniqueConstraint(
                fields=[
                    "game",
                    "year_number",
                ],
                name="unique_game_year",
            ),
        ]

    def clean(self):
        super().clean()

        errors = {}

        # ------------------------------------------------------
        # Revenue target
        # ------------------------------------------------------

        if (
            self.revenue_target is not None
            and self.revenue_target <= Decimal("0")
        ):
            errors["revenue_target"] = (
                "Revenue target must be greater "
                "than zero."
            )

        # ------------------------------------------------------
        # Operating budget
        # ------------------------------------------------------

        if (
            self.operating_budget is not None
            and self.operating_budget <= Decimal("0")
        ):
            errors["operating_budget"] = (
                "Operating budget must be greater "
                "than zero."
            )

        # ------------------------------------------------------
        # Fixed-cost shares must total 100%
        # ------------------------------------------------------

        if (
            self.labor_fixed_cost_share
            is not None
            and self.maintenance_fixed_cost_share
            is not None
        ):
            fixed_cost_share_total = (
                self.labor_fixed_cost_share
                + self.maintenance_fixed_cost_share
            )

            if (
                fixed_cost_share_total
                != Decimal("1.00")
            ):
                errors[
                    "labor_fixed_cost_share"
                ] = (
                    "Labor and maintenance "
                    "fixed-cost shares must "
                    "total 100%."
                )

                errors[
                    "maintenance_fixed_cost_share"
                ] = (
                    "Labor and maintenance "
                    "fixed-cost shares must "
                    "total 100%."
                )

        if errors:
            raise ValidationError(
                errors
            )

    def __str__(self):
        return (
            f"{self.game} — "
            f"Year {self.year_number}"
        )
       

class YearAttempt(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        COMPLETE = "COMPLETE", "Complete"
        ABANDONED = "ABANDONED", "Abandoned"

    membership = models.ForeignKey(
        GameMembership,
        on_delete=models.CASCADE,
        related_name="year_attempts",
    )

    scenario_year = models.ForeignKey(
        ScenarioYear,
        on_delete=models.CASCADE,
        related_name="attempts",
    )

    attempt_number = models.PositiveIntegerField(
        default=1,
        validators=[
            MinValueValidator(1),
        ],
    )

    status = models.CharField(
        max_length=12,
        choices=Status.choices,
        default=Status.ACTIVE,
    )

    is_current = models.BooleanField(
        default=True,
        help_text=(
            "True for the attempt that currently governs "
            "the player's progress in this simulation year."
        ),
    )

    started_at = models.DateTimeField(
        auto_now_add=True,
    )

    completed_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    class Meta:
        ordering = [
            "membership",
            "scenario_year__year_number",
            "-attempt_number",
        ]
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "membership",
                    "scenario_year",
                    "attempt_number",
                ],
                name="unique_year_attempt_number",
            ),
            models.UniqueConstraint(
                fields=[
                    "membership",
                    "scenario_year",
                ],
                condition=models.Q(
                    is_current=True
                ),
                name="unique_current_year_attempt",
            ),
        ]

    def clean(self):
        super().clean()

        errors = {}

        if (
            self.membership_id
            and self.scenario_year_id
            and self.membership.game_id
            != self.scenario_year.game_id
        ):
            errors["scenario_year"] = (
                "The membership and scenario year must belong "
                "to the same game."
            )

        if (
            self.membership_id
            and self.membership.role
            != GameMembership.Role.PLAYER
        ):
            errors["membership"] = (
                "Only player memberships can have year attempts."
            )

        if (
            self.status == self.Status.COMPLETE
            and self.completed_at is None
        ):
            errors["completed_at"] = (
                "A completed attempt must have a completion time."
            )

        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return (
            f"{self.membership.user} — "
            f"Year {self.scenario_year.year_number} — "
            f"Attempt {self.attempt_number}"
        )


class DemandValue(models.Model):
    scenario_year = models.ForeignKey(
        ScenarioYear,
        on_delete=models.CASCADE,
        related_name="demand_values",
    )

    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        related_name="true_demands",
    )

    month = models.PositiveSmallIntegerField(
        choices=FISCAL_MONTHS,
    )

    ideal_demand = models.PositiveIntegerField()

    class Meta:
        ordering = [
            "scenario_year",
            "product",
            "month",
        ]
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "scenario_year",
                    "product",
                    "month",
                ],
                name="unique_true_demand",
            ),
        ]

    @property
    def fiscal_position(self):
        return FISCAL_MONTH_POSITION[
            self.month
        ]

    def __str__(self):
        return (
            f"{self.product} — "
            f"{self.get_month_display()}: "
            f"{self.ideal_demand}"
        )


class ForecastSnapshot(models.Model):
    scenario_year = models.ForeignKey(
        ScenarioYear,
        on_delete=models.CASCADE,
        related_name="forecast_snapshots",
    )

    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        related_name="forecast_snapshots",
    )

    base_month = models.PositiveSmallIntegerField(
        choices=FISCAL_MONTHS,
        help_text=(
            "Month in which the forecast became available. "
            "August is the initial annual-planning forecast."
        ),
    )

    forecast_month = models.PositiveSmallIntegerField(
        choices=FISCAL_MONTHS,
    )

    forecast_units = models.PositiveIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "scenario_year",
                    "product",
                    "base_month",
                    "forecast_month",
                ],
                name="unique_forecast_snapshot",
            ),
        ]

    @property
    def base_month_fiscal_position(self):
        return FISCAL_MONTH_POSITION[
            self.base_month
        ]

    @property
    def forecast_month_fiscal_position(self):
        return FISCAL_MONTH_POSITION[
            self.forecast_month
        ]

    def clean(self):
        super().clean()

        base_position = (
            FISCAL_MONTH_POSITION.get(
                self.base_month
            )
        )

        forecast_position = (
            FISCAL_MONTH_POSITION.get(
                self.forecast_month
            )
        )

        if (
            base_position is None
            or forecast_position is None
        ):
            return

        # The August planning forecast contains September-August.
        if self.base_month == 8:
            return

        if forecast_position <= base_position:
            raise ValidationError({
                "forecast_month": (
                    "The forecast month must occur after the base "
                    "month in the fiscal-year sequence."
                ),
            })

    def __str__(self):
        return (
            f"{self.product}: "
            f"{self.get_base_month_display()} forecast → "
            f"{self.get_forecast_month_display()}"
        )


# ---------------------------------------------------------------------
# Player plans and monthly decisions
# ---------------------------------------------------------------------


SALES_CREDIT_RATE_CHOICES = [
    (
        Decimal("0.05"),
        "5% — Tight pricing authority",
    ),
    (
        Decimal("0.10"),
        "10% — Moderate pricing authority",
    ),
    (
        Decimal("0.20"),
        "20% — Broad pricing authority",
    ),
]


class AnnualPlan(models.Model):
    membership = models.ForeignKey(
        GameMembership,
        on_delete=models.CASCADE,
        related_name="annual_plans",
    )

    scenario_year = models.ForeignKey(
        ScenarioYear,
        on_delete=models.CASCADE,
        related_name="annual_plans",
    )

    attempt = models.ForeignKey(
        YearAttempt,
        on_delete=models.CASCADE,
        related_name="annual_plans",
        null=True,
        blank=True,
        help_text=(
            "Year attempt this annual plan belongs to. "
            "Null is retained for legacy records."
        ),
    )

    # ----------------------------------------------------------
    # Revenue commitment
    #
    # Year 1:
    #     ScenarioYear.revenue_target is fixed by the board.
    #
    # Years 2 and 3:
    #     The player's selected revenue commitment is stored here.
    #
    # If no alternative target is selected, the baseline
    # ScenarioYear.revenue_target is used.
    # ----------------------------------------------------------

    selected_revenue_target = (
        models.DecimalField(
            max_digits=14,
            decimal_places=2,
            null=True,
            blank=True,
            validators=[
                MinValueValidator(
                    Decimal("0.01")
                )
            ],
            help_text=(
                "Player-selected annual revenue commitment "
                "for Years 2 and 3."
            ),
        )
    )

    # ----------------------------------------------------------
    # Operating-budget commitment
    #
    # Year 1:
    #     ScenarioYear.operating_budget is fixed by the board.
    #
    # Years 2 and 3:
    #     The budget associated with the player's selected
    #     revenue commitment is stored here.
    #
    # The view/service layer must calculate this value from the
    # approved strategic alternative. The player does not type
    # an arbitrary budget.
    # ----------------------------------------------------------

    selected_operating_budget = (
        models.DecimalField(
            max_digits=14,
            decimal_places=2,
            null=True,
            blank=True,
            validators=[
                MinValueValidator(
                    Decimal("0.01")
                )
            ],
            help_text=(
                "Operating budget associated with the "
                "player-selected revenue commitment "
                "for Years 2 and 3."
            ),
        )
    )

    # ----------------------------------------------------------
    # Sales-credit policy
    # ----------------------------------------------------------

    sales_credit_rate = (
        models.DecimalField(
            max_digits=4,
            decimal_places=2,
            choices=(
                SALES_CREDIT_RATE_CHOICES
            ),
            default=Decimal("0.10"),
        )
    )

    # ----------------------------------------------------------
    # Maintenance policy
    # ----------------------------------------------------------

    skip_maintenance = (
        models.BooleanField(
            default=False,
        )
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "membership",
                    "scenario_year",
                ],
                condition=models.Q(
                    attempt__isnull=True
                ),
                name=(
                    "unique_legacy_annual_plan_"
                    "membership_year"
                ),
            ),
            models.UniqueConstraint(
                fields=[
                    "attempt",
                ],
                condition=models.Q(
                    attempt__isnull=False
                ),
                name="unique_annual_plan_attempt",
            ),
        ]

    # ----------------------------------------------------------
    # Effective annual revenue target
    # ----------------------------------------------------------

    @property
    def effective_revenue_target(self):
        """
        Return the revenue commitment currently governing
        the simulation year.

        Year 1:
            Uses ScenarioYear.revenue_target.

        Years 2 and 3:
            Uses selected_revenue_target when the player has
            selected one of the board's strategic alternatives.
        """
        if (
            self.selected_revenue_target
            is not None
        ):
            return (
                self.selected_revenue_target
            )

        return (
            self.scenario_year
            .revenue_target
        )

    # ----------------------------------------------------------
    # Effective annual operating budget
    # ----------------------------------------------------------

    @property
    def effective_operating_budget(self):
        """
        Return the operating budget currently governing
        the simulation year.

        Year 1:
            Uses ScenarioYear.operating_budget.

        Years 2 and 3:
            Uses selected_operating_budget when the player
            selects a strategic revenue alternative.
        """
        if (
            self.selected_operating_budget
            is not None
        ):
            return (
                self.selected_operating_budget
            )

        return (
            self.scenario_year
            .operating_budget
        )

    # ----------------------------------------------------------
    # Sales-credit target
    # ----------------------------------------------------------

    @property
    def sales_credit_target(self):
        """
        Calculate the annual sales-credit target from the
        effective revenue commitment.

        Example:

            Revenue target = $19M
            Credit policy  = 10%

            Sales-credit target = $1.9M
        """
        return (
            self.effective_revenue_target
            * self.sales_credit_rate
        )

    # ----------------------------------------------------------
    # Minimum selling-price policy
    # ----------------------------------------------------------

    @property
    def minimum_price_ratio(self):
        """
        5% policy:
            minimum selling price = 95% of standard

        10% policy:
            minimum selling price = 90% of standard

        20% policy:
            minimum selling price = 80% of standard
        """
        return (
            Decimal("1")
            - self.sales_credit_rate
        )

    # ----------------------------------------------------------
    # Maintenance / capability effect
    # ----------------------------------------------------------

    @property
    def quality_capacity_multiplier(self):
        """
        Deferring maintenance causes a 20% reduction
        in realized demand / effective capability.
        """
        if self.skip_maintenance:
            return Decimal("0.80")

        return Decimal("1.00")

    # ----------------------------------------------------------
    # Product-specific minimum permitted price
    # ----------------------------------------------------------

    def minimum_price_for_product(
        self,
        product,
    ):
        return (
            product.standard_price
            * self.minimum_price_ratio
        )

    # ----------------------------------------------------------
    # Validation
    # ----------------------------------------------------------

    def clean(self):
        super().clean()

        errors = {}

        # ------------------------------------------------------
        # Membership and scenario year must belong to
        # the same game.
        # ------------------------------------------------------

        if (
            self.membership_id
            and self.scenario_year_id
            and self.membership.game_id
            != self.scenario_year.game_id
        ):
            errors["scenario_year"] = (
                "The membership and scenario year "
                "must belong to the same game."
            )

        # ------------------------------------------------------
        # Attempt, membership, and scenario year must agree.
        # ------------------------------------------------------

        if self.attempt_id:
            if (
                self.membership_id
                and self.attempt.membership_id
                != self.membership_id
            ):
                errors["attempt"] = (
                    "The selected attempt must belong to "
                    "the same player membership."
                )

            if (
                self.scenario_year_id
                and self.attempt.scenario_year_id
                != self.scenario_year_id
            ):
                errors["attempt"] = (
                    "The selected attempt must belong to "
                    "the same simulation year."
                )

        # ------------------------------------------------------
        # Only player memberships can have annual plans.
        # ------------------------------------------------------

        if (
            self.membership_id
            and self.membership.role
            != GameMembership.Role.PLAYER
        ):
            errors["membership"] = (
                "Only player memberships can "
                "have annual operating plans."
            )

        # ------------------------------------------------------
        # Year 1:
        # Revenue target and operating budget are fixed.
        # ------------------------------------------------------

        if (
            self.scenario_year_id
            and self.scenario_year.year_number == 1
        ):
            if (
                self.selected_revenue_target
                is not None
            ):
                errors[
                    "selected_revenue_target"
                ] = (
                    "The Year 1 revenue target is "
                    "set by the board and cannot "
                    "be changed by the player."
                )

            if (
                self.selected_operating_budget
                is not None
            ):
                errors[
                    "selected_operating_budget"
                ] = (
                    "The Year 1 operating budget is "
                    "set by the board and cannot "
                    "be changed by the player."
                )

        # ------------------------------------------------------
        # Years 2 and 3:
        #
        # A selected target and its corresponding budget
        # must travel together. This prevents inconsistent
        # combinations such as selecting a higher revenue
        # commitment while retaining the baseline budget.
        # ------------------------------------------------------

        if (
            self.scenario_year_id
            and self.scenario_year.year_number > 1
        ):
            revenue_selected = (
                self.selected_revenue_target
                is not None
            )

            budget_selected = (
                self.selected_operating_budget
                is not None
            )

            if (
                revenue_selected
                != budget_selected
            ):
                if not revenue_selected:
                    errors[
                        "selected_revenue_target"
                    ] = (
                        "The selected revenue target "
                        "and operating budget must be "
                        "approved together."
                    )

                if not budget_selected:
                    errors[
                        "selected_operating_budget"
                    ] = (
                        "The selected revenue target "
                        "and operating budget must be "
                        "approved together."
                    )

        if errors:
            raise ValidationError(
                errors
            )

    def __str__(self):
        return (
            f"{self.membership} — "
            f"Year "
            f"{self.scenario_year.year_number}"
        )


class MonthlyDecision(models.Model):
    membership = models.ForeignKey(
        GameMembership,
        on_delete=models.CASCADE,
        related_name="decisions",
    )

    scenario_year = models.ForeignKey(
        ScenarioYear,
        on_delete=models.CASCADE,
        related_name="decisions",
    )

    attempt = models.ForeignKey(
        YearAttempt,
        on_delete=models.CASCADE,
        related_name="monthly_decisions",
        null=True,
        blank=True,
        help_text=(
            "Year attempt this monthly decision belongs to. "
            "Null is retained for legacy records."
        ),
    )

    month = models.PositiveSmallIntegerField(
        choices=FISCAL_MONTHS,
    )

    submitted_at = models.DateTimeField(
        auto_now_add=True,
    )

    is_finalized = models.BooleanField(
        default=False,
    )

    manufacturing_overhead = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0"),
        help_text=(
            "Unabsorbed annual fixed manufacturing cost recognized "
            "in the final month. Zero outside August."
        ),
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "membership",
                    "scenario_year",
                    "month",
                ],
                condition=models.Q(
                    attempt__isnull=True
                ),
                name="unique_legacy_monthly_decision",
            ),
            models.UniqueConstraint(
                fields=[
                    "attempt",
                    "month",
                ],
                condition=models.Q(
                    attempt__isnull=False
                ),
                name="unique_attempt_monthly_decision",
            ),
        ]

    @property
    def fiscal_position(self):
        return FISCAL_MONTH_POSITION[
            self.month
        ]

    def clean(self):
        super().clean()

        errors = {}

        if (
            self.membership_id
            and self.scenario_year_id
            and self.membership.game_id
            != self.scenario_year.game_id
        ):
            errors["scenario_year"] = (
                "The membership and scenario year must belong "
                "to the same game."
            )

        if self.attempt_id:
            if (
                self.membership_id
                and self.attempt.membership_id
                != self.membership_id
            ):
                errors["attempt"] = (
                    "The selected attempt must belong to "
                    "the same player membership."
                )

            if (
                self.scenario_year_id
                and self.attempt.scenario_year_id
                != self.scenario_year_id
            ):
                errors["attempt"] = (
                    "The selected attempt must belong to "
                    "the same simulation year."
                )

        if (
            self.membership_id
            and self.membership.role
            != GameMembership.Role.PLAYER
        ):
            errors["membership"] = (
                "Only player memberships can submit monthly decisions."
            )

        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return (
            f"{self.membership.user} — "
            f"Year {self.scenario_year.year_number}, "
            f"{self.get_month_display()}"
        )


class ProductDecision(models.Model):
    monthly_decision = models.ForeignKey(
        MonthlyDecision,
        on_delete=models.CASCADE,
        related_name="product_decisions",
    )

    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        related_name="decisions",
    )

    beginning_inventory_units = models.PositiveIntegerField(
        default=0,
        help_text="Inventory carried into the month.",
    )

    replenishment_units = models.PositiveIntegerField(
        default=0,
        help_text="New units replenished for the month.",
    )

    available_inventory_units = models.PositiveIntegerField(
        default=0,
        help_text=(
            "Beginning inventory plus new replenishment."
        ),
    )

    selling_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )

    true_demand_units = models.PositiveIntegerField(
        default=0,
    )

    adjusted_demand_units = models.PositiveIntegerField(
        default=0,
    )

    units_sold = models.PositiveIntegerField(
        default=0,
    )

    ending_inventory_units = models.PositiveIntegerField(
        default=0,
    )

    lost_sales_units = models.PositiveIntegerField(
        default=0,
    )

    revenue = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0"),
    )

    variable_production_cost = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0"),
    )

    labor_fixed_cost = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0"),
    )

    maintenance_fixed_cost = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0"),
    )

    total_production_cost = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0"),
    )

    holding_cost = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0"),
    )

    inventory_write_off = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0"),
        help_text=(
            "Final-month write-off of ending inventory at "
            "full production cost. Zero outside August."
        ),
    )


    sales_credit_collected = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0"),
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "monthly_decision",
                    "product",
                ],
                name="unique_decision_product",
            ),
        ]

    @property
    def fixed_production_cost(self):
        return (
            self.labor_fixed_cost
            + self.maintenance_fixed_cost
        )

    @property
    def total_operating_cost(self):
        return (
            self.total_production_cost
            + self.holding_cost
            + self.inventory_write_off
        )

    @property
    def contribution(self):
        return (
            self.revenue
            - self.total_operating_cost
        )


    def clean(self):
        super().clean()

        errors = {}

        if not self.product_id:
            return

        # ----------------------------------------------------------
        # Enforce annual-plan-specific selling-price floor.
        #
        # 5% policy  -> 95% floor
        # 10% policy -> 90% floor
        # 20% policy -> 80% floor
        # ----------------------------------------------------------

        if (
            self.monthly_decision_id
            and self.selling_price
            is not None
        ):
            annual_plan_query = (
                AnnualPlan.objects
                .filter(
                    membership=(
                        self.monthly_decision
                        .membership
                    ),
                    scenario_year=(
                        self.monthly_decision
                        .scenario_year
                    ),
                )
            )

            if self.monthly_decision.attempt_id:
                annual_plan_query = (
                    annual_plan_query
                    .filter(
                        attempt=(
                            self.monthly_decision
                            .attempt
                        )
                    )
                )
            else:
                annual_plan_query = (
                    annual_plan_query
                    .filter(
                        attempt__isnull=True
                    )
                )

            annual_plan = (
                annual_plan_query
                .order_by(
                    "-updated_at"
                )
                .first()
            )

            if annual_plan is None:
                errors[
                    "monthly_decision"
                ] = (
                    "An annual operating plan must "
                    "exist before monthly decisions "
                    "can be submitted."
                )

            else:
                minimum_price = (
                    annual_plan
                    .minimum_price_for_product(
                        self.product
                    )
                )

                if (
                    self.selling_price
                    < minimum_price
                ):
                    errors[
                        "selling_price"
                    ] = (
                        f"The minimum permitted "
                        f"price for "
                        f"{self.product.name} "
                        f"under the "
                        f"{annual_plan.sales_credit_rate:.0%} "
                        f"sales-credit policy is "
                        f"${minimum_price:,.2f}."
                    )

        # ----------------------------------------------------------
        # Inventory-flow validation
        # ----------------------------------------------------------

        if (
            self.monthly_decision_id
            and self.monthly_decision.is_finalized
            and (
                self.available_inventory_units
                != (
                    self.beginning_inventory_units
                    + self.replenishment_units
                )
            )
        ):
            errors[
                "available_inventory_units"
            ] = (
                "Available inventory must equal "
                "beginning inventory plus "
                "replenishment."
            )

        if (
            self.monthly_decision_id
            and self.monthly_decision.is_finalized
            and (
                self.units_sold
                + self.ending_inventory_units
                != self.available_inventory_units
            )
        ):
            errors[
                "ending_inventory_units"
            ] = (
                "Units sold plus ending inventory "
                "must equal available inventory."
            )

        if errors:
            raise ValidationError(
                errors
            )

    def __str__(self):
        return (
            f"{self.monthly_decision} — "
            f"{self.product}"
        )


# ---------------------------------------------------------------------
# Player score
# ---------------------------------------------------------------------

class PlayerYearScore(models.Model):
    membership = models.ForeignKey(
        GameMembership,
        on_delete=models.CASCADE,
        related_name="year_scores",
    )

    scenario_year = models.ForeignKey(
        ScenarioYear,
        on_delete=models.CASCADE,
        related_name="player_scores",
    )

    attempt = models.OneToOneField(
        YearAttempt,
        on_delete=models.CASCADE,
        related_name="year_score",
        null=True,
        blank=True,
        help_text=(
            "Year attempt summarized by this score. "
            "Null is retained for legacy records."
        ),
    )

    revenue = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0"),
    )

    operating_expense = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0"),
    )

    profit = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0"),
    )

    sales_credit_collected = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0"),
    )

    lost_sales_units = models.PositiveIntegerField(
        default=0,
    )

    ending_inventory_value = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0"),
    )

    score = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0"),
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        ordering = ["-score"]
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "membership",
                    "scenario_year",
                ],
                condition=models.Q(
                    attempt__isnull=True
                ),
                name="unique_legacy_player_year_score",
            ),
        ]

    def clean(self):
        super().clean()

        errors = {}

        if (
            self.membership_id
            and self.scenario_year_id
            and self.membership.game_id
            != self.scenario_year.game_id
        ):
            errors["scenario_year"] = (
                "The membership and scenario year must belong "
                "to the same game."
            )

        if self.attempt_id:
            if (
                self.membership_id
                and self.attempt.membership_id
                != self.membership_id
            ):
                errors["attempt"] = (
                    "The selected attempt must belong to "
                    "the same player membership."
                )

            if (
                self.scenario_year_id
                and self.attempt.scenario_year_id
                != self.scenario_year_id
            ):
                errors["attempt"] = (
                    "The selected attempt must belong to "
                    "the same simulation year."
                )

        if (
            self.membership_id
            and self.membership.role
            != GameMembership.Role.PLAYER
        ):
            errors["membership"] = (
                "Only player memberships can have player-year scores."
            )

        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return (
            f"{self.membership.user} — "
            f"Year {self.scenario_year.year_number}: "
            f"{self.score}"
        )