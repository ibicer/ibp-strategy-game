from django.contrib import messages
from django.contrib.auth import login as auth_login
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from decimal import Decimal
from statistics import median

from .forms import (
    AddPlayerForm,
    AnnualPlanForm,
    GameCreateForm,
    GameSettingsForm,
    InstructorSignupForm,
    MonthlyDecisionForm,
    PlayerInviteSignupForm,
)
from .models import (
    AnnualPlan,
    FISCAL_MONTHS,
    FISCAL_MONTH_NUMBERS,
    FISCAL_MONTH_POSITION,
    ForecastSnapshot,
    Game,
    GameMembership,
    GameSettings,
    InstructorProfile,
    MonthlyDecision,
    PlayerYearScore,
    Product,
    ProductDecision,
    ScenarioYear,
    YearAttempt,
)

from .services import (
    annual_cost_parameters,
    annual_revenue_budget_options,
    attempt_filter,
    beginning_inventory_by_product,
    clone_game_scenario,
    current_forecasts,
    monthly_planning_analysis,
    next_fiscal_month,
    resolve_year_attempt,
    run_month,
    validate_game_scenario,
    year_end_review_analysis,
)


MONTH_NAMES = dict(FISCAL_MONTHS)


# ---------------------------------------------------------------------
# Authorization helpers
# ---------------------------------------------------------------------

def _membership_or_403(user, game):
    """
    Return the user's membership for the selected game.

    Raise PermissionDenied when the user is not registered for the game.
    """
    membership = (
        GameMembership.objects
        .select_related("game", "user")
        .filter(
            game=game,
            user=user,
        )
        .first()
    )

    if membership is None:
        raise PermissionDenied(
            "You are not registered for this game."
        )

    return membership


def _player_or_403(user, game):
    """
    Return the user's player membership for the selected game.
    """
    membership = _membership_or_403(user, game)

    if membership.role != GameMembership.Role.PLAYER:
        raise PermissionDenied(
            "Player access is required."
        )

    return membership


def _admin_or_403(user, game):
    """
    Return the instructor membership for the selected game.
    """
    membership = _membership_or_403(user, game)

    if (
        membership.role != GameMembership.Role.ADMIN
        or game.created_by_id != user.id
    ):
        raise PermissionDenied(
            "Instructor access is required for this simulation."
        )

    return membership


def _instructor_profile_or_403(user):
    """
    Return the instructor profile for a registered instructor.
    """
    profile = (
        InstructorProfile.objects
        .filter(user=user)
        .first()
    )

    if profile is None:
        raise PermissionDenied(
            "An instructor account is required."
        )

    return profile


def _game_settings(game):
    """
    Return persistent design settings for a game.

    get_or_create keeps pre-existing games compatible with the new
    instructor-design workflow.
    """
    settings_obj, _ = (
        GameSettings.objects
        .get_or_create(game=game)
    )

    return settings_obj


def _simulation_ready(game):
    """
    Return True when the game contains a complete playable scenario.

    This is intentionally a presentation helper. It never raises to the
    instructor workspace; an invalid or incomplete scenario simply appears
    as not ready.
    """
    try:
        status = validate_game_scenario(
            game=game,
        )
    except ValidationError:
        return False

    return bool(
        status.get(
            "ready"
        )
    )


def _invite_url(request, game):
    token = game.ensure_invite_token()

    return request.build_absolute_uri(
        reverse(
            "player_invite_signup",
            kwargs={
                "invite_token": token,
            },
        )
    )


def _mean(values):
    if not values:
        return None

    return sum(values) / len(values)


def _summary_stat_row(label, values):
    """
    Return mean, median, minimum, and maximum for a non-empty sequence.
    """
    values = list(values)

    if not values:
        return {
            "label": label,
            "mean": None,
            "median": None,
            "minimum": None,
            "maximum": None,
            "count": 0,
        }

    return {
        "label": label,
        "mean": _mean(values),
        "median": median(values),
        "minimum": min(values),
        "maximum": max(values),
        "count": len(values),
    }


def _scenario_year_or_404(game, year):
    """
    Retrieve a scenario year that belongs to the selected game.
    """
    return get_object_or_404(
        ScenarioYear,
        game=game,
        year_number=year,
    )


def _validate_fiscal_month(month):
    """
    Ensure the supplied month belongs to the September-August
    simulation sequence.
    """
    if month not in FISCAL_MONTH_POSITION:
        raise PermissionDenied(
            "The requested simulation month is invalid."
        )


def _previous_forecast_base_month(month):
    """
    Return the forecast vintage used to make a decision for a month.

    September decisions use the August planning forecast.
    Every later month uses the forecast released in the preceding month.
    """
    _validate_fiscal_month(month)

    if month == 9:
        return 8

    month_position = FISCAL_MONTH_POSITION[month]

    return FISCAL_MONTH_NUMBERS[
        month_position - 1
    ]




def _current_year_attempt(
    *,
    membership,
    scenario_year,
    create=False,
):
    """
    Return the attempt that currently governs this player/year.

    New simulations always use YearAttempt. Legacy rows remain readable:
    when no YearAttempt exists and create=False, None selects only the
    legacy null-attempt records through attempt_filter().
    """
    attempt = resolve_year_attempt(
        membership=membership,
        scenario_year=scenario_year,
    )

    if attempt is not None or not create:
        return attempt

    latest_number = (
        YearAttempt.objects
        .filter(
            membership=membership,
            scenario_year=scenario_year,
        )
        .aggregate(
            maximum=Max("attempt_number")
        )["maximum"]
        or 0
    )

    return YearAttempt.objects.create(
        membership=membership,
        scenario_year=scenario_year,
        attempt_number=latest_number + 1,
        status=YearAttempt.Status.ACTIVE,
        is_current=True,
    )


def _latest_completed_attempt(
    *,
    membership,
    scenario_year,
):
    """
    Return the newest completed attempt for reporting.

    If this is a pre-attempt legacy player/year, return None so callers
    deliberately select only null-attempt legacy records.
    """
    attempt = (
        YearAttempt.objects
        .filter(
            membership=membership,
            scenario_year=scenario_year,
            status=YearAttempt.Status.COMPLETE,
        )
        .order_by(
            "-attempt_number",
            "-completed_at",
        )
        .first()
    )

    if attempt is not None:
        return attempt

    if not YearAttempt.objects.filter(
        membership=membership,
        scenario_year=scenario_year,
    ).exists():
        return None

    return None


def _start_new_attempt(
    *,
    membership,
    scenario_year,
):
    """
    Preserve prior history and create a fresh current attempt.
    """
    current_attempts = list(
        YearAttempt.objects
        .select_for_update()
        .filter(
            membership=membership,
            scenario_year=scenario_year,
            is_current=True,
        )
    )

    for attempt in current_attempts:
        attempt.is_current = False

        if attempt.status == YearAttempt.Status.ACTIVE:
            attempt.status = YearAttempt.Status.ABANDONED
            attempt.save(
                update_fields=[
                    "is_current",
                    "status",
                ]
            )
        else:
            attempt.save(
                update_fields=[
                    "is_current",
                ]
            )

    latest_number = (
        YearAttempt.objects
        .filter(
            membership=membership,
            scenario_year=scenario_year,
        )
        .aggregate(
            maximum=Max("attempt_number")
        )["maximum"]
        or 0
    )

    return YearAttempt.objects.create(
        membership=membership,
        scenario_year=scenario_year,
        attempt_number=latest_number + 1,
        status=YearAttempt.Status.ACTIVE,
        is_current=True,
    )



def _player_progress_year(
    *,
    game,
    membership,
):
    """
    Determine the player's active scenario year without mixing attempts.
    """
    scenario_years = list(
        ScenarioYear.objects
        .filter(game=game)
        .order_by("year_number")
    )

    if not scenario_years:
        raise PermissionDenied(
            "No scenario years are configured for this game."
        )

    required_months = set(FISCAL_MONTH_NUMBERS)

    for scenario_year in scenario_years:
        attempt = _current_year_attempt(
            membership=membership,
            scenario_year=scenario_year,
            create=False,
        )

        annual_plan_exists = (
            AnnualPlan.objects
            .filter(
                membership=membership,
                scenario_year=scenario_year,
                **attempt_filter(attempt),
            )
            .exists()
        )

        if not annual_plan_exists:
            return scenario_year

        completed_months = set(
            MonthlyDecision.objects
            .filter(
                membership=membership,
                scenario_year=scenario_year,
                is_finalized=True,
                **attempt_filter(attempt),
            )
            .values_list("month", flat=True)
        )

        if completed_months != required_months:
            return scenario_year

        if membership.last_reviewed_year < scenario_year.year_number:
            return scenario_year

    return scenario_years[-1]


def _next_incomplete_month(
    *,
    membership,
    scenario_year,
    attempt=None,
):
    """
    Return the first unfinished fiscal month in one specific attempt.
    """
    if attempt is None:
        attempt = _current_year_attempt(
            membership=membership,
            scenario_year=scenario_year,
            create=False,
        )

    completed_months = set(
        MonthlyDecision.objects
        .filter(
            membership=membership,
            scenario_year=scenario_year,
            is_finalized=True,
            **attempt_filter(attempt),
        )
        .values_list("month", flat=True)
    )

    return next(
        (
            month
            for month in FISCAL_MONTH_NUMBERS
            if month not in completed_months
        ),
        None,
    )

def instructor_signup(request):
    """
    Public instructor account registration.

    A successful signup creates both the Django user and its
    InstructorProfile, then signs the instructor in immediately.
    """
    if request.user.is_authenticated:
        return redirect("game_lobby")

    if request.method == "POST":
        form = InstructorSignupForm(
            request.POST
        )

        if form.is_valid():
            user = form.save()

            auth_login(
                request,
                user,
            )

            messages.success(
                request,
                (
                    "Your instructor account is ready. "
                    "You can now create your first X-Summit Bikes simulation."
                ),
            )

            return redirect(
                "instructor_workspace"
            )
    else:
        form = InstructorSignupForm()

    return render(
        request,
        "simulation/instructor_signup.html",
        {
            "form": form,
        },
    )


def player_invite_signup(
    request,
    invite_token,
):
    game = get_object_or_404(
        Game,
        invite_token=invite_token,
    )

    # --------------------------------------------------------------
    # Existing signed-in user
    # --------------------------------------------------------------
    if request.user.is_authenticated:

        # Instructors should not accidentally join games as players.
        if hasattr(
            request.user,
            "instructor_profile",
        ):
            messages.warning(
                request,
                (
                    "You are currently signed in with an "
                    "instructor account. Player invitations "
                    "must be opened with a player account."
                ),
            )

            return redirect(
                "instructor_workspace"
            )

        membership, created = (
            GameMembership.objects.get_or_create(
                game=game,
                user=request.user,
                defaults={
                    "role": (
                        GameMembership.Role.PLAYER
                    ),
                },
            )
        )

        if (
            membership.role
            != GameMembership.Role.PLAYER
        ):
            raise PermissionDenied(
                "This account cannot join the simulation "
                "as a player."
            )

        if created:
            messages.success(
                request,
                (
                    f"You have joined {game.name}. "
                    "Welcome to X-Summit Bikes."
                ),
            )
        else:
            messages.info(
                request,
                (
                    f"You are already registered for "
                    f"{game.name}."
                ),
            )

        return redirect(
            "game_lobby"
        )

    # --------------------------------------------------------------
    # New player registration
    # --------------------------------------------------------------
    if request.method == "POST":

        form = PlayerInviteSignupForm(
            request.POST
        )

        if form.is_valid():

            with transaction.atomic():

                user = form.save()

                GameMembership.objects.create(
                    game=game,
                    user=user,
                    role=(
                        GameMembership.Role.PLAYER
                    ),
                )

            auth_login(
                request,
                user,
            )

            messages.success(
                request,
                (
                    f"Welcome to {game.name}. "
                    "Your player account is ready."
                ),
            )

            return redirect(
                "game_lobby"
            )

    else:
        form = PlayerInviteSignupForm()

    context = {
        "game": game,
        "form": form,
        "invite_token": invite_token,
    }

    return render(
        request,
        "simulation/player_invite_signup.html",
        context,
    )

@login_required
def instructor_workspace(request):
    """
    Instructor home screen containing only simulations created by
    the signed-in instructor.
    """
    profile = _instructor_profile_or_403(
        request.user
    )

    games = (
        Game.objects
        .filter(
            created_by=request.user
        )
        .prefetch_related(
            "memberships"
        )
        .order_by(
            "-created_at"
        )
    )

    game_rows = []

    for game in games:
        player_count = (
            game.memberships
            .filter(
                role=GameMembership.Role.PLAYER
            )
            .count()
        )

        completed_count = (
            game.memberships
            .filter(
                role=GameMembership.Role.PLAYER,
                results_summary_reached=True,
            )
            .count()
        )

        game_rows.append({
            "game": game,
            "player_count": player_count,
            "completed_count": completed_count,
            "simulation_ready": (
                _simulation_ready(game)
            ),
        })

    return render(
        request,
        "simulation/instructor_workspace.html",
        {
            "profile": profile,
            "game_rows": game_rows,
        },
    )


# ---------------------------------------------------------------------
# Game lobby and registration
# ---------------------------------------------------------------------

@login_required
def game_lobby(request):
    """
    Common post-login lobby.

    Instructors see their instructor workspace entry point. Players see
    only simulations to which they already belong. Public browsing of
    open games is deliberately disabled; player enrollment is invitation
    based.

    For player memberships, ``has_started`` is derived from actual
    simulation activity rather than from membership creation. This lets
    the lobby distinguish a newly enrolled participant ("Start
    Simulation") from a participant who has already entered the
    simulation ("Continue Simulation").
    """
    memberships = list(
        request.user
        .game_memberships
        .select_related("game")
        .order_by("-game__created_at")
    )

    for membership in memberships:
        if membership.role == GameMembership.Role.PLAYER:
            membership.has_started = (
                membership.year_attempts.exists()
                or AnnualPlan.objects.filter(
                    membership=membership
                ).exists()
                or MonthlyDecision.objects.filter(
                    membership=membership
                ).exists()
                or membership.last_reviewed_year > 0
                or membership.results_summary_reached
            )
        else:
            membership.has_started = False

    is_instructor = (
        InstructorProfile.objects
        .filter(user=request.user)
        .exists()
    )

    context = {
        "memberships": memberships,
        "open_games": Game.objects.none(),
        "is_instructor": is_instructor,
    }

    return render(
        request,
        "simulation/game_lobby.html",
        context,
    )


@login_required
@require_POST
def join_game(request, code):
    """
    Backward-compatible endpoint.

    New player enrollment must use the private invitation URL. Existing
    members may still pass through this route safely.
    """
    game = get_object_or_404(
        Game,
        code=code,
    )

    membership = (
        GameMembership.objects
        .filter(
            game=game,
            user=request.user,
        )
        .first()
    )

    if membership is None:
        raise PermissionDenied(
            "Use the invitation link supplied by your instructor to join this simulation."
        )

    if membership.role == GameMembership.Role.ADMIN:
        return redirect(
            "admin_game_dashboard",
            code=game.code,
        )

    return redirect(
        "game_dashboard",
        code=game.code,
    )


@login_required
@transaction.atomic
def create_game(request):
    """
    Create a simulation owned by the signed-in instructor.
    """
    _instructor_profile_or_403(
        request.user
    )

    if request.method == "POST":
        form = GameCreateForm(
            request.POST
        )

        if form.is_valid():
            try:
                with transaction.atomic():
                    game = form.save(
                        commit=False
                    )

                    game.created_by = (
                        request.user
                    )

                    game.save()

                    # Automatic backend initialization. Instructors never
                    # need to manage scenario copying or data preparation.
                    clone_game_scenario(
                        target_game=game,
                    )

                    GameMembership.objects.create(
                        game=game,
                        user=request.user,
                        role=GameMembership.Role.ADMIN,
                    )

                    GameSettings.objects.create(
                        game=game
                    )

            except ValidationError:
                messages.error(
                    request,
                    (
                        "The simulation could not be created because "
                        "automatic setup did not complete successfully. "
                        "Please try again or contact the system administrator."
                    ),
                )

            else:
                messages.success(
                    request,
                    (
                        "Simulation created and initialized successfully. "
                        "Choose the retry permissions, then share the "
                        "invitation link with your participants."
                    ),
                )

                return redirect(
                    "game_design",
                    code=game.code,
                )
    else:
        form = GameCreateForm()

    return render(
        request,
        "simulation/create_game.html",
        {
            "form": form,
        },
    )


@login_required
@transaction.atomic
def game_design(request, code):
    """
    Configure instructor-controlled player retry permissions.

    Continue to Year 2 and Summarize Results are intentionally mandatory
    and therefore never appear as configurable settings.
    """
    game = get_object_or_404(
        Game,
        code=code,
    )

    _admin_or_403(
        request.user,
        game,
    )

    settings_obj = _game_settings(
        game
    )

    if request.method == "POST":
        form = GameSettingsForm(
            request.POST,
            instance=settings_obj,
        )

        if form.is_valid():
            form.save()

            messages.success(
                request,
                "Simulation design settings saved.",
            )

            return redirect(
                "admin_game_dashboard",
                code=game.code,
            )
    else:
        form = GameSettingsForm(
            instance=settings_obj
        )

    return render(
        request,
        "simulation/game_design.html",
        {
            "game": game,
            "form": form,
            "invite_url": _invite_url(
                request,
                game,
            ),
            "simulation_ready": (
                _simulation_ready(game)
            ),
        },
    )


# ---------------------------------------------------------------------
# Player dashboard and annual planning
# ---------------------------------------------------------------------

@login_required
def game_dashboard(request, code):
    game = get_object_or_404(
        Game,
        code=code,
    )

    membership = _membership_or_403(
        request.user,
        game,
    )

    # --------------------------------------------------------------
    # Administrator
    # --------------------------------------------------------------

    if membership.role == GameMembership.Role.ADMIN:
        return redirect(
            "admin_game_dashboard",
            code=game.code,
        )

    # --------------------------------------------------------------
    # Player-specific simulation year
    #
    # Do not rely on Game.current_year alone. That field is shared by
    # every player in the game. Progress is derived from this player's
    # own AOP and monthly results.
    # --------------------------------------------------------------

    scenario_year = (
        _player_progress_year(
            game=game,
            membership=membership,
        )
    )

    attempt = _current_year_attempt(
        membership=membership,
        scenario_year=scenario_year,
        create=False,
    )

    # --------------------------------------------------------------
    # Annual Operating Plan must always come before monthly S&OP.
    # --------------------------------------------------------------

    annual_plan = (
        AnnualPlan.objects
        .filter(
            membership=membership,
            scenario_year=scenario_year,
            **attempt_filter(attempt),
        )
        .first()
    )

    if annual_plan is None:
        return redirect(
            "annual_planning",
            code=game.code,
            year=scenario_year.year_number,
        )

    # --------------------------------------------------------------
    # Determine this player's next unfinished operating month.
    # --------------------------------------------------------------

    next_month = (
        _next_incomplete_month(
            membership=membership,
            scenario_year=scenario_year,
            attempt=attempt,
        )
    )

    # --------------------------------------------------------------
    # Continue monthly simulation
    # --------------------------------------------------------------

    if next_month is not None:
        return redirect(
            "plan_month",
            code=game.code,
            year=scenario_year.year_number,
            month=next_month,
        )

    # --------------------------------------------------------------
    # A completed year must be reviewed before progression.
    #
    # The GET year_complete page is the review itself. We persist
    # completion of that review when the player explicitly advances
    # from it (or opens the final combined summary).
    # --------------------------------------------------------------

    next_scenario_year = (
        ScenarioYear.objects
        .filter(
            game=game,
            year_number=(
                scenario_year.year_number
                + 1
            ),
        )
        .first()
    )

    if (
        next_scenario_year is None
        and membership.results_summary_reached
    ):
        return redirect(
            "results_summary",
            code=game.code,
        )

    # --------------------------------------------------------------
    # Year complete
    #
    # All twelve operating months have now been finalized.
    # Build the board-review analysis.
    # --------------------------------------------------------------

    review = year_end_review_analysis(
        membership=membership,
        scenario_year=scenario_year,
        attempt=attempt,
    )

    score = (
        PlayerYearScore.objects
        .filter(
            membership=membership,
            scenario_year=scenario_year,
            **attempt_filter(attempt),
        )
        .first()
    )

    # --------------------------------------------------------------
    # Board members
    #
    # The same characters and images used in the August boardroom
    # are reused for the year-end review.
    # --------------------------------------------------------------

    board_members = {
        "clarke": {
            "name": "Mrs Clarke",
            "title": "Chair",
            "image": (
                "simulation/board/clarke.png"
            ),
            "focus": (
                "Revenue achievement and "
                "financial discipline."
            ),
        },

        "daniels": {
            "name": "Mr Daniels",
            "title": (
                "Board Member · Commercial"
            ),
            "image": (
                "simulation/board/daniels.png"
            ),
            "focus": (
                "Commercial discipline and "
                "sales-credit performance."
            ),
        },

        "nauta": {
            "name": "Ms Nauta",
            "title": (
                "Board Member · Operations"
            ),
            "image": (
                "simulation/board/nauta.png"
            ),
            "focus": (
                "Carrying cost, lost sales, inventory "
                "write-off, and manufacturing overhead."
            ),
        },
    }

    # --------------------------------------------------------------
    # Context
    # --------------------------------------------------------------

    context = {
        "game": game,
        "membership": membership,
        "scenario_year": scenario_year,
        "annual_plan": annual_plan,
        "attempt": attempt,

        # Existing cumulative record.
        # Kept for compatibility, but the year-end page should not
        # display the old KPI / score terminology.
        "score": score,

        # Board characters.
        "board_members": (
            board_members
        ),

        # Complete board-review analysis.
        "review": review,

        # Convenient aliases for the template.
        "clarke_review": (
            review["clarke"]
        ),
        "daniels_review": (
            review["daniels"]
        ),
        "nauta_review": (
            review["nauta"]
        ),

        # Reward summary.
        "rewards": (
            review["rewards"]
        ),

        "next_scenario_year": (
            next_scenario_year
        ),

        "design_settings": (
            _game_settings(game)
        ),
        "allow_redo_current_year": (
            _game_settings(game).allow_redo_year_1
            if scenario_year.year_number == 1
            else _game_settings(game).allow_redo_year_2
        ),
        "allow_full_restart": (
            _game_settings(game).allow_full_restart
        ),
    }

    return render(
        request,
        "simulation/year_complete.html",
        context,
    )

@login_required
@transaction.atomic
def annual_planning(
    request,
    code,
    year,
):
    game = get_object_or_404(
        Game,
        code=code,
    )

    membership = _player_or_403(
        request.user,
        game,
    )

    scenario_year = (
        _scenario_year_or_404(
            game,
            year,
        )
    )

    player_progress_year = (
        _player_progress_year(
            game=game,
            membership=membership,
        )
    )

    if (
        scenario_year.year_number
        > player_progress_year.year_number
    ):
        messages.warning(
            request,
            "Complete the current simulation year before starting a later year.",
        )

        return redirect(
            "game_dashboard",
            code=game.code,
        )

    attempt = _current_year_attempt(
        membership=membership,
        scenario_year=scenario_year,
        create=True,
    )

    # --------------------------------------------------------------
    # Existing annual plan
    # --------------------------------------------------------------

    annual_plan = (
        AnnualPlan.objects
        .filter(
            membership=membership,
            scenario_year=scenario_year,
            **attempt_filter(attempt),
        )
        .first()
    )

    # --------------------------------------------------------------
    # Annual plan becomes locked once any month is finalized.
    # --------------------------------------------------------------

    finalized_decisions_exist = (
        MonthlyDecision.objects
        .filter(
            membership=membership,
            scenario_year=scenario_year,
            is_finalized=True,
            **attempt_filter(attempt),
        )
        .exists()
    )

    if finalized_decisions_exist:
        messages.warning(
            request,
            (
                "The annual operating plan cannot "
                "be changed after monthly simulation "
                "results have been finalized."
            ),
        )

        return redirect(
            "game_dashboard",
            code=game.code,
        )

    # --------------------------------------------------------------
    # Revenue / budget alternatives
    #
    # Year 1:
    #     one fixed board mandate.
    #
    # Year 2:
    #     baseline
    #     baseline +1%
    #     baseline +2%
    # --------------------------------------------------------------

    revenue_budget_analysis = (
        annual_revenue_budget_options(
            scenario_year=scenario_year,
        )
    )

    revenue_budget_options = (
        revenue_budget_analysis[
            "options"
        ]
    )

    form_kwargs = {
        "scenario_year": (
            scenario_year
        ),
        "attempt": attempt,
        "revenue_budget_options": (
            revenue_budget_options
        ),
    }

    # --------------------------------------------------------------
    # POST — approve annual board resolution
    # --------------------------------------------------------------

    if request.method == "POST":
        form = AnnualPlanForm(
            request.POST,
            instance=annual_plan,
            **form_kwargs,
        )

        if form.is_valid():
            plan = form.save(
                commit=False
            )

            plan.membership = (
                membership
            )

            plan.scenario_year = (
                scenario_year
            )

            plan.attempt = attempt

            # Year 1 values are fixed by ScenarioYear.
            if (
                scenario_year.year_number
                == 1
            ):
                plan.selected_revenue_target = (
                    None
                )

                plan.selected_operating_budget = (
                    None
                )

            plan.full_clean()
            plan.save()

            messages.success(
                request,
                (
                    "Annual Operating Plan approved. "
                    "Your financial, commercial, and operating "
                    "commitments are now in force."
                ),
            )

            return redirect(
                "plan_month",
                code=game.code,
                year=(
                    scenario_year.year_number
                ),
                month=9,
            )

        messages.error(
            request,
            (
                "The Annual Operating Plan could not be approved. "
                "Please complete all required decisions and "
                "review the highlighted fields."
            ),
        )

    else:
        form = AnnualPlanForm(
            instance=annual_plan,
            **form_kwargs,
        )

    # --------------------------------------------------------------
    # Products
    # --------------------------------------------------------------

    products = list(
        Product.objects
        .all()
        .order_by("name")
    )

    # --------------------------------------------------------------
    # August rolling-horizon forecast
    #
    # This is the initial September-August forecast available
    # to the player during the annual board meeting.
    # --------------------------------------------------------------

    august_forecasts = (
        current_forecasts(
            scenario_year=scenario_year,
            base_month=8,
        )
    )

    # --------------------------------------------------------------
    # Annual cost structure
    # --------------------------------------------------------------

    cost_parameters = (
        annual_cost_parameters(
            scenario_year=scenario_year
        )
    )

    # --------------------------------------------------------------
    # Effective revenue target / operating budget
    # --------------------------------------------------------------

    revenue_target_locked = (
        scenario_year.year_number
        == 1
    )

    if annual_plan is not None:
        effective_revenue_target = (
            annual_plan
            .effective_revenue_target
        )

        effective_operating_budget = (
            annual_plan
            .effective_operating_budget
        )
    else:
        effective_revenue_target = (
            scenario_year.revenue_target
        )

        effective_operating_budget = (
            scenario_year.operating_budget
        )

    # --------------------------------------------------------------
    # Sales-credit alternatives
    # --------------------------------------------------------------

    sales_credit_options = []

    credit_rates = [
        Decimal("0.05"),
        Decimal("0.10"),
        Decimal("0.20"),
    ]

    for rate in credit_rates:
        minimum_price_ratio = (
            Decimal("1")
            - rate
        )

        product_price_rows = []

        for product in products:
            minimum_price = (
                product.standard_price
                * minimum_price_ratio
            )

            maximum_credit_per_unit = (
                product.standard_price
                * rate
            )

            product_price_rows.append({
                "product": product,
                "standard_price": (
                    product.standard_price
                ),
                "minimum_price": (
                    minimum_price
                ),
                "maximum_credit_per_unit": (
                    maximum_credit_per_unit
                ),
            })

        sales_credit_options.append({
            "rate": rate,
            "percent": int(
                rate
                * Decimal("100")
            ),
            "target": (
                effective_revenue_target
                * rate
            ),
            "minimum_price_ratio": (
                minimum_price_ratio
            ),
            "minimum_price_percent": int(
                minimum_price_ratio
                * Decimal("100")
            ),
            "product_price_rows": (
                product_price_rows
            ),
            "maximum_flexibility": (
                rate
                == Decimal("0.20")
            ),
        })

    # --------------------------------------------------------------
    # Product briefing for Mrs Clarke
    # --------------------------------------------------------------

    forecast_lookup = {
        product.id: rows
        for product, rows
        in august_forecasts.items()
    }

    product_rows = []

    for cost_row in (
        cost_parameters[
            "product_rows"
        ]
    ):
        product = (
            cost_row[
                "product"
            ]
        )

        forecast_rows = (
            forecast_lookup.get(
                product.id,
                [],
            )
        )

        annual_forecast_units = sum(
            row.forecast_units
            for row in forecast_rows
        )

        product_rows.append({
            **cost_row,
            "forecast_rows": (
                forecast_rows
            ),
            "annual_forecast_units": (
                annual_forecast_units
            ),
            "standard_price": (
                product.standard_price
            ),
            "variable_unit_cost": (
                product.variable_unit_cost
            ),
            "full_unit_cost": (
                cost_row[
                    "full_unit_cost"
                ]
            ),
            "unit_cost_without_maintenance": (
                cost_row[
                    "unit_cost_without_maintenance"
                ]
            ),
        })

    # --------------------------------------------------------------
    # Maintenance comparison for Ms Nauta
    # --------------------------------------------------------------

    maintenance_product_rows = []

    for cost_row in (
        cost_parameters[
            "product_rows"
        ]
    ):
        product = (
            cost_row["product"]
        )

        maintenance_product_rows.append({
            "product": product,
            "with_maintenance": (
                cost_row[
                    "full_unit_cost"
                ]
            ),
            "without_maintenance": (
                cost_row[
                    "unit_cost_without_maintenance"
                ]
            ),
            "maintenance_cost_per_unit": (
                cost_row[
                    "maintenance_fixed_cost_per_unit"
                ]
            ),
            "variable_unit_cost": (
                product.variable_unit_cost
            ),
        })

    # --------------------------------------------------------------
    # Current selected board policy
    # --------------------------------------------------------------

    if annual_plan is not None:
        selected_credit_rate = (
            annual_plan
            .sales_credit_rate
        )

        selected_skip_maintenance = (
            annual_plan
            .skip_maintenance
        )

    else:
        selected_credit_rate = (
            Decimal("0.10")
        )

        selected_skip_maintenance = (
            False
        )

    selected_sales_credit_target = (
        effective_revenue_target
        * selected_credit_rate
    )

    selected_minimum_price_ratio = (
        Decimal("1")
        - selected_credit_rate
    )

    # --------------------------------------------------------------
    # Selected revenue option code
    # --------------------------------------------------------------

    selected_revenue_option_code = (
        "BASELINE"
    )

    if (
        annual_plan is not None
        and annual_plan.selected_revenue_target
        is not None
    ):
        for option in revenue_budget_options:
            if (
                Decimal(
                    option[
                        "revenue_target"
                    ]
                )
                == Decimal(
                    annual_plan
                    .selected_revenue_target
                )
            ):
                selected_revenue_option_code = (
                    option["code"]
                )
                break

    # --------------------------------------------------------------
    # Board summary
    # --------------------------------------------------------------

    board_summary = {
        "revenue_target": (
            effective_revenue_target
        ),
        "operating_budget": (
            effective_operating_budget
        ),
        "sales_credit_rate": (
            selected_credit_rate
        ),
        "sales_credit_target": (
            selected_sales_credit_target
        ),
        "minimum_price_ratio": (
            selected_minimum_price_ratio
        ),
        "maintenance_deferred": (
            selected_skip_maintenance
        ),
        "quality_capacity_multiplier": (
            Decimal("0.80")
            if selected_skip_maintenance
            else Decimal("1.00")
        ),
    }

    # --------------------------------------------------------------
    # Board-member information
    # --------------------------------------------------------------

    board_members = {
        "clarke": {
            "name": "Mrs Clarke",
            "initials": "MC",
            "title": "Chair",
            "focus": (
                "Overall business performance, "
                "revenue targets, and annual "
                "strategic results."
            ),
        },

        "daniels": {
            "name": "Mr Daniels",
            "initials": "MD",
            "title": (
                "Board Member · Commercial"
            ),
            "focus": (
                "Pricing discipline, monthly P&L, "
                "and sales-credit performance."
            ),
        },

        "nauta": {
            "name": "Ms Nauta",
            "initials": "MN",
            "title": (
                "Board Member · Operations"
            ),
            "focus": (
                "Forecasting, inventory, "
                "replenishment, working capital, "
                "and maintenance."
            ),
        },
    }

    # --------------------------------------------------------------
    # Year-specific boardroom guidance
    # --------------------------------------------------------------

    year_2_boardroom = (
        scenario_year.year_number
        == 2
    )

    context = {
        "game": game,
        "membership": (
            membership
        ),
        "scenario_year": (
            scenario_year
        ),

        "form": form,

        "board_members": (
            board_members
        ),

        "products": (
            products
        ),
        "rolling": (
            august_forecasts
        ),
        "product_rows": (
            product_rows
        ),

        "cost_parameters": (
            cost_parameters
        ),
        "maintenance_product_rows": (
            maintenance_product_rows
        ),

        "revenue_target_locked": (
            revenue_target_locked
        ),
        "effective_revenue_target": (
            effective_revenue_target
        ),
        "effective_operating_budget": (
            effective_operating_budget
        ),

        "revenue_budget_analysis": (
            revenue_budget_analysis
        ),
        "revenue_budget_options": (
            revenue_budget_options
        ),
        "selected_revenue_option_code": (
            selected_revenue_option_code
        ),

        "sales_credit_options": (
            sales_credit_options
        ),

        "board_summary": (
            board_summary
        ),

        "year_2_boardroom": (
            year_2_boardroom
        ),

        # Used directly by annual_planning.html to switch
        # from the Year 1 introduction flow to the Year 2
        # strategy-session flow.
        "is_year_2": (
            year_2_boardroom
        ),

        "market_expansion_percent": (
            revenue_budget_analysis.get(
                "market_expansion_percent",
                Decimal("0"),
            )
        ),
    }

    return render(
        request,
        "simulation/annual_planning.html",
        context,
    )


# ---------------------------------------------------------------------
# Monthly review annual outlook
# ---------------------------------------------------------------------

def _annual_review_outlook(
    *,
    membership,
    scenario_year,
    review_month,
    products,
    attempt=None,
):
    """
    Build the fixed September-August demand outlook used on the
    monthly result page.

    Every review always contains the same twelve fiscal-month columns.

    Completed months, including the month being reviewed:
        display realized demand after the player's pricing and capability decisions.

    Future months:
        display the latest forecast vintage released in the review month.

    Examples
    --------
    November review:
        September-November = REALIZED
        December-August    = FORECAST using base_month=November

    July review:
        September-July = REALIZED
        August         = FORECAST using base_month=July

    August review:
        September-August = REALIZED
        no forecast columns remain

    This deliberately prevents the August review from falling back to
    the initial August AOP forecast vintage.
    """

    _validate_fiscal_month(
        review_month
    )

    review_position = (
        FISCAL_MONTH_POSITION[
            review_month
        ]
    )

    realized_months = list(
        FISCAL_MONTH_NUMBERS[
            :review_position + 1
        ]
    )

    forecast_months = list(
        FISCAL_MONTH_NUMBERS[
            review_position + 1:
        ]
    )

    realized_lines = (
        ProductDecision.objects
        .filter(
            monthly_decision__membership=membership,
            monthly_decision__scenario_year=scenario_year,
            monthly_decision__month__in=realized_months,
            monthly_decision__is_finalized=True,
            **attempt_filter(
                attempt,
                prefix="monthly_decision__",
            ),
        )
        .select_related(
            "product",
            "monthly_decision",
        )
    )

    realized_map = {
        (
            line.monthly_decision.month,
            line.product_id,
        ): line.adjusted_demand_units
        for line in realized_lines
    }

    forecast_map = {}

    if forecast_months:
        forecast_rows = (
            ForecastSnapshot.objects
            .filter(
                scenario_year=scenario_year,
                base_month=review_month,
                forecast_month__in=forecast_months,
            )
            .select_related(
                "product"
            )
        )

        forecast_map = {
            (
                row.forecast_month,
                row.product_id,
            ): row.forecast_units
            for row in forecast_rows
        }

    month_columns = []

    for fiscal_month in FISCAL_MONTH_NUMBERS:
        is_realized = (
            fiscal_month in realized_months
        )

        month_columns.append({
            "month": fiscal_month,
            "month_name": MONTH_NAMES[
                fiscal_month
            ],
            "month_short": (
                MONTH_NAMES[
                    fiscal_month
                ][:3]
            ),
            "status": (
                "REALIZED"
                if is_realized
                else "FORECAST"
            ),
            "is_realized": is_realized,
            "is_forecast": (
                not is_realized
            ),
        })

    product_rows = []

    for product in products:
        cells = []

        for month_column in month_columns:
            fiscal_month = (
                month_column[
                    "month"
                ]
            )

            if month_column[
                "is_realized"
            ]:
                value = realized_map.get(
                    (
                        fiscal_month,
                        product.id,
                    )
                )
            else:
                value = forecast_map.get(
                    (
                        fiscal_month,
                        product.id,
                    )
                )

            cells.append({
                **month_column,
                "value": value,
            })

        product_rows.append({
            "product": product,
            "cells": cells,
        })

    return {
        "month_columns": month_columns,
        "product_rows": product_rows,
        "realized_months": realized_months,
        "forecast_months": forecast_months,
        "realized_colspan": len(
            realized_months
        ),
        "forecast_colspan": len(
            forecast_months
        ),
        "has_forecast": bool(
            forecast_months
        ),
        "review_month": review_month,
        "review_month_name": (
            MONTH_NAMES[
                review_month
            ]
        ),
    }


# ---------------------------------------------------------------------
# Monthly planning and simulation
# ---------------------------------------------------------------------

@login_required
@transaction.atomic
def plan_month(
    request,
    code,
    year,
    month,
):
    _validate_fiscal_month(
        month
    )

    game = get_object_or_404(
        Game,
        code=code,
    )

    membership = _player_or_403(
        request.user,
        game,
    )

    scenario_year = (
        _scenario_year_or_404(
            game,
            year,
        )
    )

    attempt = _current_year_attempt(
        membership=membership,
        scenario_year=scenario_year,
        create=True,
    )

    annual_plan = (
        AnnualPlan.objects
        .filter(
            membership=membership,
            scenario_year=scenario_year,
            **attempt_filter(attempt),
        )
        .first()
    )

    if annual_plan is None:
        messages.info(
            request,
            "Complete the Annual Operating Plan before beginning monthly S&OP.",
        )

        return redirect(
            "annual_planning",
            code=game.code,
            year=scenario_year.year_number,
        )

    expected_month = (
        _next_incomplete_month(
            membership=membership,
            scenario_year=scenario_year,
            attempt=attempt,
        )
    )

    existing_requested_decision = (
        MonthlyDecision.objects
        .filter(
            membership=membership,
            scenario_year=scenario_year,
            month=month,
            is_finalized=True,
            **attempt_filter(attempt),
        )
        .first()
    )

    if (
        expected_month is not None
        and month != expected_month
        and existing_requested_decision is None
    ):
        messages.info(
            request,
            (
                "Monthly S&OP must be completed in fiscal order. "
                f"Continue with {MONTH_NAMES[expected_month]}."
            ),
        )

        return redirect(
            "plan_month",
            code=game.code,
            year=scenario_year.year_number,
            month=expected_month,
        )

    products = list(
        Product.objects
        .all()
        .order_by("name")
    )

    # --------------------------------------------------------------
    # Existing monthly decision
    # --------------------------------------------------------------

    existing_decision = (
        MonthlyDecision.objects
        .filter(
            membership=membership,
            scenario_year=scenario_year,
            month=month,
            **attempt_filter(attempt),
        )
        .prefetch_related(
            "product_decisions__product"
        )
        .first()
    )

    if (
        existing_decision is not None
        and existing_decision.is_finalized
    ):
        return redirect(
            "month_result",
            code=game.code,
            year=(
                scenario_year.year_number
            ),
            month=month,
        )

    # --------------------------------------------------------------
    # Forecast vintage
    # --------------------------------------------------------------

    previous_base_month = (
        _previous_forecast_base_month(
            month
        )
    )

    forecast_rows = (
        ForecastSnapshot.objects
        .filter(
            scenario_year=scenario_year,
            base_month=(
                previous_base_month
            ),
            forecast_month=month,
        )
        .select_related(
            "product"
        )
    )

    forecast_map = {
        row.product_id: (
            row.forecast_units
        )
        for row in forecast_rows
    }

    # --------------------------------------------------------------
    # Beginning inventory
    # --------------------------------------------------------------

    beginning_inventory_map = (
        beginning_inventory_by_product(
            membership=membership,
            scenario_year=scenario_year,
            month=month,
            products=products,
            attempt=attempt,
        )
    )

    # --------------------------------------------------------------
    # Planning analytics
    #
    # Year 1:
    #     ±10% uncertainty
    #     guided base-stock / replenishment
    #
    # Year 2:
    #     ±25% uncertainty
    #     critical ratio shown
    #     no automatic replenishment recommendation
    #     pricing may be used to liquidate excess inventory
    # --------------------------------------------------------------

    planning_analysis = (
        monthly_planning_analysis(
            scenario_year=scenario_year,
            annual_plan=annual_plan,
            products=products,
            forecast_map=forecast_map,
            beginning_inventory_map=(
                beginning_inventory_map
            ),
            month=month,
        )
    )

    analysis_rows = (
        planning_analysis[
            "rows"
        ]
    )

    cost_parameters = (
        planning_analysis[
            "cost_parameters"
        ]
    )

    show_replenishment_guidance = (
        planning_analysis[
            "show_replenishment_guidance"
        ]
    )

    uncertainty_rate = (
        planning_analysis[
            "uncertainty_rate"
        ]
    )

    uncertainty_percent = (
        planning_analysis[
            "uncertainty_percent"
        ]
    )

    allow_inventory_liquidation_pricing = (
        planning_analysis[
            "allow_inventory_liquidation_pricing"
        ]
    )

    # --------------------------------------------------------------
    # Monthly operating-spend guardrail
    #
    # This is informational on GET and is also used by the form and
    # services layer as a hard validation rule on POST.
    # --------------------------------------------------------------

    ordering_guardrail = (
        planning_analysis[
            "ordering_guardrail"
        ]
    )

    # --------------------------------------------------------------
    # Recommended replenishment map
    #
    # Only Year 1 contains actual recommendations.
    # --------------------------------------------------------------

    recommended_replenishment_map = {}

    if show_replenishment_guidance:
        recommended_replenishment_map = {
            row["product"].id: (
                row[
                    "recommended_replenishment_units"
                ]
            )
            for row in analysis_rows
            if (
                row[
                    "recommended_replenishment_units"
                ]
                is not None
            )
        }

    # --------------------------------------------------------------
    # Initial form values
    # --------------------------------------------------------------

    initial = {}

    if existing_decision is not None:
        for line in (
            existing_decision
            .product_decisions
            .all()
        ):
            initial[
                f"inventory_{line.product_id}"
            ] = (
                line.replenishment_units
            )

            initial[
                f"price_{line.product_id}"
            ] = (
                line.selling_price
            )

    else:
        for row in analysis_rows:
            product = (
                row["product"]
            )

            if show_replenishment_guidance:
                initial[
                    f"inventory_{product.id}"
                ] = (
                    row[
                        "recommended_replenishment_units"
                    ]
                )
            else:
                # Year 2 deliberately starts without
                # an order recommendation.
                initial[
                    f"inventory_{product.id}"
                ] = 0

            initial[
                f"price_{product.id}"
            ] = (
                product.standard_price
            )

    # --------------------------------------------------------------
    # Dynamic monthly form
    # --------------------------------------------------------------

    form_kwargs = {
        "products": products,
        "annual_plan": (
            annual_plan
        ),
        "attempt": attempt,
        "forecasts": (
            forecast_map
        ),
        "beginning_inventory": (
            beginning_inventory_map
        ),
        "recommended_replenishment": (
            recommended_replenishment_map
        ),
    }

    # --------------------------------------------------------------
    # POST
    # --------------------------------------------------------------

    if request.method == "POST":
        form = MonthlyDecisionForm(
            request.POST,
            **form_kwargs,
        )

        if form.is_valid():

            # ------------------------------------------------------
            # Savepoint-protected finalization
            #
            # MonthlyDecisionForm performs the user-facing guardrail
            # checks first. run_month() repeats the same rules
            # defensively so invalid decisions cannot be finalized if
            # the UI/form layer is bypassed.
            #
            # The nested atomic block is important: if defensive
            # validation raises ValidationError, any MonthlyDecision
            # or ProductDecision changes made during this attempt are
            # rolled back while the page itself can still be rendered
            # with a clear form error instead of producing a 500 error.
            # ------------------------------------------------------

            try:
                with transaction.atomic():
                    decision, _ = (
                        MonthlyDecision.objects
                        .get_or_create(
                            membership=membership,
                            scenario_year=(
                                scenario_year
                            ),
                            month=month,
                            attempt=attempt,
                        )
                    )

                    if decision.is_finalized:
                        messages.info(
                            request,
                            (
                                f"{decision.get_month_display()} "
                                "has already been finalized."
                            ),
                        )

                        return redirect(
                            "month_result",
                            code=game.code,
                            year=(
                                scenario_year
                                .year_number
                            ),
                            month=month,
                        )

                    for product in products:
                        replenishment_units = (
                            form.cleaned_data[
                                f"inventory_{product.id}"
                            ]
                        )

                        selling_price = (
                            form.cleaned_data[
                                f"price_{product.id}"
                            ]
                        )

                        product_decision, _ = (
                            ProductDecision.objects
                            .update_or_create(
                                monthly_decision=(
                                    decision
                                ),
                                product=product,
                                defaults={
                                    "replenishment_units": (
                                        replenishment_units
                                    ),
                                    "selling_price": (
                                        selling_price
                                    ),
                                },
                            )
                        )

                        product_decision.full_clean()

                    run_month(
                        decision
                    )

            except ValidationError as exc:
                error_messages = (
                    exc.messages
                    if hasattr(
                        exc,
                        "messages",
                    )
                    else [
                        str(exc)
                    ]
                )

                for error_message in error_messages:
                    form.add_error(
                        None,
                        error_message,
                    )

                messages.error(
                    request,
                    (
                        "The monthly S&OP plan could not be finalized. "
                        "Review the warning below and adjust the "
                        "replenishment quantity or selling price."
                    ),
                )

            else:
                messages.success(
                    request,
                    (
                        f"{decision.get_month_display()} "
                        "simulation completed."
                    ),
                )

                return redirect(
                    "month_result",
                    code=game.code,
                    year=(
                        scenario_year.year_number
                    ),
                    month=month,
                )

    else:
        form = MonthlyDecisionForm(
            initial=initial,
            **form_kwargs,
        )

    # --------------------------------------------------------------
    # Attach form fields to analysis rows
    # --------------------------------------------------------------

    decision_rows = []

    for row in analysis_rows:
        product = (
            row["product"]
        )

        decision_rows.append({
            **row,
            "price_field": (
                form[
                    f"price_{product.id}"
                ]
            ),
            "replenishment_field": (
                form[
                    f"inventory_{product.id}"
                ]
            ),
        })

    # --------------------------------------------------------------
    # Previous market result
    # --------------------------------------------------------------

    previous_result = None

    if month != 9:
        previous_result = (
            MonthlyDecision.objects
            .filter(
                membership=membership,
                scenario_year=(
                    scenario_year
                ),
                month=(
                    previous_base_month
                ),
                is_finalized=True,
                **attempt_filter(attempt),
            )
            .prefetch_related(
                "product_decisions__product"
            )
            .first()
        )

    # --------------------------------------------------------------
    # Full rolling-horizon forecast available this month
    # --------------------------------------------------------------

    rolling_forecasts = (
        current_forecasts(
            scenario_year=(
                scenario_year
            ),
            base_month=(
                previous_base_month
            ),
        )
    )

    # --------------------------------------------------------------
    # Context
    # --------------------------------------------------------------

    context = {
        "game": game,
        "membership": (
            membership
        ),
        "scenario_year": (
            scenario_year
        ),
        "annual_plan": (
            annual_plan
        ),

        "month": month,
        "month_name": (
            MONTH_NAMES[
                month
            ]
        ),

        "form": form,
        "products": products,

        "forecast_map": (
            forecast_map
        ),
        "rolling": (
            rolling_forecasts
        ),

        "decision_rows": (
            decision_rows
        ),

        "beginning_inventory_map": (
            beginning_inventory_map
        ),

        "cost_parameters": (
            cost_parameters
        ),

        "previous_result": (
            previous_result
        ),

        "previous_base_month": (
            previous_base_month
        ),
        "previous_base_month_name": (
            MONTH_NAMES[
                previous_base_month
            ]
        ),

        "sales_credit_rate": (
            annual_plan
            .sales_credit_rate
        ),
        "sales_credit_target": (
            annual_plan
            .sales_credit_target
        ),
        "minimum_price_ratio": (
            annual_plan
            .minimum_price_ratio
        ),

        # Year-specific decision-support behavior.
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

        # Monthly operating-spend guardrail.
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
        "variable_cost_spend_ceiling_percent": (
            ordering_guardrail[
                "variable_cost_ceiling_percent"
            ]
        ),

        "is_year_2": (
            scenario_year.year_number
            == 2
        ),
    }

    return render(
        request,
        "simulation/plan_month.html",
        context,
    )


@login_required
def month_result(request, code, year, month):
    _validate_fiscal_month(month)

    game = get_object_or_404(Game, code=code)
    membership = _player_or_403(request.user, game)
    scenario_year = _scenario_year_or_404(game, year)

    attempt = _current_year_attempt(
        membership=membership,
        scenario_year=scenario_year,
        create=False,
    )

    annual_plan = get_object_or_404(
        AnnualPlan,
        membership=membership,
        scenario_year=scenario_year,
        **attempt_filter(attempt),
    )

    decision = get_object_or_404(
        MonthlyDecision.objects.prefetch_related(
            "product_decisions__product"
        ),
        membership=membership,
        scenario_year=scenario_year,
        month=month,
        is_finalized=True,
        **attempt_filter(attempt),
    )

    score = (
        PlayerYearScore.objects
        .filter(
            membership=membership,
            scenario_year=scenario_year,
            **attempt_filter(attempt),
        )
        .first()
    )

    forecast_base_month = _previous_forecast_base_month(month)

    forecast_rows = (
        ForecastSnapshot.objects
        .filter(
            scenario_year=scenario_year,
            base_month=forecast_base_month,
            forecast_month=month,
        )
        .select_related("product")
    )

    forecast_map = {
        row.product_id: row.forecast_units
        for row in forecast_rows
    }

    result_rows = [
        {
            "line": line,
            "forecast_units": forecast_map.get(
                line.product_id,
                0,
            ),
        }
        for line in decision.product_decisions.all()
    ]

    next_month = next_fiscal_month(month)

    products = list(
        Product.objects.all().order_by("name")
    )

    annual_outlook = _annual_review_outlook(
        membership=membership,
        scenario_year=scenario_year,
        review_month=month,
        products=products,
        attempt=attempt,
    )

    context = {
        "game": game,
        "membership": membership,
        "scenario_year": scenario_year,
        "attempt": attempt,
        "annual_plan": annual_plan,
        "decision": decision,
        "score": score,
        "forecast_base_month": forecast_base_month,
        "forecast_base_month_name": MONTH_NAMES[forecast_base_month],
        "forecast_map": forecast_map,
        "result_rows": result_rows,
        "next_month": next_month,
        "next_month_name": MONTH_NAMES.get(next_month),
        "annual_outlook": annual_outlook,
        "annual_outlook_months": annual_outlook["month_columns"],
        "annual_outlook_rows": annual_outlook["product_rows"],
        "realized_colspan": annual_outlook["realized_colspan"],
        "forecast_colspan": annual_outlook["forecast_colspan"],
        "has_forecast_outlook": annual_outlook["has_forecast"],
        "is_year_complete": next_month is None,
    }

    return render(
        request,
        "simulation/month_result.html",
        context,
    )


@login_required
@require_POST
@transaction.atomic
def advance_year(request, code, year):
    """
    Advance from the current completed attempt to the next scenario year.
    Prior attempts remain immutable history.
    """
    game = get_object_or_404(Game, code=code)
    membership = _player_or_403(request.user, game)
    scenario_year = _scenario_year_or_404(game, year)

    attempt = _current_year_attempt(
        membership=membership,
        scenario_year=scenario_year,
        create=False,
    )

    completed_months = set(
        MonthlyDecision.objects
        .filter(
            membership=membership,
            scenario_year=scenario_year,
            is_finalized=True,
            **attempt_filter(attempt),
        )
        .values_list("month", flat=True)
    )

    if completed_months != set(FISCAL_MONTH_NUMBERS):
        messages.error(
            request,
            (
                f"Year {year} cannot be closed yet. "
                "All twelve monthly S&OP cycles must be finalized."
            ),
        )
        return redirect("game_dashboard", code=game.code)

    if membership.last_reviewed_year < scenario_year.year_number:
        membership.last_reviewed_year = scenario_year.year_number
        membership.save(update_fields=["last_reviewed_year"])

    next_year_number = scenario_year.year_number + 1
    next_scenario_year = (
        ScenarioYear.objects
        .filter(
            game=game,
            year_number=next_year_number,
        )
        .first()
    )

    if next_scenario_year is None:
        messages.info(
            request,
            "There is no additional scenario year available yet.",
        )
        return redirect("game_dashboard", code=game.code)

    next_attempt = _current_year_attempt(
        membership=membership,
        scenario_year=next_scenario_year,
        create=False,
    )

    next_plan = (
        AnnualPlan.objects
        .filter(
            membership=membership,
            scenario_year=next_scenario_year,
            **attempt_filter(next_attempt),
        )
        .first()
    )

    if next_plan is None:
        if next_attempt is None:
            next_attempt = _current_year_attempt(
                membership=membership,
                scenario_year=next_scenario_year,
                create=True,
            )

        game.current_year = next_year_number
        game.current_month = 8
        game.save(update_fields=["current_year", "current_month"])

        messages.success(
            request,
            (
                f"Year {year} is complete. "
                f"Welcome to Year {next_year_number}. "
                "Begin with the Annual Operating Plan board meeting."
            ),
        )
        return redirect(
            "annual_planning",
            code=game.code,
            year=next_year_number,
        )

    messages.info(
        request,
        (
            f"Year {next_year_number} has already been started. "
            "Resuming your saved progress."
        ),
    )
    return redirect("game_dashboard", code=game.code)


@login_required
@require_POST
@transaction.atomic
def reset_year(request, code, year):
    """
    Redo one year by creating a new attempt.

    Historical AnnualPlan, MonthlyDecision, ProductDecision and
    PlayerYearScore rows are preserved.
    """
    game = get_object_or_404(Game, code=code)
    membership = _player_or_403(request.user, game)
    scenario_year = _scenario_year_or_404(game, year)
    settings_obj = _game_settings(game)

    if (
        scenario_year.year_number == 1
        and not settings_obj.allow_redo_year_1
    ):
        raise PermissionDenied(
            "Your instructor has not enabled a Year 1 redo for this simulation."
        )

    if (
        scenario_year.year_number == 2
        and not settings_obj.allow_redo_year_2
    ):
        raise PermissionDenied(
            "Your instructor has not enabled a Year 2 redo for this simulation."
        )

    membership.results_summary_reached = False
    membership.last_reviewed_year = min(
        membership.last_reviewed_year,
        scenario_year.year_number - 1,
    )
    membership.save(
        update_fields=[
            "results_summary_reached",
            "last_reviewed_year",
        ]
    )

    attempt = _start_new_attempt(
        membership=membership,
        scenario_year=scenario_year,
    )

    game.current_year = scenario_year.year_number
    game.current_month = 8
    game.save(update_fields=["current_year", "current_month"])

    messages.success(
        request,
        (
            f"Year {scenario_year.year_number} Attempt "
            f"{attempt.attempt_number} is ready. "
            "Your earlier attempt has been preserved for instructor review."
        ),
    )

    return redirect(
        "annual_planning",
        code=game.code,
        year=scenario_year.year_number,
    )


@login_required
@require_POST
@transaction.atomic
def restart_from_year_one(request, code):
    """
    Restart the simulation from Year 1 without deleting attempt history.
    """
    game = get_object_or_404(Game, code=code)
    membership = _player_or_403(request.user, game)
    settings_obj = _game_settings(game)

    if not settings_obj.allow_full_restart:
        raise PermissionDenied(
            "Your instructor has not enabled a full simulation restart."
        )

    first_scenario_year = (
        ScenarioYear.objects
        .filter(game=game)
        .order_by("year_number")
        .first()
    )

    if first_scenario_year is None:
        raise PermissionDenied(
            "No scenario years are configured for this game."
        )

    # No historical result rows are deleted. Any currently active attempts
    # in later years are retired so Year 1 becomes the governing path.
    current_attempts = list(
        YearAttempt.objects
        .select_for_update()
        .filter(
            membership=membership,
            scenario_year__game=game,
            is_current=True,
        )
    )

    for old_attempt in current_attempts:
        old_attempt.is_current = False
        if old_attempt.status == YearAttempt.Status.ACTIVE:
            old_attempt.status = YearAttempt.Status.ABANDONED
            old_attempt.save(
                update_fields=["is_current", "status"]
            )
        else:
            old_attempt.save(update_fields=["is_current"])

    attempt = _start_new_attempt(
        membership=membership,
        scenario_year=first_scenario_year,
    )

    membership.results_summary_reached = False
    membership.last_reviewed_year = 0
    membership.save(
        update_fields=[
            "results_summary_reached",
            "last_reviewed_year",
        ]
    )

    game.current_year = first_scenario_year.year_number
    game.current_month = 8
    game.save(update_fields=["current_year", "current_month"])

    messages.success(
        request,
        (
            "Your simulation has been restarted from Year 1. "
            f"Year 1 Attempt {attempt.attempt_number} is now active; "
            "all prior attempts remain preserved."
        ),
    )

    return redirect(
        "annual_planning",
        code=game.code,
        year=first_scenario_year.year_number,
    )


def _build_results_summary_context(
    *,
    game,
    membership,
    mark_player_reached=False,
):
    """
    Build the combined summary from the latest completed attempt in each year.
    """
    scenario_years = list(
        ScenarioYear.objects
        .filter(game=game)
        .order_by("year_number")
    )

    if not scenario_years:
        raise PermissionDenied(
            "No scenario years are configured for this game."
        )

    required_months = set(FISCAL_MONTH_NUMBERS)
    year_rows = []

    for scenario_year in scenario_years:
        attempt = _latest_completed_attempt(
            membership=membership,
            scenario_year=scenario_year,
        )

        annual_plan = (
            AnnualPlan.objects
            .filter(
                membership=membership,
                scenario_year=scenario_year,
                **attempt_filter(attempt),
            )
            .order_by("-updated_at")
            .first()
        )

        if annual_plan is None:
            raise PermissionDenied(
                "This player has not completed all simulation years."
            )

        completed_months = set(
            MonthlyDecision.objects
            .filter(
                membership=membership,
                scenario_year=scenario_year,
                is_finalized=True,
                **attempt_filter(attempt),
            )
            .values_list("month", flat=True)
        )

        if completed_months != required_months:
            raise PermissionDenied(
                "This player has not completed all twelve monthly cycles in every simulation year."
            )

        review = year_end_review_analysis(
            membership=membership,
            scenario_year=scenario_year,
            attempt=attempt,
        )

        score = (
            PlayerYearScore.objects
            .filter(
                membership=membership,
                scenario_year=scenario_year,
                **attempt_filter(attempt),
            )
            .order_by("-updated_at")
            .first()
        )

        if score is None:
            raise PermissionDenied(
                f"Year {scenario_year.year_number} results are incomplete."
            )

        year_rows.append({
            "scenario_year": scenario_year,
            "attempt": attempt,
            "annual_plan": annual_plan,
            "score": score,
            "review": review,
            "clarke": review["clarke"],
            "daniels": review["daniels"],
            "nauta": review["nauta"],
            "rewards": review["rewards"],
        })

    year_1 = next(
        (
            row for row in year_rows
            if row["scenario_year"].year_number == 1
        ),
        None,
    )
    year_2 = next(
        (
            row for row in year_rows
            if row["scenario_year"].year_number == 2
        ),
        None,
    )

    if year_1 is None or year_2 is None:
        raise PermissionDenied(
            "The combined results summary requires both Year 1 and Year 2."
        )

    total_rewards_earned = sum(
        row["rewards"]["earned"]
        for row in year_rows
    )
    total_rewards_available = sum(
        row["rewards"]["available"]
        for row in year_rows
    )
    final_year_number = max(
        row["scenario_year"].year_number
        for row in year_rows
    )

    if mark_player_reached:
        changed = False

        if not membership.results_summary_reached:
            membership.results_summary_reached = True
            changed = True

        if membership.last_reviewed_year < final_year_number:
            membership.last_reviewed_year = final_year_number
            changed = True

        if changed:
            membership.save(
                update_fields=[
                    "results_summary_reached",
                    "last_reviewed_year",
                ]
            )

    return {
        "game": game,
        "membership": membership,
        "year_rows": year_rows,
        "year_1": year_1,
        "year_2": year_2,
        "total_rewards_earned": total_rewards_earned,
        "total_rewards_available": total_rewards_available,
        "design_settings": _game_settings(game),
    }

@login_required
def results_summary(request, code):
    """
    Player-facing combined Year 1 / Year 2 results summary.
    """
    game = get_object_or_404(
        Game,
        code=code,
    )

    membership = _player_or_403(
        request.user,
        game,
    )

    try:
        context = (
            _build_results_summary_context(
                game=game,
                membership=membership,
                mark_player_reached=True,
            )
        )
    except PermissionDenied as exc:
        messages.info(
            request,
            str(exc),
        )

        return redirect(
            "game_dashboard",
            code=game.code,
        )

    context["instructor_read_only"] = False

    return render(
        request,
        "simulation/results_summary.html",
        context,
    )


@login_required
def instructor_player_summary(
    request,
    code,
    membership_id,
):
    """
    Instructor read-only view of one player's final two-year summary.
    """
    game = get_object_or_404(
        Game,
        code=code,
    )

    _admin_or_403(
        request.user,
        game,
    )

    membership = get_object_or_404(
        GameMembership.objects.select_related(
            "user"
        ),
        pk=membership_id,
        game=game,
        role=GameMembership.Role.PLAYER,
    )

    context = (
        _build_results_summary_context(
            game=game,
            membership=membership,
            mark_player_reached=False,
        )
    )

    context[
        "instructor_read_only"
    ] = True

    return render(
        request,
        "simulation/results_summary.html",
        context,
    )


# ---------------------------------------------------------------------
# Administrator views
# ---------------------------------------------------------------------

@login_required
def admin_game_dashboard(request, code):
    """
    Instructor dashboard with invitation access, player progress,
    individual summary links, and cohort descriptive statistics.
    """
    game = get_object_or_404(
        Game,
        code=code,
    )

    admin_membership = _admin_or_403(
        request.user,
        game,
    )

    settings_obj = _game_settings(
        game
    )

    scenario_years = list(
        ScenarioYear.objects
        .filter(game=game)
        .order_by("year_number")
    )

    players = list(
        game.memberships
        .filter(
            role=GameMembership.Role.PLAYER
        )
        .select_related("user")
        .order_by(
            "user__last_name",
            "user__first_name",
            "user__username",
        )
    )

    player_rows = []

    for membership in players:
        year_cells = []
        all_years_complete = True

        for scenario_year in scenario_years:
            completed_attempt = _latest_completed_attempt(
                membership=membership,
                scenario_year=scenario_year,
            )

            score = (
                PlayerYearScore.objects
                .filter(
                    membership=membership,
                    scenario_year=scenario_year,
                    **attempt_filter(completed_attempt),
                )
                .order_by("-updated_at")
                .first()
            )

            if score is None:
                all_years_complete = False

            year_cells.append({
                "scenario_year": scenario_year,
                "score": score,
            })

        if membership.results_summary_reached:
            progress_label = "Completed"
        else:
            progress_year = (
                _player_progress_year(
                    game=game,
                    membership=membership,
                )
            )

            progress_attempt = _current_year_attempt(
                membership=membership,
                scenario_year=progress_year,
                create=False,
            )

            next_month = (
                _next_incomplete_month(
                    membership=membership,
                    scenario_year=progress_year,
                    attempt=progress_attempt,
                )
            )

            if next_month is None:
                progress_label = (
                    f"Year {progress_year.year_number} review"
                )
            else:
                progress_label = (
                    f"Year {progress_year.year_number} · "
                    f"{MONTH_NAMES[next_month]}"
                )

        player_rows.append({
            "membership": membership,
            "user": membership.user,
            "year_cells": year_cells,
            "progress_label": progress_label,
            "can_view_summary": (
                all_years_complete
                and bool(scenario_years)
            ),
        })

    # --------------------------------------------------------------
    # Cohort statistics
    #
    # Cohort statistics use each player's latest completed attempt for the year.
    # Earlier attempts remain stored but are not double-counted.
    # --------------------------------------------------------------

    cohort_years = []

    for scenario_year in scenario_years:
        scores = []

        for membership in players:
            completed_attempt = _latest_completed_attempt(
                membership=membership,
                scenario_year=scenario_year,
            )

            score = (
                PlayerYearScore.objects
                .filter(
                    membership=membership,
                    scenario_year=scenario_year,
                    **attempt_filter(completed_attempt),
                )
                .order_by("-updated_at")
                .first()
            )

            if score is not None:
                scores.append(score)

        revenue_values = [
            score.revenue
            for score in scores
        ]

        expense_values = [
            score.operating_expense
            for score in scores
        ]

        profit_values = [
            score.profit
            for score in scores
        ]

        credit_values = [
            score.sales_credit_collected
            for score in scores
        ]

        lost_sales_values = [
            Decimal(score.lost_sales_units)
            for score in scores
        ]

        inventory_values = [
            score.ending_inventory_value
            for score in scores
        ]

        roic_values = [
            (
                score.profit
                / score.operating_expense
            )
            for score in scores
            if score.operating_expense
            != Decimal("0")
        ]

        statistic_rows = [
            _summary_stat_row(
                "Revenue",
                revenue_values,
            ),
            _summary_stat_row(
                "Operating expense",
                expense_values,
            ),
            _summary_stat_row(
                "Profit",
                profit_values,
            ),
            _summary_stat_row(
                "ROIC",
                roic_values,
            ),
            _summary_stat_row(
                "Sales credits",
                credit_values,
            ),
            _summary_stat_row(
                "Lost sales units",
                lost_sales_values,
            ),
            _summary_stat_row(
                "Ending inventory value",
                inventory_values,
            ),
        ]

        cohort_years.append({
            "scenario_year": scenario_year,
            "statistic_rows": statistic_rows,
            "completed_players": len(scores),
        })

    context = {
        "simulation_ready": _simulation_ready(game),
        "game": game,
        "admin_membership": (
            admin_membership
        ),
        "design_settings": (
            settings_obj
        ),
        "players": players,
        "player_rows": player_rows,
        "cohort_years": cohort_years,
        "invite_url": _invite_url(
            request,
            game,
        ),
        "registered_player_count": (
            len(players)
        ),
        "completed_player_count": sum(
            1
            for player in players
            if player.results_summary_reached
        ),
    }

    return render(
        request,
        "simulation/admin_dashboard.html",
        context,
    )


@login_required
@transaction.atomic
def add_player(request, code):
    game = get_object_or_404(
        Game,
        code=code,
    )

    _admin_or_403(
        request.user,
        game,
    )

    if request.method == "POST":
        form = AddPlayerForm(request.POST)

        if form.is_valid():
            user = form.cleaned_data["user"]
            team_name = form.cleaned_data["team_name"]

            if user == game.created_by:
                messages.error(
                    request,
                    (
                        "The game creator is already registered as an "
                        "administrator and cannot also be added as a player."
                    ),
                )
            else:
                membership, created = (
                    GameMembership.objects.update_or_create(
                        game=game,
                        user=user,
                        defaults={
                            "role": GameMembership.Role.PLAYER,
                            "team_name": team_name,
                        },
                    )
                )

                if created:
                    message = "Player added to the game."
                else:
                    message = (
                        f"{membership.user} was updated as a player."
                    )

                messages.success(
                    request,
                    message,
                )

                return redirect(
                    "admin_game_dashboard",
                    code=game.code,
                )
    else:
        form = AddPlayerForm()

    context = {
        "game": game,
        "form": form,
    }

    return render(
        request,
        "simulation/add_player.html",
        context,
    )