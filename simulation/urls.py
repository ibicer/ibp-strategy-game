from django.urls import path

from . import views


urlpatterns = [
    # ==============================================================
    # ACCOUNT REGISTRATION
    # ==============================================================

    # --------------------------------------------------------------
    # Instructor signup
    # --------------------------------------------------------------
    path(
        "instructors/signup/",
        views.instructor_signup,
        name="instructor_signup",
    ),

    # --------------------------------------------------------------
    # Player signup through secure game invitation
    # --------------------------------------------------------------
    path(
        "invite/<uuid:invite_token>/",
        views.player_invite_signup,
        name="player_invite_signup",
    ),

    # ==============================================================
    # MAIN WORKSPACES
    # ==============================================================

    # --------------------------------------------------------------
    # General game lobby
    # --------------------------------------------------------------
    path(
        "",
        views.game_lobby,
        name="game_lobby",
    ),

    # --------------------------------------------------------------
    # Instructor workspace
    # --------------------------------------------------------------
    path(
        "instructor/",
        views.instructor_workspace,
        name="instructor_workspace",
    ),

    # ==============================================================
    # GAME CREATION AND DESIGN
    # ==============================================================

    # --------------------------------------------------------------
    # Create a new simulation
    # --------------------------------------------------------------
    path(
        "games/new/",
        views.create_game,
        name="create_game",
    ),

    # --------------------------------------------------------------
    # Configure instructor-controlled simulation options
    # --------------------------------------------------------------
    path(
        "games/<slug:code>/design/",
        views.game_design,
        name="game_design",
    ),

    # ==============================================================
    # PLAYER REGISTRATION — LEGACY COMPATIBILITY
    # ==============================================================

    # --------------------------------------------------------------
    # Existing code-based join route
    #
    # Retained temporarily for backward compatibility.
    # New players should normally join through player_invite_signup.
    # --------------------------------------------------------------
    path(
        "games/<slug:code>/join/",
        views.join_game,
        name="join_game",
    ),

    # ==============================================================
    # PLAYER SIMULATION
    # ==============================================================

    # --------------------------------------------------------------
    # Player dashboard / resume simulation
    # --------------------------------------------------------------
    path(
        "games/<slug:code>/",
        views.game_dashboard,
        name="game_dashboard",
    ),

    # --------------------------------------------------------------
    # Annual Operating Plan
    # --------------------------------------------------------------
    path(
        "games/<slug:code>/year/<int:year>/planning/",
        views.annual_planning,
        name="annual_planning",
    ),

    # --------------------------------------------------------------
    # Monthly S&OP planning
    # --------------------------------------------------------------
    path(
        (
            "games/<slug:code>/year/<int:year>/"
            "month/<int:month>/plan/"
        ),
        views.plan_month,
        name="plan_month",
    ),

    # --------------------------------------------------------------
    # Monthly result and review
    # --------------------------------------------------------------
    path(
        (
            "games/<slug:code>/year/<int:year>/"
            "month/<int:month>/result/"
        ),
        views.month_result,
        name="month_result",
    ),

    # --------------------------------------------------------------
    # Advance from completed year
    # --------------------------------------------------------------
    path(
        "games/<slug:code>/year/<int:year>/advance/",
        views.advance_year,
        name="advance_year",
    ),

    # --------------------------------------------------------------
    # Final two-year player results summary
    # --------------------------------------------------------------
    path(
        "games/<slug:code>/results-summary/",
        views.results_summary,
        name="results_summary",
    ),

    # --------------------------------------------------------------
    # Redo selected year
    # --------------------------------------------------------------
    path(
        "games/<slug:code>/year/<int:year>/reset/",
        views.reset_year,
        name="reset_year",
    ),

    # --------------------------------------------------------------
    # Restart complete simulation from Year 1
    # --------------------------------------------------------------
    path(
        "games/<slug:code>/restart/",
        views.restart_from_year_one,
        name="restart_from_year_one",
    ),

    # ==============================================================
    # INSTRUCTOR GAME MANAGEMENT
    # ==============================================================

    # --------------------------------------------------------------
    # Instructor dashboard for one simulation
    # --------------------------------------------------------------
    path(
        "games/<slug:code>/instructor/",
        views.admin_game_dashboard,
        name="admin_game_dashboard",
    ),

    # --------------------------------------------------------------
    # Instructor read-only view of one player's summary
    # --------------------------------------------------------------
    path(
        (
            "games/<slug:code>/instructor/"
            "players/<int:membership_id>/summary/"
        ),
        views.instructor_player_summary,
        name="instructor_player_summary",
    ),

    # --------------------------------------------------------------
    # Manual player enrollment
    #
    # Retained as an instructor utility / fallback.
    # The normal workflow uses the secure invitation link.
    # --------------------------------------------------------------
    path(
        "games/<slug:code>/instructor/add-player/",
        views.add_player,
        name="add_player",
    ),
]