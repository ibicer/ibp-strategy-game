from decimal import Decimal

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm
from django.db import transaction
from django.utils.text import slugify

from .models import (
    AnnualPlan,
    Game,
    GameSettings,
    InstructorProfile,
    INSTRUCTOR_TITLE_CHOICES,
)
from .services import (
    maximum_price_before_zero_demand,
    monthly_ordering_guardrail_status,
    proposed_replenishment_spend,
)


# =====================================================================
# INSTRUCTOR REGISTRATION
# =====================================================================


class InstructorSignupForm(UserCreationForm):
    """
    Create a Django user and the corresponding InstructorProfile.

    First name, surname, email, username, and password are stored on
    the Django user model. Institution and professional title are stored
    on InstructorProfile.
    """

    first_name = forms.CharField(
        max_length=150,
        label="First name",
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "Enter your first name",
                "autocomplete": "given-name",
            }
        ),
    )

    last_name = forms.CharField(
        max_length=150,
        label="Surname",
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "Enter your surname",
                "autocomplete": "family-name",
            }
        ),
    )

    institution = forms.CharField(
        max_length=180,
        label="Institution",
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "University, school, or organization",
                "autocomplete": "organization",
            }
        ),
    )

    title = forms.ChoiceField(
        choices=INSTRUCTOR_TITLE_CHOICES,
        label="Title",
        widget=forms.Select(
            attrs={
                "class": "form-select",
            }
        ),
    )

    email = forms.EmailField(
        label="Email address",
        widget=forms.EmailInput(
            attrs={
                "class": "form-control",
                "placeholder": "name@institution.com",
                "autocomplete": "email",
            }
        ),
    )

    class Meta(UserCreationForm.Meta):
        model = get_user_model()

        fields = [
            "first_name",
            "last_name",
            "institution",
            "title",
            "email",
            "username",
            "password1",
            "password2",
        ]

    def __init__(
        self,
        *args,
        **kwargs,
    ):
        super().__init__(
            *args,
            **kwargs,
        )

        self.fields["username"].widget.attrs.update({
            "class": "form-control",
            "placeholder": "Choose a username",
            "autocomplete": "username",
        })

        self.fields["password1"].widget.attrs.update({
            "class": "form-control",
            "placeholder": "Create a password",
            "autocomplete": "new-password",
        })

        self.fields["password2"].widget.attrs.update({
            "class": "form-control",
            "placeholder": "Confirm your password",
            "autocomplete": "new-password",
        })

    def clean_email(self):
        email = (
            self.cleaned_data
            .get("email", "")
            .strip()
            .lower()
        )

        User = get_user_model()

        if (
            email
            and User.objects.filter(
                email__iexact=email
            ).exists()
        ):
            raise forms.ValidationError(
                "An account already exists with this email address. "
                "Please sign in instead."
            )

        return email

    @transaction.atomic
    def save(
        self,
        commit=True,
    ):
        user = super().save(
            commit=False
        )

        user.first_name = (
            self.cleaned_data["first_name"]
            .strip()
        )

        user.last_name = (
            self.cleaned_data["last_name"]
            .strip()
        )

        user.email = (
            self.cleaned_data["email"]
            .strip()
            .lower()
        )

        if not commit:
            return user

        user.save()

        InstructorProfile.objects.create(
            user=user,
            institution=(
                self.cleaned_data["institution"]
                .strip()
            ),
            title=self.cleaned_data["title"],
        )

        return user


# =====================================================================
# PLAYER REGISTRATION THROUGH INVITATION
# =====================================================================


class PlayerInviteSignupForm(UserCreationForm):
    """
    Player account creation used only after a valid game invitation
    link has been resolved by the view.

    The game itself is intentionally not editable in this form.
    The view creates the GameMembership after the user is saved.
    """

    first_name = forms.CharField(
        max_length=150,
        label="First name",
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "Enter your first name",
                "autocomplete": "given-name",
            }
        ),
    )

    last_name = forms.CharField(
        max_length=150,
        label="Surname",
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "Enter your surname",
                "autocomplete": "family-name",
            }
        ),
    )

    email = forms.EmailField(
        label="Email address",
        widget=forms.EmailInput(
            attrs={
                "class": "form-control",
                "placeholder": "Enter your email address",
                "autocomplete": "email",
            }
        ),
    )

    class Meta(UserCreationForm.Meta):
        model = get_user_model()

        fields = [
            "first_name",
            "last_name",
            "email",
            "username",
            "password1",
            "password2",
        ]

    def __init__(
        self,
        *args,
        **kwargs,
    ):
        super().__init__(
            *args,
            **kwargs,
        )

        self.fields["username"].widget.attrs.update({
            "class": "form-control",
            "placeholder": "Choose a username",
            "autocomplete": "username",
        })

        self.fields["password1"].widget.attrs.update({
            "class": "form-control",
            "placeholder": "Create a password",
            "autocomplete": "new-password",
        })

        self.fields["password2"].widget.attrs.update({
            "class": "form-control",
            "placeholder": "Confirm your password",
            "autocomplete": "new-password",
        })

    def clean_email(self):
        email = (
            self.cleaned_data
            .get("email", "")
            .strip()
            .lower()
        )

        User = get_user_model()

        if (
            email
            and User.objects.filter(
                email__iexact=email
            ).exists()
        ):
            raise forms.ValidationError(
                "An account already exists with this email address. "
                "Please sign in and use the invitation link again."
            )

        return email

    def save(
        self,
        commit=True,
    ):
        user = super().save(
            commit=False
        )

        user.first_name = (
            self.cleaned_data["first_name"]
            .strip()
        )

        user.last_name = (
            self.cleaned_data["last_name"]
            .strip()
        )

        user.email = (
            self.cleaned_data["email"]
            .strip()
            .lower()
        )

        if commit:
            user.save()

        return user


# =====================================================================
# GAME CREATION
# =====================================================================


class GameCreateForm(forms.ModelForm):
    """
    Instructor-facing game setup.

    The public game code is generated automatically from the game name.
    The invitation UUID is generated by Game.save().
    """

    class Meta:
        model = Game

        fields = [
            "name",
            "description",
            "registration_deadline",
        ]

        widgets = {
            "name": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": (
                        "e.g. Schulich MBA - Supply Chain"
                    ),
                }
            ),

            "description": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 3,
                    "placeholder": (
                        "Optional note for this class, cohort, "
                        "or executive program."
                    ),
                }
            ),

            "registration_deadline": (
                forms.DateTimeInput(
                    attrs={
                        "class": "form-control",
                        "type": "datetime-local",
                    },
                    format="%Y-%m-%dT%H:%M",
                )
            ),
        }

    def __init__(
        self,
        *args,
        **kwargs,
    ):
        super().__init__(
            *args,
            **kwargs,
        )

        self.fields[
            "registration_deadline"
        ].input_formats = [
            "%Y-%m-%dT%H:%M",
        ]

        self.fields[
            "registration_deadline"
        ].required = False

    def _unique_game_code(
        self,
        name,
    ):
        base_code = (
            slugify(name)
            or "x-summit-bikes"
        )[:32]

        candidate = base_code
        suffix = 2

        while Game.objects.filter(
            code=candidate
        ).exists():
            suffix_text = f"-{suffix}"

            candidate = (
                base_code[
                    : 40 - len(suffix_text)
                ]
                + suffix_text
            )

            suffix += 1

        return candidate

    def save(
        self,
        commit=True,
    ):
        game = super().save(
            commit=False
        )

        if not game.code:
            game.code = (
                self._unique_game_code(
                    self.cleaned_data["name"]
                )
            )

        # A newly created simulation is immediately available
        # for invitation-based player registration.
        game.status = Game.Status.OPEN

        if commit:
            game.save()

        return game


# =====================================================================
# GAME DESIGN SETTINGS
# =====================================================================


class GameSettingsForm(forms.ModelForm):
    """
    Instructor-controlled player retry permissions.

    Continue to Year 2 and Summarize Results are deliberately absent:
    those are mandatory parts of the simulation flow.
    """

    class Meta:
        model = GameSettings

        fields = [
            "allow_redo_year_1",
            "allow_redo_year_2",
            "allow_full_restart",
        ]

        labels = {
            "allow_redo_year_1": (
                "Allow players to redo Year 1"
            ),
            "allow_redo_year_2": (
                "Allow players to redo Year 2"
            ),
            "allow_full_restart": (
                "Allow players to start over from Year 1"
            ),
        }

        help_texts = {
            "allow_redo_year_1": (
                "After the Year 1 review, players may restart "
                "Year 1 and create a new attempt."
            ),
            "allow_redo_year_2": (
                "After the Year 2 review, players may restart "
                "Year 2 and create a new attempt."
            ),
            "allow_full_restart": (
                "Players may restart the complete simulation "
                "from the beginning of Year 1."
            ),
        }

        widgets = {
            "allow_redo_year_1": forms.CheckboxInput(
                attrs={
                    "class": "form-check-input",
                }
            ),
            "allow_redo_year_2": forms.CheckboxInput(
                attrs={
                    "class": "form-check-input",
                }
            ),
            "allow_full_restart": forms.CheckboxInput(
                attrs={
                    "class": "form-check-input",
                }
            ),
        }


# =====================================================================
# ANNUAL BOARDROOM PLAN
# =====================================================================


class AnnualPlanForm(forms.ModelForm):
    """
    Strategic decisions made during the August board meeting.

    Year 1
    ------
    The revenue target and operating budget are fixed by the board.

    The player chooses:
        - Sales-credit policy
        - Maintenance policy

    Year 2
    ------
    The player additionally chooses one of the board-approved
    revenue / budget packages:
        - Baseline
        - Baseline +1%
        - Baseline +2%

    The revenue target and operating budget are always selected
    together. The player never types either value manually.
    """

    MAINTENANCE_CHOICES = [
        (
            False,
            "Maintain Equipment",
        ),
        (
            True,
            "Defer Maintenance",
        ),
    ]

    skip_maintenance = (
        forms.TypedChoiceField(
            choices=(
                MAINTENANCE_CHOICES
            ),
            coerce=lambda value: (
                str(value).lower()
                in {
                    "true",
                    "1",
                }
            ),
            widget=(
                forms.RadioSelect()
            ),
            label=(
                "Maintenance policy"
            ),
            required=True,
        )
    )

    class Meta:
        model = AnnualPlan

        fields = [
            "sales_credit_rate",
            "skip_maintenance",
        ]

        widgets = {
            "sales_credit_rate": (
                forms.RadioSelect(
                    attrs={
                        "class": (
                            "sales-credit-radio"
                        ),
                    }
                )
            ),
        }

        labels = {
            "sales_credit_rate": (
                "Select the annual "
                "sales-credit policy"
            ),
        }

        help_texts = {
            "sales_credit_rate": (
                "The selected percentage "
                "determines both the annual "
                "sales-credit target and the "
                "minimum selling price available "
                "to the sales team."
            ),
        }

    def __init__(
        self,
        *args,
        scenario_year=None,
        revenue_budget_options=None,
        attempt=None,
        **kwargs,
    ):
        """
        revenue_budget_options is expected to be the ``options``
        list returned by annual_revenue_budget_options().

        Example item:

        {
            "code": "PLUS_2",
            "label": "Baseline +2%",
            "revenue_target": Decimal(...),
            "operating_budget": Decimal(...),
            "board_preferred": True,
        }
        """
        super().__init__(
            *args,
            **kwargs,
        )

        self.scenario_year = (
            scenario_year
        )

        self.attempt = (
            attempt
        )

        if (
            self.scenario_year is None
            and self.instance
            and self.instance.scenario_year_id
        ):
            self.scenario_year = (
                self.instance.scenario_year
            )

        self.revenue_budget_options = (
            revenue_budget_options
            or []
        )

        self.revenue_budget_option_map = {
            str(option["code"]): option
            for option in (
                self.revenue_budget_options
            )
        }

        year_number = (
            self.scenario_year.year_number
            if self.scenario_year
            is not None
            else None
        )

        # ----------------------------------------------------------
        # Years 2 and 3:
        # add the board-approved revenue / budget choice.
        # ----------------------------------------------------------

        if (
            year_number is not None
            and year_number > 1
        ):
            choices = [
                (
                    str(option["code"]),
                    str(option["label"]),
                )
                for option in (
                    self.revenue_budget_options
                )
            ]

            self.fields[
                "revenue_budget_option"
            ] = forms.ChoiceField(
                choices=choices,
                label=(
                    "Select the annual "
                    "revenue commitment"
                ),
                help_text=(
                    "Each revenue commitment "
                    "comes with its corresponding "
                    "board-approved operating budget."
                ),
                widget=forms.RadioSelect(
                    attrs={
                        "class": (
                            "revenue-target-radio"
                        ),
                    }
                ),
                required=True,
            )

            # Put the strategic commitment before
            # the commercial and maintenance policies.
            self.order_fields([
                "revenue_budget_option",
                "sales_credit_rate",
                "skip_maintenance",
            ])

            selected_target = (
                self.instance
                .selected_revenue_target
                if (
                    self.instance
                    and self.instance.pk
                )
                else None
            )

            initial_code = None

            if (
                selected_target
                is not None
            ):
                for option in (
                    self.revenue_budget_options
                ):
                    if (
                        Decimal(
                            option[
                                "revenue_target"
                            ]
                        )
                        == Decimal(
                            selected_target
                        )
                    ):
                        initial_code = str(
                            option["code"]
                        )
                        break

            if (
                initial_code is None
                and self.revenue_budget_options
            ):
                # Baseline is the default presentation.
                initial_code = str(
                    self.revenue_budget_options[
                        0
                    ][
                        "code"
                    ]
                )

            self.fields[
                "revenue_budget_option"
            ].initial = (
                initial_code
            )

    def clean_sales_credit_rate(self):
        rate = self.cleaned_data.get(
            "sales_credit_rate"
        )

        allowed_rates = {
            Decimal("0.05"),
            Decimal("0.10"),
            Decimal("0.20"),
        }

        if rate not in allowed_rates:
            raise forms.ValidationError(
                (
                    "Select one of the "
                    "board-approved "
                    "sales-credit policies."
                )
            )

        return rate

    def clean(self):
        cleaned_data = (
            super().clean()
        )

        if self.scenario_year is None:
            return cleaned_data

        year_number = (
            self.scenario_year
            .year_number
        )

        # ----------------------------------------------------------
        # Year 1:
        # target and budget remain fixed.
        # ----------------------------------------------------------

        if year_number == 1:
            self.instance.selected_revenue_target = (
                None
            )

            self.instance.selected_operating_budget = (
                None
            )

            return cleaned_data

        # ----------------------------------------------------------
        # Years 2 and 3:
        # revenue target and budget must come from one
        # board-approved strategic package.
        # ----------------------------------------------------------

        selected_code = (
            cleaned_data.get(
                "revenue_budget_option"
            )
        )

        if not selected_code:
            return cleaned_data

        option = (
            self.revenue_budget_option_map
            .get(
                str(selected_code)
            )
        )

        if option is None:
            self.add_error(
                "revenue_budget_option",
                (
                    "Select one of the "
                    "board-approved annual "
                    "revenue commitments."
                ),
            )

            return cleaned_data

        self.instance.selected_revenue_target = (
            Decimal(
                option[
                    "revenue_target"
                ]
            )
        )

        self.instance.selected_operating_budget = (
            Decimal(
                option[
                    "operating_budget"
                ]
            )
        )

        return cleaned_data


# =====================================================================
# MONTHLY EXECUTIVE DECISION
# =====================================================================


class MonthlyDecisionForm(forms.Form):
    """
    Monthly executive decision form.

    The player chooses:
        1. Selling price
        2. New replenishment quantity

    Year 1
    ------
    The simulation provides guided replenishment support,
    including an initial newsvendor recommendation.

    Year 2
    ------
    The player receives the wider demand range and decision
    economics, but must determine the replenishment quantity
    independently. No automatic order quantity is pre-filled.

    The annual sales-credit policy determines the minimum
    permitted selling price for each product.
    """

    def __init__(
        self,
        *args,
        products,
        annual_plan,
        attempt=None,
        forecasts=None,
        beginning_inventory=None,
        recommended_replenishment=None,
        **kwargs,
    ):
        super().__init__(
            *args,
            **kwargs,
        )

        self.attempt = attempt

        self.products = list(
            products
        )

        self.annual_plan = (
            annual_plan
        )

        self.scenario_year = (
            annual_plan.scenario_year
        )

        self.year_number = (
            self.scenario_year
            .year_number
        )

        self.forecasts = (
            forecasts or {}
        )

        self.beginning_inventory = (
            beginning_inventory or {}
        )

        self.recommended_replenishment = (
            recommended_replenishment
            or {}
        )

        self.show_replenishment_guidance = (
            self.year_number == 1
        )

        # Monthly ordering guardrail (180% of AOP budget)
        self.ordering_guardrail = (
            monthly_ordering_guardrail_status(
                annual_plan=self.annual_plan,
                scenario_year=self.scenario_year,
            )
        )

        for product in self.products:
            minimum_price = (
                self.annual_plan
                .minimum_price_for_product(
                    product
                )
            )

            forecast_units = int(
                self.forecasts.get(
                    product.id,
                    0,
                )
            )

            beginning_units = int(
                self.beginning_inventory.get(
                    product.id,
                    0,
                )
            )

            # ------------------------------------------------------
            # Year 1:
            # use the service recommendation when available.
            #
            # Year 2+:
            # intentionally do NOT provide an automatic order
            # quantity. The player must calculate the quantity.
            # ------------------------------------------------------

            if self.show_replenishment_guidance:
                fallback_replenishment = max(
                    0,
                    (
                        forecast_units
                        - beginning_units
                    ),
                )

                supplied_recommendation = (
                    self.recommended_replenishment
                    .get(
                        product.id
                    )
                )

                if supplied_recommendation is None:
                    recommended_units = (
                        fallback_replenishment
                    )
                else:
                    recommended_units = int(
                        supplied_recommendation
                    )
            else:
                recommended_units = 0

            price_field_name = (
                f"price_{product.id}"
            )

            replenishment_field_name = (
                f"inventory_{product.id}"
            )

            # ------------------------------------------------------
            # SELLING PRICE
            # ------------------------------------------------------

            if self.year_number >= 2:
                price_help_text = (
                    f"Standard price: "
                    f"${product.standard_price:,.2f}. "
                    f"Under the "
                    f"{self.annual_plan.sales_credit_rate:.0%} "
                    f"sales-credit policy, the minimum "
                    f"permitted price is "
                    f"${minimum_price:,.2f}. "
                    f"In Year {self.year_number}, price can "
                    f"also be used strategically to reduce "
                    f"excess inventory when necessary."
                )
            else:
                price_help_text = (
                    f"Standard price: "
                    f"${product.standard_price:,.2f}. "
                    f"Under the "
                    f"{self.annual_plan.sales_credit_rate:.0%} "
                    f"sales-credit policy, the minimum "
                    f"permitted price is "
                    f"${minimum_price:,.2f}."
                )

            self.fields[
                price_field_name
            ] = forms.DecimalField(
                label=(
                    f"{product.name} "
                    "selling price"
                ),
                min_value=(
                    minimum_price
                ),
                decimal_places=2,
                max_digits=12,
                initial=(
                    product.standard_price
                ),
                help_text=(
                    price_help_text
                ),
                widget=forms.NumberInput(
                    attrs={
                        "class": (
                            "form-control"
                        ),
                        "min": str(
                            minimum_price
                        ),
                        "step": "1",
                        "inputmode": (
                            "decimal"
                        ),
                        "data-product-id": (
                            product.id
                        ),
                        "data-standard-price": (
                            str(
                                product
                                .standard_price
                            )
                        ),
                        "data-minimum-price": (
                            str(
                                minimum_price
                            )
                        ),
                        "data-maximum-price": (
                            str(
                                maximum_price_before_zero_demand(product)
                            )
                        ),
                    }
                ),
            )

            # ------------------------------------------------------
            # REPLENISHMENT
            # ------------------------------------------------------

            if self.show_replenishment_guidance:
                replenishment_help_text = (
                    f"Beginning inventory: "
                    f"{beginning_units:,} units. "
                    f"Current mean forecast: "
                    f"{forecast_units:,} units."
                )
            else:
                replenishment_help_text = (
                    f"Beginning inventory: "
                    f"{beginning_units:,} units. "
                    f"Current mean forecast: "
                    f"{forecast_units:,} units. "
                    "Determine the order quantity "
                    "from the demand range, critical "
                    "ratio, and current inventory "
                    "position."
                )

            self.fields[
                replenishment_field_name
            ] = forms.IntegerField(
                label=(
                    f"{product.name} "
                    "new replenishment"
                ),
                min_value=0,
                initial=(
                    recommended_units
                ),
                help_text=(
                    replenishment_help_text
                ),
                widget=forms.NumberInput(
                    attrs={
                        "class": (
                            "form-control"
                        ),
                        "min": "0",
                        "step": "1",
                        "inputmode": (
                            "numeric"
                        ),
                        "data-product-id": (
                            product.id
                        ),
                        "data-beginning-inventory": (
                            beginning_units
                        ),
                        "data-guided-replenishment": (
                            "true"
                            if (
                                self
                                .show_replenishment_guidance
                            )
                            else "false"
                        ),
                    }
                ),
            )

    def clean(self):
        cleaned_data = (
            super().clean()
        )

        # ----------------------------------------------------------
        # Validate annual-plan-specific selling-price floors.
        # ----------------------------------------------------------

        for product in self.products:
            price_field = (
                f"price_{product.id}"
            )

            selling_price = (
                cleaned_data.get(
                    price_field
                )
            )

            if selling_price is None:
                continue

            minimum_price = (
                self.annual_plan
                .minimum_price_for_product(
                    product
                )
            )

            if (
                Decimal(selling_price)
                < minimum_price
            ):
                self.add_error(
                    price_field,
                    (
                        f"The minimum permitted "
                        f"price for "
                        f"{product.name} under the "
                        f"{self.annual_plan.sales_credit_rate:.0%} "
                        f"sales-credit policy is "
                        f"${minimum_price:,.2f}."
                    ),
                )

        replenishment_by_product = {}

        for product in self.products:
            replenishment_by_product[product.id] = (
                cleaned_data.get(
                    f"inventory_{product.id}",
                    0,
                ) or 0
            )

            price = cleaned_data.get(
                f"price_{product.id}"
            )

            if price is None:
                continue

            maximum_price = (
                maximum_price_before_zero_demand(
                    product
                )
            )

            if Decimal(price) > maximum_price:
                self.add_error(
                    f"price_{product.id}",
                    (
                        f"The selling price cannot exceed "
                        f"${maximum_price:,.2f}. At that threshold "
                        "the demand model reaches zero demand."
                    ),
                )

        if self.errors:
            return cleaned_data

        proposed = proposed_replenishment_spend(
            annual_plan=self.annual_plan,
            scenario_year=self.scenario_year,
            replenishment_by_product=replenishment_by_product,
        )

        remaining = Decimal(
            self.ordering_guardrail[
                "remaining_allowed_spend"
            ]
        )

        if (
            self.ordering_guardrail[
                "ordering_blocked"
            ]
            and proposed["total_units"] > 0
        ):
            raise forms.ValidationError(
                "The variable-cost production ceiling has already been reached or exceeded. Set all replenishment quantities to zero before continuing."
            )

        if Decimal(proposed["total_spend"]) > remaining:
            raise forms.ValidationError(
                (
                    "The proposed replenishment exceeds the remaining variable-cost production allowance. "
                    f"Only ${remaining:,.2f} of additional variable production cost is available."
                )
            )

        return cleaned_data


# =====================================================================
# PLAYER ENROLLMENT
# =====================================================================


class AddPlayerForm(forms.Form):
    user = forms.ModelChoiceField(
        queryset=(
            get_user_model()
            .objects
            .all()
            .order_by(
                "username"
            )
        ),
        label="User",
        widget=forms.Select(
            attrs={
                "class": "form-select",
            }
        ),
    )

    team_name = forms.CharField(
        max_length=120,
        required=False,
        label="Team name",
        help_text=(
            "Optional. Leave blank for an "
            "individual player."
        ),
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": (
                    "Executive team name"
                ),
            }
        ),
    )