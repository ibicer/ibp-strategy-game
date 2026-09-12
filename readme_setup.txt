X-SUMMIT BIKES — DJANGO PROJECT README
Integrated Business Planning Simulation

Authors
-------
Murat Kristal
Işık Biçer

Schulich School of Business
York University


1. PURPOSE OF THIS README
-------------------------

This file is intended to live in the root folder of the Django project,
next to manage.py.

It explains how to:

- create and activate the Python environment
- install dependencies
- migrate the database
- seed the master simulation
- run the Django development server
- reset the development database
- understand the master simulation architecture
- prepare the application for production deployment
- troubleshoot common access and scenario-initialization problems


2. PROJECT STARTUP — LOCAL DEVELOPMENT
--------------------------------------

Open Terminal and move to the Django project folder:

    cd /path/to/supply_chain_strategy_game_v2

The folder should contain:

    manage.py
    simulation/
    config/ or the Django project package
    requirements.txt
    README.txt


3. CREATE / ACTIVATE VIRTUAL ENVIRONMENT
----------------------------------------

If the virtual environment has not been created:

    python3 -m venv venv

Activate it on macOS/Linux:

    source venv/bin/activate

Activate it on Windows:

    venv\Scripts\activate


4. INSTALL DEPENDENCIES
-----------------------

Install the required Python packages:

    pip install -r requirements.txt


5. APPLY DATABASE MIGRATIONS
----------------------------

Run:

    python manage.py migrate

Only when models were intentionally changed and migration files do not yet
exist:

    python manage.py makemigrations
    python manage.py migrate


6. SEED THE MASTER SIMULATION
-----------------------------

The application depends on a master simulation with game code:

    summit-bikes

This master game provides the reusable simulation scenario for newly created
instructor games.

The seed management command creates or refreshes:

- the master Summit Bikes game
- the seeded instructor account
- the seeded player account
- instructor/player memberships
- products
- Scenario Year 1
- Scenario Year 2
- true-demand rows
- rolling forecast snapshots

Run the seed command from the same folder as manage.py.

IMPORTANT:
The exact command name is the filename located in:

    simulation/management/commands/

For example, if the file is:

    simulation/management/commands/seed_simulation.py

run:

    python manage.py seed_simulation

If the seed file has another name, use that filename without ".py".

To list all available Django commands:

    python manage.py help


7. RUN THE DEVELOPMENT SERVER
-----------------------------

Start Django with:

    python manage.py runserver

Then open:

    http://127.0.0.1:8000/

To use another port:

    python manage.py runserver 8001


8. NORMAL DEVELOPMENT STARTUP SEQUENCE
--------------------------------------

For an existing database:

    source venv/bin/activate
    pip install -r requirements.txt
    python manage.py migrate
    python manage.py runserver

For a fresh database:

    source venv/bin/activate
    pip install -r requirements.txt
    python manage.py migrate
    python manage.py <seed_command>
    python manage.py runserver


9. RESET THE DEVELOPMENT DATABASE
---------------------------------

To remove all development data while keeping the schema/migrations:

    python manage.py flush

Confirm with:

    yes

Then reseed:

    python manage.py migrate
    python manage.py <seed_command>

WARNING:
Do not run flush on a production database.


10. MASTER GAME ARCHITECTURE
----------------------------

The game with code:

    summit-bikes

is the master simulation.

It contains the reusable scenario definition.

When an instructor creates a new game, the application automatically creates
that instructor-owned game and copies the required scenario definition into
it.

The automatic initialization includes:

- ScenarioYear
- DemandValue
- ForecastSnapshot

Product records are shared globally.

The following player-specific data is NOT copied:

- GameMembership
- YearAttempt
- AnnualPlan
- MonthlyDecision
- ProductDecision
- PlayerYearScore

Instructors should never need to clone scenario data manually.


11. INSTRUCTOR WORKFLOW
-----------------------

The intended instructor experience is:

    Instructor Signup
        ->
    Instructor Workspace
        ->
    Create Simulation
        ->
    Automatic Scenario Initialization
        ->
    Configure Retry Permissions
        ->
    Copy Private Invitation Link
        ->
    Monitor Players
        ->
    Review Individual and Cohort Results

The instructor should not need shell access or manual database setup after
the master simulation has been seeded.


12. PLAYER WORKFLOW
-------------------

Players normally join through the private invitation link supplied by the
instructor.

The intended flow is:

    Invitation Link
        ->
    Player Signup / Sign In
        ->
    Simulation Lobby
        ->
    Continue Simulation
        ->
    Annual Operating Plan
        ->
    Monthly S&OP Decisions
        ->
    Year-End Review
        ->
    Next Year / Final Results


13. IMPORTANT DEVELOPMENT RULES
-------------------------------

- Run Django commands from the directory containing manage.py.
- Keep the correct virtual environment active.
- Keep requirements.txt synchronized with installed dependencies.
- Seed the master game before instructors create simulations.
- Do not manually clone scenario data during normal use.
- Do not commit production passwords or SECRET_KEY values.
- Use separate browser sessions when testing instructor and player accounts.


14. TROUBLESHOOTING
-------------------

403 Forbidden for a player
~~~~~~~~~~~~~~~~~~~~~~~~~~

Check that the player has a GameMembership for the requested game.

Also confirm that the game contains ScenarioYear records.

In the Django shell:

    python manage.py shell

Then:

    from simulation.models import Game

    game = Game.objects.get(code="your-game-code")

    print(
        list(
            game.scenario_years.values_list(
                "year_number",
                flat=True,
            )
        )
    )

A properly initialized two-year game should show:

    [1, 2]


403 Forbidden for an instructor
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Confirm:

- the instructor is logged into the intended account
- the instructor created the game
- the instructor has ADMIN membership for that game


New instructor game has no scenario years
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Confirm the master game exists:

    from simulation.models import Game

    Game.objects.get(code="summit-bikes")

Then confirm the master contains scenario years:

    master = Game.objects.get(code="summit-bikes")

    print(
        list(
            master.scenario_years.values_list(
                "year_number",
                flat=True,
            )
        )
    )

Expected:

    [1, 2]

If the master is missing, rerun the seed management command.


Static images do not appear
~~~~~~~~~~~~~~~~~~~~~~~~~~~

In development:

- verify STATIC_URL / STATICFILES settings
- confirm files exist under simulation/static/

In production:

    python manage.py collectstatic --noinput


Migration problems
~~~~~~~~~~~~~~~~~~

Check migration state:

    python manage.py showmigrations

Then:

    python manage.py migrate


15. PRODUCTION DEPLOYMENT
-------------------------

Do not use Django's development runserver as the public production server.

A production deployment should include:

- DEBUG=False
- production SECRET_KEY from environment variables
- valid ALLOWED_HOSTS
- production database
- HTTPS
- secure cookies
- static-file collection
- WSGI/ASGI application server
- reverse proxy or supported hosting platform
- database backups


16. PRODUCTION DEPLOYMENT COMMANDS
----------------------------------

Typical preparation sequence:

    source venv/bin/activate

    pip install -r requirements.txt

    python manage.py migrate

    python manage.py collectstatic --noinput

    python manage.py check --deploy

On the first deployment, seed the master simulation:

    python manage.py <seed_command>


17. PRODUCTION APPLICATION SERVER
---------------------------------

Use a production WSGI/ASGI server such as Gunicorn, depending on the hosting
environment.

Typical WSGI syntax:

    gunicorn <django_project_package>.wsgi:application

Replace:

    <django_project_package>

with the actual Python package containing wsgi.py.


18. DEPLOYMENT CHECKLIST
------------------------

Before launch:

    [ ] virtual environment activated
    [ ] dependencies installed
    [ ] production environment variables configured
    [ ] DEBUG=False
    [ ] ALLOWED_HOSTS configured
    [ ] production database configured
    [ ] database backup created
    [ ] migrations applied
    [ ] master summit-bikes simulation seeded
    [ ] static files collected
    [ ] python manage.py check --deploy reviewed
    [ ] HTTPS configured
    [ ] instructor signup tested
    [ ] instructor game creation tested
    [ ] invitation signup tested
    [ ] player Simulation Lobby tested
    [ ] annual planning tested
    [ ] monthly simulation tested
    [ ] instructor dashboard tested
    [ ] final results tested


19. USEFUL COMMANDS
-------------------

    python manage.py help
    python manage.py check
    python manage.py check --deploy
    python manage.py showmigrations
    python manage.py makemigrations
    python manage.py migrate
    python manage.py shell
    python manage.py collectstatic --noinput
    python manage.py runserver
    python manage.py flush


20. AUTHORS AND COPYRIGHT
-------------------------

Murat Kristal
Schulich School of Business, York University

Işık Biçer
Schulich School of Business, York University

Copyright © 2026 Murat Kristal and Işık Biçer.
All rights reserved.
